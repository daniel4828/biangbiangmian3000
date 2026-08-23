"""
AI story generation — supports Anthropic and OpenAI-compatible providers.

Supported model prefixes:
  claude-*      → Anthropic SDK (ANTHROPIC_API_KEY)
  glm-*         → Zhipu AI (ZHIPU_API_KEY)
  deepseek-*    → DeepSeek (DEEPSEEK_API_KEY)
  qwen-*        → Alibaba Qwen/DashScope (QWEN_API_KEY)
  gpt-*         → OpenAI (OPENAI_API_KEY) — used for news mode (DeepSeek censors news content)
"""

import json
import logging
import os
import re
import textwrap
import time

import anthropic
import openai

import database
import languages

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4-flash"

# briefing mode (issue #444) — env var BRIEFING_MODEL, default gpt-5.6-luna,
# verified against the OpenAI models API with a fallback chain, cached for
# process lifetime. Retired gpt-5.1/gpt-5 here (#731, Daniel 2026-08-14): luna
# is $0.20/$1.20 against their $1.25/$10.00 for the same job.
BRIEFING_MODEL_FALLBACKS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5-mini")
_briefing_model_cache: str | None = None

# issue #514: if the OpenAI account runs out of quota mid-run (429
# insufficient_quota), briefing/paste/podcast calls have no OpenAI fallback
# model to try (DeepSeek can't be used — it censors news content). Rather
# than fail the whole pregen key, retry once on Claude for these purposes only.
_QUOTA_FALLBACK_MODEL = "claude-sonnet-5"
_QUOTA_FALLBACK_PURPOSES = {"briefing", "briefing_fact_check"}

# Per-session story generation progress: key → {phase, msg, percent, translate_warn?}
_story_progress: dict[str, dict] = {}


class StoryCancelled(Exception):
    """The user pressed Cancel on the generation loading screen (#828)."""


# Progress keys the user asked to abandon. Checked from _set_progress() rather
# than from a handful of explicit checkpoints in routes/story.py: every
# generation mode already reports each phase (retry attempt, translation start,
# per-sentence translation progress) through _set_progress, so hanging the
# check there interrupts all of them at every step for free — and a mode added
# later inherits it without anyone remembering to wire a checkpoint in.
_cancelled_keys: set[str] = set()


def request_cancel(key: str | None) -> None:
    if key:
        _cancelled_keys.add(key)


def clear_cancel(key: str | None) -> None:
    """Must run in the generation thread's finally: a leftover flag would kill
    the next run for the same deck the instant it starts."""
    _cancelled_keys.discard(key)


def is_cancelled(key: str | None) -> bool:
    return bool(key) and key in _cancelled_keys


def raise_if_cancelled(key: str | None) -> None:
    if is_cancelled(key):
        raise StoryCancelled("Story generation cancelled")


def _set_progress(key: str | None, **kwargs) -> None:
    if key:
        raise_if_cancelled(key)
        _story_progress[key] = kwargs


# Cumulative log lines per progress key (issue #642). _set_progress overwrites
# the whole dict on every update, so its single `msg` can only ever show the
# current step — never what already happened or where a run got stuck. The
# loading screen renders these lines under the progress bar.
_story_log: dict[str, list[str]] = {}
_STORY_LOG_MAX = 200


def reset_story_log(key: str | None) -> None:
    if key:
        _story_log.pop(key, None)


def log_progress(key: str | None, line: str) -> None:
    """Append one Chinese log line for the loading screen, and mirror it to the
    server log so the same trace is available in journalctl after the fact."""
    logger.info("story-log  %s", line)
    if not key:
        return
    lines = _story_log.setdefault(key, [])
    lines.append(line)
    del lines[:-_STORY_LOG_MAX]


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

def _openai_client(model: str) -> openai.OpenAI:
    if model.startswith("deepseek-"):
        return openai.OpenAI(
            base_url="https://api.deepseek.com/v1",
            api_key=os.environ["DEEPSEEK_API_KEY"],
        )
    elif model.startswith("glm-"):
        return openai.OpenAI(
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            api_key=os.environ["ZHIPU_API_KEY"],
        )
    elif model.startswith("qwen-"):
        return openai.OpenAI(
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ["QWEN_API_KEY"],
        )
    elif model.startswith("gpt-"):
        return openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    raise ValueError(f"Unknown provider for model: {model}")


# gpt-5 系列关闭推理时用的最低档 —— 两代的名字互不兼容（2026-08-12 实测，#724）：
#   gpt-5 / gpt-5-mini   支持 minimal, low, medium, high   —— 发 'none' 报 400
#   gpt-5.1 / gpt-5.6-*  支持 none, low, medium, high, xhigh —— 5.6 发 'minimal' 报 400
# 实测生成故事句子时 low 要烧 403 个推理 token（比 none 贵 6.2 倍、慢一倍），
# 而输出质量看不出差别 —— 这类任务不需要思维链。
_GPT_MIN_EFFORT = {
    "gpt-5": "minimal",
    "gpt-5-mini": "minimal",
    "gpt-5.1": "none",
    "gpt-5.6-luna": "none",
    "gpt-5.6-terra": "none",
    "gpt-5.6-sol": "none",
}
# 表里没有的 gpt 模型走这个值：任何一代都接受，绝不会 400。新模型上线时
# 宁可多花钱，也不能因为猜错档位名而让整条流程静默挂掉。
_GPT_SAFE_EFFORT = "low"

# OpenAI 的 id 可能带日期快照后缀（gpt-5.1-2026-04-14）—— 查表前先剥掉，
# 否则每出一个快照都要往表里加一行。同 database.stats._SNAPSHOT_SUFFIX_RE。
_SNAPSHOT_SUFFIX_RE = re.compile(r"(-\d{4}-\d{2}-\d{2}|-\d{8})$")


def _gpt_reasoning_effort(model: str, thinking: bool) -> str:
    """gpt-5 系列该发哪个 reasoning_effort。

    thinking=True 时用 "low"（够用且各代通用）；默认的 thinking=False 查
    _GPT_MIN_EFFORT，未知模型回落到 _GPT_SAFE_EFFORT。
    """
    if thinking:
        return _GPT_SAFE_EFFORT
    base = _SNAPSHOT_SUFFIX_RE.sub("", model)
    return _GPT_MIN_EFFORT.get(model) or _GPT_MIN_EFFORT.get(base) or _GPT_SAFE_EFFORT


def _extract_prompt(messages: list) -> str:
    """Join the content of ALL messages (for cost-modal display).

    System messages carry the format guides, so they must show too (issue #579)
    — non-user roles are prefixed with a [role] marker so the reader can tell
    them apart. Truncation to a display-safe length happens in
    database.log_api_call, not here — never trust the caller to have already
    truncated.
    """
    parts = []
    for m in messages:
        content = m.get("content", "")
        role = m.get("role", "user")
        parts.append(content if role == "user" else f"[{role}]\n{content}")
    return "\n\n".join(parts)


def _call_api(model: str, messages: list, max_tokens: int, purpose: str,
              thinking: bool = False) -> str:
    """Call the appropriate provider, log usage, and return the raw text response.

    thinking: enable the provider's thinking/reasoning mode (default False — disabled).
              deepseek-v4-flash defaults to thinking=on server-side, so we must
              explicitly disable it for tasks that don't need chain-of-thought.
              GLM-4.5+ 同理。gpt-5 系列自 #724 起也尊重这个参数：False 时发该
              模型支持的最低 reasoning_effort（见 _gpt_reasoning_effort）。
    """
    t0 = time.time()
    prompt_text = _extract_prompt(messages)
    if model.startswith("claude-"):
        client = anthropic.Anthropic()
        msg = client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
        elapsed = time.time() - t0
        cached_tokens = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        logger.info("[%s] API call done in %.1fs — in=%d out=%d cached=%d purpose=%s",
                    model, elapsed, msg.usage.input_tokens, msg.usage.output_tokens, cached_tokens, purpose)
        if getattr(msg, "stop_reason", None) == "max_tokens":
            # #743: caller-side JSON salvage relies on knowing when a reply was
            # cut off — this is the signal, not an exception.
            logger.warning("[%s] response truncated (stop_reason=max_tokens, max_tokens=%d, "
                           "output_tokens=%d, purpose=%s)", model, max_tokens,
                           msg.usage.output_tokens, purpose)
        text = msg.content[0].text.strip()
        database.log_api_call(
            # Log the requested model id, not msg.model (the API returns a dated
            # snapshot name like "claude-sonnet-4-6-20260115" that the pricing
            # table can't match exactly — see database.stats._lookup_pricing).
            model=model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            purpose=purpose,
            cached_input_tokens=cached_tokens,
            prompt=prompt_text,
            response=text,
        )
        return text
    else:
        client = _openai_client(model)
        extra: dict = {}
        if model.startswith("deepseek-"):
            extra["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
            logger.debug("[%s] thinking=%s", model, thinking)
        elif model.startswith(("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")):
            # GLM-4.5 起支持 thinking 模式且默认开启 —— 生成句子不需要思维链，
            # 显式关闭以省时省钱（旧 glm-4-flash/air 不认识该参数，故不加）。
            extra["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
            logger.debug("[%s] thinking=%s", model, thinking)
        try:
            if model.startswith("gpt-"):
                # gpt-5 series (Chat Completions): max_completion_tokens replaces max_tokens
                # and is shared with internal reasoning tokens; custom temperature is not
                # supported. reasoning_effort 由 _gpt_reasoning_effort 按模型决定 ——
                # 与 DeepSeek/GLM 分支一样尊重 thinking 参数（#724）。
                effort = _gpt_reasoning_effort(model, thinking)
                logger.debug("[%s] reasoning_effort=%s (thinking=%s)", model, effort, thinking)
                resp = client.chat.completions.create(
                    model=model, max_completion_tokens=max_tokens, messages=messages,
                    reasoning_effort=effort,
                )
            else:
                resp = client.chat.completions.create(
                    model=model, max_tokens=max_tokens, messages=messages, **extra
                )
        except Exception as e:
            if (purpose in _QUOTA_FALLBACK_PURPOSES and "insufficient_quota" in str(e)
                    and model != _QUOTA_FALLBACK_MODEL):
                logger.warning("[%s] insufficient_quota on purpose=%s — falling back to %s",
                               model, purpose, _QUOTA_FALLBACK_MODEL)
                return _call_api(_QUOTA_FALLBACK_MODEL, messages, max_tokens, purpose, thinking)
            raise
        elapsed = time.time() - t0
        choice = resp.choices[0]
        content = choice.message.content
        reasoning = getattr(choice.message, "reasoning_content", None)
        reasoning_chars = len(reasoning) if reasoning else 0

        # Cache-hit tokens: DeepSeek reports prompt_cache_hit_tokens directly;
        # OpenAI reports prompt_tokens_details.cached_tokens. Use whichever is
        # present (nonzero) for this provider — the other is always 0/absent.
        deepseek_cached = getattr(resp.usage, "prompt_cache_hit_tokens", 0) or 0
        openai_cached = getattr(
            getattr(resp.usage, "prompt_tokens_details", None), "cached_tokens", 0
        ) or 0
        cached_tokens = deepseek_cached or openai_cached

        logger.info("[%s] API call done in %.1fs — in=%d out=%d cached=%d reasoning_chars=%d purpose=%s",
                    model, elapsed,
                    resp.usage.prompt_tokens, resp.usage.completion_tokens,
                    cached_tokens, reasoning_chars, purpose)
        database.log_api_call(
            # Log the requested model id, not resp.model (the API returns a
            # dated snapshot name that the pricing table can't match exactly —
            # see database.stats._lookup_pricing).
            model=model,
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
            purpose=purpose,
            cached_input_tokens=cached_tokens,
            prompt=prompt_text,
            response=content,
        )

        if choice.finish_reason == "length":
            if reasoning_chars > 0 and not content:
                logger.warning(
                    "[%s] thinking mode exhausted max_tokens=%d (%d reasoning chars) "
                    "— no content produced. Pass thinking=False or increase max_tokens.",
                    model, max_tokens, reasoning_chars,
                )
            else:
                # #743: this is the signal callers (e.g. generate_podcast_sentences'
                # JSON salvage) rely on to know a reply was cut off mid-content.
                logger.warning("[%s] response truncated (finish_reason=length, max_tokens=%d, "
                               "completion_tokens=%d, content_chars=%d, purpose=%s)",
                               model, max_tokens, resp.usage.completion_tokens,
                               len(content or ""), purpose)

        if not content and not reasoning:
            logger.warning("[%s] empty response — no content and no reasoning (purpose=%s)",
                           model, purpose)

        return (content or "").strip()


def resolve_briefing_model() -> str:
    """Resolve the OpenAI model id used for briefing mode (issue #444).

    Reads BRIEFING_MODEL (default "gpt-5.6-luna"), verifies on first use that
    the id actually exists via the OpenAI models API, and falls back through
    luna → terra → gpt-5-mini if not (or if the id is some other unlisted
    string). The resolved id is cached for the process lifetime — the models
    API is only ever hit once per process. OpenAI only (briefing is OpenAI-only,
    same reasoning as news/paste: DeepSeek censors news content).
    """
    global _briefing_model_cache
    if _briefing_model_cache is not None:
        return _briefing_model_cache

    requested = os.environ.get("BRIEFING_MODEL") or BRIEFING_MODEL_FALLBACKS[0]
    candidates = [requested] + [m for m in BRIEFING_MODEL_FALLBACKS if m != requested]

    try:
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        available = {m.id for m in client.models.list().data}
        for candidate in candidates:
            if candidate in available:
                _briefing_model_cache = candidate
                logger.info("briefing model resolved: %s (requested=%s, available=%d models)",
                           candidate, requested, len(available))
                return candidate
        logger.warning("briefing model: none of %s found via OpenAI models API — using last "
                       "resort %s", candidates, BRIEFING_MODEL_FALLBACKS[-1])
        _briefing_model_cache = BRIEFING_MODEL_FALLBACKS[-1]
        return _briefing_model_cache
    except Exception as e:
        logger.warning("briefing model: could not verify via OpenAI models API (%s) — "
                       "falling back to %s", e, BRIEFING_MODEL_FALLBACKS[-1])
        _briefing_model_cache = BRIEFING_MODEL_FALLBACKS[-1]
        return _briefing_model_cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Shared 1-6 difficulty value → CEFR label, for non-zh story prompts (issue
# #596). Mirrors importer._CEFR_TO_INT (A1=1 … C2=6).
_CEFR_LEVELS = {1: "A1", 2: "A2", 3: "B1", 4: "B2", 5: "C1", 6: "C2"}

# ── 自定义提示词模板（issue #581）──────────────────────────────────────────────
# 每个可自定义模式一份内置模板；database.get_prompt_template(mode) 有自定义行时
# 覆盖内置。渲染用 _render_prompt 做逐记号字符串替换（不是 str.format——模板里
# 的 JSON 示例花括号无需转义）。默认模板的渲染结果与旧的内联 f-string 逐字一致，
# 提示词质量不受重构影响。目前仅覆盖 zh；fr 走原内置路径。
DEFAULT_PROMPT_TEMPLATES: dict[str, str] = {
    "story": """Write a short Mandarin Chinese story to help an HSK 4-5 learner review vocabulary.

{grammar_block}Target words (each must appear verbatim in at least one sentence):
{words}

Rules:
- Each target word MUST appear verbatim in at least one sentence
- Write the sentences in the same order as the target word list above
- For items marked [SENTENCE]: use that exact text as the sentence, unchanged
- Use proper Chinese punctuation — include commas（，）where natural pauses occur
- Use only HSK 1-{max_hsk} vocabulary for non-target words; each sentence must contain exactly ONE target word from the list — do not use other target words from the list in that sentence
- Keep each sentence short and simple
{topic_block}- The sentences must form a coherent narrative with the same recurring characters
- NEVER highlight, quote, or mark target words in any way — no "quotes", no 「brackets」, no （parentheses）, no bold, no underline; write them as plain text embedded naturally in the sentence
- NEVER use markdown formatting (**bold**, _italic_, etc.) anywhere in the output — write plain text only

Return ONLY a numbered list of Chinese sentences, no explanation:
1. ...
2. ...""",
    "qa": """Answer the following question in Mandarin Chinese, one sentence at a time, to help an HSK 4-5 learner review vocabulary.
Question: {topic}

{grammar_block}Target words (each must appear verbatim in at least one sentence):
{words}

Rules:
- Each target word MUST appear verbatim in at least one sentence
- Write the sentences in the same order as the target word list above
- For items marked [SENTENCE]: use that exact text as the sentence, unchanged
- Use proper Chinese punctuation — include commas（，）where natural pauses occur
- Use only HSK 1-{max_hsk} vocabulary for non-target words; each sentence must contain exactly ONE target word from the list — do not use other target words from the list in that sentence
- Keep each sentence short and simple
- The sentences together should form a coherent, informative answer to the question above
- Do NOT use fictional characters or narrative story structure
- NEVER highlight, quote, or mark target words in any way — no "quotes", no 「brackets」, no （parentheses）, no bold, no underline; write them as plain text embedded naturally in the sentence
- NEVER use markdown formatting (**bold**, _italic_, etc.) anywhere in the output — write plain text only

Return ONLY a numbered list of Chinese sentences, no explanation:
1. ...
2. ...""",
    "expository": """Write a short informative text in Mandarin Chinese about the following topic, to help an HSK 4-5 learner review vocabulary.
Topic: {topic}

{grammar_block}Target words (each must appear verbatim in at least one sentence):
{words}

Rules:
- Each target word MUST appear verbatim in at least one sentence
- Write the sentences in the same order as the target word list above
- For items marked [SENTENCE]: use that exact text as the sentence, unchanged
- Use proper Chinese punctuation — include commas（，）where natural pauses occur
- Use only HSK 1-{max_hsk} vocabulary for non-target words; each sentence must contain exactly ONE target word from the list — do not use other target words from the list in that sentence
- Keep each sentence short and simple
- The sentences together should form a coherent, factual explanation of the topic above
- Do NOT use fictional characters or narrative story structure
- NEVER highlight, quote, or mark target words in any way — no "quotes", no 「brackets」, no （parentheses）, no bold, no underline; write them as plain text embedded naturally in the sentence
- NEVER use markdown formatting (**bold**, _italic_, etc.) anywhere in the output — write plain text only

Return ONLY a numbered list of Chinese sentences, no explanation:
1. ...
2. ...""",
    # Renamed from "podcast" to "knowledge" (issue #654) — episodes now cover
    # podcast/video/article sources, not just podcasts. Old mode='podcast'
    # stories still call generate_podcast_sentences below, which now looks up
    # this same "knowledge" template — see database/core.py's one-time
    # prompt_presets rename so Daniel's saved custom template keeps applying.
    # {summary} 现在多数情况下是中文转录全文而非德语摘要（issue #661），
    # 措辞已改成不预设语言，占位符名字本身保留不变（见 generate_podcast_sentences
    # 注释——改名会让已保存的自定义模板失效）。
    # #737：#634 的"每句至少含一个专有名词"太松——三档原则让模型大量降到 B/C 档，
    # 于是句子既不带年份/数字这类硬事实，又反复围着素材开头的同一件事打转。
    #
    # #741：下面这份就是线上实际发出去的提示词。它由 Daniel 自己在界面里调出来的
    # "facts" 版本（事实清单 → 配词、关键词例外、输出前自检、facts_omitted）与 #737
    # 的硬事实/覆盖面规则合并而成，合并后 prompt_presets 里 knowledge 的自定义版本
    # 全部设为 is_active=0——**提示词只保留这一份，改它就走 PR**。
    # 两份并存时 _story_prompt_template() 数据库优先，改了代码线上却零效果（#737 的
    # 教训），而且实际内容不在版本库里、没有 diff 也没有历史。
    # 若哪天又在界面里存了自定义版本，它会重新盖过这里——排查提示词问题先查那张表。
    "knowledge": """任务：下面是一期播客/视频/文章素材的内容。它可能是原始转录全文，也可能是一份内容摘要，
语言可能是中文，也可能是德语。请用简体中文写出一组句子，每句复习一个目标词，
同时复述素材中的一个具体事实。使用者是 HSK 4-5 的学习者。

单集标题：{title}
素材内容：
{summary}
目标词汇：
{words}

一、句子的双重任务
1. 每句必须恰好包含一个目标词，原样出现，不拆开、不改写、不换近义词。
2. 每句必须复述素材中的一个具体事实：谁、是什么、多少、何时、为什么、结果如何。
3. 一个事实只用一次，不要在多句中重复同一个信息；换个说法重复也算同一个事实。
4. 句子总数等于目标词数量。

二、先挑事实，再配词
5. 动笔前先在内部列出素材里的事实清单，条数不少于目标词数量。
6. 【覆盖面】这些事实必须分散在素材的开头、中间和结尾，素材里出现的每一个话题
   都至少覆盖一条。严禁只用开头的内容，也严禁所有句子都挤在同一个话题上。
7. 【硬事实优先】挑事实时优先挑带硬数据的：年份、日期、数量、金额、百分比、
   名次、时长、年龄；其次是带人名、公司名、机构名、地名的。两类都有的最优先。
8. 再为每条事实挑一个最容易自然搭配的目标词。为了容纳目标词，允许换个角度重述
   这条事实（从人物、时间、原因、影响等角度说），但事实本身、数字和名字不能变。
9. 句子按事实在素材里出现的先后顺序输出，让整组句子读起来像一份内容提纲；
   同一话题的句子排在一起，不要来回跳。
10. 如果严格按顺序会逼出生硬的搭配，允许调整事实顺序——
    宁可顺序乱一点，也不要把不搭的词硬塞进某个事实。
11. 句子之间不需要连接词，不需要情节，各自独立成立即可。
12. 严禁空洞句子，例如「内容很有趣」「他说了很多」「这个话题很重要」。
13. 只能使用素材中明确写出的信息，禁止编造、补充背景知识或加入个人评论。

三、难度控制
14. 每句 10 到 30 个汉字，标点不计入。
15. 除目标词和第 16 条的关键词外，只使用 HSK 1-{max_hsk} 的词汇。
16. 【关键词例外】素材中的人名、地名、机构名、数字、年份、金额和核心术语，
    即使超出 HSK 等级也必须原样保留，这些正是最值得记住的内容。
    绝不许简化成「一个人」「一个地方」「很多」「好几年」这类模糊说法。
    关键词数量不设上限——只要句子读得通，一句里出现三四个数字或名字也很好。
17. 如果某个事实除了关键词以外还需要难词才能说清楚，
    就换一种更简单的说法，实在不行才放弃这个事实。
18. 只用简体字和中文标点。禁止繁体字、markdown、拼音和括号注释。
19. 不要给目标词加引号、括号或任何标记，直接写进句子里。

四、reasoning_zh 的写法
20. 每句都要写 reasoning_zh：先写「事实：」加上该句复述的那条事实，
    照抄其中的名字和数字（用素材原文的语言写这部分），
    再用一句中文说明这句话在讲什么。面向 HSK 4-5 学习者，中文部分用词不要太难。
21. 把事实写出来正是为了自查：任何两句的「事实：」都不许相同。

五、输出前自检（内部进行，不要输出）
- 句子数是否等于目标词数？
- 每个目标词是否恰好出现一次？
- 是否有句子不含具体事实？
- 是否有两句复述了同一个事实？
- 这些事实是否分散在素材的各个部分、覆盖了各个话题，而不是全挤在开头？
- 是否有句子把素材给出的数字或名字丢掉、写成了模糊说法？
- 除关键词外，是否有超出 HSK {max_hsk} 的词？

{extra_hint}

仅返回如下 JSON 数组，不加任何其他文字（reasoning_zh 在前，sentence_zh 在后）：
[
  {"reasoning_zh": "事实：… + 一句中文说明", "sentence_zh": "含目标词的句子", "target_word": "词汇"}
]

如果确实有事实因为第 17 条被放弃，最多列 2 条，写在数组最后一个元素里，
加一个 "facts_omitted" 字段（事实原文 + 放弃原因），不要因此改变数组长度：
[
  ...,
  {"reasoning_zh": "...", "sentence_zh": "...", "target_word": "...",
   "facts_omitted": [{"fact_de": "...", "reason": "..."}]}
]""",
}

# 编辑器界面展示给用户的可用记号（routes/story.py 的 GET 接口返回）。
PROMPT_TEMPLATE_VARIABLES: dict[str, list[str]] = {
    "story": ["words", "max_hsk", "grammar_block", "topic_block"],
    "qa": ["words", "max_hsk", "grammar_block", "topic"],
    "expository": ["words", "max_hsk", "grammar_block", "topic"],
    "knowledge": ["words", "summary", "title", "max_hsk", "extra_hint"],
}


def _render_prompt(template: str, variables: dict[str, str]) -> str:
    for k, v in variables.items():
        template = template.replace("{" + k + "}", v)
    return template


def _story_prompt_template(mode: str) -> str:
    """自定义模板（DB）优先，否则内置默认。查询失败一律回退默认——
    提示词渲染绝不能因为模板表出问题而阻断生成。"""
    try:
        return database.get_prompt_template(mode) or DEFAULT_PROMPT_TEMPLATES[mode]
    except Exception as e:
        logger.warning("prompt template lookup failed for %s: %s", mode, e)
        return DEFAULT_PROMPT_TEMPLATES[mode]


def generate_story(cards: list[dict], topic: str | None = None, max_hsk: int = 2,
                   model: str = DEFAULT_MODEL,
                   progress_key: str | None = None,
                   grammar_focus: str | None = None,
                   grammar_pct: int = 75,
                   mode: str = "story",
                   lang: str = "zh") -> tuple[list[dict], str]:
    """
    Generate sentences (in `lang`) covering all target vocab words.

    cards:         list of dicts with keys word_id, word_zh, pinyin, definition, pos
    topic:         optional theme/question/topic to guide the content
    max_hsk:       maximum HSK level for non-target background vocabulary (1-6, zh only)
    model:         model ID to use for generation
    grammar_focus: optional grammar pattern to encourage (e.g. "把字句", zh only)
    grammar_pct:   approximate percentage of sentences that should use the grammar (0-100)
    mode:          "story" | "qa" | "expository"
    lang:          "zh" | "fr" — determines prompt language, level system, and matching rules
    Returns: (sentences, prompt_text)
      sentences: list of {word_ids: [int, ...], sentence_zh, sentence_en, sentence_de, sentence_fr}.
                 Multiple cards may share one sentence. Each card's word_id appears in exactly one sentence.
      prompt_text: the full prompt string sent to the AI.
    Returns ([], "") immediately if cards is empty (no API call made).
    """
    if not cards:
        return [], ""

    word_id_set = {c["word_id"] for c in cards}

    def _is_sentence(word_zh: str) -> bool:
        if lang != "zh" and word_zh.endswith('.'):
            return True
        return word_zh.endswith(('。', '！', '？', '!', '?'))

    # French articles to strip from the target word before matching it inside a
    # generated sentence — the AI may adapt/drop the article to fit the sentence.
    _FR_ARTICLE_PREFIXES = ("le ", "la ", "les ", "un ", "une ", "des ", "du ", "de la ", "de l'", "l'")

    def _word_in_sentence(word_zh: str, sentence_zh: str) -> bool:
        if lang != "zh":
            w = word_zh.casefold()
            s = sentence_zh.casefold()
            for prefix in _FR_ARTICLE_PREFIXES:
                if w.startswith(prefix):
                    w = w[len(prefix):]
                    break
            # Word-boundary match so short words don't match inside longer ones
            # (e.g. "art" inside "partir"); tolerate a plural suffix.
            return re.search(rf"(?<!\w){re.escape(w)}(?:s|es)?(?!\w)", s) is not None
        if '...' in word_zh or '…' in word_zh:
            chars = [c for c in word_zh if c not in '.…']
            return all(c in sentence_zh for c in chars)
        return word_zh in sentence_zh

    word_list_lines = []
    for i, c in enumerate(cards):
        if _is_sentence(c['word_zh']):
            word_list_lines.append(f"{i + 1}. [SENTENCE] {c['word_zh']}")
        else:
            word_list_lines.append(f"{i + 1}. {c['word_zh']}")
    word_list = "\n".join(word_list_lines)

    if grammar_focus:
        n_sentences = max(1, round(len(cards) * grammar_pct / 100))
        grammar_first = (
            f"GRAMMAR FOCUS: Use the pattern 「{grammar_focus}」 in roughly "
            f"{n_sentences} of the sentences (about {grammar_pct}%).\n\n"
        )
    else:
        grammar_first = ""

    if lang == "zh":
        # zh 提示词内容与多语言管线之前完全一致，只是搬进了 DEFAULT_PROMPT_TEMPLATES
        # （issue #581）——用户可按模式在 DB 里覆盖模板，动态部分用记号替换。
        variables = {
            "grammar_block": grammar_first,
            "words": word_list,
            "max_hsk": str(max_hsk),
        }
        if mode == "qa":
            variables["topic"] = topic or "Describe something interesting."
        elif mode == "expository":
            variables["topic"] = topic or "an interesting subject"
        else:
            variables["topic_block"] = (
                f"- The story should be set around this topic or theme: {topic}\n" if topic else ""
            )
        tpl_mode = mode if mode in ("qa", "expository") else "story"
        prompt = _render_prompt(_story_prompt_template(tpl_mode), variables)
    else:
        cfg = languages.get_lang_config(lang)
        lang_name = cfg["name_en"]
        learner = cfg["learner_level"]
        # Background-vocabulary cap follows the setup-modal slider (issue #596):
        # the shared 1-6 value maps to CEFR A1…C2 instead of HSK levels.
        cefr_cap = _CEFR_LEVELS.get(max_hsk, "B1")
        background_vocab = f"CEFR A1-{cefr_cap}"

        if mode == "qa":
            task_line = f"Answer the following question in {lang_name}, one sentence at a time, to help a {learner} learner review vocabulary.\nQuestion: {topic or 'Describe something interesting.'}"
            style_rule = "- The sentences together should form a coherent, informative answer to the question above\n- Do NOT use fictional characters or narrative story structure"
        elif mode == "expository":
            task_line = f"Write a short informative text in {lang_name} about the following topic, to help a {learner} learner review vocabulary.\nTopic: {topic or 'an interesting subject'}"
            style_rule = "- The sentences together should form a coherent, factual explanation of the topic above\n- Do NOT use fictional characters or narrative story structure"
        else:
            task_line = f"Write a short {lang_name} story to help a {learner} learner review vocabulary."
            topic_clause = f"- The story should be set around this topic or theme: {topic}\n" if topic else ""
            style_rule = f"{topic_clause}- The sentences must form a coherent narrative with the same recurring characters"

        prompt = f"""{task_line}

{grammar_first}Target words (each must appear verbatim in at least one sentence):
{word_list}

Rules:
- Each target word MUST appear verbatim in at least one sentence
- Write the sentences in the same order as the target word list above
- For items marked [SENTENCE]: use that exact text as the sentence, unchanged
- Use natural {lang_name} punctuation
- Use only simple {background_vocab} level vocabulary for non-target words; each sentence must contain exactly ONE target word from the list — do not use other target words from the list in that sentence
- Keep each sentence short and simple (max {cfg["sentence_limit"]})
{style_rule}
- Each target word must appear in exactly the given form; you may adapt or drop its leading article (le/la/les/un/une) to fit the sentence
- NEVER highlight, quote, or mark target words in any way — no "quotes", no 「brackets」, no （parentheses）, no bold, no underline; write them as plain text embedded naturally in the sentence
- NEVER use markdown formatting (**bold**, _italic_, etc.) anywhere in the output — write plain text only

Return ONLY a numbered list of {lang_name} sentences, no explanation:
1. ...
2. ..."""

    max_tokens = 8192

    logger.info("[%s] generate_story: %d 张卡片 mode=%s", model, len(cards), mode)
    logger.debug("Prompt:\n%s", prompt)

    card_by_id = {c["word_id"]: c for c in cards}
    t_start = time.time()
    missing_hint = ""
    last_partial: tuple | None = None  # (sentences, missing_word_ids)
    for attempt in range(3):
        retry_label = f" (retry {attempt}/{2})" if attempt > 0 else ""
        _set_progress(progress_key, phase="request", attempt=attempt + 1,
                      msg=f"Sending request to AI…{retry_label}", percent=max(5, 10 - attempt * 4))
        raw = _call_api(model, [{"role": "user", "content": prompt + missing_hint}], max_tokens,
                        purpose="story")

        logger.debug("Raw response attempt=%d (%d chars):\n%s", attempt + 1, len(raw), raw)

        # Parse numbered list: extract lines like "1. 句子"
        sentences_zh = []
        for line in raw.splitlines():
            m = re.match(r'^\d+\.\s+(.+)', line.strip())
            if m:
                sentences_zh.append(m.group(1).strip())

        if not sentences_zh:
            if not raw:
                logger.error("generate_story: attempt %d — empty response from API "
                             "(model=%s, max_tokens=%d). "
                             "If this is a reasoning model, it may have exhausted its token budget on thinking.",
                             attempt + 1, model, max_tokens)
            else:
                logger.error("generate_story: attempt %d — no numbered sentences found "
                             "(response was %d chars):\n%.500s…",
                             attempt + 1, len(raw), raw)
            continue

        # Match target words to sentences by string search
        seen_ids: set[int] = set()
        parsed = []
        for s_zh in sentences_zh:
            word_ids = []
            for card in cards:
                wid = card["word_id"]
                if wid not in seen_ids and _word_in_sentence(card["word_zh"], s_zh):
                    word_ids.append(wid)
                    seen_ids.add(wid)
                    break  # one target word per sentence
            parsed.append({"word_ids": word_ids, "sentence_zh": s_zh, "tokens": []})

        missing_ids = [wid for wid in word_id_set if wid not in seen_ids]
        if missing_ids:
            missing_words = [card_by_id[wid]["word_zh"] for wid in missing_ids if wid in card_by_id]
            logger.warning("generate_story: attempt %d — words missing: %s", attempt + 1, missing_words)
            _set_progress(progress_key, phase="warning", attempt=attempt + 1,
                          msg=f"⚠ Attempt {attempt + 1}: missing {missing_words} — retrying",
                          percent=0)
            missing_ratio = len(missing_ids) / len(cards)
            last_partial = (parsed, missing_ids)
            if missing_ratio < 0.05:
                _patch_missing(parsed, missing_ids, card_by_id, lang=lang)
                _set_progress(progress_key, phase="translating",
                              msg="Translating sentences…", percent=88)
                _fill_translations(parsed, progress_key=progress_key, lang=lang)
                _set_progress(progress_key, phase="ai_done",
                              msg=f"✓ {len(parsed)} sentences (attempt {attempt + 1})", percent=93)
                return parsed, prompt
            missing_hint = (
                f"\n\nIMPORTANT: Your previous attempt was missing these words "
                f"— each MUST appear verbatim in a sentence: {', '.join(missing_words)}"
            )
            continue

        logger.info("generate_story: success — %d sentences covering %d words (attempt %d) in %.1fs",
                    len(parsed), len(cards), attempt + 1, time.time() - t_start)
        _set_progress(progress_key, phase="translating",
                      msg="Translating sentences…", percent=88)
        _fill_translations(parsed, progress_key=progress_key, lang=lang)
        logger.info("generate_story: DONE — %.1fs total", time.time() - t_start)
        _set_progress(progress_key, phase="ai_done",
                      msg=f"✓ {len(parsed)} sentences (attempt {attempt + 1})", percent=93)
        return parsed, prompt

    if last_partial is not None:
        parsed, missing_ids = last_partial
        if len(missing_ids) / len(cards) < 0.03:
            _patch_missing(parsed, missing_ids, card_by_id, lang=lang)
            _set_progress(progress_key, phase="translating",
                          msg="Translating sentences…", percent=88)
            _fill_translations(parsed, progress_key=progress_key, lang=lang)
            _set_progress(progress_key, phase="ai_done",
                          msg=f"✓ {len(parsed)} sentences (patched)", percent=93)
            return parsed, prompt

    missing_count = len(last_partial[1]) if last_partial else len(cards)
    raise RuntimeError(
        f"Story generation failed after 3 attempts "
        f"({missing_count} word(s) still missing from the story). "
        "Please try again or switch to a different model."
    )


def _patch_missing(sentences: list[dict], missing_word_ids: list[int],
                   card_by_id: dict[int, dict], lang: str = "zh") -> None:
    """Append fallback sentences for words the AI failed to include."""
    for wid in missing_word_ids:
        card = card_by_id.get(wid)
        if not card:
            continue
        if lang == "zh":
            fallback_zh = card.get("source_sentence") or f"我学了{card['word_zh']}这个词。"
        else:
            fallback_zh = card.get("source_sentence") or f"J'ai appris le mot {card['word_zh']}."
        sentences.append({"word_ids": [wid], "sentence_zh": fallback_zh,
                          "sentence_en": "", "sentence_de": "", "sentence_fr": ""})


def regenerate_entry_fields(
    word: dict,
    characters: list[dict],
    fields: list[str],
    model: str = DEFAULT_MODEL,
) -> dict:
    """
    Regenerate specified fields for a vocabulary entry using SKILL.md format.

    fields: subset of ["notes", "examples", "etymology", "compounds"]
    Returns a dict with a subset of:
      notes:      str  (German prose)
      examples:   list[{zh, pinyin, english, de}]
      characters: list[{char, etymology?, compounds?}]
    """
    if not fields:
        return {}

    note_type = word.get("note_type", "vocabulary")
    word_zh   = word.get("word_zh", "")
    trad      = word.get("traditional", "")
    pinyin_   = word.get("pinyin", "")
    eng       = word.get("definition", "")
    de        = word.get("definition_de", "")
    hsk       = word.get("hsk_level", "")
    register  = word.get("register", "")

    want_notes    = "notes" in fields
    want_examples = "examples" in fields
    want_etym     = "etymology" in fields
    want_comp     = "compounds" in fields
    want_meanings = "other_meanings" in fields
    want_chars    = want_etym or want_comp or want_meanings
    want_def      = any(f in fields for f in ("definition", "definition_zh", "definition_de", "definition_fr", "pos"))

    # --- Entry header ---
    trad_line = f" / traditional: {trad}" if trad and trad != word_zh else ""
    entry_block = (
        f"type: {note_type}\n"
        f"simplified: {word_zh}{trad_line}\n"
        f"pinyin: {pinyin_}\n"
        f"english: {eng}\n"
        f"german: {de}\n"
        f"hsk: {hsk}\n"
        f"register: {register}"
    )

    # --- Character list (only when needed) ---
    char_block = ""
    if want_chars and characters:
        lines = [
            f"  - {c['char']} (pinyin: {c.get('pinyin', '')}, HSK {c.get('hsk_level', '?')})"
            for c in characters
        ]
        char_block = "\nCharacters:\n" + "\n".join(lines)

    # --- Per-field instructions ---
    sections: list[str] = []

    if want_def:
        def_lines = ["Generate concise one-line definitions. Keep each under 10 words."]
        if "pos" in fields:
            def_lines.append('- pos: part of speech abbreviation (e.g. "v.", "n.", "adj.", "adv.", "expr.", "pron.", "conj.")')
        if "definition" in fields:
            def_lines.append('- definition: English definition (e.g. "to study; to learn")')
        if "definition_zh" in fields:
            def_lines.append('- definition_zh: Chinese definition (e.g. "学习；研究")')
        if "definition_de" in fields:
            def_lines.append('- definition_de: German definition (e.g. "studieren; lernen")')
        if "definition_fr" in fields:
            def_lines.append('- definition_fr: French definition (e.g. "étudier; apprendre")')
        sections.append("DEFINITION FIELDS:\n" + "\n".join(def_lines))

    if want_notes:
        if note_type == "sentence":
            sections.append(
                "NOTES: Write a German explanation for this sentence (2-3 paragraphs).\n"
                "Include: breakdown of key vocabulary + grammar; list key components as "
                "「- 词语 (pinyin) — meaning」; explain grammar structures used."
            )
        else:
            sections.append(
                "NOTES: Write German usage notes (2-4 paragraphs). Include:\n"
                "- Opening sentence on what the word means and how it's used\n"
                "- **Häufige Ausdrücke:** with 3-5 collocations (「- 词语 (pinyin) — German meaning」)\n"
                "- **Wichtiger Unterschied:** comparing with a similar word (if applicable)\n"
                "- **Kulturelle Anmerkung:** (if relevant)"
            )

    if want_examples:
        sections.append(
            "EXAMPLES: Generate 3-4 example sentences. Each must use the target word verbatim.\n"
            "Each example: {\"zh\": \"<sentence>\", \"pinyin\": \"<full pinyin>\", "
            "\"english\": \"<translation>\", \"de\": \"<German translation>\"}"
        )

    if want_chars:
        n_chars = len(characters)
        char_field_lines = []
        if want_meanings:
            char_field_lines.append(
                "  - other_meanings: array of 2-4 short German strings giving the core meaning(s) of this single character "
                "(e.g. [\"tragen\", \"mitnehmen\", \"begleiten\"]). REQUIRED — do not omit."
            )
        if want_etym:
            char_field_lines.append(
                "  - etymology: 2-4 sentences German PROSE (NO bullet points) on components, "
                "historical origin, meaning evolution"
            )
        if want_comp:
            char_field_lines.append(
                "  - compounds: 3-5 common compound words using this character. "
                "Each: {\"simplified\": \"词\", \"pinyin\": \"...\", \"meaning\": \"German (NO colons)\"}"
            )
        sections.append(
            f"CHARACTER DATA: Return EXACTLY {n_chars} object(s) in the \"characters\" array — "
            f"one per character listed above. Do NOT skip any character.\n"
            + "\n".join(char_field_lines)
        )

    # --- JSON template ---
    json_keys = []
    if "pos" in fields:
        json_keys.append('  "pos": "v."')
    if "definition" in fields:
        json_keys.append('  "definition": "<English>"')
    if "definition_zh" in fields:
        json_keys.append('  "definition_zh": "<中文>"')
    if "definition_de" in fields:
        json_keys.append('  "definition_de": "<Deutsch>"')
    if "definition_fr" in fields:
        json_keys.append('  "definition_fr": "<français>"')
    if want_notes:
        json_keys.append('  "notes": "<German prose>"')
    if want_examples:
        json_keys.append('  "examples": [{"zh": "...", "pinyin": "...", "english": "...", "de": "..."}]')
    if want_chars:
        char_obj_keys = '"char": "X"'
        if want_meanings:
            char_obj_keys += ', "other_meanings": ["...", "..."]'
        if want_etym:
            char_obj_keys += ', "etymology": "..."'
        if want_comp:
            char_obj_keys += ', "compounds": [{"simplified": "...", "pinyin": "...", "meaning": "..."}]'
        # Show one example object per actual character so the AI knows the expected array length
        char_example = "{" + char_obj_keys + "}"
        char_array = ", ".join([char_example] * max(len(characters), 1))
        json_keys.append(f'  "characters": [{char_array}]')

    json_template = "{\n" + ",\n".join(json_keys) + "\n}"

    prompt = (
        f"You are a Chinese dictionary expert generating SRS flashcard content.\n\n"
        f"Entry:\n{entry_block}{char_block}\n\n"
        f"Generate ONLY the fields listed. All German text must be in German.\n\n"
        + "\n\n".join(sections)
        + f"\n\nReturn ONLY valid JSON with exactly these top-level keys:\n{json_template}"
    )

    logger.info("[%s] regenerate_entry_fields: %s fields=%s", model, word_zh, fields)
    raw = _call_api(model, [{"role": "user", "content": prompt}], max_tokens=2400,
                    purpose=f"regen:{word_zh}")

    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("regenerate_entry_fields: JSON parse error for %s: %s", word_zh, e)
        return {}


# ---------------------------------------------------------------------------
# Full dictionary entry as YAML (issue #627) — the in-app twin of the de-zh-bot
# skill. The output goes straight into importer.import_yaml_content(), so it
# must follow docs/yaml-format.md exactly; every field below has a consumer in
# importer._entry_to_word / _process_characters / _process_word_relations.
# ---------------------------------------------------------------------------

_ENTRY_YAML_EXAMPLE = """- type: word
  date: "03/27"
  simplified: 生态
  traditional: 生態
  pinyin: shēngtài
  english: ecology / ecosystem / (figurative) environment
  german: Ökologie / Ökosystem / (übertragen) Umfeld
  definition_zh: 生物与环境相互作用形成的系统；引申指互相关联的整体
  pos: noun
  hsk: "5"
  register: formal_written
  measure_word:
    - simplified: 种
      pinyin: zhǒng
      meaning: Art oder Typ (für Ökosysteme)
  note: |
    Ein Substantiv, das im wissenschaftlichen Sinne "Ökologie" und im weiteren
    Sinne "Ökosystem" bedeutet. Ursprünglich aus der Biologie, hat es sich auf
    vernetzte Systeme in Wirtschaft und Gesellschaft ausgeweitet.

    **Häufige Ausdrücke:**
    - 生态环境 (shēngtài huánjìng) — ökologische Umwelt
    - 生态系统 (shēngtài xìtǒng) — Ökosystem
  examples:
    - zh: 保护生态环境是我们每个人的责任。
      pinyin: Bǎohù shēngtài huánjìng shì wǒmen měi gè rén de zérèn.
      english: Protecting the ecological environment is everyone's responsibility.
      de: Die ökologische Umwelt zu schützen ist die Verantwortung eines jeden.
    - zh: 阿里巴巴构建了一个庞大的商业生态系统。
      pinyin: Ālǐbābā gòujiànle yī gè pángdà de shāngyè shēngtài xìtǒng.
      english: Alibaba has built a vast business ecosystem.
      de: Alibaba hat ein riesiges Geschäftsökosystem aufgebaut.
  synonyms:
    - simplified: 环境
      pinyin: huánjìng
      meaning: Umwelt, Umgebung
  word_analyses:
    - char_only: 生
      pinyin: shēng
      hsk: "1"
    - type: word
      simplified: 态
      traditional: 態
      pinyin: tài
      english: state, condition, form
      hsk: "4"
      characters:
        - char: 态
          simplified: 态
          traditional: 態
          pinyin: tài
          hsk: "4"
          detailed_analysis: true
          meaning_in_context: Zustand, Beschaffenheit
          compounds:
            - simplified: 状态
              pinyin: zhuàngtài
              meaning: Zustand, Verfassung
            - simplified: 态度
              pinyin: tàidù
              meaning: Haltung, Einstellung
          etymology: |
            Phonosemantische Verbindung. Die traditionelle Form 態 besteht aus
            dem Radikal 心 (Herz) und der phonetischen Komponente 能 (néng,
            "Fähigkeit"). Das Herzradikal weist auf einen geistigen Zustand hin.
"""

_ENTRY_YAML_PROMPT = """You are a Chinese dictionary expert producing an SRS flashcard entry for a
German-speaking learner (HSK 4-5). Generate a complete YAML entry for: {word}

Return ONLY the YAML — a single list item starting with "- type:". No markdown
fences, no commentary before or after.

TYPE — pick exactly one:
  word        a single word or compound (most common)
  chengyu     a four-character idiom
  expression  a fixed multi-word phrase or colloquial expression
  sentence    a full sentence (ends with 。？！)

REQUIRED FIELDS (all types): type, simplified, pinyin, english, german,
definition_zh, pos, hsk, date: "{today}".
  - traditional: only when it differs from simplified
  - register: one of spoken_colloquial, spoken_neutral, neutral, formal_written,
    literary, slang
  - hsk: a quoted single digit "1"-"6" (use the closest level for non-HSK words)
  - definition_zh: the meaning in simple Chinese (HSK 1-4 vocabulary)
  - pos: noun, verb, adjective, adverb, conjunction, measure word, …

LANGUAGE RULES — these fields MUST be German:
  note, explanations, etymology, meaning_in_context, compounds[].meaning,
  measure_word[].meaning, synonyms[].meaning, antonyms[].meaning, examples[].de
  examples[].english is English; simplified/pinyin are Chinese/pinyin.

CONTENT:
  - note (word/chengyu/expression) or explanations (sentence): German prose
    block scalar (|) explaining usage, common collocations, nuance
  - examples: 2-4 sentences, EACH with all four keys zh, pinyin, english, de
  - measure_word: only for nouns
  - synonyms / antonyms: include when they add real value (always for chengyu)
  - word_analyses: for word/chengyu/expression cover every component character:
      * HSK 1-2 characters → `char_only:` + pinyin + hsk
      * HSK 3+ characters → `type: word` block with a nested `characters:` list
        whose entry has detailed_analysis: true, meaning_in_context, 2-4
        compounds and a prose `etymology:` block scalar (no bullet points)
    For `sentence`, cover the 2-4 most important vocabulary items instead.

YAML SAFETY — the output is parsed by a strict loader:
  - Never use double quotes for meaning/english/german fields. If a colon is
    unavoidable inside an inline string, wrap it in single quotes; better yet,
    rephrase to avoid the colon.
  - note / explanations / etymology use block scalars (|) — colons are safe there.
  - Indent consistently with two spaces; no tab characters.

EXAMPLE of the expected shape (for 生态):

{example}
"""


# ---------------------------------------------------------------------------
# The French twin (issue #726) — same contract, different format: the entry is
# the French half of docs/yaml-format.md ("法语格式"), ported from the de-fr-bot
# skill. importer._normalize_fr_entry maps word/level/examples[].fr onto the
# internal keys, so everything downstream stays shared with Chinese.
# ---------------------------------------------------------------------------

_ENTRY_YAML_EXAMPLE_FR = """- type: word
  date: "07/21"
  word: parler
  pos: verbe
  english: to speak, to talk
  german: sprechen, reden
  level: "A1"
  register: neutral
  note: |
    Regelmäßiges Verb auf -er. Grundverb für „sprechen" — mit Sprache direkt
    danach (parler français), mit „de" für „über etwas sprechen" (parler de qc),
    mit „à" für den Gesprächspartner (parler à qn).

    **Häufige Ausdrücke:**
    - parler couramment — fließend sprechen
    - entendre parler de — von etwas hören

    **Étymologie:** Vom lateinischen *parabolare* („in Gleichnissen reden"),
    abgeleitet von *parabola* — derselbe Ursprung wie dt. „Parabel".
  examples:
    - fr: Je parle un peu français.
      english: I speak a little French.
      german: Ich spreche ein wenig Französisch.
    - fr: Nous avons parlé de toi hier.
      english: We talked about you yesterday.
      german: Wir haben gestern über dich gesprochen.
  synonyms:
    - word: discuter
      meaning: diskutieren, sich unterhalten
  antonyms:
    - word: se taire
      meaning: schweigen
  conjugations:
    présent:
      je: parle
      tu: parles
      il/elle: parle
      nous: parlons
      vous: parlez
      ils/elles: parlent
    passé composé:
      je: ai parlé
      tu: as parlé
      il/elle: a parlé
      nous: avons parlé
      vous: avez parlé
      ils/elles: ont parlé
    imparfait:
      je: parlais
      tu: parlais
      il/elle: parlait
      nous: parlions
      vous: parliez
      ils/elles: parlaient
    futur simple:
      je: parlerai
      tu: parleras
      il/elle: parlera
      nous: parlerons
      vous: parlerez
      ils/elles: parleront
    conditionnel présent:
      je: parlerais
      tu: parlerais
      il/elle: parlerait
      nous: parlerions
      vous: parleriez
      ils/elles: parleraient
    subjonctif présent:
      que je: parle
      que tu: parles
      qu'il/elle: parle
      que nous: parlions
      que vous: parliez
      qu'ils/elles: parlent
    impératif:
      tu: parle
      nous: parlons
      vous: parlez
    participe présent: parlant
    participe passé: parlé (avoir)

- type: word
  date: "07/21"
  word: le chat
  pos: nom (m)
  english: cat
  german: die Katze, der Kater
  level: "A1"
  register: neutral
  gender: m
  note: |
    Männliches Substantiv. Für weibliche Katzen sagt man "la chatte".

    **Étymologie:** Vom lateinischen *cattus*.
  examples:
    - fr: Le chat dort sur le canapé.
      english: The cat is sleeping on the couch.
      german: Die Katze schläft auf dem Sofa.
  forms:
    nombre:
      pluriel: chats

- type: word
  date: "07/21"
  word: vert
  pos: adjectif
  english: green
  german: grün
  level: "A1"
  register: neutral
  note: |
    Regelmäßiges Adjektiv, stimmt in Genus und Numerus mit dem Substantiv überein.
  examples:
    - fr: Le pull vert est à moi.
      english: The green sweater is mine.
      german: Der grüne Pullover gehört mir.
  forms:
    genre:
      féminin: verte
      féminin pluriel: vertes
    nombre:
      pluriel: verts
"""

_ENTRY_YAML_PROMPT_FR = """You are a French dictionary expert producing an SRS flashcard entry for a
German-speaking learner (CEFR B1, everyday spoken French). Generate a complete
YAML entry for: {word}

Return ONLY the YAML — a single list item starting with "- type:". No markdown
fences, no commentary before or after.

TYPE — pick exactly one:
  word        a single word (verb, noun, adjective, adverb)
  expression  a multi-word phrase acting as a unit (idiom, collocation)
  sentence    a full sentence

The headword key is named after the type: `word:`, `expression:` or `sentence:`.
There is no `simplified` field and no Chinese in the output.

REQUIRED FIELDS: type, the headword key, english, german, level, date: "{today}".
  - pos: French part of speech — verbe, nom (m), nom (f), adjectif, adverbe,
    locution … NOUNS ALWAYS CARRY THEIR GENDER. Omit pos for `sentence`.
  - level: a quoted CEFR string "A1"…"C2"
  - register: one of spoken_colloquial, spoken_neutral, neutral, formal_written,
    literary, slang
  - english / german: concise glosses; separate distinct meanings with " / "
  - gender: REQUIRED FOR EVERY NOUN, omitted for everything else. One of
    m, f, mf (structurally both, e.g. some nouns for people). This is the
    entry's OWN grammatical gender, separate from any inflected forms below.
  - forms: REQUIRED FOR EVERY NOUN AND ADJECTIVE, omitted for verbs/sentences.
    A mapping {{dimension: {{slot: form}}}} of inflected surface forms — nouns
    need "nombre: {{pluriel: <plural form>}}"; adjectives need BOTH
    "genre: {{féminin: <feminine>, féminin pluriel: <feminine plural>}}" AND
    "nombre: {{pluriel: <masculine plural>}}". Only include forms that
    genuinely differ from the headword (an invariable adjective like "rose"
    still needs its plural "roses" if that changes).

LANGUAGE RULES — these fields MUST be German:
  note, explanations, synonyms[].meaning, antonyms[].meaning, examples[].german
  examples[].english is English; the headword and examples[].fr are French.

CONTENT:
  - note (word/expression): German prose block scalar (|) covering usage,
    common collocations, false-friend warnings, and a short
    "**Étymologie:** …" line (1-2 sentences) — French has no separate
    etymology column, so it belongs in the note.
  - examples: 2-4 sentences, EACH with all three keys fr, english, german
  - synonyms / antonyms: {{word, meaning}} items; include when they add real value
  - conjugations: REQUIRED FOR EVERY VERB, omitted for everything else.
    All of: présent, passé composé, imparfait, futur simple,
    conditionnel présent, subjonctif présent, impératif, participe présent,
    participe passé. Person keys are exactly je, tu, il/elle, nous, vous,
    ils/elles (subjonctif: que je, que tu, qu'il/elle, que nous, que vous,
    qu'ils/elles; impératif: only tu, nous, vous). The person key stays `je`
    even where elision would give `j'…`, and the form is what follows the
    pronoun (ai parlé, irai). passé composé includes the auxiliary; participe
    passé names it in parentheses — parlé (avoir), allé (être). Pronominal
    verbs keep the reflexive pronoun in the form (me lève). Never guess
    irregular forms.
  - sentence type: use `explanations` (German block scalar) instead of note,
    add `source_de` with the German original, and optionally
    similar_sentences: [{{fr, german}}].

YAML SAFETY — the output is parsed by a strict loader:
  - Never use double quotes for meaning/english/german fields. If a colon is
    unavoidable inside an inline string, wrap it in single quotes; better yet,
    rephrase to avoid the colon.
  - A French apostrophe inside a single-quoted string must be doubled
    ('l''école'). Prefer leaving such values unquoted.
  - note / explanations use block scalars (|) — colons are safe there.
  - Indent consistently with two spaces; no tab characters.

EXAMPLES of the expected shape (a verb with conjugations, a noun with
gender+forms, an adjective with forms):

{example}
"""


# ---------------------------------------------------------------------------
# The Spanish twin (issue #805) — same Romance-family contract as French
# (docs/yaml-format.md "西班牙语格式"), just different vocabulary: gender,
# forms (plural/feminine), and a fuller conjugation table (Spanish distinguishes
# more tenses in everyday speech than the French set already covers).
# importer._normalize_romance_entry(entry, "es") maps word/level/examples[].es
# onto the internal keys the same way it does for French.
# ---------------------------------------------------------------------------

_ENTRY_YAML_EXAMPLE_ES = """- type: word
  date: "07/21"
  word: hablar
  pos: verbo
  english: to speak, to talk
  german: sprechen, reden
  level: "A1"
  register: neutral
  note: |
    Regelmäßiges Verb auf -ar. Grundverb für „sprechen" — mit Sprache direkt
    danach (hablar español), mit „de" für „über etwas sprechen" (hablar de algo),
    mit „con" für den Gesprächspartner (hablar con alguien).

    **Etimología:** Vom lateinischen *fabulari* („erzählen, plaudern").
  examples:
    - es: Hablo un poco de español.
      english: I speak a little Spanish.
      german: Ich spreche ein wenig Spanisch.
    - es: Hablamos de ti ayer.
      english: We talked about you yesterday.
      german: Wir haben gestern über dich gesprochen.
  synonyms:
    - word: charlar
      meaning: plaudern, sich unterhalten
  antonyms:
    - word: callarse
      meaning: schweigen
  conjugations:
    presente:
      yo: hablo
      tú: hablas
      él/ella: habla
      nosotros: hablamos
      vosotros: habláis
      ellos/ellas: hablan
    pretérito perfecto:
      yo: he hablado
      tú: has hablado
      él/ella: ha hablado
      nosotros: hemos hablado
      vosotros: habéis hablado
      ellos/ellas: han hablado
    pretérito indefinido:
      yo: hablé
      tú: hablaste
      él/ella: habló
      nosotros: hablamos
      vosotros: hablasteis
      ellos/ellas: hablaron
    imperfecto:
      yo: hablaba
      tú: hablabas
      él/ella: hablaba
      nosotros: hablábamos
      vosotros: hablabais
      ellos/ellas: hablaban
    futuro:
      yo: hablaré
      tú: hablarás
      él/ella: hablará
      nosotros: hablaremos
      vosotros: hablaréis
      ellos/ellas: hablarán
    condicional:
      yo: hablaría
      tú: hablarías
      él/ella: hablaría
      nosotros: hablaríamos
      vosotros: hablaríais
      ellos/ellas: hablarían
    presente de subjuntivo:
      yo: hable
      tú: hables
      él/ella: hable
      nosotros: hablemos
      vosotros: habléis
      ellos/ellas: hablen
    participio: hablado
    gerundio: hablando

- type: word
  date: "07/21"
  word: el gato
  pos: sustantivo (m)
  english: cat
  german: die Katze, der Kater
  level: "A1"
  register: neutral
  gender: m
  note: |
    Männliches Substantiv. Für weibliche Katzen sagt man "la gata".

    **Etimología:** Vom lateinischen *cattus*.
  examples:
    - es: El gato duerme en el sofá.
      english: The cat is sleeping on the couch.
      german: Die Katze schläft auf dem Sofa.
  forms:
    numero:
      plural: gatos

- type: word
  date: "07/21"
  word: verde
  pos: adjetivo
  english: green
  german: grün
  level: "A1"
  register: neutral
  note: |
    Regelmäßiges Adjektiv. Endet auf -e, deshalb gleiche Form für Maskulin
    und Femininum — nur die Pluralform ändert sich.
  examples:
    - es: El jersey verde es mío.
      english: The green sweater is mine.
      german: Der grüne Pullover gehört mir.
  forms:
    numero:
      plural: verdes
"""

_ENTRY_YAML_PROMPT_ES = """You are a Spanish dictionary expert producing an SRS flashcard entry for a
German-speaking learner (CEFR A2, everyday spoken Spanish). Generate a complete
YAML entry for: {word}

Return ONLY the YAML — a single list item starting with "- type:". No markdown
fences, no commentary before or after.

TYPE — pick exactly one:
  word        a single word (verb, noun, adjective, adverb)
  expression  a multi-word phrase acting as a unit (idiom, collocation)
  sentence    a full sentence

The headword key is named after the type: `word:`, `expression:` or `sentence:`.
There is no `simplified` field and no Chinese in the output.

REQUIRED FIELDS: type, the headword key, english, german, level, date: "{today}".
  - pos: Spanish part of speech — verbo, sustantivo (m), sustantivo (f),
    adjetivo, adverbio, locución … NOUNS ALWAYS CARRY THEIR GENDER. Omit pos
    for `sentence`.
  - level: a quoted CEFR string "A1"…"C2"
  - register: one of spoken_colloquial, spoken_neutral, neutral, formal_written,
    literary, slang
  - english / german: concise glosses; separate distinct meanings with " / "
  - gender: REQUIRED FOR EVERY NOUN, omitted for everything else. One of
    m, f, mf (structurally both, e.g. some nouns for people). This is the
    entry's OWN grammatical gender, separate from any inflected forms below.
  - forms: REQUIRED FOR EVERY NOUN AND ADJECTIVE, omitted for verbs/sentences.
    A mapping {{dimension: {{slot: form}}}} of inflected surface forms — nouns
    need "numero: {{plural: <plural form>}}"; adjectives need "numero:
    {{plural: <plural form>}}" and, when the adjective actually varies by
    gender (most -o/-a adjectives do, -e ones usually don't), also "genero:
    {{femenino: <feminine>, femenino plural: <feminine plural>}}". Only include
    forms that genuinely differ from the headword.

LANGUAGE RULES — these fields MUST be German:
  note, explanations, synonyms[].meaning, antonyms[].meaning, examples[].german
  examples[].english is English; the headword and examples[].es are Spanish.

CONTENT:
  - note (word/expression): German prose block scalar (|) covering usage,
    common collocations, false-friend warnings, and a short
    "**Etimología:** …" line (1-2 sentences) — Spanish has no separate
    etymology column, so it belongs in the note.
  - examples: 2-4 sentences, EACH with all three keys es, english, german
  - synonyms / antonyms: {{word, meaning}} items; include when they add real value
  - conjugations: REQUIRED FOR EVERY VERB, omitted for everything else.
    All of: presente, pretérito perfecto, pretérito indefinido, imperfecto,
    futuro, condicional, presente de subjuntivo, participio, gerundio.
    Person keys are exactly yo, tú, él/ella, nosotros, vosotros, ellos/ellas
    (omit vosotros only if you are certain the learner only needs Latin
    American Spanish — default to including it). participio/gerundio are
    plain strings (no person). Never guess irregular forms (ser, estar, ir,
    tener, hacer, poder, querer, decir, poner, saber, venir … are all
    irregular — get them right).
  - sentence type: use `explanations` (German block scalar) instead of note,
    add `source_de` with the German original, and optionally
    similar_sentences: [{{es, german}}].

YAML SAFETY — the output is parsed by a strict loader:
  - Never use double quotes for meaning/english/german fields. If a colon is
    unavoidable inside an inline string, wrap it in single quotes; better yet,
    rephrase to avoid the colon.
  - A Spanish apostrophe (rare, but e.g. in loanwords) inside a single-quoted
    string must be doubled. Prefer leaving such values unquoted.
  - note / explanations use block scalars (|) — colons are safe there.
  - Indent consistently with two spaces; no tab characters.

EXAMPLES of the expected shape (a verb with conjugations, a noun with
gender+forms, an adjective with forms):

{example}
"""


# lang -> (prompt template, example block). Every Romance language shares the
# same contract (docs/multilang.md); adding a new one is one more entry here
# plus its own prompt/example pair, no other code path changes (issue #805).
_ENTRY_YAML_TEMPLATES = {
    "zh": (_ENTRY_YAML_PROMPT, _ENTRY_YAML_EXAMPLE),
    "fr": (_ENTRY_YAML_PROMPT_FR, _ENTRY_YAML_EXAMPLE_FR),
    "es": (_ENTRY_YAML_PROMPT_ES, _ENTRY_YAML_EXAMPLE_ES),
}


def generate_word_entry_yaml(word_zh: str, model: str = DEFAULT_MODEL,
                             lang: str = "zh") -> str:
    """Generate a complete dictionary YAML entry for one word.

    `lang` picks the prompt and the output format: 'zh' produces a de-zh-bot
    entry, 'fr'/'es' the de-fr-bot-style Romance format (issues #726, #805).
    Returns YAML ready for importer.import_yaml_content(). Raises ValueError
    if the model returns something that isn't a YAML list item — callers
    surface that to the user rather than importing garbage.

    Non-Chinese output is wrapped in a `lang:` header rather than relying on
    the target deck's language: the entry format and the lang have to agree, so
    stating it in the document removes a whole class of "French entry imported
    as Chinese" bugs.
    """
    from datetime import date as _date

    template, example = _ENTRY_YAML_TEMPLATES.get(lang, (_ENTRY_YAML_PROMPT, _ENTRY_YAML_EXAMPLE))
    prompt = template.format(
        word=word_zh,
        today=_date.today().strftime("%m/%d"),
        example=example,
    )

    logger.info("[%s] generate_word_entry_yaml (%s): %s", model, lang, word_zh)
    raw = _call_api(model, [{"role": "user", "content": prompt}], max_tokens=4000,
                    purpose=f"add_word:{word_zh}")

    # Strip markdown fences if the model wrapped the YAML despite being told not to
    fenced = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)

    # Drop any prose before the list item (some models add a lead-in line)
    start = raw.find("- type:")
    if start == -1:
        logger.error("generate_word_entry_yaml: no list item in response for %r: %s",
                     word_zh, raw[:300])
        raise ValueError("AI did not return a YAML entry")

    entry = raw[start:].rstrip() + "\n"
    if lang == "zh":
        return entry
    return f"lang: {lang}\nentries:\n" + textwrap.indent(entry, "  ")


DICTIONARY_PROMPT = """You are a Chinese dictionary for a German-speaking learner (Daniel, HSK 4-5).
He types a word, phrase, or sentence in Chinese, German, or English and wants a
structured lookup, not a chat reply. Never use Japanese.

Behavior rules:
- Chinese input -> give the FULL dictionary entry directly. List EVERY sense
  (meaning) of the word/phrase, each as its own group. Do not ask him to
  choose anything and do not present alternative translations to pick from
  — there is nothing to choose, he already has the Chinese word.
- German or English input (word, phrase, or a full sentence) -> always give an
  ANALYSIS: an overall translation plus one or more groups of candidate
  Chinese translations, each option labeled a/b/c... (NEVER use 1/2/3).
  Exactly one option per group may be "recommended": true, and the
  recommendation should lean toward spoken/colloquial Chinese (kǒuyǔ) — this
  is Daniel's strongest, most repeated preference.
- A full German sentence as input -> first give the whole-sentence Chinese
  translation + pinyin (the "sentence" field), THEN break the sentence into
  its components (each a candidate group of its own) so every component is
  individually addable as a word.
- Explanations, usage notes, and example sentence translations are always in
  GERMAN. A French equivalent may optionally be added to a single-word option
  (the "fr" field) — never for phrases or sentences.

Quality rules (these decide whether the entry is useful at all):
- At most 3-5 options per group, and only GENUINELY DIFFERENT translations —
  different register, connotation, or part of speech. Do not pad a group with
  near-synonyms; two options that a learner would use interchangeably are one
  option.
- EVERY option must carry example_zh + example_pinyin + example_de. An option
  without an example is unusable: register claims are only believable when the
  sentence shows them.
- "usage" must say WHEN to use it (register, context, connotation), not repeat
  the translation.
- Pinyin always with tone marks (pài, not pai4 or pai).
- "register" must be one of: spoken_colloquial, spoken_neutral, neutral,
  formal_written, literary, slang.
- For Chinese input, "headline" is the input word itself and each group's
  "label" names one sense in German (e.g. "1. Ökologie (Biologie)").
- "kind" is exactly one of: "chinese" (any Chinese input), "word",
  "phrase", "sentence" (German/English input, by length). Only "sentence"
  makes the "sentence" field appear.

Output ONLY raw JSON — no markdown code fence, no prose before or after it.
Match this shape exactly (omit "sentence" unless kind == "sentence"; "fr" is
optional; every group needs at least one option; option "key" values are
a/b/c... never digits):

{{
  "input_lang": "de",
  "kind": "phrase",
  "headline": "派任务",
  "headline_pinyin": "pài rènwu",
  "headline_de": "jemandem eine Aufgabe geben",
  "notes": "kurze deutsche Anmerkung, optional",
  "sentence": {{
    "zh": "你下周什么时候有空？",
    "pinyin": "Nǐ xià zhōu shénme shíhou yǒu kòng?",
    "de": "Wann hast du nächste Woche Zeit?"
  }},
  "groups": [
    {{
      "label": "setzen / assign (Verb)",
      "options": [
        {{
          "key": "a",
          "zh": "派",
          "pinyin": "pài",
          "de": "beauftragen, jdm. eine Aufgabe geben",
          "fr": "assigner",
          "usage": "sehr umgangssprachlich, im Alltag am natürlichsten.",
          "register": "spoken_colloquial",
          "recommended": true,
          "example_zh": "老师又给我派任务了。",
          "example_pinyin": "Lǎoshī yòu gěi wǒ pài rènwu le.",
          "example_de": "Der Lehrer hat mir schon wieder eine Aufgabe gegeben."
        }}
      ]
    }}
  ]
}}

Input to look up: {query}"""


# ---------------------------------------------------------------------------
# Romance-language dictionary (issue #805) — ported from the de-fr-bot skill,
# generalized to fr/es via {lang_name}/{level}. The JSON CONTRACT IS IDENTICAL
# to DICTIONARY_PROMPT above (same "headline"/"kind"/"sentence"/"groups[].
# options[]" shape, same field names, "zh"/"pinyin" included) so the /dict
# frontend never has to branch on language — the target-language word/pinyin
# just goes in the "zh"/"pinyin" slots regardless of what language they
# actually hold (pinyin is simply omitted for Romance languages, which have
# no tone-mark transcription need).
# ---------------------------------------------------------------------------

DICTIONARY_PROMPT_ROMANCE = """You are a {lang_name} dictionary for a German-speaking learner (Daniel, {level}).
He types a word, phrase, or sentence in {lang_name}, German, or English and wants
a structured lookup, not a chat reply. Never use Japanese or Chinese.

Behavior rules:
- {lang_name} input -> give the FULL dictionary entry directly. List EVERY sense
  (meaning) of the word/phrase, each as its own group. Do not ask him to
  choose anything and do not present alternative translations to pick from
  — there is nothing to choose, he already has the {lang_name} word.
- German or English input (word, phrase, or a full sentence) -> always give an
  ANALYSIS: an overall translation plus one or more groups of candidate
  {lang_name} translations, each option labeled a/b/c... (NEVER use 1/2/3).
  Exactly one option per group may be "recommended": true, and the
  recommendation should lean toward everyday spoken {lang_name} (langage
  courant) — this is Daniel's strongest, most repeated preference.
- A full German sentence as input -> first give the whole-sentence {lang_name}
  translation (the "sentence" field, no pinyin — leave "pinyin" out), THEN
  break the sentence into its components (each a candidate group of its own)
  so every component is individually addable as a word.
- Explanations, usage notes, and example sentence translations are always in
  GERMAN.

Quality rules (these decide whether the entry is useful at all):
- At most 3-5 options per group, and only GENUINELY DIFFERENT translations —
  different register, connotation, or part of speech. Do not pad a group with
  near-synonyms; two options that a learner would use interchangeably are one
  option.
- EVERY option must carry example_zh (the {lang_name} example sentence) +
  example_de. An option without an example is unusable: register claims are
  only believable when the sentence shows them. Omit example_pinyin entirely
  — {lang_name} uses the Latin alphabet, there is no separate transcription.
- "usage" must say WHEN to use it (register, context, connotation), not repeat
  the translation.
- Do not fill in "pinyin" or "headline_pinyin" for {lang_name} — leave them out.
- "register" must be one of: spoken_colloquial, spoken_neutral, neutral,
  formal_written, literary, slang.
- For {lang_name} input, "headline" is the input word itself and each group's
  "label" names one sense in German (e.g. "1. Ökologie (Biologie)").
- "kind" is exactly one of: "chinese" (reused here to mean "target-language
  input", i.e. any {lang_name} input), "word", "phrase", "sentence"
  (German/English input, by length). Only "sentence" makes the "sentence"
  field appear.

Output ONLY raw JSON — no markdown code fence, no prose before or after it.
Match this shape exactly (omit "sentence" unless kind == "sentence"; omit
"pinyin"/"headline_pinyin" always; every group needs at least one option;
option "key" values are a/b/c... never digits). Note: the JSON field is
literally named "zh" for historical reasons (this contract is shared with the
Chinese dictionary) — put the {lang_name} word/sentence there:

{{
  "input_lang": "de",
  "kind": "phrase",
  "headline": "assigner une tâche",
  "headline_de": "jemandem eine Aufgabe geben",
  "notes": "kurze deutsche Anmerkung, optional",
  "sentence": {{
    "zh": "Tu es libre quand la semaine prochaine ?",
    "de": "Wann hast du nächste Woche Zeit?"
  }},
  "groups": [
    {{
      "label": "assigner / donner une tâche (Verb)",
      "options": [
        {{
          "key": "a",
          "zh": "confier une tâche",
          "de": "jdm. eine Aufgabe anvertrauen",
          "usage": "neutral, im Alltag üblich.",
          "register": "spoken_neutral",
          "recommended": true,
          "example_zh": "Le prof m'a encore confié une tâche.",
          "example_de": "Der Lehrer hat mir schon wieder eine Aufgabe gegeben."
        }}
      ]
    }}
  ]
}}

Input to look up: {query}"""


def _strip_code_fence(raw: str) -> str:
    """Some models wrap JSON in ```json ... ``` despite being told not to."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL)
    return fenced.group(1) if fenced else raw


def dictionary_lookup(query: str, lang: str = "zh", model: str | None = None) -> tuple[dict, str]:
    """Look up a word/phrase/sentence for the /dict page (#746).

    Returns (parsed result dict, model actually used). Raises ValueError if
    the model's response doesn't parse into the expected shape — callers must
    not store or return a placeholder, since a blank dictionary entry is worse
    than an error (routes/dictionary.py turns this into a 500).

    lang is the target vocabulary language: 'zh', 'fr' or 'es' (#805).
    Silently answering a French/Spanish request with the Chinese prompt (or
    vice versa) would produce a plausible-looking but wrong entry, exactly the
    failure #726 guarded against on the add-word side — so an unsupported lang
    raises rather than falling back.
    """
    use_model = model or DEFAULT_MODEL
    if lang == "zh":
        prompt = DICTIONARY_PROMPT.format(query=query)
    elif lang in ("fr", "es"):
        cfg = languages.get_lang_config(lang)
        prompt = DICTIONARY_PROMPT_ROMANCE.format(
            query=query, lang_name=cfg["name_en"], level=cfg["learner_level"],
        )
    else:
        raise ValueError(f"dictionary_lookup: language {lang!r} is not supported yet (zh/fr/es only)")

    logger.info("[%s] dictionary_lookup: %s", use_model, query)
    raw = _call_api(use_model, [{"role": "user", "content": prompt}], max_tokens=4000,
                    purpose="dictionary", thinking=False)

    stripped = _strip_code_fence(raw).strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"dictionary_lookup: could not parse AI response as JSON: {e}. "
            f"Raw response (first 500 chars): {raw[:500]!r}"
        )

    if not isinstance(result, dict):
        raise ValueError(
            f"dictionary_lookup: expected a JSON object, got {type(result).__name__}. "
            f"Raw response (first 500 chars): {raw[:500]!r}"
        )

    groups = result.get("groups")
    if not isinstance(groups, list):
        raise ValueError(
            f"dictionary_lookup: 'groups' missing or not a list. "
            f"Raw response (first 500 chars): {raw[:500]!r}"
        )
    for group in groups:
        options = group.get("options") if isinstance(group, dict) else None
        if not isinstance(options, list) or not options:
            raise ValueError(
                f"dictionary_lookup: a group is missing non-empty 'options'. "
                f"Raw response (first 500 chars): {raw[:500]!r}"
            )
        for option in options:
            if not isinstance(option, dict) or not option.get("zh"):
                raise ValueError(
                    f"dictionary_lookup: an option is missing non-empty 'zh'. "
                    f"Raw response (first 500 chars): {raw[:500]!r}"
                )

    return result, use_model


SENTENCE_QUESTION_PROMPT = """You are helping Daniel, a German-speaking Chinese
learner (HSK 4-5), understand a sentence from his flashcard review. Story
sentences are AI-generated and occasionally come out grammatically wrong or
unnatural — this feature exists precisely so he can catch that.

Sentence: {sentence_zh}
Target word being reviewed: {word_zh}

Answer in two parts, in this order:
1. First, judge the sentence itself: is it natural? Any grammar mistakes? Would
   a native speaker actually say this? If something is off, say so plainly —
   do not politely call a bad sentence good just to avoid conflict. If the
   sentence is fine, say briefly that it's fine and move on; don't manufacture
   a problem.
2. Then answer this question about the sentence: {question}

Write your whole answer in SIMPLE CHINESE using only HSK 4-5 vocabulary or
easier. Never use Japanese. If you need a word above that level, append its
reading and German meaning in parentheses right after it, like this:
比较（bǐjiào - vergleichsweise）. Keep the answer short — a few sentences, not
an essay. Plain text only, no markdown, no JSON."""


def ask_about_sentence(sentence_zh: str, question: str = "", word_zh: str | None = None,
                        lang: str = "zh", model: str = DEFAULT_MODEL) -> str:
    """Answer a one-off question about a review sentence (issue #853).

    Single-turn, no follow-up — same shape as dictionary_lookup(). The prompt
    always asks the model to judge the sentence's own quality first (story
    generation occasionally produces awkward or wrong sentences) before
    answering whatever Daniel actually typed; an empty question defaults to
    "is anything wrong with this sentence?".

    lang is accepted for future non-Chinese decks but the answer language is
    always simple Chinese per the issue — there is nothing to switch on yet.
    Returns plain text (not JSON); callers render it with textContent.
    """
    q = question.strip() if question else "这句话有没有问题？"
    prompt = SENTENCE_QUESTION_PROMPT.format(
        sentence_zh=sentence_zh, word_zh=word_zh or "(none)", question=q,
    )
    logger.info("[%s] ask_about_sentence: %s", model, sentence_zh)
    raw = _call_api(model, [{"role": "user", "content": prompt}], max_tokens=800,
                    purpose="sentence_question", thinking=False)
    return raw.strip()


def generate_character_info(char: str, pinyin: str, model: str = DEFAULT_MODEL) -> dict:
    """
    Generate etymology and translation for a single Chinese character.
    Returns: {etymology: str, translation: str}
    """
    prompt = f"""For the Chinese character {char} (pinyin: {pinyin}), provide:
1. An etymological description: explain the character's components, historical origin, and meaning evolution (2-4 sentences)
2. A concise English translation: 2-5 words covering the core meaning

Return ONLY valid JSON, no explanation, no markdown:
{{"etymology": "<etymological description>", "translation": "<concise English meaning>"}}"""

    raw = _call_api(model, [{"role": "user", "content": prompt}], max_tokens=400,
                    purpose=f"hanzi:{char}")

    json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    try:
        result = json.loads(raw)
        return {
            "etymology": result.get("etymology", ""),
            "translation": result.get("translation", ""),
        }
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.error("generate_character_info: JSON parse error: %s", e)
        return {"etymology": "", "translation": ""}


_ENRICH_MODEL = "deepseek-v4-flash"


def enrich_word(word: dict, characters: list[dict], model: str = DEFAULT_MODEL) -> dict:
    """
    Determine HSK level for a word and fill missing character data (etymology, other_meanings).
    Always uses DeepSeek — the model parameter is ignored.
    Only requests data for fields that are currently empty.
    Returns: {hsk_level: int|None, characters: [{char, etymology, other_meanings}]}
    """
    # Identify which characters need which fields
    chars_needing_data = []
    for c in characters:
        needs = []
        if not c.get("etymology"):
            needs.append("etymology")
        if not c.get("other_meanings"):
            needs.append("other_meanings (array of short English meanings)")
        if needs:
            chars_needing_data.append(
                f'  - {c["char"]} (pinyin: {c.get("pinyin", "")}) → needs: {", ".join(needs)}'
            )

    char_section = ""
    if chars_needing_data:
        char_section = (
            "\n\nFor each character below, provide only the requested fields:\n"
            + "\n".join(chars_needing_data)
            + '\n\nReturn these under "characters" as an array of objects with keys: '
            '"char", "etymology" (2–4 sentences on origin & components), '
            '"other_meanings" (array of 2–5 short English strings).'
        )
    else:
        char_section = '\n\nNo character data needed — return "characters": [].'

    prompt = f"""You are a Chinese language expert. For the word {word["word_zh"]} \
({word.get("pinyin", "")}) — {word.get("definition", "")}:

1. What is its HSK level (1–6)? Return null if it is not in the standard HSK list.{char_section}

Return ONLY valid JSON, no explanation, no markdown:
{{
  "hsk_level": <integer 1-6 or null>,
  "characters": [
    {{"char": "<char>", "etymology": "<text>", "other_meanings": ["<m1>", "<m2>"]}}
  ]
}}"""

    raw = _call_api(_ENRICH_MODEL, [{"role": "user", "content": prompt}], max_tokens=800,
                    purpose=f"enrich:{word['word_zh']}")

    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    try:
        result = json.loads(raw)
        return {
            "hsk_level": result.get("hsk_level"),
            "characters": result.get("characters", []),
        }
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.error("enrich_word: JSON parse error: %s", e)
        return {"hsk_level": None, "characters": []}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fill_translations(sentences: list[dict], progress_key: str | None = None,
                       lang: str = "zh") -> None:
    """Translate sentence_zh → sentence_de in-place using Google Translate."""
    try:
        import translator as _t
        source = languages.get_lang_config(lang)["translator_source"]
        texts = [s.get("sentence_zh", "") for s in sentences]
        total = len(texts)

        if progress_key and total > 0:
            _set_progress(progress_key, phase="translating",
                          msg=f"Translating… 0/{total}", percent=88)

        # Real per-chunk progress (#756): the loading screen used to sit at
        # 0/N for the whole translation and then jump straight to N/N.
        def _on_progress(done: int, n: int) -> None:
            _set_progress(progress_key, phase="translating",
                          msg=f"Translating… {done}/{n}",
                          percent=88 + 4 * done / n)

        t0 = time.time()
        de_results = _t.translate_batch(texts, target="de", source=source,
                                        on_progress=_on_progress if progress_key else None)
        logger.info("translate DE done in %.1fs (%d sentences)", time.time() - t0, total)

        if progress_key and total > 0:
            _set_progress(progress_key, phase="translating",
                          msg=f"Translating… {total}/{total}", percent=92)

        for s, de in zip(sentences, de_results):
            s["sentence_de"] = de
            s.setdefault("sentence_en", "")
            s.setdefault("sentence_fr", "")
    except StoryCancelled:
        raise  # a cancel is not a translation failure — let it out (#828)
    except Exception as e:
        err = str(e)
        vpn_hint = " (VPN issue?)" if any(k in err.lower() for k in ("eof", "connect", "timeout", "proxy", "ssl")) else ""
        logger.warning("_fill_translations: fallback to empty — %s%s", e, vpn_hint)
        if progress_key and progress_key in _story_progress:
            _story_progress[progress_key]["translate_warn"] = f"⚠ Translation failed{vpn_hint}"
        for s in sentences:
            s.setdefault("sentence_en", "")
            s.setdefault("sentence_de", "")
            s.setdefault("sentence_fr", "")


def _fallback_sentences(cards: list[dict], lang: str = "zh") -> list[dict]:
    """Minimal sentences used when the AI response cannot be parsed."""
    fallback = (lambda w: f"我学了{w}这个词。") if lang == "zh" else (lambda w: f"J'ai appris le mot {w}.")
    result = [
        {
            "word_ids": [c["word_id"]],
            "sentence_zh": fallback(c["word_zh"]),
            "sentence_en": "",
            "sentence_de": "",
            "sentence_fr": "",
        }
        for c in cards
    ]
    _fill_translations(result, lang=lang)
    return result


_FIX_BATCH = 25  # words per DeepSeek call


def _needs_comma_fix(text: str | None) -> bool:
    if not text or len(text) < 15:
        return False
    return "," not in text and ";" not in text and "/" not in text


def fix_definition_commas(cards: list[dict]) -> int:
    """Add missing commas to English/German definitions of today's due words.

    Sends batches to DeepSeek, updates entries in-place and in the DB.
    Returns the number of entries actually updated.
    """
    to_fix = [
        {"id": c["word_id"], "word_zh": c["word_zh"],
         "en": c.get("definition"), "de": c.get("definition_de")}
        for c in cards
        if _needs_comma_fix(c.get("definition")) or _needs_comma_fix(c.get("definition_de"))
    ]
    # deduplicate by word_id
    seen: set[int] = set()
    unique: list[dict] = []
    for item in to_fix:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    if not unique:
        return 0

    logger.info("fix_commas  %d entries need comma repair", len(unique))
    total_fixed = 0

    for i in range(0, len(unique), _FIX_BATCH):
        batch = unique[i:i + _FIX_BATCH]
        word_lines = "\n".join(
            f'{item["word_zh"]} | EN: {item["en"] or ""} | DE: {item["de"] or ""}'
            for item in batch
        )
        prompt = (
            "The following Chinese vocabulary definitions are missing commas between "
            "their separate meanings. Add commas (or slashes where a slash is the natural "
            "separator) to make the meanings clearly distinct. Do not add or remove meanings, "
            "only insert the missing punctuation.\n\n"
            "Return ONLY a JSON array. Each element: "
            '{"word_zh": "...", "en": "fixed English or null", "de": "fixed German or null"}\n\n'
            f"Words:\n{word_lines}"
        )
        try:
            raw = _call_api("deepseek-v4-flash",
                            [{"role": "user", "content": prompt}],
                            max_tokens=2000, purpose="fix_commas")
            # extract JSON array from response
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                logger.warning("fix_commas  no JSON array in response")
                continue
            updates = json.loads(m.group())
            id_map = {item["word_zh"]: item["id"] for item in batch}
            for upd in updates:
                wid = id_map.get(upd.get("word_zh"))
                if not wid:
                    continue
                fields: dict = {}
                if upd.get("en"):
                    fields["definition"] = upd["en"]
                if upd.get("de"):
                    fields["definition_de"] = upd["de"]
                if fields:
                    database.update_word(wid, fields)
                    # also patch the in-memory card dicts so the story prompt sees new values
                    for c in cards:
                        if c.get("word_id") == wid:
                            c.update(fields)
                    total_fixed += 1
        except Exception as e:
            logger.warning("fix_commas  batch error: %s", e)

    logger.info("fix_commas  updated %d entries", total_fixed)
    return total_fixed


def generate_kahneman_sentences(
    cards: list[dict],
    chapter: dict,
    model: str = DEFAULT_MODEL,
    progress_key: str | None = None,
    attempt_label: str = "",
) -> list[dict]:
    """Generate sentences in the style of Kahneman's "Speaking of..." chapter endings.

    Each sentence uses one vocabulary word and implicitly reveals the cognitive bias
    described in `chapter`. Returns list of sentence dicts with concept fields attached.

    cards:   vocab cards assigned to this chapter (word_id, word_zh, pinyin, definition)
    chapter: {number, title_zh, title_en, concept_zh, concept_en, examples_zh}
    """
    if not cards:
        return []

    def _word_in_sentence(word_zh: str, sentence_zh: str) -> bool:
        if '...' in word_zh or '…' in word_zh:
            chars = [c for c in word_zh if c not in '.…']
            return all(c in sentence_zh for c in chars)
        return word_zh in sentence_zh

    examples_block = "\n".join(f"  {ex}" for ex in chapter.get("examples_zh", []))
    concept_label = f"第{chapter['number']}章《{chapter['title_zh']}》：{chapter['concept_zh']}"
    concept_en = f"Chapter {chapter['number']}: {chapter['title_en']}"
    concept_zh = f"第{chapter['number']}章：{chapter['title_zh']}"
    summary_zh = (chapter.get("summary_zh") or "").strip()
    summary_block = f"\n本章机制与典型情境：\n{summary_zh}\n" if summary_zh else ""

    def _build_prompt(batch: list[dict]) -> str:
        word_list = "\n".join(
            f"{i + 1}. {c['word_zh']}（{c.get('pinyin', '')}）— {c.get('definition', '')}"
            for i, c in enumerate(batch)
        )
        return f"""任务：模仿《思考，快与慢》每章末尾"示例"部分的风格，写若干中文句子帮助HSK 4-5学习者复习词汇。
每个句子应该是某人在日常情境中说的一句话，自然地透露出一种认知偏误或心理定势，而不直接点明偏误名称。

本章概念：{concept_label}
{summary_block}
风格范例（模仿这种语气和结构）：
{examples_block}

目标词汇（每句话必须恰好包含其中一个，原文出现）：
{word_list}

写作步骤（对每个词汇按此顺序思考）：
1. 先在 reasoning_zh 里确定：要展示本章偏误的哪个具体情境（谁、在什么场合、犯了什么思维错误）
2. 再写 sentence_zh：把这个情境浓缩成某人说的一句话，并自然地包含目标词汇

规则：
- 每句话恰好包含一个目标词汇，词汇必须以原文形式出现
- 每个目标词汇都必须有自己的句子，一个都不能漏
- 用自然口语风格，隐性透露本章所描述的认知偏误
- 句子里不要直接提及偏误名称或心理学术语
- 句子要简短（不超过28个字）
- 不要使用markdown格式

reasoning_zh 的规则：
- 用中文写，1-2句话，简明扼要，说明这句话为什么体现了本章的认知偏误
- 可以点明偏误名称，帮助学习者理解
- 面向HSK 4-5学习者，用词不要太难

仅返回如下JSON数组，不加任何其他文字（reasoning_zh 在前，sentence_zh 在后）：
[
  {{"reasoning_zh": "解释内容", "sentence_zh": "句子内容"}},
  {{"reasoning_zh": "解释内容", "sentence_zh": "句子内容"}}
]"""

    _set_progress(progress_key, phase="request", msg=f"生成第{chapter['number']}章句子…{attempt_label}", percent=20)

    sentences: list[dict] = []
    remaining = list(cards)

    # Incremental retries: keep good sentences, re-request only the words the
    # model skipped — resending the full list just reproduces the same gaps.
    for attempt in range(3):
        if not remaining:
            break
        prompt = _build_prompt(remaining)
        raw = _call_api(model, [{"role": "user", "content": prompt}], 4096, purpose="kahneman")

        json_start = raw.find("[")
        json_end = raw.rfind("]") + 1
        if json_start == -1 or json_end == 0:
            logger.warning("kahneman attempt %d: no JSON array found", attempt + 1)
            continue

        try:
            items = json.loads(raw[json_start:json_end])
        except json.JSONDecodeError as e:
            logger.warning("kahneman attempt %d: JSON parse error: %s", attempt + 1, e)
            continue

        for item in items:
            s_zh = item.get("sentence_zh", "").strip()
            if not s_zh:
                continue
            matched = None
            for card in remaining:
                if _word_in_sentence(card["word_zh"], s_zh):
                    matched = card
                    break
            if matched is None:
                # Sentence contains none of the still-missing words; keeping it
                # would create an orphan once the word is re-requested.
                continue
            remaining.remove(matched)
            sentences.append({
                "word_ids": [matched["word_id"]],
                "sentence_zh": s_zh,
                "sentence_en": "",
                "concept_en": concept_en,
                "concept_zh": concept_zh,
                "reasoning_zh": item.get("reasoning_zh", "").strip(),
                "tokens": [],
            })

        if remaining:
            logger.warning(
                "kahneman attempt %d: missing words (will re-request): %s",
                attempt + 1, [c["word_zh"] for c in remaining],
            )

    # Per-word fallback so every card ends up with a sentence.
    for card in remaining:
        logger.warning("kahneman: using fallback sentence for %s", card["word_zh"])
        sentences.append({
            "word_ids": [card["word_id"]],
            # The Chinese filler sentence would be nonsense in a French deck;
            # for other languages fall back to the word itself, which is at
            # least honest about being a fallback (issue #806).
            "sentence_zh": (card.get("source_sentence")
                            or (f"我学了{card['word_zh']}这个词。" if lang == "zh"
                                else f"{card['word_zh']}.")),
            "sentence_en": "",
            "concept_en": concept_en,
            "concept_zh": concept_zh,
            "reasoning_zh": "",
            "tokens": [],
        })

    _fill_translations(sentences, progress_key=progress_key)
    return sentences


# Max words per AI call for news mode — mirrors MAX_KAHNEMAN_BATCH (routes/story.py):
# large batches make the model skip words and dilute sentence quality.
MAX_NEWS_BATCH = 10

# Max words per AI call for podcast mode (issue #634). Deliberately much larger
# than MAX_NEWS_BATCH: the topic-coverage rule ("every topic in the summary gets
# at least one sentence") only works when one call sees ALL the words, so the
# default stays one single call. This is just the ceiling above which rule
# adherence starts slipping — and gpt-5 shares the 8192 budget with reasoning.
MAX_PODCAST_BATCH = 20

# Podcast top-up rounds (issue #642): keep re-requesting the words the model
# skipped instead of dumping them on the 我学了X这个词。fallback after 3 rounds.
# From PODCAST_SOLO_ROUND onwards each remaining word gets its own call — by
# then only a few are left, and one word per call is the shape the model is
# least able to wriggle out of.
MAX_PODCAST_ROUNDS = 6
PODCAST_SOLO_ROUND = 4


def generate_news_sentences(
    cards: list[dict],
    articles: list[dict],
    model: str = "gpt-5-mini",
    max_hsk: int = 2,
    progress_key: str | None = None,
    attempt_label: str = "",
    generic: bool = False,
) -> list[dict]:
    """Generate a Chinese summary sentence per target word, summarizing `articles`.

    Sentences all together form a coherent briefing/summary. Each sentence uses
    exactly one target word (in HSK-limited background vocabulary otherwise) and is
    tagged with which article it refers to, a one-line Chinese headline, and a short
    Chinese background explanation — stored as concept_zh/reasoning_zh/source_url.

    cards:    vocab cards to cover (word_id, word_zh, pinyin, definition)
    articles: [{url, title, text}, ...] — pasted texts (url/title optional)
    generic:  False = news-briefing framing (mode="news"); True = plain content
              summary of arbitrary pasted texts (mode="paste")
    """
    if not cards or not articles:
        return []

    def _word_in_sentence(word_zh: str, sentence_zh: str) -> bool:
        if '...' in word_zh or '…' in word_zh:
            chars = [c for c in word_zh if c not in '.…']
            return all(c in sentence_zh for c in chars)
        return word_zh in sentence_zh

    # generic=True swaps the news-briefing framing for a plain content summary
    # (pasted content can be an email, blog post, book excerpt — not just news).
    noun = "内容" if generic else "文章"
    goal = "对这些内容的连贯中文摘要" if generic else "对这些文章的连贯新闻简报"
    block_header = "内容（按 0 开始编号）" if generic else "新闻文章（按 0 开始编号）"
    coherence_rule = (
        "- 所有句子合起来必须构成一篇连贯的中文摘要，覆盖内容的关键信息" if generic
        else "- 所有句子合起来必须像一段连贯的新闻简报，覆盖文章的关键信息")
    headline_rule = (
        "- headline_zh 是该段内容主题的中文一句话标题" if generic
        else "- headline_zh 是该文章对应新闻事件的中文一句话标题")
    background_rule = (
        "- background_zh 是2-3句中文背景说明，帮助学习者理解这部分内容" if generic
        else "- background_zh 是2-3句中文背景说明，帮助学习者理解这条新闻的来龙去脉")

    articles_block = "\n\n".join(
        f"{noun}{i}（标题：{a.get('title') or '（无标题）'}）：\n{a.get('text', '').strip()}"
        for i, a in enumerate(articles)
    )

    def _build_prompt(batch: list[dict]) -> str:
        word_list = "\n".join(
            f"{i + 1}. {c['word_zh']}（{c.get('pinyin', '')}）— {c.get('definition', '')}"
            for i, c in enumerate(batch)
        )
        return f"""任务：根据下面提供的{noun}，写一组中文句子，合起来构成{goal}，
帮助HSK 4-5学习者复习词汇。

{block_header}：
{articles_block}

目标词汇（每句话必须恰好包含其中一个，原文出现）：
{word_list}

规则：
- 每句话恰好包含一个目标词汇，词汇必须以原文形式出现
- 每个目标词汇都必须有自己的句子，一个都不能漏
{coherence_rule}
- 非目标词汇只使用HSK 1-{max_hsk}的词汇，尽量简单
- 句子要简短（不超过15个字）
- 所有输出（句子、标题、背景说明）只用简体中文，绝对不要出现繁体字
- 不要使用markdown格式
- article_idx 是该句子所总结/涉及的{noun}编号（上面的 0 开始编号）
{headline_rule}
{background_rule}

仅返回如下JSON数组，不加任何其他文字：
[
  {{"sentence_zh": "句子内容", "article_idx": 0, "headline_zh": "标题", "background_zh": "背景说明"}},
  {{"sentence_zh": "句子内容", "article_idx": 0, "headline_zh": "标题", "background_zh": "背景说明"}}
]"""

    _set_progress(progress_key, phase="request",
                  msg=f"{'生成内容摘要句子' if generic else '生成新闻简报句子'}…{attempt_label}", percent=20)

    sentences: list[dict] = []
    remaining = list(cards)

    for attempt in range(3):
        if not remaining:
            break
        prompt = _build_prompt(remaining)
        # 8192: gpt-5 series shares this budget with internal reasoning tokens,
        # so leave generous headroom above the ~2-3k tokens of actual output.
        raw = _call_api(model, [{"role": "user", "content": prompt}], 8192,
                        purpose="paste" if generic else "news")

        json_start = raw.find("[")
        json_end = raw.rfind("]") + 1
        if json_start == -1 or json_end == 0:
            logger.warning("news attempt %d: no JSON array found", attempt + 1)
            continue

        try:
            items = json.loads(raw[json_start:json_end])
        except json.JSONDecodeError as e:
            logger.warning("news attempt %d: JSON parse error: %s", attempt + 1, e)
            continue

        for item in items:
            s_zh = item.get("sentence_zh", "").strip()
            if not s_zh:
                continue
            matched = None
            for card in remaining:
                if _word_in_sentence(card["word_zh"], s_zh):
                    matched = card
                    break
            if matched is None:
                continue
            remaining.remove(matched)
            article_idx = item.get("article_idx")
            source_url = source_title = source_name = None
            if isinstance(article_idx, int) and 0 <= article_idx < len(articles):
                _art = articles[article_idx]
                source_url = _art.get("url") or None
                source_title = _art.get("title") or None
                source_name = _art.get("source_name") or None
            sentences.append({
                "word_ids": [matched["word_id"]],
                "sentence_zh": s_zh,
                "sentence_en": "",
                "concept_en": "",
                "concept_zh": item.get("headline_zh", "").strip(),
                "reasoning_zh": item.get("background_zh", "").strip(),
                "source_url": source_url,
                "source_title": source_title,
                "source_name": source_name,
                "tokens": [],
            })

        if remaining:
            logger.warning(
                "news attempt %d: missing words (will re-request): %s",
                attempt + 1, [c["word_zh"] for c in remaining],
            )

    # Per-word fallback so every card ends up with a sentence.
    for card in remaining:
        logger.warning("news: using fallback sentence for %s", card["word_zh"])
        sentences.append({
            "word_ids": [card["word_id"]],
            # The Chinese filler sentence would be nonsense in a French deck;
            # for other languages fall back to the word itself, which is at
            # least honest about being a fallback (issue #806).
            "sentence_zh": (card.get("source_sentence")
                            or (f"我学了{card['word_zh']}这个词。" if lang == "zh"
                                else f"{card['word_zh']}.")),
            "sentence_en": "",
            "concept_en": "",
            "concept_zh": "",
            "reasoning_zh": "",
            "source_url": None,
            "tokens": [],
        })

    _fill_translations(sentences, progress_key=progress_key)
    return sentences


def _briefing_word_match(word_zh: str, sentence_zh: str) -> bool:
    if '...' in word_zh or '…' in word_zh:
        chars = [c for c in word_zh if c not in '.…']
        return all(c in sentence_zh for c in chars)
    return word_zh in sentence_zh


# Leading articles the AI may adapt/drop to fit a target word into a sentence
# (same set generate_story uses) — needed to match e.g. "le chat" against a
# sentence that only contains "chat".
_ROMANCE_ARTICLE_PREFIXES = {
    "fr": ("le ", "la ", "les ", "un ", "une ", "des ", "du ", "de la ", "de l'", "l'"),
    "es": ("el ", "la ", "los ", "las ", "un ", "una ", "unos ", "unas ", "del ", "al "),
}


def _card_surface_forms(card: dict, lang: str) -> list[str]:
    """Every surface form that counts as "this card's word" in `lang`.

    For conjugating languages the knowledge prompt explicitly allows the model
    to adapt a word's form ("réduire" -> "a réduit"), so matching on the
    headword alone would discard most correct sentences and replace them with
    fallbacks. #803 stores the full conjugation/inflection table per entry, so
    the accepted set is simply the headword plus everything in entry_forms.
    Chinese has no forms and skips the lookup entirely.
    """
    word = card.get("word_zh") or ""
    if lang == "zh" or not card.get("word_id"):
        return [word]
    forms = [word]
    try:
        grouped = database.get_entry_forms(card["word_id"])
        for paradigm in grouped.values():
            for slots in paradigm.values():
                forms.extend(f for f in slots.values() if f)
    except Exception as e:
        logger.warning("could not load stored forms for word %s — %s", card.get("word_id"), e)
    # Longest first so a multi-word form wins over a bare headword prefix.
    return sorted({f for f in forms if f}, key=len, reverse=True)


def _word_match(word_zh: str, sentence_zh: str, lang: str = "zh") -> bool:
    """Target-word-in-sentence check for knowledge mode (issue #806) — zh uses
    the plain substring/abbreviated-sentence rule (_briefing_word_match);
    fr/es strip a leading article and match on a word boundary, tolerating a
    plural suffix (same approach as generate_story's non-zh matching)."""
    if lang == "zh":
        return _briefing_word_match(word_zh, sentence_zh)
    w = word_zh.casefold()
    s = sentence_zh.casefold()
    for prefix in _ROMANCE_ARTICLE_PREFIXES.get(lang, ()):
        if w.startswith(prefix):
            w = w[len(prefix):]
            break
    return re.search(rf"(?<!\w){re.escape(w)}(?:s|es)?(?!\w)", s) is not None


def validate_briefing_items(items: list[dict], cards: list[dict],
                            include_context: bool = True) -> list[str]:
    """Python-only validation (no AI) of a raw briefing sentence array (issue #444).

    Checks:
      a) every target word appears exactly once across all sentences
      b) no two consecutive context sentences (sentences with no target word)
      c) target-word sentences are at most 18 characters
    Returns a list of human-readable violation descriptions — empty means valid.

    include_context=False (podcast mode, issue #482): context sentences are not
    allowed at all — rule b) is replaced by a flat "no context sentences" check
    instead of the consecutive-run check.
    """
    issues: list[str] = []
    if not cards:
        return issues

    word_counts = {c["word_id"]: 0 for c in cards}
    is_context: list[bool] = []
    for item in items:
        s_zh = (item.get("sentence_zh") or "").strip()
        matched_any = False
        for c in cards:
            if _briefing_word_match(c["word_zh"], s_zh):
                word_counts[c["word_id"]] += 1
                matched_any = True
        is_context.append(not matched_any)

    missing = [c["word_zh"] for c in cards if word_counts[c["word_id"]] == 0]
    duplicated = [c["word_zh"] for c in cards if word_counts[c["word_id"]] > 1]
    if missing:
        issues.append(f"目标词缺失：{'、'.join(missing)}")
    if duplicated:
        issues.append(f"目标词重复出现（每个词必须恰好出现一次）：{'、'.join(duplicated)}")

    if not include_context:
        if any(is_context):
            issues.append("存在不含目标词的上下文句子（此模式不允许上下文句，每句都必须含一个目标词）")
    else:
        run = 0
        for ctx in is_context:
            run = run + 1 if ctx else 0
            if run >= 2:
                issues.append("存在连续两个以上不含目标词的上下文句子（每个目标句前最多只能有一个上下文句）")
                break

    if len(items) > 2 * len(cards):
        issues.append(f"句子总数（{len(items)}）超过目标词数量两倍的上限（{2 * len(cards)}）")

    # article_idx must be non-decreasing across the sequence (issue #454) —
    # the AI is told to process articles one at a time, in order. None/missing
    # article_idx is not a violation (just skipped when tracking the max seen).
    max_idx_seen = None
    for pos, item in enumerate(items):
        idx = item.get("article_idx")
        if not isinstance(idx, int):
            continue
        if max_idx_seen is not None and idx < max_idx_seen:
            issues.append(
                f"文章顺序回跳：句子{pos}属于文章{idx}但之前已进入文章{max_idx_seen}"
            )
            break
        max_idx_seen = idx if max_idx_seen is None else max(max_idx_seen, idx)

    for item in items:
        s_zh = (item.get("sentence_zh") or "").strip()
        if s_zh and any(_briefing_word_match(c["word_zh"], s_zh) for c in cards) and len(s_zh) > 18:
            issues.append(f"目标句超过18字（{len(s_zh)}字）：{s_zh}")

    # Context sentences must be a single short clause (issue #511) — long,
    # multi-clause sentences with several commas were a recurring complaint.
    # Only checked when context sentences are allowed at all.
    if include_context:
        for item in items:
            s_zh = (item.get("sentence_zh") or "").strip()
            if not s_zh or any(_briefing_word_match(c["word_zh"], s_zh) for c in cards):
                continue
            pause_count = sum(s_zh.count(p) for p in "，、；")
            if len(s_zh) > 25 or pause_count >= 2:
                issues.append(
                    f"上下文句子过长或分句过多，必须是单独一个短句（≤25字，最多一个逗号）："
                    f"{s_zh}")

    return issues


def _dedupe_consecutive_briefing_context(items: list[dict], cards: list[dict],
                                         include_context: bool = True) -> list[dict]:
    """Fallback repair when consecutive context-only sentences survive the
    validation retry: keep only the LAST sentence of each consecutive
    context-only run, dropping the extras (issue #444 acceptance criteria).

    include_context=False (podcast mode, issue #482): context sentences are
    never allowed, so every one of them is simply dropped instead of being
    collapsed one-per-run."""
    if not include_context:
        return [item for item in items
                if (item.get("sentence_zh") or "").strip()
                and any(_briefing_word_match(c["word_zh"], item["sentence_zh"]) for c in cards)]
    fixed: list[dict] = []
    buf: list[dict] = []
    for item in items:
        s_zh = (item.get("sentence_zh") or "").strip()
        is_ctx = not (s_zh and any(_briefing_word_match(c["word_zh"], s_zh) for c in cards))
        if is_ctx:
            buf.append(item)
        else:
            if buf:
                fixed.append(buf[-1])
                buf = []
            fixed.append(item)
    if buf:
        fixed.append(buf[-1])
    return fixed


def fact_check_briefing(articles: list[dict], items: list[dict], model: str,
                        generic: bool = False) -> list[str]:
    """One extra AI call (issue #454) to catch hallucinated facts in the
    generated briefing sentences — numbers, names, causality invented rather
    than taken from the source articles.

    generic=True: paste mode (#481) — same check, wording swapped from
    "news article" framing to plain "content" framing (source can be an
    email, blog post, book excerpt — not just news).

    Returns a list of Chinese issue descriptions ("句子N：问题描述"); an empty
    list means either everything checked out or the check itself failed —
    fact-checking is best-effort and must never block story generation.
    """
    if not articles or not items:
        return []

    noun = "内容" if generic else "文章"
    articles_block = "\n\n".join(
        f"{noun}{i}（标题：{a.get('title') or '（无标题）'}）：\n{a.get('text', '').strip()}"
        for i, a in enumerate(articles)
    )
    sentences_block = "\n".join(
        f"{i}. {item.get('sentence_zh', '')}" for i, item in enumerate(items)
    )
    source_desc = "原始内容" if generic else "原始新闻文章"
    prompt = f"""任务：核对下面每一句中文摘要句子是否符合{source_desc}的事实。

{noun}（按 0 开始编号）：
{articles_block}

生成的摘要句子（按 0 开始编号）：
{sentences_block}

请重点检查：数字（金额、人数、日期等）、人名/地名/机构名、因果关系是否准确，
以及是否有原文中完全没有提到、凭空捏造的内容。

只返回如下 JSON，不加任何其他文字：
- 全部符合事实：{{"ok": true}}
- 存在问题：{{"ok": false, "issues": ["句子N：问题描述", ...]}}"""

    try:
        raw = _call_api(model, [{"role": "user", "content": prompt}], 2048, purpose="briefing_fact_check")
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.warning("briefing fact-check: no JSON object found in response")
            return []
        result = json.loads(raw[json_start:json_end])
        if result.get("ok"):
            return []
        issues = result.get("issues") or []
        return [str(i) for i in issues]
    except Exception as e:
        logger.warning("briefing fact-check: call failed (%s) — skipping", e)
        return []


def _repair_briefing_sentences(articles: list[dict], items: list[dict], issues: list[str],
                               model: str, generic: bool = False) -> list[dict]:
    """Targeted repair (issue #511) for sentences flagged by the second
    fact-check pass, instead of accepting hallucinated sentences wholesale.

    `issues` is the fact_check_briefing() output — strings shaped like
    "句子N：问题描述", where N is the index into `items`. Parses out the
    indices, bundles just those sentences (plus their source article text and
    the fact-check verdict) into ONE repair call, and asks the AI to rewrite
    only those sentences. Returns a new items list with the rewrites spliced
    in at their original positions; on any failure (unparseable issues, bad
    JSON, missing indices) returns `items` unchanged so the caller can fall
    back to its existing accept-with-warning behavior.
    """
    idx_pattern = re.compile(r"句子\s*(\d+)")
    flagged: dict[int, str] = {}
    for issue in issues:
        m = idx_pattern.search(issue)
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < len(items):
            flagged[idx] = issue
    if not flagged:
        logger.warning("briefing repair: could not parse sentence indices from issues — "
                       "skipping repair")
        return items

    noun = "内容" if generic else "文章"
    problems_block_parts = []
    for idx in sorted(flagged):
        item = items[idx]
        article_idx = item.get("article_idx")
        source_text = ""
        if isinstance(article_idx, int) and 0 <= article_idx < len(articles):
            source_text = articles[article_idx].get("text", "").strip()
        problems_block_parts.append(
            f"句子{idx}（原句）：{item.get('sentence_zh', '')}\n"
            f"核查意见：{flagged[idx]}\n"
            f"来源{noun}原文：{source_text}"
        )
    problems_block = "\n\n".join(problems_block_parts)

    prompt = f"""任务：下面这些中文句子被事实核查发现有问题（编造了原文没有的细节，或加入了主观评论/情绪/气氛描写）。
请仅根据各自的来源原文重写每一句，不要引入原文没有的新信息。

{problems_block}

重写要求：
- 如果原句包含目标词汇（见下方"原句"是否明显在描述一个词），重写后的句子必须保持8到18个字，
  只使用来源原文明确陈述的事实，原来的目标词必须【原样、恰好出现一次】保留在句子中
- 如果原句不含目标词汇（上下文句子），重写后必须是单独一个短句，不超过25个字，最多一个逗号
- 只允许使用来源原文明确陈述的事实，禁止主观评论、情绪、气氛或场景描写
- 所有输出只用简体中文，不要使用markdown格式

仅返回如下JSON数组（顺序不限，用 idx 标明对应哪一句），不加任何其他文字：
[
  {{"idx": {sorted(flagged)[0]}, "sentence_zh": "重写后的句子"}}
]"""

    try:
        raw = _call_api(model, [{"role": "user", "content": prompt}], 2048, purpose="briefing_repair")
        r_start, r_end = raw.find("["), raw.rfind("]") + 1
        if r_start == -1 or r_end == 0:
            logger.warning("briefing repair: no JSON array in response — keeping original")
            return items
        repaired = json.loads(raw[r_start:r_end])
    except Exception as e:
        logger.warning("briefing repair: call/parse failed (%s) — keeping original", e)
        return items

    new_items = list(items)
    applied = 0
    for entry in repaired:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        new_sentence = (entry.get("sentence_zh") or "").strip()
        if not isinstance(idx, int) or idx not in flagged or not new_sentence:
            continue
        replaced = dict(new_items[idx])
        replaced["sentence_zh"] = new_sentence
        new_items[idx] = replaced
        applied += 1

    if applied == 0:
        logger.warning("briefing repair: response had no usable rewrites — keeping original")
        return items

    logger.info("briefing repair: rewrote %d/%d flagged sentence(s)", applied, len(flagged))
    return new_items


def generate_briefing_sentences(
    cards: list[dict],
    articles: list[dict],
    model: str = "gpt-5-mini",
    # HSK 1-5 background vocabulary (issue #448): Daniel is HSK 4-5 — capping
    # the non-target words at HSK 1-2 made sentences childish and was the
    # tightest remaining constraint after the #444 rework.
    max_hsk: int = 3,
    progress_key: str | None = None,
    attempt_label: str = "",
    progress_extra: dict | None = None,
    generic: bool = False,
    include_context: bool = True,
) -> list[dict]:
    """News flow mode (issue #399, reworked in #444): one flowing Chinese news
    summary instead of one forced sentence per word.

    The AI writes a coherent summary in which each target word appears exactly
    once — in whatever order produces the most natural summary (word order is
    free, issue #444) — but plain context sentences (facts, numbers — no target
    word) are allowed in between, so nothing has to be padded artificially. At
    most ONE context sentence may precede a target sentence; two consecutive
    context sentences are never allowed. We then scan the sentences in order:
    a sentence containing a target word becomes a card sentence; the context
    sentence before it (since the previous card) is attached to it — Chinese
    into reasoning_zh (background popup), German (Google Translate, no extra AI
    cost) into context_de (shown on the card). Target-word order = order of
    appearance in the summary (arbitrary, chosen by the AI).

    After generation, `validate_briefing_items` checks the raw output in Python
    (no AI): every target word exactly once, no consecutive context sentences,
    target sentences ≤18 chars. On violation we retry ONCE with the concrete
    issues fed back into the prompt. If violations persist: consecutive context
    runs are collapsed to their last sentence (extras dropped); anything else
    is accepted with a logged warning — the existing per-word missing-word
    retry loop and fallback-sentence mechanism below still guarantee every
    card gets a sentence.

    progress_extra: extra fields merged into every progress update (issue #407) —
    the chunker passes words_done/words_total/articles so the loading screen can
    show real overall progress; words_done is advanced here as words get covered.

    generic: False = news-briefing framing (mode="briefing"); True = plain
    content-summary framing for arbitrary pasted text (mode="paste", issue
    #481) — same pipeline (context sentences, validation, dedup, fact-check),
    only the prompt wording swaps "news article/briefing" for "content/summary".

    include_context: False (mode="podcast", issue #482) — no context sentences
    at all: every sentence in the output must contain exactly one target word.
    Combines with generic=True for podcast (content framing, no context).
    """
    if not cards or not articles:
        return []

    # generic=True (paste mode, #481) swaps every "news article/briefing" noun
    # in the prompt for a plain "content/summary" one — the pipeline itself
    # (context sentences, validation, dedup, fact-check) is untouched.
    noun = "内容" if generic else "文章"
    task_line = (
        "任务：根据下面提供的内容，写一篇连贯的中文摘要（自然地从一部分过渡到下一部分），\n"
        "帮助HSK 4-5学习者复习词汇。" if generic else
        "任务：根据下面的新闻文章，写一篇连贯的中文新闻摘要（像新闻串播一样，从一条新闻自然过渡到下一条），\n"
        "帮助HSK 4-5学习者复习词汇。")
    fact_rule = (
        "- 【重要】含目标词汇的句子也必须传达该内容中的一个具体事实（是什么、涉及谁、在哪里、有多少），\n"
        "  读者只看这一句也能学到这部分内容。严禁没有信息量的空洞句子，\n"
        "  例如\"组织很大。\"\"火箭很快。\"\"它指代。\"\"未知很大。\"这类句子绝对不可以出现\n"
        "- 【重要】只允许使用原文中明确陈述的事实——禁止添加原文没有的主观评论、情绪、\n"
        "  气氛或场景描写（例如猜测人物的心情、渲染现场气氛、评价某件事做得好不好）。\n"
        "  宁可句子写得朴素平实，也绝不可以编造原文没有的细节" if generic else
        "- 【重要】含目标词汇的句子也必须传达该新闻中的一个具体事实（谁、做了什么、在哪里、多少），\n"
        "  读者只看这一句也能学到新闻内容。严禁没有信息量的空洞句子，\n"
        "  例如\"组织很大。\"\"火箭很快。\"\"它指代。\"\"未知很大。\"这类句子绝对不可以出现\n"
        "- 【重要】只允许使用原文中明确陈述的事实——禁止添加原文没有的主观评论、情绪、\n"
        "  气氛或场景描写（例如猜测人物的心情、渲染现场气氛、评价某件事做得好不好）。\n"
        "  宁可句子写得朴素平实，也绝不可以编造原文没有的细节")
    ctx_word = "其上下文句" if include_context else "所有句子"
    order_rule = (
        f"- 【逐段处理】必须按{noun}编号顺序依次处理：先写完{noun}0涉及的所有句子（含{ctx_word}），\n"
        f"  再开始写{noun}1的句子，以此类推——article_idx 在整个输出中只能递增或不变，绝不允许\n"
        f"  写到{noun}1之后又跳回去写{noun}0的句子" if generic else
        f"- 【逐篇处理】必须按文章编号顺序依次处理：先写完文章0涉及的所有句子（含{ctx_word}），\n"
        "  再开始写文章1的句子，以此类推——article_idx 在整个输出中只能递增或不变，绝不允许\n"
        "  写到文章1之后又跳回去写文章0的句子")

    # include_context=False (podcast mode, issue #482): every sentence must
    # contain a target word — no context sentences at all.
    context_rule = (
        "- 不是每句话都要包含目标词汇：目标词句子之间【最多插入一个】不含目标词的上下文句子，\n"
        "  用来交代事实、数字和背景，让摘要自然连贯——绝对不允许连续出现两个或以上不含目标词的上下文句子\n"
        "- 只有当下一个目标句确实需要这段背景才能读懂时才插入上下文句子——能省则省，\n"
        "  不要为了凑数或过渡而硬加\n"
        "- 因此句子总数不能超过目标词数量的两倍"
        if include_context else
        "- 【重要】每一句话都必须恰好包含一个目标词汇——不允许出现任何不含目标词的句子，\n"
        "  因此句子总数必须恰好等于目标词数量"
    )
    context_hsk_rule = (
        "\n- 上下文句子【不受 HSK 词汇限制】——它最终会被翻译成德文显示在卡片正面，可以自由\n"
        "  使用专有名词、数字和任何词汇来准确传达事实；但【必须是单独的一个短句】，\n"
        "  不超过25个字，最多包含一个逗号——绝不能是多个分句拼接而成的长句"
        if include_context else ""
    )
    context_target_word_note = (
        "；不含目标词的上下文句子填 null" if include_context else "（本模式没有上下文句子）"
    )
    hsk_overflow_rule = (
        "\n  如果某个事实需要更难的词才能表达，把它放进上下文句子里，目标句只保留简单的部分"
        if include_context else
        "\n  如果某个事实需要更难的词才能表达，就换一种更简单的说法，或省略这个细节"
    )
    example_block = (
        '[\n'
        '  {"sentence_zh": "上下文句子", "target_word": null, "article_idx": 0},\n'
        '  {"sentence_zh": "含目标词的句子", "target_word": "词汇", "article_idx": 0}\n'
        ']'
    ) if include_context else (
        '[\n'
        '  {"sentence_zh": "含目标词的句子", "target_word": "词汇", "article_idx": 0}\n'
        ']'
    )

    extra = dict(progress_extra or {})
    base_done = extra.get("words_done", 0)
    words_total = extra.get("words_total")

    def _progress(msg: str) -> None:
        if not progress_key:
            return
        fields = dict(extra)
        if words_total:
            done = base_done + (len(cards) - len(remaining))
            fields["words_done"] = done
            fields["percent"] = 15 + int(70 * done / max(words_total, 1))
        else:
            fields.setdefault("percent", 20)
        _set_progress(progress_key, phase="request", msg=msg, **fields)

    articles_block = "\n\n".join(
        f"{noun}{i}（标题：{a.get('title') or '（无标题）'}）：\n{a.get('text', '').strip()}"
        for i, a in enumerate(articles)
    )

    def _build_prompt(batch: list[dict], extra_hint: str = "") -> str:
        word_list = "\n".join(
            f"{i + 1}. {c['word_zh']}（{c.get('pinyin', '')}）— {c.get('definition', '')}"
            for i, c in enumerate(batch)
        )
        return f"""{task_line}

{noun}（按 0 开始编号）：
{articles_block}

目标词汇（每个词必须在整篇摘要中恰好出现一次，以原文形式出现）：
{word_list}

【词序自由】你可以任意安排这些目标词在摘要中出现的先后顺序——不必按上面列表的顺序，
请选择能写出最自然、最连贯摘要的顺序。

规则：
- 摘要按句子输出为 JSON 数组，数组顺序就是阅读顺序
{context_rule}
- 一句话最多包含一个目标词汇
- 【难度控制，严格遵守】含目标词汇的句子长度为8到18个字，其中除目标词外只允许
  HSK 1-{max_hsk} 的词汇——这是学习者自己选择的难度上限，超纲词会让句子无法学习。{hsk_overflow_rule}
{fact_rule}{context_hsk_rule}
- 所有输出只用简体中文，绝对不要出现繁体字
- 不要使用markdown格式
- article_idx 是该句子所涉及的{noun}编号（上面的 0 开始编号）
- target_word 是该句包含的目标词汇原文{context_target_word_note}
{order_rule}
{extra_hint}

仅返回如下JSON数组，不加任何其他文字：
{example_block}"""

    sentences: list[dict] = []
    remaining = list(cards)
    validation_retried = False
    fact_check_done = False

    _progress(f"生成新闻总结…{attempt_label}")

    for attempt in range(3):
        if not remaining:
            break
        if attempt > 0:
            _progress(f"补漏 {len(remaining)} 个词（第{attempt + 1}轮）…{attempt_label}")
        expected_cards = list(remaining)
        prompt = _build_prompt(remaining)
        # 8192: gpt-5 series shares this budget with internal reasoning tokens,
        # and context sentences add output on top of the card sentences.
        raw = _call_api(model, [{"role": "user", "content": prompt}], 8192, purpose="briefing")

        json_start = raw.find("[")
        json_end = raw.rfind("]") + 1
        if json_start == -1 or json_end == 0:
            logger.warning("briefing attempt %d: no JSON array found", attempt + 1)
            continue

        try:
            items = json.loads(raw[json_start:json_end])
        except json.JSONDecodeError as e:
            logger.warning("briefing attempt %d: JSON parse error: %s", attempt + 1, e)
            continue

        # Python-only validation + a single retry (issue #444) — only once per
        # call, on whichever attempt first produces parseable JSON.
        if not validation_retried:
            validation_retried = True
            issues = validate_briefing_items(items, expected_cards, include_context=include_context)
            if issues:
                logger.warning("briefing attempt %d: validation issues, retrying once: %s",
                               attempt + 1, issues)
                hint = "\n【上一次的结果有以下问题，请修正后重新生成整篇摘要】\n" + \
                       "\n".join(f"- {i}" for i in issues)
                retry_raw = _call_api(
                    model, [{"role": "user", "content": _build_prompt(remaining, extra_hint=hint)}],
                    8192, purpose="briefing",
                )
                r_start, r_end = retry_raw.find("["), retry_raw.rfind("]") + 1
                if r_start != -1 and r_end != 0:
                    try:
                        retry_items = json.loads(retry_raw[r_start:r_end])
                        items = retry_items
                        remaining_issues = validate_briefing_items(items, expected_cards, include_context=include_context)
                        if remaining_issues:
                            logger.warning(
                                "briefing: validation issues persist after retry (accepting with "
                                "fallback repair): %s", remaining_issues)
                    except json.JSONDecodeError as e:
                        logger.warning("briefing: validation retry JSON parse error (%s) — "
                                       "keeping original attempt", e)
                else:
                    logger.warning("briefing: validation retry produced no JSON array — "
                                   "keeping original attempt")
                # Fallback repair: collapse any remaining consecutive context runs
                # to their last sentence — safe no-op if already valid.
                items = _dedupe_consecutive_briefing_context(items, expected_cards, include_context=include_context)

        # AI fact-check against the source articles (issue #454) — runs once,
        # right after Python validation and before any translation. On issues,
        # retry generation once with the concrete problems fed back in; the
        # retry's own fact-check result (if any) is logged only, never retried
        # again, to avoid an unbounded loop.
        if not fact_check_done:
            fact_check_done = True
            _progress(f"核对事实…{attempt_label}")
            fc_issues = fact_check_briefing(articles, items, model, generic=generic)
            if fc_issues:
                logger.warning("briefing attempt %d: fact-check issues, retrying once: %s",
                               attempt + 1, fc_issues)
                fc_hint = "\n【事实核查发现以下问题，请修正后重新生成整篇摘要，确保严格符合原文事实】\n" + \
                          "\n".join(f"- {i}" for i in fc_issues)
                fc_retry_raw = _call_api(
                    model, [{"role": "user", "content": _build_prompt(remaining, extra_hint=fc_hint)}],
                    8192, purpose="briefing",
                )
                fc_r_start, fc_r_end = fc_retry_raw.find("["), fc_retry_raw.rfind("]") + 1
                if fc_r_start != -1 and fc_r_end != 0:
                    try:
                        fc_retry_items = json.loads(fc_retry_raw[fc_r_start:fc_r_end])
                        items = _dedupe_consecutive_briefing_context(fc_retry_items, expected_cards, include_context=include_context)
                        second_fc_issues = fact_check_briefing(articles, items, model, generic=generic)
                        if second_fc_issues:
                            logger.warning(
                                "briefing: fact-check issues persist after retry, attempting "
                                "targeted repair: %s", second_fc_issues)
                            items = _repair_briefing_sentences(
                                articles, items, second_fc_issues, model, generic=generic)
                    except json.JSONDecodeError as e:
                        logger.warning("briefing: fact-check retry JSON parse error (%s) — "
                                       "keeping original attempt", e)
                else:
                    logger.warning("briefing: fact-check retry produced no JSON array — "
                                   "keeping original attempt")

        # Scan in reading order: context sentences accumulate until the next
        # sentence containing a still-uncovered target word, then attach to it.
        # We match by scanning the text ourselves — the AI's target_word tag is
        # not trusted (it can lie), only the actual sentence content counts.
        context_buf: list[str] = []
        for item in items:
            s_zh = item.get("sentence_zh", "").strip()
            if not s_zh:
                continue
            matched = None
            for card in remaining:
                if _briefing_word_match(card["word_zh"], s_zh):
                    matched = card
                    break
            if matched is None:
                context_buf.append(s_zh)
                continue
            remaining.remove(matched)
            article_idx = item.get("article_idx")
            source_url = source_title = source_name = None
            if isinstance(article_idx, int) and 0 <= article_idx < len(articles):
                _art = articles[article_idx]
                source_url = _art.get("url") or None
                source_title = _art.get("title") or None
                source_name = _art.get("source_name") or None
            context_zh = " ".join(context_buf)
            context_buf = []
            sentences.append({
                "word_ids": [matched["word_id"]],
                "sentence_zh": s_zh,
                "sentence_en": "",
                "concept_en": "",
                "concept_zh": "",
                "reasoning_zh": context_zh,
                "context_zh": context_zh,
                "source_url": source_url,
                "source_title": source_title,
                "source_name": source_name,
                "tokens": [],
            })

        _progress(f"生成新闻总结…{attempt_label}")
        if remaining:
            logger.warning(
                "briefing attempt %d: missing words (will re-request): %s",
                attempt + 1, [c["word_zh"] for c in remaining],
            )

    # Per-word fallback so every card ends up with a sentence.
    for card in remaining:
        logger.warning("briefing: using fallback sentence for %s", card["word_zh"])
        sentences.append({
            "word_ids": [card["word_id"]],
            # The Chinese filler sentence would be nonsense in a French deck;
            # for other languages fall back to the word itself, which is at
            # least honest about being a fallback (issue #806).
            "sentence_zh": (card.get("source_sentence")
                            or (f"我学了{card['word_zh']}这个词。" if lang == "zh"
                                else f"{card['word_zh']}.")),
            "sentence_en": "",
            "concept_en": "",
            "concept_zh": "",
            "reasoning_zh": "",
            "context_zh": "",
            "source_url": None,
            "tokens": [],
        })

    _fill_translations(sentences, progress_key=progress_key)

    # Context → German via Google Translate (translator.py), per Daniel's design:
    # keep the AI's job small, translation is mechanical. Only non-empty contexts
    # go into the batch — empty lines break translate_batch's newline splitting.
    ctx_texts = [s.pop("context_zh", "") or "" for s in sentences]
    for s in sentences:
        s["context_de"] = None
    nonempty = [(i, t) for i, t in enumerate(ctx_texts) if t]
    if nonempty:
        try:
            import translator as _t
            de_list = _t.translate_batch([t for _, t in nonempty], target="de")
            for (i, _), de in zip(nonempty, de_list):
                sentences[i]["context_de"] = de.strip() or None
        except Exception as e:
            logger.warning("briefing: context translation failed — %s", e)

    return sentences


def _parse_json_array_salvage(raw: str) -> tuple[list, bool]:
    """从 AI 回复里解析 JSON 对象数组；数组被截断时救回其中完整的对象。

    返回 (items, truncated)。truncated=True 表示整体解析失败、结果是逐个对象
    扫描救回来的。#743：一次 154 个词的调用正好用满输出预算，回复在数组中间
    断掉，原来整轮作废，几十句已经写好的句子和已经花掉的钱一起丢了。
    """
    json_start = raw.find("[")
    if json_start == -1:
        return [], True

    json_end = raw.rfind("]") + 1
    if json_end != 0:
        try:
            items = json.loads(raw[json_start:json_end])
            if isinstance(items, list):
                return items, False
        except json.JSONDecodeError:
            pass

    # 括号深度扫描，逐个切出顶层 {...} 片段——必须正确处理字符串内的
    # 花括号/引号（句子里可能出现 { } "），否则切割点会错位。
    items = []
    depth = 0
    in_string = False
    escape = False
    obj_start = None
    for i in range(json_start + 1, len(raw)):
        ch = raw[i]
        if obj_start is None:
            if ch == "{":
                obj_start = i
                depth = 1
                in_string = False
                escape = False
            continue
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[obj_start:i + 1])
                    if isinstance(obj, dict):
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = None
    return items, True


def _podcast_max_tokens(model: str, n_words: int) -> int:
    """#743：写死 8192 时 154 个词的一次性调用根本装不下（每句约需
    150-200 tokens 用于 reasoning_zh + sentence_zh + target_word），回复在数组
    中间被截断。按批次词数估算预算，按模型族封顶——gpt-5 系列的输出预算与内部
    reasoning tokens 共享，给它更大的上限；deepseek/glm/qwen/claude 的单次
    输出上限就在 8192 量级，写更大会被 provider 拒绝。
    """
    needed = 1500 + 200 * n_words
    cap = 16384 if model.startswith("gpt-") else 8192
    return max(4096, min(needed, cap))


# Knowledge mode for non-Chinese decks (issue #806). The Chinese prompt lives
# in DEFAULT_PROMPT_TEMPLATES["knowledge"] and is user-editable via the prompt
# preset UI (that UI is Chinese-only); this is its target-language counterpart,
# carrying the same rules — one target word per sentence, every sentence
# retells one concrete fact from the material, hard data preferred, no
# invented content. The source material may be in any language; only the
# output language is fixed here.
_KNOWLEDGE_PROMPT_NON_ZH = """Task: below is the content of a podcast/video/article. It may be a raw
transcript or a summary, and it may be in any language. Write a set of
sentences IN {lang_name} — one sentence per target word — where each sentence
also retells one concrete fact from the material. The learner is at {learner}.

Title: {title}
Material:
{summary}
Target words:
{words}

1. Each sentence must contain exactly ONE target word. You may adapt the
   word's form (conjugation, agreement, article) so the sentence is
   grammatical — that is expected in {lang_name}, not a violation.
2. Each sentence must retell one concrete fact from the material: who, what,
   how much, when, why, with what result.
3. Use each fact exactly once. Rephrasing the same information counts as the
   same fact.
4. The number of sentences equals the number of target words.
5. Before writing, list the material's facts internally — at least as many as
   there are target words — spread across its beginning, middle and end, and
   covering every topic it raises. Never pile every sentence onto the opening
   topic.
6. Prefer facts carrying hard data: years, dates, amounts, sums, percentages,
   rankings, durations, ages; then facts naming people, companies,
   institutions, places. Facts with both come first.
7. Output the sentences in the order the facts appear in the material, so the
   set reads like an outline. Reorder only when strict order would force an
   unnatural pairing.
8. No connectives or plot between sentences — each stands on its own.
9. No empty sentences ("it was interesting", "he said a lot").
10. Use ONLY information stated in the material. Never invent, never add
    background knowledge, never comment.
11. Keep each sentence to about {sentence_limit}.
12. Apart from the target word and the key terms in rule 13, use only simple
    {background} vocabulary.
13. KEY-TERM EXCEPTION: names of people, places and institutions, numbers,
    years, amounts and core terminology from the material must be kept
    verbatim even when they are above that level — they are exactly what is
    worth remembering. Never blur them into "someone", "somewhere", "a lot",
    "several years". There is no cap on how many appear in one sentence.
14. Never mark the target word in any way — no quotes, brackets, bold or
    parentheses. Write it plainly inside the sentence.
15. Never use markdown anywhere in the output.

For every sentence also write reasoning_zh: start with "Fact: " and the fact
that sentence retells, copying its names and numbers verbatim from the
material, then one short sentence in German saying what the sentence is
about. Writing the fact out is the self-check — no two sentences may carry
the same one.

Self-check before answering (internally, do not output): as many sentences as
target words? each target word used exactly once? every sentence carrying a
concrete fact? no fact used twice? facts spread over the whole material? no
number or name blurred away?

{extra_hint}

Return ONLY this JSON array, no other text (reasoning_zh first, sentence_zh
second; the key names are historical — sentence_zh holds the {lang_name}
sentence):
[
  {{"reasoning_zh": "Fact: … + one German sentence", "sentence_zh": "the {lang_name} sentence containing the target word", "target_word": "the word"}}
]"""


def generate_podcast_sentences(
    cards: list[dict],
    source: dict,              # {"title", "kind", "url", "material"} — see routes/story.py's knowledge branch; caller ensures non-empty "material"
    model: str = DEFAULT_MODEL,     # #640: DeepSeek, same as kahneman
    max_hsk: int = 3,
    progress_key: str | None = None,
    attempt_label: str = "",
    lang: str = "zh",
) -> tuple[list[dict], str]:
    """Podcast/knowledge mode rework (issue #561) — a lean single-purpose
    pipeline that replaces reuse of the briefing machinery (originally #482).

    Prompt rewritten in #634: the sentences no longer have to add up to a
    summary of the episode, and no longer have to each state a fact from it —
    review-queue words like 咳嗽 have nothing to do with the episode, so that
    demand only produced contortions and invented facts. Instead each sentence
    just has to happen "in the world of the episode" (kahneman's approach),
    anchored by a hard rule that every sentence names a proper noun from the
    material, plus a topic-coverage rule so the sentences don't all pile onto
    the material's first topic.

    Tightened in #737: the anchor rule now demands a proper noun OR a hard
    datum (year, amount, percentage, rank …) copied verbatim from the material,
    tier A (fact sentence) is the explicit default rather than one of three
    equal options, and the model must first pick as many distinct facts as
    there are words and spend each one exactly once — #634's version let the
    model drift into tier B/C commentary about the material's opening topic,
    so the sentences carried neither numbers nor new information.

    One main call + up to 2 missing-word retry rounds + fallback sentences.
    Deliberately has NO fact-check and NO whole-material validation retry —
    those are what make briefing slow and expensive, and podcast/knowledge
    content (unlike news) is not something to fact-check against an external
    source in the same way. `material` is normally the item's full transcript
    (transcript_zh, truncated to 15000 chars by the caller — issue #661;
    Daniel explicitly asked for transcript-based cards, #561 had switched to
    summary_de purely to cut cost/latency, which stopped mattering once cost
    was confirmed to be ~$0.003/generation) with summary_de as a fallback for
    rows that only have a summary (old synced-down data, etc.). The prompt
    template's {summary} placeholder is reused for whichever text is passed
    in, so custom prompt_presets Daniel already saved keep working unchanged.

    Multi-source selections (issue #752) used to be crammed into one call
    with a "source_index" tag per sentence so the model could interleave
    them. Daniel didn't want interleaving — he wants source A finished before
    source B starts. Issue #776 removed that machinery: routes/story.py now
    calls this function once per selected source, each with its own slice of
    the due-word list, and concatenates the results in source order. This
    function therefore only ever sees a single source again, and the prompt
    is back to being byte-for-byte the pre-#752 single-source version.

    attempt_label: chunk marker like " (2/3)" appended to every progress
    message — the route batches cards by MAX_NEWS_BATCH and calls this once
    per chunk (same convention as generate_briefing_sentences).

    Returns (sentences, prompt) like the other generators in this module. The
    prompt is what was *actually sent*, joined across rounds — retry rounds add
    an extra_hint, so a prompt rebuilt afterwards would not be the one that
    produced these sentences, which is the whole point of keeping it (#697).
    """
    if not cards or not source:
        return [], ""

    # 模板可被用户自定义覆盖（issue #581）；默认渲染结果与旧内联 f-string 逐字一致。
    # #654: 查的是 "knowledge" 模板——mode='podcast' 的历史故事也走这个函数，
    # 迁移后它们的自定义模板同样存在 mode='knowledge' 下（见 database/core.py）。
    # {summary} 占位符名字保留不变（issue #661 起实际传入的多为转录全文而非
    # 摘要）——改名会让 Daniel 已保存的自定义模板失效。
    tpl = _story_prompt_template("knowledge")
    title_block = source.get("title") or ""
    summary_block = source.get("material") or ""

    def _build_prompt(batch: list[dict], extra_hint: str = "") -> str:
        if lang == "zh":
            word_list = "\n".join(
                f"{i + 1}. {c['word_zh']}（{c.get('pinyin', '')}）— {c.get('definition', '')}"
                for i, c in enumerate(batch)
            )
            return _render_prompt(tpl, {
                "title": title_block,
                "summary": summary_block,
                "words": word_list,
                "max_hsk": str(max_hsk),
                "extra_hint": extra_hint,
            })
        # Non-Chinese decks (issue #806): knowledge mode is language-agnostic —
        # the material's language never mattered, only the output language
        # does. The background-vocabulary slider's shared 1-6 value maps to a
        # CEFR cap here, exactly as in generate_sentences.
        cfg = languages.get_lang_config(lang)
        word_list = "\n".join(
            f"{i + 1}. {c['word_zh']} — {c.get('definition_de') or c.get('definition') or ''}"
            for i, c in enumerate(batch)
        )
        return _KNOWLEDGE_PROMPT_NON_ZH.format(
            lang_name=cfg["name_en"],
            learner=cfg["learner_level"],
            title=title_block,
            summary=summary_block,
            words=word_list,
            background=f"CEFR A1-{_CEFR_LEVELS.get(max_hsk, 'B1')}",
            sentence_limit=cfg["sentence_limit"],
            extra_hint=extra_hint,
        )

    sentences: list[dict] = []
    remaining = list(cards)
    prompts_sent: list[str] = []
    # Looked up once per generation, not once per candidate sentence.
    forms_by_card = {c["word_id"]: _card_surface_forms(c, lang) for c in cards}

    def _run_round(batch: list[dict], extra_hint: str, label: str) -> None:
        """One AI call for `batch`; moves every word it covered out of `remaining`."""
        # #743: all-at-once batching (issue #563) can mean 30+ sentences in one
        # response — a fixed 8192 cap left no room for that many, so the model's
        # reply got cut off mid-array and the whole round used to be discarded.
        # Budget scales with batch size (see _podcast_max_tokens); the gpt-5
        # series shares this budget with internal reasoning tokens.
        prompt = _build_prompt(batch, extra_hint)
        prompts_sent.append(f"── {label} ──\n{prompt}" if prompts_sent else prompt)
        raw = _call_api(model, [{"role": "user", "content": prompt}],
                         _podcast_max_tokens(model, len(batch)), purpose="podcast")

        items, truncated = _parse_json_array_salvage(raw)
        if not items:
            log_progress(progress_key, f"{label}：AI 回复无法解析，本轮作废")
            return
        if truncated:
            log_progress(progress_key, f"{label}：AI 回复被截断，救回 {len(items)} 条")

        # Scan in order: not trusting the AI's target_word tag, only the actual
        # sentence content counts (same approach as briefing).
        got = 0
        for item in items:
            s_zh = (item.get("sentence_zh") or "").strip()
            if not s_zh:
                continue
            matched = next(
                (c for c in remaining
                 if any(_word_match(f, s_zh, lang) for f in forms_by_card[c["word_id"]])),
                None)
            if matched is None:
                continue          # no target word → drop (this mode allows no context sentences)
            remaining.remove(matched)
            got += 1
            sentences.append({
                "word_ids": [matched["word_id"]],
                "sentence_zh": s_zh,
                "sentence_en": "",
                "concept_en": "",
                "concept_zh": "",
                # #634: the prompt now makes the model plan topic/anchor/tier in
                # reasoning_zh before writing — keep it, it shows in the card's
                # background popup like kahneman's does.
                "reasoning_zh": (item.get("reasoning_zh") or "").strip(),
                "source_url": source.get("url"),
                "source_title": source.get("title"),
                "tokens": [],
            })
        log_progress(progress_key, f"{label}：拿到 {got} 句，还差 {len(remaining)} 个词")

    log_progress(progress_key, f"开始生成播客句子：{len(cards)} 个词，模型 {model}")

    # Keep re-requesting the words the model skipped until none are left
    # (issue #642). The old 3-round cap left plenty of words on the fallback
    # sentence 我学了X这个词。
    for round_no in range(1, MAX_PODCAST_ROUNDS + 1):
        if not remaining:
            break
        missing = "、".join(c["word_zh"] for c in remaining)
        if round_no == 1:
            hint = ""
            msg = f"生成播客句子…{attempt_label}"
            batches = [remaining]
        else:
            # #642: the retry used to resend the *identical* prompt, so the model
            # simply reproduced the same omissions. Name the skipped words and
            # relax topic coverage — with a handful of words left, "cover every
            # topic" is an instruction that cannot be satisfied.
            hint = (f"\n\n【补漏轮】上一轮你漏掉了这些词：{missing}。"
                    f"这一轮只需要为上面列出的每一个词各写一句话，一个都不能少。"
                    f"词少的时候不必覆盖素材里的所有话题，但每句仍然必须包含"
                    f"一个来自素材的专有名词。") if lang == "zh" else (
                    f"\n\nRETRY ROUND: you skipped these words last time: {missing}. "
                    f"Write one sentence for each of them — none may be missing. "
                    f"With only a few words left you no longer have to cover every "
                    f"topic in the material, but each sentence must still name a "
                    f"proper noun or a hard datum taken from it.")
            msg = f"补漏 {len(remaining)} 个词（第{round_no}轮）…{attempt_label}"
            # Late rounds go one word per call: with only a few words left this
            # is the most reliable shape — the model has no room to "pick the
            # easy ones" and drop the rest.
            batches = ([[c] for c in remaining] if round_no >= PODCAST_SOLO_ROUND
                       else [list(remaining)])
        _set_progress(progress_key, phase="request", msg=msg,
                      percent=min(20 + round_no * 12, 90))
        if round_no > 1:
            log_progress(progress_key,
                         f"第{round_no}轮补漏"
                         f"{'（每词单独一次调用）' if round_no >= PODCAST_SOLO_ROUND else ''}"
                         f"：{missing}")
        for batch in batches:
            if not any(c in remaining for c in batch):
                continue          # already covered by an earlier batch this round
            _run_round(batch, hint, f"第{round_no}轮")

    # Per-word fallback so every card ends up with a sentence. After #642 this
    # only triggers when every round failed outright (API errors, censored
    # replies) — not merely because the model skipped a word.
    for card in remaining:
        log_progress(progress_key, f"⚠️ {card['word_zh']}：{MAX_PODCAST_ROUNDS} 轮都没写出句子，用兜底句")
        sentences.append({
            "word_ids": [card["word_id"]],
            # The Chinese filler sentence would be nonsense in a French deck;
            # for other languages fall back to the word itself, which is at
            # least honest about being a fallback (issue #806).
            "sentence_zh": (card.get("source_sentence")
                            or (f"我学了{card['word_zh']}这个词。" if lang == "zh"
                                else f"{card['word_zh']}.")),
            "sentence_en": "",
            "concept_en": "",
            "concept_zh": "",
            "reasoning_zh": "",
            "source_url": source.get("url"),
            "source_title": source.get("title"),
            "tokens": [],
        })

    _fill_translations(sentences, progress_key=progress_key, lang=lang)
    return sentences, "\n\n".join(prompts_sent)


def summarize_news_items(items: list[dict], model: str = "gpt-5-mini",
                         max_items: int = 8, progress_key: str | None = None) -> list[dict]:
    """News auto mode, step 1 of 2: pick the most important of today's fetched
    news items and condense each into a short summary. The result feeds
    generate_news_sentences exactly like pasted articles do.

    items: news_fetcher.fetch_all() output [{url, title, text, source_name}]
    Returns [{url, title, text}] — text is the AI's condensed summary.

    Falls back to the first max_items raw items when the AI reply is unusable —
    that is still real news content, only the selection/condensing is skipped
    (a network failure upstream raises news_fetcher.NewsFetchError instead).
    """
    if not items:
        return []

    _set_progress(progress_key, phase="request", msg="Selecting today's top news…", percent=12)
    listing = "\n\n".join(
        f"[{i}] ({it.get('source_name', '')}) {it.get('title', '')}\n{(it.get('text') or '')[:400]}"
        for i, it in enumerate(items)
    )
    prompt = f"""Below are today's news items fetched from German and international sources.

{listing}

Task: choose the {max_items} most important items for a daily world-news briefing.
Balance the selection: German domestic news, international news, and China-related news (when available).
Skip near-duplicates covering the same event.

Return ONLY a JSON array, no other text:
[
  {{"idx": 0, "summary": "3-5 sentence factual English summary of the item"}}
]
idx is the item number in square brackets above."""

    try:
        raw = _call_api(model, [{"role": "user", "content": prompt}], 8192, purpose="news-select")
        start, end = raw.find("["), raw.rfind("]") + 1
        picked = json.loads(raw[start:end]) if start != -1 and end != 0 else []
        articles = []
        for p in picked:
            idx = p.get("idx")
            summary = (p.get("summary") or "").strip()
            if isinstance(idx, int) and 0 <= idx < len(items) and summary:
                it = items[idx]
                articles.append({"url": it.get("url", ""), "title": it.get("title", ""),
                                 "source_name": it.get("source_name", ""), "text": summary})
        if articles:
            logger.info("[%s] summarize_news_items: %d/%d items selected",
                        model, len(articles), len(items))
            return articles[:max_items]
        logger.warning("summarize_news_items: empty/unusable selection, falling back to raw items")
    except Exception as e:
        logger.warning("summarize_news_items failed (%s), falling back to raw items", e)
    return [{"url": it.get("url", ""), "title": it.get("title", ""),
             "source_name": it.get("source_name", ""), "text": (it.get("text") or "")[:600]}
            for it in items[:max_items]]


_PODCAST_DETAIL_WORDS = {
    "short": "~150",
    "medium": "~300",
    "detailed": "900-1300",
}


def build_podcast_summary_prompt(transcript: str, title: str, detail_level: str) -> str:
    """Shared prompt builder for both the API summary path
    (summarize_podcast_transcript, gpt/DeepSeek) and the free NotebookLM
    chat.ask path (podcast._summarize_via_notebooklm, #510) — same prompt
    text, same JSON contract, so parse_podcast_summary_json below can parse
    either response the same way."""
    words_target = _PODCAST_DETAIL_WORDS.get(detail_level, _PODCAST_DETAIL_WORDS["detailed"])
    # Transcripts can be long (auto-captions of a 30-60min episode) — cap input
    # to keep the request within a reasonable token budget. Raised to 30000
    # (#541) so a "detailed" summary can actually cover the whole episode.
    excerpt = transcript[:30000]

    return f"""You are summarizing a podcast/video episode for a German-speaking learner of
Chinese (HSK 4-5 level, learning towards HSK 6).

Episode title: {title}

Transcript (auto/manual captions, may contain minor recognition errors). The transcript
language is WHATEVER the source material actually is — it may be Chinese, German, English,
or a mix (#651: this pipeline now also ingests YouTube videos, which are frequently German
or English). Do NOT assume it is Chinese. Regardless of the transcript's language, your two
summaries below have a FIXED output language each: summary_zh is always Chinese, summary_de
is always German — translate/summarize into those languages no matter what language the
source is in. For a German or English source, the Chinese summary IS the learning material
(that's the point — it lets Daniel read the content in Chinese even though the source wasn't).
{excerpt}

Task:
1. Write a detailed German-language summary of what is discussed in the episode, so the
   listener understands the content before listening. Target length: {words_target} words.
   Structure it into multiple paragraphs, in the style of the table.media briefings:
   - Wrap every paragraph in <p>...</p> tags.
   - Each paragraph MUST begin with ONE lead sentence that summarizes the whole paragraph,
     wrapped in <b>...</b> tags. The remaining sentences of the paragraph give the details.
     Someone reading only the bold lead sentences must get the complete skeleton of the
     episode. Example paragraph:
     <p><b>Hinter den Enthüllungen stehen diverse problematische Anreize.</b> Dazu gehört
     ein exzessiver Fokus auf quantitativen Output. ...</p>
   Be concrete: include the specific facts, numbers,
   names and arguments actually mentioned in the episode — do not stay generic or vague.
   - Whenever you name a company, organization, brand or institution, add its common Chinese
     name in parentheses right after it, e.g. "Airbnb (爱彼迎)", "Lawn Tennis Association
     (英国草地网球协会)". If no established Chinese name exists, give a natural Chinese rendering.
   - Annotate Chinese vocabulary generously. Whenever the German text expresses a concept,
     term or set phrase that is HSK 5 or above in Chinese, add its pinyin AND Chinese form
     in parentheses right after the German rendering, in the format "pinyin/汉字",
     e.g. "Rezession (jīngjì shuāituì/经济衰退)", "Lieferkette (gōngyìng liàn/供应链)", so
     Daniel links the German meaning to the Chinese word — and its pronunciation.
     This is NOT limited to the words you extract in Task 3 — annotate any non-basic Chinese
     term the episode uses, including ones that did not make that list. When in doubt,
     annotate: a redundant annotation costs Daniel nothing, a missing one costs him the link
     between the German meaning and the Chinese word he is about to hear. For words that ARE
     in the Task 3 list, use the same pinyin (with tone marks) you give there.
   - If (and ONLY if) the transcript contains timestamps, add an approximate timestamp in
     parentheses when you introduce each major topic, e.g. "(ca. 12:30)", so the listener can
     jump to it. If the transcript contains no timestamps, do NOT invent any.
   Wrap the most important vocabulary/terms/names in <strong>...</strong> HTML tags (these
   become the highlighted words in the email).
2. Translate that German summary into Chinese — in full. This is NOT a shorter teaser and
   NOT an independently written summary: it is the SAME text in Chinese. Same number of
   paragraphs, same order, same facts, numbers, names and arguments, nothing dropped and
   nothing added. It is shown above the German version so Daniel can read the whole episode
   summary in Chinese first.
   - Same markup: every paragraph wrapped in <p>...</p>, and each paragraph MUST begin with
     the translated lead sentence wrapped in <b>...</b>. Paragraph N of the Chinese text
     must correspond to paragraph N of the German text. Example paragraph:
     <p><b>这些丑闻背后是一套有问题的激励机制。</b>其中之一是过度看重论文数量。……</p>
   - Write it at HSK 4-5 level — his actual reading level. Use plain, common vocabulary and
     short sentences. This is a comprehension aid, not another study exercise, so do NOT
     reach for literary or specialist words where an everyday one works. Splitting one long
     German sentence into two short Chinese ones is fine — dropping its content is not.
   - Do NOT carry over the "pinyin/汉字" annotations from the German version — the text is
     already Chinese. Likewise, write company/organization names directly in their Chinese
     form ("爱彼迎"), without repeating the foreign name.
   - <strong> highlights are not needed here; keep only <p> and <b>.
3. Extract the 20-35 most important Chinese words/phrases from the transcript that are HSK
   level 5 or above (i.e. non-basic vocabulary Daniel would benefit from pre-learning). For
   each, give pinyin and a German definition.
4. Suggest a short CHINESE title that summarizes what this episode is actually ABOUT (the
   people, event or argument involved) — like a real article headline, not a generic label.
   Maximum 15 characters, written at HSK 4-5 level like summary_zh, since this is the title
   Daniel reads in the list. Do NOT write anything generic like "某人的视频", "Reel 总结" or
   "内容总结" — those carry zero information about the content. No quotes, no punctuation
   at the end.

Return ONLY a JSON object, no other text, no markdown fences:
{{
  "title_suggestion": "<简短中文标题，最多 15 字，HSK 4-5 水平>",
  "summary_de": "<German HTML summary: <p> paragraphs, each starting with a <b> lead sentence, <strong> highlights>",
  "summary_zh": "<德语总结的完整中文翻译：同样的 <p> 段落，每段首句用 <b> 包住，HSK 4-5 水平的简单中文>",
  "words": [
    {{"word": "词语", "pinyin": "cí yǔ", "definition_de": "kurze deutsche Definition", "hsk": 5}}
  ]
}}"""


def parse_podcast_summary_json(raw: str) -> dict:
    """Parse a podcast-summary JSON reply (shared by both the API path and
    the NotebookLM chat.ask path, #510). Strips NotebookLM-style citation
    markers like "[1]" before parsing — harmless no-op for plain API
    responses, which never contain them.

    Returns {"summary_zh": str, "summary_de": str, "words": [...], "title_suggestion": str};
    summary_de is "" and words is [] on any parse failure (mirrors the previous inline
    behavior in summarize_podcast_transcript).

    summary_zh (#631) is a bonus, not a requirement: an older model reply — or
    one that simply omitted the field — still yields a perfectly usable German
    summary, so callers gate success on summary_de alone and treat an empty
    summary_zh as "no Chinese intro this time".

    title_suggestion (#781) is the same kind of bonus: NotebookLM's chat.ask path
    and older prompt versions never produced it, so it defaults to "" on omission.
    Callers must keep gating success on summary_de alone — a missing title
    suggestion must never fail an otherwise-good summary.
    """
    raw = re.sub(r"\[\d+\]", "", raw)
    start, end = raw.find("{"), raw.rfind("}") + 1
    try:
        data = json.loads(raw[start:end]) if start != -1 and end != 0 else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    summary_de = (data.get("summary_de") or "").strip()
    summary_zh = (data.get("summary_zh") or "").strip()
    words = []
    for w in data.get("words") or []:
        word = (w.get("word") or "").strip()
        if not word:
            continue
        words.append({
            "word": word,
            "pinyin": (w.get("pinyin") or "").strip(),
            "definition_de": (w.get("definition_de") or "").strip(),
            "hsk": w.get("hsk") if isinstance(w.get("hsk"), int) else 5,
        })
    title_suggestion = (data.get("title_suggestion") or "").strip()
    return {"summary_zh": summary_zh, "summary_de": summary_de, "words": words,
            "title_suggestion": title_suggestion}


def summarize_podcast_transcript(transcript: str, title: str,
                                 detail_level: str = "detailed",
                                 china_critical: bool = False) -> dict:
    """Podcast crawler (issue #479): one AI call that turns a raw Chinese
    transcript into a German summary + a list of HSK5+ vocabulary worth
    reviewing before listening.

    Uses DEFAULT_MODEL (DeepSeek) first when DEEPSEEK_API_KEY is configured —
    unlike the news briefing, a podcast content summary isn't the
    censorship-sensitive case that forced news onto OpenAI, so a cheap
    DeepSeek pass is preferred to save money (#532). Falls back to
    resolve_briefing_model() (OpenAI/gpt) if DeepSeek is unavailable or its
    reply fails to parse — gpt is the paid-but-reliable backstop.

    `china_critical` (#731) is the exception Daniel flags at paste time: for
    material critical of China, DeepSeek is the censorship-sensitive case
    after all, and it doesn't announce it — it quietly waters the summary
    down or refuses, and a watered-down summary still parses, so no fallback
    would ever trigger. The flag therefore drops DeepSeek from the candidate
    list entirely rather than merely reordering it.

    This is the "api" summarizer path — podcast.summarize() (#510) tries the
    free NotebookLM chat.ask path first when podcast_config.summarizer=auto,
    falling back to this function.

    Returns {"summary_de": str, "words": [{"word", "pinyin", "definition_de", "hsk"}]}.
    Falls back to an empty-ish result (summary_de note, words=[]) on any
    parse/API failure — callers store status='error' and move on, they don't
    crash the whole crawl run over one bad transcript.
    """
    prompt = build_podcast_summary_prompt(transcript, title, detail_level)

    candidates = []
    if os.environ.get("DEEPSEEK_API_KEY") and not china_critical:
        candidates.append(DEFAULT_MODEL)
    fallback = resolve_briefing_model()
    if fallback not in candidates:
        candidates.append(fallback)
    primary = candidates[0]
    for model in candidates:
        try:
            raw = _call_api(model, [{"role": "user", "content": prompt}], 8192, purpose="podcast-summary")
            result = parse_podcast_summary_json(raw)
            if result["summary_de"]:
                if model != primary:
                    logger.info("summarize_podcast_transcript: fell back to %s after %s failed", model, primary)
                return result
            logger.warning("summarize_podcast_transcript: empty summary_de in AI reply (%s)", model)
        except Exception as e:
            logger.warning("summarize_podcast_transcript failed on %s (%s)", model, e)
    return {"summary_zh": "", "summary_de": "", "words": []}


# Only the head of the body is sent to the metadata extractor: title, author,
# outlet and date live in the first screenful of every article ever written,
# and feeding a 15000-char body to pull four short strings out of it is money
# burnt for nothing.
_METADATA_SAMPLE_CHARS = 3000


def extract_article_metadata(text: str) -> dict:
    """One cheap DeepSeek call (issue #833) to pull title/author/source
    URL/publication date out of a pasted article body, for the fields Daniel
    left blank in the paste form.

    Returns a dict with any subset of {"title", "author", "source_url",
    "published_at"} — keys the model couldn't find are simply absent.

    Returns {} on ANY failure (empty text, API error, unparseable reply)
    rather than raising: this is a convenience on top of an article body the
    user already handed us, and losing the whole paste because a nice-to-have
    title lookup failed would be absurd. Same contract as translate_title.

    published_at is only returned when it parses as YYYY-MM-DD — models
    happily answer "last Wednesday" or "März 2024", and a junk date in the
    column is worse than no date.
    """
    text = (text or "").strip()
    if not text:
        return {}
    sample = text[:_METADATA_SAMPLE_CHARS]
    prompt = (
        "Extract bibliographic metadata from the beginning of this article. "
        "Reply with ONLY a JSON object, no markdown fences, no explanation:\n"
        '{"title": "...", "author": "...", "source_url": "...", "published_at": "YYYY-MM-DD"}\n'
        "Use null for anything that is not clearly stated in the text — do NOT "
        "guess an author, a URL or a date, and do not invent a title that is "
        "not there. The title must be the article's own headline, copied "
        "verbatim in its original language.\n\n"
        f"Article:\n{sample}"
    )
    try:
        raw = _call_api(DEFAULT_MODEL, [{"role": "user", "content": prompt}], 300,
                        purpose="knowledge-metadata")
    except Exception as e:
        logger.warning("extract_article_metadata: AI call failed: %s", e)
        return {}

    start, end = raw.find("{"), raw.rfind("}") + 1
    try:
        data = json.loads(raw[start:end]) if start != -1 and end != 0 else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("extract_article_metadata: could not parse reply: %s", raw[:200])
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    for key in ("title", "author", "source_url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    published = data.get("published_at")
    if isinstance(published, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published.strip()):
        out["published_at"] = published.strip()
    return out


def translate_title(title: str) -> str | None:
    """One cheap DeepSeek call (issue #651) to translate a non-English episode
    title into English, stored in podcast_episodes.title_en — YouTube videos
    frequently have Chinese/German titles and the podcast manager UI wants a
    consistent English column to scan. Returns None on any failure (empty
    title, API error, empty reply) rather than raising — a missing title_en
    must never fail episode processing, it's a cosmetic nicety."""
    title = (title or "").strip()
    if not title:
        return None
    prompt = (
        "Translate this podcast/video episode title into natural English. "
        "Reply with ONLY the translated title, no quotes, no explanation, "
        "no markdown. If it is already English, reply with it unchanged.\n\n"
        f"Title: {title}"
    )
    try:
        raw = _call_api(DEFAULT_MODEL, [{"role": "user", "content": prompt}], 200,
                         purpose="podcast-title-translate")
    except Exception as e:
        logger.warning("translate_title failed for %r: %s", title, e)
        return None
    raw = raw.strip().strip('"').strip("'")
    return raw or None


def estimate_story_tokens(num_cards: int) -> int:
    """Rough token estimate for generating a story with num_cards words.

    Input:  ~200 base + 13 tokens/card
    Output: ~75 tokens/card + 100 overhead
    """
    return 200 + 13 * num_cards + 75 * num_cards + 100


def get_deepseek_balance() -> dict | None:
    """Fetch the current DeepSeek account balance (issue #508 — a 'real anchor'
    shown alongside the estimated cost breakdown in the API Costs modal).

    Returns {"balance": str, "currency": str} or None if the key isn't
    configured or the request fails for any reason. This is a nice-to-have —
    it must never raise or block the cost modal from rendering.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        info = (data.get("balance_infos") or [{}])[0]
        balance = info.get("total_balance")
        currency = info.get("currency")
        if balance is None or currency is None:
            return None
        return {"balance": balance, "currency": currency}
    except Exception as e:
        logger.debug("get_deepseek_balance failed: %s", e)
        return None


def _get_alibaba_balance() -> dict | None:
    """Fetch the Alibaba Cloud account balance via the BSS OpenAPI
    QueryAccountBalance action (issue #580), signed with the classic RPC
    HMAC-SHA1 scheme — stdlib only, no alibabacloud_bssopenapi dependency.

    Reuses the Tingwu AccessKey pair (ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET).
    Tries the international endpoint first (Daniel's account uses
    dashscope-intl), then the mainland one. Returns {"balance", "currency"}
    or None — never raises (nice-to-have, same contract as DeepSeek)."""
    ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not (ak and sk):
        return None
    import base64
    import hashlib
    import hmac
    import urllib.parse
    import urllib.request
    import uuid
    from datetime import datetime, timezone

    params = {
        "Action": "QueryAccountBalance",
        "Version": "2017-12-14",
        "Format": "JSON",
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def _pct(s: str) -> str:
        return urllib.parse.quote(str(s), safe="-_.~")

    canonical = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + _pct(canonical)
    params["Signature"] = base64.b64encode(
        hmac.new((sk + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    query = urllib.parse.urlencode(params)
    # The signature covers only the query string, not the host — one signature
    # works for both endpoints.
    for host in ("business.ap-southeast-1.aliyuncs.com", "business.aliyuncs.com"):
        try:
            with urllib.request.urlopen(f"https://{host}/?{query}", timeout=5) as resp:
                data = json.loads(resp.read())
            d = data.get("Data") or {}
            if d.get("AvailableAmount") is not None:
                return {"balance": d["AvailableAmount"], "currency": d.get("Currency") or "CNY"}
        except Exception as e:
            logger.debug("alibaba balance via %s failed: %s", host, e)
    return None


# Provider-balance cache (issue #580): the fetches block the cost modal, so a
# 5-minute cache keeps repeat opens instant. Balances only change when Daniel
# tops up or generates something — staleness is harmless.
_balance_cache: dict = {"at": 0.0, "data": None}
_BALANCE_CACHE_TTL_SECONDS = 300


def get_provider_balances() -> list[dict]:
    """Balance rows for every configured AI provider (issue #580).

    Row shape: {"provider": str, "balance": str|None, "currency": str|None,
    "unsupported": bool, "note": str|None}. Providers whose key isn't
    configured are omitted; providers with no balance API (OpenAI, Anthropic,
    Zhipu) get unsupported=True with a pointer to their console. Fetch
    failures show balance=None so the frontend can say "unavailable"."""
    import time as _time
    now = _time.time()
    if _balance_cache["data"] is not None and now - _balance_cache["at"] < _BALANCE_CACHE_TTL_SECONDS:
        return _balance_cache["data"]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ds = ex.submit(get_deepseek_balance)
        f_ali = ex.submit(_get_alibaba_balance)
        ds, ali = f_ds.result(), f_ali.result()

    rows: list[dict] = []

    def _row(provider: str, balance: dict | None = None, note: str | None = None,
             unsupported: bool = False) -> dict:
        return {"provider": provider,
                "balance": (balance or {}).get("balance"),
                "currency": (balance or {}).get("currency"),
                "unsupported": unsupported, "note": note}

    if os.environ.get("DEEPSEEK_API_KEY"):
        rows.append(_row("DeepSeek", ds))
    if os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID"):
        rows.append(_row("Alibaba", ali))
    if os.environ.get("ZHIPU_API_KEY"):
        rows.append(_row("Zhipu", unsupported=True, note="no balance API — bigmodel.cn console"))
    if os.environ.get("OPENAI_API_KEY"):
        rows.append(_row("OpenAI", unsupported=True, note="no balance API — platform.openai.com"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        rows.append(_row("Anthropic", unsupported=True, note="no balance API — console.anthropic.com"))

    _balance_cache.update(at=now, data=rows)
    return rows

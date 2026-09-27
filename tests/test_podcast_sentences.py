"""ai.generate_podcast_sentences（#561，提示词于 #634 重写）。

AI 一律打桩在 ai._call_api 上——打在某个提供商的客户端上会随默认模型变化而静默失效。
"""
import json

import pytest

import ai


CARDS = [
    {"word_id": 1, "word_zh": "承认", "pinyin": "chéngrèn", "definition": "admit"},
    {"word_id": 2, "word_zh": "顺便", "pinyin": "shùnbiàn", "definition": "by the way"},
]


def _sources(material, title="标题"):
    """#776: generate_podcast_sentences takes a single source dict (one AI
    call = one source; routes/story.py loops over sources itself)."""
    return {"title": title, "kind": "podcast", "url": None, "material": material}


def _reply(items):
    return json.dumps(items, ensure_ascii=False)


def test_reasoning_is_kept(monkeypatch):
    """#634：模型先想话题/锚点/档位再写句子，这段推理要存进 reasoning_zh
    （背景弹窗里显示），以前被硬编码成空字符串。"""
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: _reply([
        {"reasoning_zh": "话题：字节跳动的AI路线。", "sentence_zh": "张一鸣承认公司暂时落后。",
         "target_word": "承认"},
        {"reasoning_zh": "话题：抖音电商。", "sentence_zh": "他在抖音上刷视频，顺便就下了单。",
         "target_word": "顺便"},
    ]))
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert [s["reasoning_zh"] for s in sentences] == [
        "话题：字节跳动的AI路线。", "话题：抖音电商。"]
    assert [s["word_ids"] for s in sentences] == [[1], [2]]


def test_missing_reasoning_is_not_fatal(monkeypatch):
    """模型漏掉 reasoning_zh 时只是少一段背景说明，句子本身照常入库。"""
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: _reply([
        {"sentence_zh": "张一鸣承认公司暂时落后。"},
        {"sentence_zh": "他在抖音上刷视频，顺便就下了单。"},
    ]))
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert [s["reasoning_zh"] for s in sentences] == ["", ""]
    assert len(sentences) == 2


def test_skipped_word_is_re_requested(monkeypatch):
    """#642：模型漏词时继续补漏，而不是 3 轮后丢给兜底句。
    补漏轮的提示词必须点名漏掉的词——原来重发的是一模一样的提示词。"""
    prompts = []

    def fake_call(model, messages, *a, **kw):
        prompts.append(messages[0]["content"])
        if len(prompts) == 1:                      # 第一轮只写了一个词
            return _reply([{"sentence_zh": "张一鸣承认公司暂时落后。"}])
        return _reply([{"sentence_zh": "他在抖音上刷视频，顺便就下了单。"}])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert len(prompts) == 2
    assert "顺便" in prompts[1] and "补漏轮" in prompts[1]
    assert sorted(s["word_ids"][0] for s in sentences) == [1, 2]
    assert not any("我学了" in s["sentence_zh"] for s in sentences)


def test_fallback_only_after_all_rounds(monkeypatch):
    """AI 始终不写句子时仍然收敛：每张卡都有句子，且轮数有上限。"""
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: _reply([]))
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert len(sentences) == 2
    assert all("我学了" in s["sentence_zh"] for s in sentences)


def test_progress_log_is_recorded(monkeypatch):
    """#642：加载界面的日志按 progress_key 累积，供 /api/story-progress 返回。"""
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: _reply([
        {"sentence_zh": "张一鸣承认公司暂时落后。"},
        {"sentence_zh": "他在抖音上刷视频，顺便就下了单。"},
    ]))
    key = "test/reading/zh"
    ai.reset_story_log(key)
    ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"), progress_key=key)

    log = ai._story_log[key]
    assert any("开始生成播客句子" in line for line in log)
    assert any("还差 0 个词" in line for line in log)
    ai.reset_story_log(key)


# ── #697: 返回实际发出去的提示词，供加星句子回看 ──────────────────────────────

def test_returns_the_prompt_actually_sent(monkeypatch):
    """加星句子要能翻到生成它的提示词，所以这里必须把提示词交出来——
    而且是真发出去的那份，不是事后重建的。"""
    sent = []

    def fake_call(model, messages, *a, **kw):
        sent.append(messages[0]["content"])
        return _reply([{"sentence_zh": "张一鸣承认公司暂时落后。"},
                       {"sentence_zh": "他在抖音上刷视频，顺便就下了单。"}])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("字节跳动的一集"))

    assert prompt == sent[0]
    assert "字节跳动的一集" in prompt      # 素材
    assert "承认" in prompt and "顺便" in prompt   # 目标词


def test_prompt_excludes_ads_even_with_a_custom_template(monkeypatch):
    """#1236: cards read the raw transcript, so the ad rule must be fixed
    outside the user-editable template and survive a custom preset."""
    monkeypatch.setattr(ai, "_story_prompt_template", lambda mode: "素材：{summary}")
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: _reply([]))
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    _, prompt = ai.generate_podcast_sentences(CARDS, _sources("正文。广告口播。"))

    assert "广告、赞助口播、联盟推广、产品推销和购买号召" in prompt
    assert "不得用于生成卡片句子或 reasoning_zh" in prompt


def test_prompt_includes_every_retry_round(monkeypatch):
    """补漏轮带 extra_hint，提示词和第一轮不同——只留第一轮就说不清
    这些句子到底是被什么提示词写出来的。"""
    def fake_call(model, messages, *a, **kw):
        if "补漏轮" not in messages[0]["content"]:
            return _reply([{"sentence_zh": "张一鸣承认公司暂时落后。"}])
        return _reply([{"sentence_zh": "他在抖音上刷视频，顺便就下了单。"}])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    _, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert "补漏轮" in prompt
    assert prompt.count("【补漏轮】") == 1


def test_no_material_returns_empty_prompt(monkeypatch):
    """没有素材（没有可用的 source dict）时不调 AI，也就没有提示词可存。
    caller（routes/story.py）在真正调用前就已经把无素材的条目过滤掉了，所以
    这里模拟"过滤后什么都不剩"的情形：source={}。"""
    monkeypatch.setattr(ai, "_call_api",
                        lambda *a, **kw: pytest.fail("素材为空时不该调 AI"))
    sentences, prompt = ai.generate_podcast_sentences(CARDS, {})
    assert sentences == [] and prompt == ""


# ── #743: 截断 JSON 救回 ────────────────────────────────────────────────────

def test_salvage_parses_complete_array():
    raw = json.dumps([
        {"sentence_zh": "第一句。"},
        {"sentence_zh": "第二句。"},
    ], ensure_ascii=False)
    items, truncated = ai._parse_json_array_salvage(raw)
    assert truncated is False
    assert [i["sentence_zh"] for i in items] == ["第一句。", "第二句。"]


def test_salvage_recovers_truncated_array():
    """一个 3 元素数组的字符串，从中间某处砍掉——只留前两个完整对象。"""
    full = json.dumps([
        {"sentence_zh": "第一句。"},
        {"sentence_zh": "第二句。"},
        {"sentence_zh": "第三句，这句被砍掉了。"},
    ], ensure_ascii=False)
    # 砍在第三个对象的开头之后、闭合花括号之前——模拟 max_tokens 截断。
    cut_at = full.rfind('"sentence_zh"')
    truncated_raw = full[:cut_at] + '"sentence_zh": "第三句，这句'

    items, truncated = ai._parse_json_array_salvage(truncated_raw)
    assert truncated is True
    assert len(items) == 2
    assert [i["sentence_zh"] for i in items] == ["第一句。", "第二句。"]


def test_salvage_handles_braces_and_quotes_in_content():
    """句子内容里含 { } " 转义时不会解析错位。"""
    raw = json.dumps([
        {"sentence_zh": '他说："这个{词}很难"，然后笑了。'},
        {"sentence_zh": "第二句正常。"},
    ], ensure_ascii=False)
    items, truncated = ai._parse_json_array_salvage(raw)
    assert truncated is False
    assert items[0]["sentence_zh"] == '他说："这个{词}很难"，然后笑了。'
    assert items[1]["sentence_zh"] == "第二句正常。"


def test_salvage_with_braces_and_quotes_when_truncated():
    """截断场景下同样要正确跳过字符串内的花括号/引号，不能在里面误判对象结束。"""
    full = json.dumps([
        {"sentence_zh": '他说："这个{词}很难"，然后笑了。'},
        {"sentence_zh": "第二句正常。"},
        {"sentence_zh": "第三句被砍掉。"},
    ], ensure_ascii=False)
    cut_at = full.rfind('"sentence_zh"')
    truncated_raw = full[:cut_at] + '"sentence_zh": "第三句被'

    items, truncated = ai._parse_json_array_salvage(truncated_raw)
    assert truncated is True
    assert len(items) == 2
    assert items[0]["sentence_zh"] == '他说："这个{词}很难"，然后笑了。'
    assert items[1]["sentence_zh"] == "第二句正常。"


def test_end_to_end_truncated_reply_still_yields_sentences(monkeypatch):
    """原来：截断的一轮整轮作废，句子数为 0。现在：救回完整的那几条。"""
    full = json.dumps([
        {"sentence_zh": "张一鸣承认公司暂时落后。"},
        {"sentence_zh": "他在抖音上刷视频，顺便就下了单。"},
    ], ensure_ascii=False)
    cut_at = full.rfind("]")
    truncated_raw = full[:cut_at]  # 砍掉收尾的 ]

    def fake_call(model, messages, *a, **kw):
        return truncated_raw

    monkeypatch.setattr(ai, "_call_api", fake_call)
    sentences, prompt = ai.generate_podcast_sentences(CARDS, _sources("Zusammenfassung"))

    assert len(sentences) > 0
    assert not all("我学了" in s["sentence_zh"] for s in sentences)


# ── #743: 输出预算按词数计算 ─────────────────────────────────────────────────

def test_max_tokens_floor_for_small_batch():
    assert ai._podcast_max_tokens("deepseek-v4-flash", 1) == 4096


def test_max_tokens_caps_at_gpt_ceiling_for_large_batch():
    assert ai._podcast_max_tokens("gpt-5.6-luna", 154) == 16384


def test_max_tokens_caps_at_non_gpt_ceiling_for_large_batch():
    assert ai._podcast_max_tokens("deepseek-v4-flash", 154) == 8192

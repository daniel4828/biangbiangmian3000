"""
Code-based (AI-free) vocabulary annotation for podcast summaries (#638).

#631 asked the summarizing model to mark HSK5+ vocabulary with "pinyin/汉字".
Models forget: the German summary regularly ships bare Chinese like "(浙江)",
and the Chinese summary got no annotations at all. Everything needed to do it
deterministically is already in the repo, so no AI call is involved here:

  * `static/hsk_levels.json` — 4991 words with their HSK 1-6 level
  * `entries.word_zh`        — the words Daniel already studies (his collection)
  * `annotate/baseline_zh.txt` — HSK 3.0 1-4, the floor he arrived with (#922)
  * `jieba` / `pypinyin`     — segmentation and toned pinyin
  * `translator.py`          — Google Translate for the German gloss

A word is "new" when it is NOT in Daniel's collection AND is either HSK 5+ or
absent from the HSK list entirely (per #638: annotate generously, a redundant
annotation costs nothing, a missing one costs him a lookup).

Two entry points, deliberately different because the two texts need different
things:

  annotate_zh_summary()  Chinese summary -> inline "词（pīnyīn - Gloss）",
                         first occurrence only, skips person/place names.
  annotate_de_summary()  German summary -> prefixes bare Chinese runs with
                         pinyin ("(浙江)" -> "(Zhèjiāng/浙江)"). No gloss: the
                         German meaning is already right there in the sentence.
                         No name filtering either — a Chinese name inside German
                         prose is exactly what Daniel cannot pronounce.

Both are best-effort: any failure returns the original text unchanged, because
losing a whole episode over a missing pinyin table would be absurd.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_HSK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "hsk_levels.json")

# Words at or below this level count as known — Daniel reads at HSK 4-5.
KNOWN_HSK_MAX = 4

_CJK = r"一-鿿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_ALL_CJK = re.compile(f"^[{_CJK}]+$")

_hsk_cache: dict[str, int] | None = None
_char_cache: dict[str, int] | None = None
_translation_cache: dict[str, str] = {}


def _hsk_levels() -> dict[str, int]:
    """The HSK 1-6 word list, loaded once. An unreadable file degrades to an
    empty table, which makes every word look unknown — noisy, but it still
    annotates rather than silently doing nothing."""
    global _hsk_cache
    if _hsk_cache is None:
        try:
            with open(_HSK_PATH, encoding="utf-8") as f:
                _hsk_cache = {k: int(v) for k, v in json.load(f).items()}
        except Exception as e:
            logger.error("zh_annotate: cannot read %s — %s", _HSK_PATH, e)
            _hsk_cache = {}
    return _hsk_cache


def _char_levels() -> dict[str, int]:
    """Lowest HSK level each character appears at, derived from the word list
    itself. The list only carries 696 single-character entries, far too few to
    judge "is this character basic?", but a character used in any HSK 1-4 word
    ("变" in "变化") is basic by definition. Built once, alongside the word
    table."""
    global _char_cache
    if _char_cache is None:
        levels: dict[str, int] = {}
        for word, level in _hsk_levels().items():
            for ch in word:
                if level < levels.get(ch, 99):
                    levels[ch] = level
        _char_cache = levels
    return _char_cache


def _known_words(words: list[str]) -> set[str]:
    """Which of these words Daniel already knows: studied here
    (entries.word_zh), marked as known without ever studying it (#710,
    known_words), or part of the HSK 3.0 1-4 baseline he brought with him
    (#922, annotate/baseline_zh.txt). The two database lists are queried per
    call (both grow daily, and it is two indexed IN queries); the baseline is a
    static file cached for the process.

    This union is the ONE place that answers "does Daniel know this word".
    Inline annotations in both summaries and the HSK word table under an
    episode all come through here, which is why marking a word known makes it
    disappear from all of them at once."""
    if not words:
        return set()
    from annotate.baseline import baseline_words
    known = set(words) & baseline_words("zh")
    try:
        import database
        known |= database.word_zh_exists(words) | database.known_words_exists(words)
    except Exception as e:
        logger.warning("zh_annotate: collection lookup failed — %s", e)
    return known


def _is_new_word(word: str, hsk: dict[str, int], known: set[str],
                 skip_transparent: bool = False) -> bool:
    """Is this a word Daniel would have to look up? Not in his collection, and
    either HSK 5+ or missing from the list entirely.

    `skip_transparent` (Chinese summary only) drops the main source of noise in
    the "missing from the list" bucket: the list holds 4991 dictionary words,
    so ordinary compounds built from basic characters ("十年", "巨大变化",
    "死掉") land there too. If every character of an unlisted word is itself
    HSK 1-4, Daniel can read it — no annotation. The German summary does not
    use this: a bare Chinese run in German prose needs its pinyin regardless of
    how common its characters are."""
    if word in known:
        return False
    level = hsk.get(word)
    if level is not None:
        return level > KNOWN_HSK_MAX
    if skip_transparent:
        chars = _char_levels()
        return not all(chars.get(ch, 99) <= KNOWN_HSK_MAX for ch in word)
    return True


def pinyin_of(text: str) -> str:
    """Toned pinyin for one word, syllables joined without spaces
    ("就业" -> "jiùyè"). Returns "" if pypinyin is unavailable."""
    try:
        from pypinyin import pinyin as _pinyin, Style
        return "".join(s[0] for s in _pinyin(text, style=Style.TONE))
    except Exception as e:
        logger.warning("zh_annotate: pinyin failed for %r — %s", text, e)
        return ""


def _gloss_de(word: str) -> str:
    """German gloss via Google Translate, memoized for the process. Returns ""
    when translation is unavailable or hands back the input unchanged (which is
    what translator.translate_zh does on failure) — the caller then annotates
    with pinyin alone instead of printing "就业（jiùyè - 就业）"."""
    if word in _translation_cache:
        return _translation_cache[word]
    gloss = ""
    try:
        import translator
        result = (translator.translate_zh(word, target="de") or "").strip()
        if result and result != word:
            gloss = result
    except Exception as e:
        logger.warning("zh_annotate: translation failed for %r — %s", word, e)
    _translation_cache[word] = gloss
    return gloss


def _segment(text: str) -> list[tuple[str, str]]:
    """(word, pos) pairs from jieba. An import failure yields no pairs, so the
    caller returns the text untouched."""
    try:
        import jieba.posseg as pseg
        return [(w.word, w.flag) for w in pseg.cut(text)]
    except Exception as e:
        logger.warning("zh_annotate: segmentation failed — %s", e)
        return []


def find_new_words(text: str) -> list[str]:
    """The annotatable words of a Chinese text, in order of first appearance:
    multi-character, all-CJK, not in the collection, HSK 5+ or unlisted.

    Proper names are NOT filtered out (#961): a place or person he cannot
    pronounce blocks the sentence exactly like any other unknown word, and the
    German summary has always annotated them for that reason."""
    return _new_words_from_pairs(_segment(text))


def _new_words_from_pairs(pairs: list[tuple[str, str]]) -> list[str]:
    if not pairs:
        return []
    candidates: list[str] = []
    for word, _pos in pairs:
        if len(word) >= 2 and _ALL_CJK.match(word) and word not in candidates:
            candidates.append(word)
    hsk = _hsk_levels()
    known = _known_words(candidates)
    return [w for w in candidates
            if _is_new_word(w, hsk, known, skip_transparent=True)]


def extract_new_words(text: str) -> list[dict]:
    """Scan `text` for every new word (same criteria as annotate_zh_summary,
    reusing _segment/_new_words_from_pairs) and return
    [{word, pinyin, definition_de, hsk}] in order of first appearance.
    `hsk` is None for words absent from the HSK 1-6 table.

    `text` may be plain Chinese or HTML with embedded Chinese runs (e.g. a
    German summary) — jieba tokenizes non-CJK characters (tags, punctuation,
    German words) into their own tokens, and only whole-token, multi-char,
    all-CJK candidates are considered (see _new_words_from_pairs), so no
    pre-stripping of HTML is needed.

    Best-effort like the rest of this module: any failure returns []."""
    if not text or not text.strip():
        return []
    try:
        pairs = _segment(text)
        if not pairs:
            return []
        words = _new_words_from_pairs(pairs)
        hsk = _hsk_levels()
        return [
            {
                "word": w,
                "pinyin": pinyin_of(w),
                "definition_de": _gloss_de(w),
                "hsk": hsk.get(w),
            }
            for w in words
        ]
    except Exception as e:
        logger.warning("zh_annotate: extract_new_words failed — %s", e)
        return []


def annotate_zh_summary(text: str) -> str:
    """Annotate the first occurrence of every new word inline:
    "对就业的影响" -> "对就业（jiùyè - Beschäftigung）的影响".

    The text is rebuilt from the segmentation instead of being string-replaced:
    jieba's tokens concatenate back to the exact input, and a token boundary is
    the only place an annotation may go. Replacing by string would let a short
    word ("模型") match inside a longer one already annotated ("大语言模型（…）")
    and mangle it."""
    if not text or not text.strip():
        return text
    try:
        pairs = _segment(text)
        if not pairs:
            return text
        new_words = set(_new_words_from_pairs(pairs))
        if not new_words:
            return text
        out, done = [], set()
        for word, _pos in pairs:
            out.append(word)
            if word not in new_words or word in done:
                continue
            done.add(word)
            py = pinyin_of(word)
            gloss = _gloss_de(word)
            if not py and not gloss:
                continue
            note = f"{py} - {gloss}" if py and gloss else (py or gloss)
            out.append(f"（{note}）")
        return "".join(out)
    except Exception as e:
        logger.warning("zh_annotate: Chinese annotation failed — %s", e)
        return text


def annotate_de_summary(html: str) -> str:
    """Prefix pinyin to Chinese runs in the German summary that the model left
    bare: "Provinz Zhejiang (浙江)" -> "Provinz Zhejiang (Zhèjiāng/浙江)".

    A run already preceded by "/" is one the model annotated itself
    ("(jīngjì shuāituì/经济衰退)") and is left alone. Runs made up entirely of
    HSK1-4 / collection words ("(中国)") need no help either. HTML tags are
    never touched — they contain no CJK."""
    if not html or not html.strip():
        return html
    try:
        runs = _CJK_RUN.findall(html)
        if not runs:
            return html
        hsk = _hsk_levels()
        # Judge a run word by word: a run is worth annotating if any of its
        # words is new. Names are NOT skipped here — see the module docstring.
        run_words = {run: [w for w, _pos in _segment(run)] or [run] for run in runs}
        known = _known_words(sorted({w for ws in run_words.values() for w in ws}))

        def _replace(m: re.Match) -> str:
            run = m.group(0)
            if m.start() > 0 and html[m.start() - 1] == "/":
                return run  # already annotated by the model
            if not any(_is_new_word(w, hsk, known) for w in run_words.get(run, [run])):
                return run
            py = pinyin_of(run)
            return f"{py}/{run}" if py else run

        return _CJK_RUN.sub(_replace, html)
    except Exception as e:
        logger.warning("zh_annotate: German annotation failed — %s", e)
        return html


# ---------------------------------------------------------------------------
# Script detection (#904)
# ---------------------------------------------------------------------------

_CJK_CHAR_RE = re.compile(r"[一-鿿]")

# A German summary legitimately carries a few Chinese asides — the annotations
# this very module inserts ("(bólínqiáng/柏林墙)") plus whatever names the model
# spells out in hanzi. Measured over the production knowledge base those sit at
# or below 0.023 CJK, while summaries the model mistakenly wrote *in Chinese*
# start at 0.225. 0.10 lands in the empty middle of that gap.
NON_CHINESE_TEXT_MAX_CJK = 0.10


def cjk_ratio(text: str) -> float:
    """Fraction of `text`'s non-whitespace characters that are CJK (0.0 for
    empty text). The shared primitive behind every "which script is this
    written in?" check in the app — podcast.py picks a translation direction
    with it, ai.py and knowledge/rendition.py use it to catch a summary_de
    that isn't German (#904)."""
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return 0.0
    return len(_CJK_CHAR_RE.findall(stripped)) / len(stripped)


def is_chinese_text(text: str, threshold: float = 0.2) -> bool:
    """True when at least `threshold` of `text` is CJK. See cjk_ratio()."""
    return cjk_ratio(text) >= threshold

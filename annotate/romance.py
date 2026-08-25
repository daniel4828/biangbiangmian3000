"""Romance-language (French/Spanish) knowledge-base annotation (#804) — the
counterpart of zh_annotate.py for languages.py's "romance" family, dispatched
to by annotate/__init__.py.

No stemming happens here — that is the entire point of #803's entry_forms
table. Every conjugated/inflected surface form Daniel has already studied is
stored verbatim (database.forms_lookup), so a plain exact-string lookup
already knows "parlons" belongs to a word he knows without any linguistic
reduction here. The same table-driven logic extends to the CEFR A1-A2 baseline
(#922, annotate/baseline_fr.txt): it too is stored fully inflected, because a
lemma-only list would leave "mangeons" flagged as new.

Best-effort throughout, like zh_annotate: any failure returns the original
text unannotated and an empty word list. A missing gloss costs Daniel a
lookup; losing the whole summary over it would be absurd.
"""
import logging
import os
import re

import database
import languages

from . import baseline

logger = logging.getLogger(__name__)

# Inline-annotate at most this many distinct new words per summary — past
# that the parentheses would drown the text. Every new word still lands in
# new_words (the bottom vocabulary table), just not inline.
_MAX_INLINE = 40

# French elision prefixes ("l'économie" -> "économie"). Spanish doesn't elide
# this way, so this list is only ever consulted for lang == "fr"; it's a
# no-op (never matches) for "es".
_ELISION_PREFIXES = ("l", "d", "qu", "j", "n", "s", "c", "m", "t")

# A "word" is a run of Unicode letters, optionally with one internal
# apostrophe ("l'économie", "aujourd'hui", "qu'est-ce" splits on the hyphen
# into two words, which is fine — both get looked up independently).
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)

_SENTENCE_END = (".", "!", "?", "…")

# summary_de (and therefore every rendition translated from it) is HTML:
# <p> paragraphs with a <b> lead sentence. Tag names are runs of letters too,
# so without this the annotator would happily gloss "strong" inside a
# <strong> tag and destroy the markup. Every match inside a tag is skipped.
_TAG_RE = re.compile(r"<[^>]*>")

_stopword_cache: dict[str, set[str]] = {}


def stopwords(lang: str) -> set[str]:
    """Function-word list for `lang`, loaded once per process. An unreadable
    file degrades to an empty set — same "worse annotation, not a crash"
    posture as zh_annotate's HSK table.

    Public since #912: lang_detect.py reuses these lists as the "this text
    really is French/Spanish" evidence, so the two features can never drift
    apart over what counts as a function word."""
    if lang in _stopword_cache:
        return _stopword_cache[lang]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"stopwords_{lang}.txt")
    words: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.add(line.lower())
    except Exception as e:
        logger.warning("annotate.romance: cannot read %s — %s", path, e)
    _stopword_cache[lang] = words
    return words


def _strip_elision(token: str) -> str:
    """"l'économie" -> "économie". Only strips a prefix that is both
    apostrophe-joined AND a known elision word — an apostrophe inside an
    ordinary word ("aujourd'hui") is left whole, since "aujourd" isn't in
    the prefix list."""
    for sep in ("'", "’"):
        if sep in token:
            prefix, _, rest = token.partition(sep)
            if rest and prefix.lower() in _ELISION_PREFIXES:
                return rest
    return token


def _is_sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip()
    if not before:
        return True
    return before[-1] in _SENTENCE_END


def annotate_summary(text: str, lang: str) -> tuple[str, list[dict]]:
    """(annotated_text, new_words); new_words is
    [{word, lemma, definition_de}] in order of first appearance. Never
    raises."""
    if not text or not text.strip():
        return text, []
    try:
        return _annotate(text, lang)
    except Exception as e:
        logger.warning("annotate.romance: failed for lang=%s — %s", lang, e)
        return text, []


def _annotate(text: str, lang: str) -> tuple[str, list[dict]]:
    stop = stopwords(lang)
    tag_spans = [(m.start(), m.end()) for m in _TAG_RE.finditer(text)]

    def _in_tag(pos: int) -> bool:
        return any(start <= pos < end for start, end in tag_spans)

    matches = [m for m in _WORD_RE.finditer(text) if not _in_tag(m.start())]
    if not matches:
        return text, []

    # One pass to find every token's lookup key (lowercased, elision-
    # stripped) and whether it looks like a proper noun (capitalized, not at
    # a sentence start) — those are skipped entirely, never annotated and
    # never listed as a new word.
    tagged = []  # (match, lookup_key)
    order: list[str] = []
    seen: set[str] = set()
    for m in matches:
        raw = m.group(0)
        core = _strip_elision(raw)
        low = core.lower()
        is_proper = core[:1].isupper() and not _is_sentence_start(text, m.start())
        annotatable = len(low) >= 2 and low not in stop and not is_proper
        tagged.append((m, low if annotatable else None))
        if annotatable and low not in seen:
            seen.add(low)
            order.append(low)

    if not order:
        return text, []

    known = (database.forms_lookup(order, lang)
             | database.known_words_exists(order, lang)
             | (set(order) & baseline.baseline_words(lang)))
    new_cores = [w for w in order if w not in known]
    if not new_cores:
        return text, []

    glosses = _glosses(new_cores, lang)
    new_words = [
        {"word": w, "lemma": w, "definition_de": glosses.get(w) or None}
        for w in new_cores
    ]

    inline_set = set(new_cores[:_MAX_INLINE])
    out: list[str] = []
    last_end = 0
    annotated_once: set[str] = set()
    for m, key in tagged:
        out.append(text[last_end:m.start()])
        out.append(m.group(0))
        last_end = m.end()
        if key and key in inline_set and key not in annotated_once:
            annotated_once.add(key)
            gloss = glosses.get(key)
            if gloss:
                out.append(f" ({gloss})")
    out.append(text[last_end:])
    return "".join(out), new_words


def _glosses(words: list[str], lang: str) -> dict[str, str]:
    """German glosses for `words` via Google Translate, one batched request.
    A failed batch degrades to no glosses at all — the words are still
    flagged as new (no parenthetical, still listed in new_words), matching
    zh_annotate._gloss_de's "missing gloss, not a missing word" contract."""
    try:
        import translator
        source = languages.get_lang_config(lang)["translator_source"]
        translated = translator.translate_batch(words, target="de", source=source)
        out = {}
        for w, t in zip(words, translated):
            t = (t or "").strip()
            if t and t.lower() != w.lower():
                out[w] = t
        return out
    except Exception as e:
        logger.warning("annotate.romance: gloss translation failed — %s", e)
        return {}

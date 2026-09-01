"""Knowledge-base vocabulary annotation dispatch (#804).

Every language the app knows about has one entry in languages.py that names
an "annotator" implementation (a family-level field — see languages.py's
_SINITIC_BASE/_ROMANCE_BASE). annotate_summary() is the single call site
knowledge/rendition.py (and, for completeness, anything else that wants an
annotated summary) goes through, so a new language only ever needs a new
languages.py entry pointing at an existing annotator, never a new dispatch
site.

  "zh"      -> zh_annotate.py (#638): zero-AI, HSK-table + jieba + pypinyin.
               Untouched by #804 — wrapped here, not modified.
  "romance" -> annotate/romance.py (#804): entry_forms exact-match lookup
               (no stemming) + a per-language stopword list + Google
               Translate glosses for whatever's left.
"""
import logging

import languages

logger = logging.getLogger(__name__)


def annotate_summary(text: str, lang: str) -> tuple[str, list[dict]]:
    """(text, new_words) for `text` in `lang`.

    Since #1001 the text comes back UNCHANGED: nothing is written into it
    inline any more. Every new word is tappable in the reader (#967) and a
    modifier key / left swipe shows all the glosses under the words (#996),
    which is the same information without the parentheses breaking up the
    prose. What this function still does — and the only reason it exists — is
    decide WHICH words are new, per language. new_words is a list
    of dicts (shape varies slightly per annotator — see the individual
    implementations) in order of first appearance.

    Never raises: an annotator failure, or an unrecognized "annotator" name
    (should not happen — every registered language in languages.py names one
    of the two below), degrades to the text unannotated with no new words.
    That mirrors the "a missing gloss is a minor inconvenience" contract
    each individual annotator already promises for its own internal
    failures."""
    if not text or not text.strip():
        return text, []
    annotator = languages.get_lang_config(lang).get("annotator")
    try:
        if annotator == "zh":
            import zh_annotate
            return text, zh_annotate.extract_new_words(text)
        if annotator == "romance":
            from . import romance
            return romance.annotate_summary(text, lang)
        logger.warning("annotate: unknown annotator %r for lang=%s", annotator, lang)
    except Exception as e:
        logger.warning("annotate: dispatch failed for lang=%s — %s", lang, e)
    return text, []


def all_words(text: str, lang: str) -> list[dict]:
    """Every annotatable word of `text` in `lang`, not filtered to the new
    ones — counterpart to annotate_summary() for the "show every gloss"
    gesture (#996) extended past the new-word table (#1018). Same
    never-raises contract."""
    if not text or not text.strip():
        return []
    annotator = languages.get_lang_config(lang).get("annotator")
    try:
        if annotator == "zh":
            import zh_annotate
            return zh_annotate.extract_all_words(text)
        if annotator == "romance":
            from . import romance
            return romance.all_words(text, lang)
        logger.warning("annotate: unknown annotator %r for lang=%s", annotator, lang)
    except Exception as e:
        logger.warning("annotate: all_words dispatch failed for lang=%s — %s", lang, e)
    return []

"""Per-language reading renditions of a book chapter's AI summary (#894).

The chapter summarizer (routes/books.py's summarize endpoint) writes
title_zh/title_en/concept_zh/summary_zh/examples_zh exactly once, in
Chinese, no matter how many languages Daniel later reads this book in.
Every other language's chapter view is a *translated* (not translated-and-
annotated — see below) derivative of those four _zh fields, generated
lazily on first request and cached in book_chapter_renditions.

This is deliberately its own module, not a rendition "mode" bolted onto
knowledge/rendition.py: that module translates one blob of summary HTML and
annotates it with new-word markup, because an episode summary is itself
reading material. A chapter summary is a recall outline for a book Daniel
is already reading (annotated) page by page — translating it again would
just be noise, so this module only translates, never annotates (#894's
"明确不做" section).
"""
import logging

import database
import languages
import translator

logger = logging.getLogger(__name__)

# The chapter's four Chinese fields are short (a title, a one-sentence
# concept, a 300-500 char summary, a handful of one-line quotes) — nothing
# here needs knowledge/rendition.py's HTML-chunking machinery, which exists
# only because episode summaries are markup that can run past the free
# Google endpoint's ~5000 char request limit.
_SEP = "\n"


class ChapterRenditionError(Exception):
    """No rendition could be produced. Callers must report the reason and
    keep serving the Chinese fields — #894 is explicit that a half- or
    un-translated chapter must never be stored or served as if it were a
    real rendition in that language."""


def _translate_one(text: str | None, *, target: str, source: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        result = translator.translate_strict(text, target=target, source=source)
    except Exception as e:
        raise ChapterRenditionError(f"translation failed: {e}") from e
    result = (result or "").strip()
    if not result:
        raise ChapterRenditionError("translator returned an empty segment")
    return result


def _translate_lines(lines: list[str], *, target: str, source: str) -> list[str]:
    """Translate a list of short strings, preserving order and count.

    Sent as one newline-joined request (cheaper, and Google's line-splitting
    is usually stable for short single-sentence quotes); if the line count
    that comes back doesn't match what went out, each line is retried on its
    own — slower, but keeps every example aligned with its source quote.
    """
    if not lines:
        return []
    joined = _translate_one(_SEP.join(lines), target=target, source=source) or ""
    out = joined.split(_SEP)
    if len(out) != len(lines):
        logger.info(
            "books.rendition: line-count mismatch (%d vs %d), retrying line by line",
            len(out), len(lines))
        out = [_translate_one(line, target=target, source=source) for line in lines]
    return [(line or "").strip() for line in out]


def get_or_create_chapter_rendition(chapter: dict, lang: str, fields: str = "full") -> dict:
    """{"title", "concept", "summary", "examples"} for `chapter` in `lang`.

    `chapter` must have "id", "title_zh", "ref_label", "concept_zh", and —
    when fields="full" — "summary_zh"/"examples_zh" too (list_chapters()'s
    rows lack the last two on purpose; that's fine for fields="short").

    fields="short" only translates title/concept (what the chapter list
    shows); fields="full" also translates summary/examples (what the
    per-chapter popup shows). A "short" call never blanks an already-cached
    "full" row's summary/examples back to empty, and a later "full" call on
    a "short"-only cached row translates just the two missing fields rather
    than re-translating title/concept for no reason.

    Raises ChapterRenditionError on any failure; nothing is written to the
    database in that case.
    """
    if lang == languages.DEFAULT_LANG:
        raise ChapterRenditionError("zh has no rendition — read the _zh columns directly")
    if not languages.is_valid_lang(lang):
        raise ChapterRenditionError(f"unknown language: {lang!r}")

    chapter_id = chapter["id"]
    cached = database.get_chapter_rendition(chapter_id, lang)
    need_full = fields == "full"
    have_short = cached and cached.get("title") is not None and cached.get("concept") is not None
    have_full = have_short and cached.get("summary") is not None
    if have_short and (not need_full or have_full):
        return {
            "title": cached["title"], "concept": cached["concept"],
            "summary": cached.get("summary"), "examples": cached.get("examples") or [],
        }

    target = languages.get_lang_config(lang)["translator_source"]
    source = "zh-CN"

    title = cached["title"] if have_short else _translate_one(
        chapter.get("title_zh") or chapter.get("ref_label"), target=target, source=source)
    concept = cached["concept"] if have_short else _translate_one(
        chapter.get("concept_zh"), target=target, source=source)

    summary = cached.get("summary") if cached else None
    examples = cached.get("examples") if cached else None
    if need_full and summary is None:
        summary = _translate_one(chapter.get("summary_zh"), target=target, source=source)
    if need_full and examples is None:
        examples_zh = chapter.get("examples_zh") or []
        examples = _translate_lines(list(examples_zh), target=target, source=source)

    database.save_chapter_rendition(
        chapter_id, lang, title=title, concept=concept,
        summary=summary, examples=examples or [])
    logger.info("books.rendition: generated chapter %s lang=%s fields=%s",
               chapter_id, lang, fields)
    return {"title": title, "concept": concept, "summary": summary, "examples": examples or []}

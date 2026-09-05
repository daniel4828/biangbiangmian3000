"""Book reader (#836): uploaded EPUB/PDF → source-language pages in the
database, read later one page at a time in whichever language Daniel is
studying.

Extraction is per format (epub.py / pdf.py); everything downstream is shared.
`ingest_file()` below is the single entry point — routes/books.py does not
call the format modules directly, for the same reason knowledge/ingest.py is
the only way into the knowledge base: two parallel paths mean a bug fixed on
one of them comes back on the other (#643).

Translating and annotating a page is deliberately *not* done here. That is
knowledge/rendition.py's render_html(), the same pipeline the knowledge base
uses, so a book page and an episode summary are annotated by identical rules.
"""
import logging
import os
import re

import database
import zh_annotate

from . import epub, pdf
from .epub import BookExtractionError
from .paginate import DEFAULT_CHAR_BUDGET, paginate

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("epub", "pdf")

# Deliberately tiny: past the Chinese check below, the only remaining choice
# is German vs English, both of which Daniel's books are written in, and a
# full language-detection dependency for a binary decision the upload form
# can also just ask about is not worth it.
_DE_MARKERS = re.compile(
    r"\b(und|der|die|das|nicht|ist|ich|sie|mit|auch|eine|dass|sich|auf|für|"
    r"aber|noch|schon|werden|über)\b", re.IGNORECASE)
_EN_MARKERS = re.compile(
    r"\b(the|and|was|that|with|have|this|from|they|would|there|their|"
    r"which|about|been|were|said|what)\b", re.IGNORECASE)

# Same threshold knowledge/rendition.py's _source_lang_of() and podcast.py's
# _is_chinese_text() use for "is this Chinese?" — one constant repeated
# across the app rather than each caller picking its own cutoff.
_CJK_DETECT_THRESHOLD = 0.2


def detect_source_lang(blocks: list[dict], default: str = "de") -> str:
    """Guess 'zh', 'de' or 'en' from a sample of the text. Only ever a
    default for the upload form — the caller may override it, because
    getting this wrong means every page is translated from the wrong source
    (#1050: a Chinese book run through the German/English word-count
    heuristic below scores 0 on both and used to silently fall through to
    `default`, which would then send an already-Chinese page through
    de->zh translation — Google Translate mostly no-ops on that, so it would
    have looked like a successful translation instead of the bug it is).
    """
    sample = " ".join(b.get("text", "") for b in blocks[:80])[:5000]
    if not sample.strip():
        return default
    if zh_annotate.cjk_ratio(sample) >= _CJK_DETECT_THRESHOLD:
        return "zh"
    de, en = len(_DE_MARKERS.findall(sample)), len(_EN_MARKERS.findall(sample))
    if de == en:
        return default
    return "de" if de > en else "en"


def format_from_filename(filename: str) -> str:
    """'epub' or 'pdf' for an uploaded filename; raises for anything else."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext not in SUPPORTED_FORMATS:
        raise BookExtractionError(
            f"unsupported file type {ext or '(none)'!r} — only EPUB and PDF are supported")
    return ext


def ingest_file(path: str, filename: str, *, title: str | None = None,
                source_lang: str | None = None,
                char_budget: int = DEFAULT_CHAR_BUDGET) -> dict:
    """Extract, paginate and store one uploaded book.

    Returns {"book_id", "title", "page_count", "source_lang"}. Raises
    BookExtractionError (before writing anything) when the file yields no
    readable text — nothing half-built is left in the database.
    """
    fmt = format_from_filename(filename)
    extracted = (epub if fmt == "epub" else pdf).extract(path)
    blocks = extracted["blocks"]

    pages = paginate(blocks, char_budget)
    if not pages:
        raise BookExtractionError("no readable text found in this file")

    resolved_lang = source_lang or detect_source_lang(blocks)
    resolved_title = (title or extracted.get("title")
                      or os.path.splitext(os.path.basename(filename))[0])

    book_id = database.create_book(
        resolved_title, extracted.get("author"), resolved_lang, fmt, path, char_budget)
    database.add_pages(book_id, pages)
    logger.info("books: ingested %r (%s, %s) → book %s, %d page(s)",
                resolved_title, fmt, resolved_lang, book_id, len(pages))
    return {"book_id": book_id, "title": resolved_title,
            "page_count": len(pages), "source_lang": resolved_lang}

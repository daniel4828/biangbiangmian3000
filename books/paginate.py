"""Cutting a book into fixed-size reading pages (#836).

EPUB has no page numbers at all and a PDF's real pages are whatever the
typesetter chose, so "one page" here is a fixed character budget — the same
unit for both formats, stable across re-openings, and cheap to jump around
in. The book's own page/chapter markers survive as each page's `ref_label`,
shown next to the reader's page counter.

Two rules the rest of the feature depends on:

  * pages are only ever cut at paragraph boundaries (a page never starts
    mid-sentence), and
  * the output is HTML — <p> paragraphs — because that is what
    knowledge/rendition.py's translator expects: it splits on tags, sends
    only text nodes to Google Translate, and puts the markup back untouched.
"""
import html
import re

DEFAULT_CHAR_BUDGET = 1200

# A single paragraph past this length is split at sentence boundaries: some
# books (and most PDF text layers) produce multi-page "paragraphs", and one
# page carrying ten screens of text defeats the whole page model.
_HARD_SPLIT_FACTOR = 2
# Two alternatives, not one (#1050): Chinese sentences run straight from one
# 。！？／； into the next character with no space at all, so requiring \s+
# after the punctuation (as the Latin side does, to avoid a false split on
# something like "3.14" or "Dr. Müller") would never match a Chinese book —
# every over-long Chinese paragraph would fall straight through to the hard
# character cut below, landing mid-sentence exactly as often as an unsplit
# one would. \s* (zero or more) after the CJK punctuation lets it split with
# no separator present at all.
_SENTENCE_END = re.compile(r"(?<=[。！？；])\s*|(?<=[.!?;])\s+")


def _split_long_paragraph(text: str, budget: int) -> list[str]:
    """Break an over-long paragraph into budget-sized pieces at sentence
    ends, falling back to a hard character cut for text with no sentence
    punctuation at all (tables of contents, poetry, broken PDF layers)."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(text):
        if current and len(current) + len(sentence) > budget:
            pieces.append(current.strip())
            current = ""
        current += sentence + " "
    if current.strip():
        pieces.append(current.strip())
    out = []
    for piece in pieces:
        while len(piece) > budget * _HARD_SPLIT_FACTOR:
            out.append(piece[:budget])
            piece = piece[budget:]
        if piece:
            out.append(piece)
    return out


def paginate(blocks: list[dict], char_budget: int = DEFAULT_CHAR_BUDGET) -> list[dict]:
    """`blocks` (paragraph-level {"text", "ref_label"} dicts, in reading
    order) → pages: {"source_text": "<p>…</p>…", "ref_label": str|None}.

    A page's ref_label is that of its first paragraph, so the label always
    points at where the page *starts* — which is what a reader looking for
    "page 214 of the PDF" wants.
    """
    pages: list[dict] = []
    current: list[str] = []
    current_len = 0
    current_label: str | None = None

    def flush():
        nonlocal current, current_len, current_label
        if current:
            pages.append({
                "source_text": "".join(f"<p>{html.escape(p)}</p>" for p in current),
                "ref_label": current_label,
            })
        current, current_len, current_label = [], 0, None

    for block in blocks:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        parts = ([text] if len(text) <= char_budget * _HARD_SPLIT_FACTOR
                 else _split_long_paragraph(text, char_budget))
        for part in parts:
            if current and current_len + len(part) > char_budget:
                flush()
            if not current:
                current_label = block.get("ref_label")
            current.append(part)
            current_len += len(part)
    flush()
    return pages

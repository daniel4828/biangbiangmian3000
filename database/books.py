"""Book reader storage (#836). All SQL for uploaded books lives here —
`books/` (extraction) and `routes/books.py` (API) only call into this module.

See schema.sql's books / book_pages / book_renditions / book_progress block
for what each table is for. The one invariant worth repeating: a book is
paginated exactly once, at upload. `page_no` is 1-based and contiguous, and
both the cached renditions and Daniel's reading position are keyed by it, so
nothing here offers a way to re-cut an existing book.
"""
import json

from .core import get_db


def create_book(title: str, author: str | None, source_lang: str, fmt: str,
                file_path: str | None, char_budget: int) -> int:
    """Insert the book row. page_count stays 0 until add_pages() runs."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO books (title, author, source_lang, format, file_path, char_budget)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (title, author, source_lang, fmt, file_path, char_budget),
    )
    conn.commit()
    book_id = cur.lastrowid
    conn.close()
    return book_id


def add_pages(book_id: int, pages: list[dict]) -> int:
    """Store the paginated source text and set page_count, in one transaction.

    `pages` is the output of books.paginate.paginate(): dicts with
    "source_text" (HTML) and optional "ref_label", already in reading order.
    Numbering is assigned here so it can never disagree with page_count.
    """
    conn = get_db()
    conn.executemany(
        "INSERT INTO book_pages (book_id, page_no, source_text, ref_label) VALUES (?, ?, ?, ?)",
        [(book_id, i, p["source_text"], p.get("ref_label"))
         for i, p in enumerate(pages, start=1)],
    )
    conn.execute("UPDATE books SET page_count = ? WHERE id = ?", (len(pages), book_id))
    conn.commit()
    conn.close()
    return len(pages)


def get_book(book_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_books() -> list[dict]:
    """Every book, newest first, each with its per-language reading progress
    as {lang: last_page} — the list screen shows "continue reading" links and
    should not need one request per book to do it."""
    conn = get_db()
    books = [dict(r) for r in conn.execute(
        "SELECT * FROM books ORDER BY created_at DESC, id DESC").fetchall()]
    progress: dict[int, dict] = {}
    for r in conn.execute("SELECT book_id, lang, last_page FROM book_progress").fetchall():
        progress.setdefault(r["book_id"], {})[r["lang"]] = r["last_page"]
    conn.close()
    for book in books:
        book["progress"] = progress.get(book["id"], {})
    return books


def delete_book(book_id: int) -> bool:
    """Delete the book and (via ON DELETE CASCADE) its pages, renditions and
    progress. Returns whether the row existed — the caller reports a 404
    rather than pretending a missing book was deleted."""
    conn = get_db()
    cur = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_page(book_id: int, page_no: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM book_pages WHERE book_id = ? AND page_no = ?",
        (book_id, page_no),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_page_by_id(page_id: int) -> dict | None:
    """One page by its own row id, not (book_id, page_no) — routes/audio.py's
    book_page track builder (#1050) is keyed on book_pages.id because
    audio_tracks is unique per owner_id and page_no repeats across every
    book (page 1 of book A and page 1 of book B must not collide)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM book_pages WHERE id = ?", (page_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_pages(book_id: int) -> list[dict]:
    """Every page in order, source_text included — used only by the chapter
    rescan flow (#881) to byte-compare a fresh re-parse against what's
    already stored before touching anything."""
    conn = get_db()
    rows = conn.execute(
        "SELECT page_no, source_text, ref_label FROM book_pages "
        "WHERE book_id = ? ORDER BY page_no",
        (book_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_page_ref_labels(book_id: int, ref_labels: list[str | None]) -> None:
    """Overwrite ref_label page by page, positionally 1-based against
    `ref_labels` (#881's chapter rescan). Never touches source_text or
    page_no/page_count — re-cutting an existing book is exactly what #836
    forbids, because it would shift every cached rendition and reading
    position by an unknown amount. The caller (routes/books.py) has already
    verified `ref_labels` has exactly as many entries as this book has pages."""
    conn = get_db()
    conn.executemany(
        "UPDATE book_pages SET ref_label = ? WHERE book_id = ? AND page_no = ?",
        [(label, book_id, i) for i, label in enumerate(ref_labels, start=1)],
    )
    conn.commit()
    conn.close()


def update_book(book_id: int, *, title: str | None = None, author: str | None = None,
                source_lang: str | None = None) -> bool:
    """Patch editable metadata (#882). Deliberately narrow: format/page_count/
    char_budget describe the actual pagination and are never touched here —
    changing them would make the row disagree with book_pages. A field left
    as None (not passed by the caller) is left alone; routes/books.py is
    responsible for validating non-empty title / allowed source_lang before
    calling this. Returns whether the book existed."""
    conn = get_db()
    fields, params = [], []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if author is not None:
        fields.append("author = ?")
        params.append(author)
    if source_lang is not None:
        fields.append("source_lang = ?")
        params.append(source_lang)
    if not fields:
        conn.close()
        return get_book(book_id) is not None
    params.append(book_id)
    cur = conn.execute(f"UPDATE books SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def get_book_rendition(book_id: int, page_no: int, lang: str) -> dict | None:
    """Cached translation+annotation of one page, or None. `new_words` comes
    back already JSON-decoded (a malformed blob degrades to an empty list —
    the page text is the point, the word table is the extra)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM book_renditions WHERE book_id = ? AND page_no = ? AND lang = ?",
        (book_id, page_no, lang),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    try:
        out["new_words"] = json.loads(out["new_words"] or "[]")
    except (ValueError, TypeError):
        out["new_words"] = []
    return out


def save_book_rendition(book_id: int, page_no: int, lang: str, text: str,
                        new_words: list) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO book_renditions (book_id, page_no, lang, text, new_words)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(book_id, page_no, lang) DO UPDATE SET
               text = excluded.text,
               new_words = excluded.new_words,
               created_at = datetime('now','localtime')""",
        (book_id, page_no, lang, text, json.dumps(new_words or [], ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_book_progress(book_id: int, lang: str) -> int | None:
    conn = get_db()
    row = conn.execute(
        "SELECT last_page FROM book_progress WHERE book_id = ? AND lang = ?",
        (book_id, lang),
    ).fetchone()
    conn.close()
    return row["last_page"] if row else None


def set_book_progress(book_id: int, lang: str, page_no: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO book_progress (book_id, lang, last_page) VALUES (?, ?, ?)
           ON CONFLICT(book_id, lang) DO UPDATE SET
               last_page = excluded.last_page,
               updated_at = datetime('now','localtime')""",
        (book_id, lang, page_no),
    )
    conn.commit()
    conn.close()


# ── Chapters (#864) ─────────────────────────────────────────────────────────
# A book's table of contents is derived from book_pages.ref_label the first
# time it's asked for, then cached in book_chapters — never re-derived, so a
# chapter's start/end pages stay stable even if summaries are added later.

def derive_chapters(book_id: int) -> list[dict]:
    """Group this book's pages into chapters by consecutive ref_label and
    store the grouping. Idempotent: if chapters already exist for this book,
    they're returned as-is without touching book_pages again.

    Returns [] (no rows inserted) when there's nothing to group into more
    than one chapter — all-NULL ref_label (a PDF, or an EPUB with no
    headings) or a single label spanning the whole book. Callers must not
    treat an empty list as an error; it means "this book has no chapter
    structure to show", which the route turns into available=False.
    """
    existing = list_chapters(book_id)
    if existing:
        return existing

    conn = get_db()
    pages = conn.execute(
        "SELECT page_no, ref_label FROM book_pages WHERE book_id = ? ORDER BY page_no",
        (book_id,),
    ).fetchall()

    groups: list[dict] = []
    for row in pages:
        label = row["ref_label"]
        if groups and groups[-1]["ref_label"] == label:
            groups[-1]["end_page"] = row["page_no"]
        else:
            groups.append({"ref_label": label, "start_page": row["page_no"],
                           "end_page": row["page_no"]})

    if len(groups) <= 1:
        # Nothing to distinguish chapters by — a fabricated single chapter
        # spanning the whole book would just be a worse page list.
        conn.close()
        return []

    conn.executemany(
        """INSERT INTO book_chapters (book_id, number, ref_label, start_page, end_page)
           VALUES (?, ?, ?, ?, ?)""",
        [(book_id, i, g["ref_label"], g["start_page"], g["end_page"])
         for i, g in enumerate(groups, start=1)],
    )
    conn.commit()
    conn.close()
    return list_chapters(book_id)


def rescan_chapter_labels(book_id: int) -> dict:
    """Regroup this book's chapters from freshly-updated ref_labels (#881),
    called right after update_page_ref_labels() during a rescan. Unlike
    derive_chapters() this is never idempotent-short-circuited by existing
    rows, because the whole point is to redo the grouping — but any chapter
    that already has status='summarized' cost real AI money and is never
    discarded or overwritten by this, only left in place.

    Numbering matches derive_chapters(): sequential position among the
    freshly-grouped chapters. A number already held by a summarized chapter
    keeps that chapter's stored ref_label/start_page/end_page untouched; the
    fresh group at that position is simply not inserted. Every other
    position gets a fresh, freshly-derived row.

    Returns {"chapters": [...], "stale_summarized_count": n} where n counts
    summarized chapters whose stored start/end page no longer matches the
    freshly-derived boundaries at their number — the caller surfaces that so
    Daniel knows some of the table of contents may now be misaligned with an
    old summary.
    """
    conn = get_db()
    summarized = conn.execute(
        "SELECT number, start_page, end_page FROM book_chapters "
        "WHERE book_id = ? AND status = 'summarized'",
        (book_id,),
    ).fetchall()
    kept = {r["number"]: (r["start_page"], r["end_page"]) for r in summarized}

    conn.execute(
        "DELETE FROM book_chapters WHERE book_id = ? AND status != 'summarized'",
        (book_id,),
    )
    conn.commit()

    pages = conn.execute(
        "SELECT page_no, ref_label FROM book_pages WHERE book_id = ? ORDER BY page_no",
        (book_id,),
    ).fetchall()
    groups: list[dict] = []
    for row in pages:
        label = row["ref_label"]
        if groups and groups[-1]["ref_label"] == label:
            groups[-1]["end_page"] = row["page_no"]
        else:
            groups.append({"ref_label": label, "start_page": row["page_no"],
                           "end_page": row["page_no"]})

    stale_summarized_count = 0
    inserts = []
    if len(groups) > 1:  # same "not worth it" threshold as derive_chapters
        for number, g in enumerate(groups, start=1):
            if number in kept:
                if kept[number] != (g["start_page"], g["end_page"]):
                    stale_summarized_count += 1
                continue
            inserts.append((book_id, number, g["ref_label"], g["start_page"], g["end_page"]))

    if inserts:
        conn.executemany(
            """INSERT INTO book_chapters (book_id, number, ref_label, start_page, end_page)
               VALUES (?, ?, ?, ?, ?)""",
            inserts,
        )
        conn.commit()
    conn.close()
    return {"chapters": list_chapters(book_id), "stale_summarized_count": stale_summarized_count}


_CHAPTER_LIST_COLUMNS = (
    "id, number, ref_label, title_zh, title_en, concept_zh, "
    "start_page, end_page, status, error"
)


def list_chapters(book_id: int) -> list[dict]:
    """Chapter list without the large text fields (summary_zh/examples_zh) —
    the table-of-contents view doesn't need them, and a book with many
    chapters shouldn't pull every summary just to render the list."""
    conn = get_db()
    rows = conn.execute(
        f"SELECT {_CHAPTER_LIST_COLUMNS} FROM book_chapters "
        "WHERE book_id = ? ORDER BY number",
        (book_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_chapter(book_id: int, number: int) -> dict | None:
    """One full chapter (including summary_zh/examples_zh), or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM book_chapters WHERE book_id = ? AND number = ?",
        (book_id, number),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    try:
        out["examples_zh"] = json.loads(out["examples_zh"] or "[]")
    except (ValueError, TypeError):
        out["examples_zh"] = []
    return out


def get_chapter_by_id(chapter_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM book_chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    try:
        out["examples_zh"] = json.loads(out["examples_zh"] or "[]")
    except (ValueError, TypeError):
        out["examples_zh"] = []
    return out


def save_chapter_summary(book_id: int, number: int, *, title_zh: str, title_en: str,
                         concept_zh: str, summary_zh: str, examples_zh: list) -> None:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM book_chapters WHERE book_id = ? AND number = ?",
        (book_id, number),
    ).fetchone()
    conn.execute(
        """UPDATE book_chapters SET
               title_zh = ?, title_en = ?, concept_zh = ?, summary_zh = ?,
               examples_zh = ?, status = 'summarized', error = NULL,
               summarized_at = datetime('now','localtime')
           WHERE book_id = ? AND number = ?""",
        (title_zh, title_en, concept_zh, summary_zh,
         json.dumps(examples_zh or [], ensure_ascii=False), book_id, number),
    )
    conn.commit()
    conn.close()
    # A fresh Chinese summary makes any cached translation of the old one
    # stale (#894, same rule #804 set for knowledge_renditions) — leaving it
    # would show Daniel a French/Spanish chapter that disagrees with the
    # Chinese he just regenerated.
    if row:
        delete_chapter_renditions(row["id"])


# ── Per-language chapter renditions (#894) ──────────────────────────────────
# Same idea/shape as knowledge_renditions (#804): the AI writes the _zh
# columns on book_chapters exactly once, every other language's chapter view
# is a cached translation of those columns. See books/rendition.py for the
# translate-and-cache orchestration (including the "short vs. full fields"
# merge that this module deliberately does NOT do — see save below).

def get_chapter_rendition(chapter_id: int, lang: str) -> dict | None:
    """The cached rendition for (chapter_id, lang), or None. `examples` comes
    back JSON-decoded (a malformed blob degrades to an empty list, same
    contract as get_chapter()'s examples_zh)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM book_chapter_renditions WHERE chapter_id = ? AND lang = ?",
        (chapter_id, lang),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    try:
        out["examples"] = json.loads(out["examples"]) if out["examples"] else []
    except (ValueError, TypeError):
        out["examples"] = []
    return out


def save_chapter_rendition(chapter_id: int, lang: str, *, title: str | None,
                           concept: str | None, summary: str | None,
                           examples: list | None) -> None:
    """Store (or overwrite) the rendition for (chapter_id, lang) with exactly
    the fields given — a plain overwrite. The list view only ever translates
    title/concept (cheap, one call per chapter shown); the summary popup
    later also translates summary/examples. Merging a "short" write with an
    already-cached "full" one (so the short write doesn't blank summary/
    examples back to NULL) is books/rendition.py's job, not this function's —
    it reads the existing row first and passes this function the complete,
    already-merged record."""
    conn = get_db()
    conn.execute(
        """INSERT INTO book_chapter_renditions (chapter_id, lang, title, concept, summary, examples)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(chapter_id, lang) DO UPDATE SET
               title = excluded.title,
               concept = excluded.concept,
               summary = excluded.summary,
               examples = excluded.examples,
               created_at = datetime('now','localtime')""",
        (chapter_id, lang, title, concept, summary,
         json.dumps(examples or [], ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def delete_chapter_renditions(chapter_id: int) -> None:
    """Clear every cached language rendition for one chapter."""
    conn = get_db()
    conn.execute("DELETE FROM book_chapter_renditions WHERE chapter_id = ?", (chapter_id,))
    conn.commit()
    conn.close()


def set_chapter_error(book_id: int, number: int, error: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE book_chapters SET status = 'error', error = ? WHERE book_id = ? AND number = ?",
        (error, book_id, number),
    )
    conn.commit()
    conn.close()


def chapter_source_text(book_id: int, number: int) -> str | None:
    """The chapter's pages' source_text, concatenated in page order. None if
    the chapter (or its pages) don't exist."""
    conn = get_db()
    chapter = conn.execute(
        "SELECT start_page, end_page FROM book_chapters WHERE book_id = ? AND number = ?",
        (book_id, number),
    ).fetchone()
    if not chapter:
        conn.close()
        return None
    rows = conn.execute(
        """SELECT source_text FROM book_pages
           WHERE book_id = ? AND page_no BETWEEN ? AND ? ORDER BY page_no""",
        (book_id, chapter["start_page"], chapter["end_page"]),
    ).fetchall()
    conn.close()
    return "\n".join(r["source_text"] for r in rows)

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


_CHAPTER_LIST_COLUMNS = (
    "number, ref_label, title_zh, title_en, concept_zh, "
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

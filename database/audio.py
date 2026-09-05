"""Time-aligned audio tracks (#1047 umbrella, this module added by #1048).

One table, `audio_tracks`, holds every (owner, lang, variant) → mp3 + cue
list, no matter which of the four alignment paths produced it (see
audio/__init__.py's module docstring). `audio/` and `routes/audio.py` are the
only callers — everything else that needs SQL for this feature belongs here,
same rule as every other table in this package.
"""
import json
import os

from .core import get_db


def get_audio_track(owner_kind: str, owner_id: int, lang: str,
                    variant: str = "fulltext") -> dict | None:
    """The cached track for (owner_kind, owner_id, lang, variant), or None.
    `cues` comes back JSON-decoded — a malformed blob is a bug worth seeing,
    not something to paper over, so this lets json.JSONDecodeError propagate
    rather than silently returning an empty cue list for the wrong reason."""
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM audio_tracks
           WHERE owner_kind = ? AND owner_id = ? AND lang = ? AND variant = ?""",
        (owner_kind, owner_id, lang, variant),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    out["cues"] = json.loads(out["cues_json"])
    return out


def get_audio_track_by_id(track_id: int) -> dict | None:
    """Looked up by primary key — routes/audio.py's file-serving endpoint
    only has the track id (from the earlier /api/audio/track response), not
    the (owner, lang, variant) tuple."""
    conn = get_db()
    row = conn.execute("SELECT * FROM audio_tracks WHERE id = ?", (track_id,)).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    out["cues"] = json.loads(out["cues_json"])
    return out


def save_audio_track(owner_kind: str, owner_id: int, lang: str, variant: str,
                     audio_path: str, duration_ms: int | None, cues: list,
                     source: str, voice: str | None,
                     source_text: str | None = None) -> int:
    """Store (or overwrite) the track for (owner_kind, owner_id, lang,
    variant). Regenerating is a plain replace — there is never a reason to
    keep a stale audio file whose cues have just been superseded (same
    contract as book_renditions/knowledge_renditions: the cache is a cache,
    not a history).

    `source_text` (#1049) is the plain text handed to the aligner — the
    frontend's alignment anchor for mapping cue char offsets onto the
    rendered HTML. Optional/defaulted to None only so existing callers
    (and the pre-#1049 test fixtures) don't have to change; a NULL here
    just means the frontend falls back to audio-only playback."""
    conn = get_db()
    conn.execute(
        """INSERT INTO audio_tracks
               (owner_kind, owner_id, lang, variant, audio_path, duration_ms,
                cues_json, source, voice, source_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(owner_kind, owner_id, lang, variant) DO UPDATE SET
               audio_path  = excluded.audio_path,
               duration_ms = excluded.duration_ms,
               cues_json   = excluded.cues_json,
               source      = excluded.source,
               voice       = excluded.voice,
               source_text = excluded.source_text,
               created_at  = datetime('now','localtime')""",
        (owner_kind, owner_id, lang, variant, audio_path, duration_ms,
         json.dumps(cues, ensure_ascii=False), source, voice, source_text),
    )
    conn.commit()
    # cursor.lastrowid is not reliably the row's id when the ON CONFLICT
    # branch fired (it's only guaranteed after a genuine INSERT) — look the
    # row up by its natural key instead of guessing.
    track_id = conn.execute(
        """SELECT id FROM audio_tracks
           WHERE owner_kind = ? AND owner_id = ? AND lang = ? AND variant = ?""",
        (owner_kind, owner_id, lang, variant),
    ).fetchone()["id"]
    conn.close()
    return track_id


def _unreferenced_paths(conn, rows) -> list[str]:
    """Of the audio_path values in `rows` (each an sqlite3.Row with "id" and
    "audio_path", about to be deleted), return only the ones no OTHER
    audio_tracks row still points at.

    audio_path is content-addressed (sha256(voice|text) —
    audio/tts_track.py._cache_path), so the exact same mp3 can legitimately
    be shared by two different owners (a book page and an episode that
    happen to render to identical text+voice). Callers must never unlink a
    file some other row is still using — deciding that HERE, in the same
    transaction that deletes the rows, is deliberate: a separate
    audio_path_in_use()-style check is one a future caller could forget to
    call, and by the time that bug shows up the file is already gone.
    """
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    out = []
    for path in {r["audio_path"] for r in rows}:
        placeholders = ",".join("?" * len(ids))
        still_used = conn.execute(
            f"SELECT 1 FROM audio_tracks WHERE audio_path = ? AND id NOT IN ({placeholders}) LIMIT 1",
            [path, *ids],
        ).fetchone()
        if not still_used:
            out.append(path)
    return out


def delete_audio_tracks(owner_kind: str, owner_id: int) -> list[str]:
    """Delete every track belonging to (owner_kind, owner_id) — called when
    the owner itself is deleted. Returns the audio_path of every mp3 that is
    now actually safe to unlink from disk (see _unreferenced_paths — a path
    still used by some OTHER row is excluded); this module never touches the
    filesystem itself (same division of labor as database.delete_book(),
    which leaves the uploaded file to routes/books.py)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, audio_path FROM audio_tracks WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    ).fetchall()
    safe_to_delete = _unreferenced_paths(conn, rows)
    conn.execute(
        "DELETE FROM audio_tracks WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    )
    conn.commit()
    conn.close()
    return safe_to_delete


# ---------------------------------------------------------------------------
# Local ASR job queue (#1053) — see schema.sql's audio_jobs comment for why
# this is a queue of WORK rather than a cache keyed like audio_tracks.
# ---------------------------------------------------------------------------

def enqueue_audio_job(owner_kind: str, owner_id: int, lang: str, audio_path: str,
                      variant: str = "fulltext", text_hint: str | None = None) -> int:
    """Queue a local-ASR transcription for scripts/audio_worker.py to pick up
    later. Returns the new job's id."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO audio_jobs (owner_kind, owner_id, lang, variant, audio_path, text_hint)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (owner_kind, owner_id, lang, variant, audio_path, text_hint),
    )
    conn.commit()
    job_id = cur.lastrowid
    conn.close()
    return job_id


def claim_next_audio_job() -> dict | None:
    """Atomically take the oldest 'pending' job and mark it 'running', or
    return None if there is none. The SELECT-then-UPDATE happens inside a
    single transaction (BEGIN IMMEDIATE takes the write lock up front) so two
    workers racing each other can never both claim the same row — the whole
    reason this isn't a plain SELECT followed by a separate UPDATE."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM audio_jobs WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """UPDATE audio_jobs SET status = 'running', started_at = datetime('now','localtime'),
                   attempts = attempts + 1
               WHERE id = ?""",
            (row["id"],),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    out = dict(row)
    out["status"] = "running"
    out["attempts"] = out["attempts"] + 1
    return out


def finish_audio_job(job_id: int, error: str | None = None) -> None:
    """Mark a job 'done' (error is None) or 'error' (error is a message) and
    stamp finished_at. Never called for a job being requeued — see
    requeue_audio_job() for that path, which deliberately goes back to
    'pending' instead."""
    conn = get_db()
    conn.execute(
        """UPDATE audio_jobs SET status = ?, error = ?, finished_at = datetime('now','localtime')
           WHERE id = ?""",
        ("error" if error else "done", error, job_id),
    )
    conn.commit()
    conn.close()


def requeue_audio_job(job_id: int) -> None:
    """Put an interrupted job (Daniel came back while whisper.cpp was still
    running) back to 'pending' — never 'error': transcription is idempotent,
    so a job that got interrupted deserves an unconditional retry on the next
    quiet window, not a failure Daniel has to notice and clear."""
    conn = get_db()
    conn.execute(
        """UPDATE audio_jobs SET status = 'pending', started_at = NULL, finished_at = NULL
           WHERE id = ?""",
        (job_id,),
    )
    conn.commit()
    conn.close()


def list_audio_jobs(statuses: tuple[str, ...] = ("pending", "running")) -> list[dict]:
    """Jobs in any of `statuses`, oldest first — used by routes/tasks.py's
    header-indicator collector and by tests."""
    conn = get_db()
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM audio_jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
        statuses,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_audio_job_for_owner(owner_kind: str, owner_id: int) -> dict | None:
    """Most recent audio_jobs row for (owner_kind, owner_id), any status —
    used by GET /api/podcast/episodes/{id} (#1073) so the detail page can say
    *why* an audiobook (kind='audiobook', #1054/#1068) still has no summary:
    queued, actively transcribing, or failed with a reason. None for an
    episode that never had a local-ASR job (i.e. every non-audiobook item).
    Picks the latest by id rather than created_at — a requeue after an
    interrupted run doesn't insert a new row (see requeue_audio_job), so id
    order and created_at order agree; id is just cheaper to sort on."""
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM audio_jobs WHERE owner_kind = ? AND owner_id = ?
           ORDER BY id DESC LIMIT 1""",
        (owner_kind, owner_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def audio_disk_usage(owner_kind: str, owner_id: int) -> int:
    """Total bytes of every DISTINCT audio_path this owner's audio_tracks rows
    point at, for files that still exist on disk (#1054's disk-usage line on
    the knowledge detail page). This is "how much is on disk for this item",
    NOT "how much would be freed by deleting it" — a content-addressed path
    (see _unreferenced_paths) shared with another owner is still counted
    here, same as it's still fully present on disk either way."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT audio_path FROM audio_tracks WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    ).fetchall()
    conn.close()
    total = 0
    for r in rows:
        try:
            total += os.path.getsize(r["audio_path"])
        except OSError:
            pass
    return total


def delete_audio_tracks_for_book(book_id: int) -> list[str]:
    """Same contract as delete_audio_tracks(), for every page of one book at
    once (#1050) — routes/books.py's delete_book() has book_id, not the list
    of book_pages.id its pages happen to have, and this is one query instead
    of one delete_audio_tracks() call per page.

    Must be called BEFORE database.delete_book() — it looks pages up via
    book_pages.book_id, which no longer exists once the book row's deletion
    cascades book_pages away.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT id, audio_path FROM audio_tracks
           WHERE owner_kind = 'book_page'
             AND owner_id IN (SELECT id FROM book_pages WHERE book_id = ?)""",
        (book_id,),
    ).fetchall()
    safe_to_delete = _unreferenced_paths(conn, rows)
    conn.execute(
        """DELETE FROM audio_tracks
           WHERE owner_kind = 'book_page'
             AND owner_id IN (SELECT id FROM book_pages WHERE book_id = ?)""",
        (book_id,),
    )
    conn.commit()
    conn.close()
    return safe_to_delete

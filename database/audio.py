"""Time-aligned audio tracks (#1047 umbrella, this module added by #1048).

One table, `audio_tracks`, holds every (owner, lang, variant) → mp3 + cue
list, no matter which of the four alignment paths produced it (see
audio/__init__.py's module docstring). `audio/` and `routes/audio.py` are the
only callers — everything else that needs SQL for this feature belongs here,
same rule as every other table in this package.
"""
import json

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


def delete_audio_tracks(owner_kind: str, owner_id: int) -> list[str]:
    """Delete every track belonging to (owner_kind, owner_id) — called when
    the owner itself is deleted. Returns the audio_path of every row removed
    so the caller can also delete the mp3 files on disk; this module never
    touches the filesystem itself (same division of labor as
    database.delete_book(), which leaves the uploaded file to routes/books.py)."""
    conn = get_db()
    paths = [r["audio_path"] for r in conn.execute(
        "SELECT audio_path FROM audio_tracks WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    ).fetchall()]
    conn.execute(
        "DELETE FROM audio_tracks WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    )
    conn.commit()
    conn.close()
    return paths

"""Storage for the conjugation pattern scheduler (#1395).

Plain data in, plain data out: the scheduling math lives in conj_patterns.py,
this module only reads and writes rows (same rule as every other table: SQL
stays in the database package).
"""
from .core import get_db

_STATE_COLUMNS = ("lang", "pattern", "mode", "state", "stability", "difficulty",
                  "due", "last_review", "reps", "lapses", "day", "day_before",
                  "day_failed")


def get_conj_state(lang: str, pattern: str, mode: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conj_patterns WHERE lang = ? AND pattern = ? AND mode = ?",
        (lang, pattern, mode),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_conj_states(lang: str) -> dict[tuple[str, str], dict]:
    """Every stored pattern state of `lang`: {(pattern, mode): row}."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM conj_patterns WHERE lang = ?", (lang,)).fetchall()
    conn.close()
    return {(r["pattern"], r["mode"]): dict(r) for r in rows}


def save_conj_state(row: dict) -> None:
    """Insert or replace one pattern state; `row` needs lang, pattern, mode —
    missing state fields get their defaults."""
    values = {
        "state": "new", "stability": None, "difficulty": None, "due": None,
        "last_review": None, "reps": 0, "lapses": 0, "day": None,
        "day_before": None, "day_failed": 0,
    }
    values.update({k: row[k] for k in _STATE_COLUMNS if k in row})
    for key in ("lang", "pattern", "mode"):
        if not values.get(key):
            raise ValueError(f"save_conj_state: {key} is required")
    cols = ", ".join(_STATE_COLUMNS)
    marks = ", ".join("?" for _ in _STATE_COLUMNS)
    conn = get_db()
    conn.execute(
        f"INSERT OR REPLACE INTO conj_patterns ({cols}) VALUES ({marks})",
        tuple(values[c] for c in _STATE_COLUMNS),
    )
    conn.commit()
    conn.close()


def delete_conj_state(lang: str, pattern: str, mode: str) -> None:
    conn = get_db()
    conn.execute(
        "DELETE FROM conj_patterns WHERE lang = ? AND pattern = ? AND mode = ?",
        (lang, pattern, mode),
    )
    conn.commit()
    conn.close()


def add_conj_log(lang: str, pattern: str, mode: str, ok: bool, reviewed_day: str,
                 card_id: int | None = None, sentence_id: int | None = None,
                 slot: str | None = None, lemma: str | None = None,
                 form: str | None = None) -> int:
    """Append one conjugation rating; returns the log id (for undo)."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO conj_review_log
           (lang, pattern, mode, ok, card_id, sentence_id, slot, lemma, form, reviewed_day)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lang, pattern, mode, 1 if ok else 0, card_id, sentence_id, slot, lemma,
         form, reviewed_day),
    )
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    return log_id


def delete_conj_log(log_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM conj_review_log WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()


def conj_introduced_today(lang: str, mode: str, day: str) -> int:
    """Number of patterns whose FIRST rating in `mode` happened on `day`
    (= the new patterns already introduced today)."""
    conn = get_db()
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM (
               SELECT pattern, MIN(reviewed_day) AS first_day
               FROM conj_review_log WHERE lang = ? AND mode = ?
               GROUP BY pattern
           ) WHERE first_day = ?""",
        (lang, mode, day),
    ).fetchone()
    conn.close()
    return int(row["n"])


def conj_log_stats(lang: str) -> dict[tuple[str, str], dict]:
    """{(pattern, mode): {"total": n, "correct": n}} over the whole log."""
    conn = get_db()
    rows = conn.execute(
        """SELECT pattern, mode, COUNT(*) AS total, COALESCE(SUM(ok), 0) AS correct
           FROM conj_review_log WHERE lang = ? GROUP BY pattern, mode""",
        (lang,),
    ).fetchall()
    conn.close()
    return {(r["pattern"], r["mode"]): {"total": r["total"], "correct": r["correct"]}
            for r in rows}

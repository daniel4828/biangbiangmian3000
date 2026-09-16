"""Persistent cache for the read-along "译" block-translation feature (#1177).

routes/knowledge.py's translate_sentences() is the only caller: it looks up
whichever texts are already cached before spending a Google Translate round
trip on the rest, then saves whatever came back. All SQL for this feature
lives here — routes/knowledge.py never touches sentence_translations directly.
"""
import hashlib

from .core import get_db

# SQLite's default build caps a statement at 999 bound parameters. A batch of
# texts is bound twice in the IN (...) clause below (lang/target are fixed),
# so this stays comfortably under that with room to spare even if a caller
# ever sends more than the ~40-per-request batch size the frontend uses.
_CHUNK_SIZE = 400


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_sentence_translations(texts: list[str], lang: str, target: str) -> dict[str, str]:
    """Returns {text: translation} for whichever of `texts` are already
    cached. Texts with no cached row are simply absent from the result —
    the caller decides what to do about a miss (translate it)."""
    if not texts:
        return {}
    conn = get_db()
    out: dict[str, str] = {}
    hash_to_text: dict[str, str] = {_hash(t): t for t in texts}
    hashes = list(hash_to_text.keys())
    for i in range(0, len(hashes), _CHUNK_SIZE):
        chunk = hashes[i:i + _CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT text_hash, translation FROM sentence_translations
                WHERE lang = ? AND target = ? AND text_hash IN ({placeholders})""",
            (lang, target, *chunk),
        ).fetchall()
        for row in rows:
            text = hash_to_text.get(row["text_hash"])
            if text is not None:
                out[text] = row["translation"]
    conn.close()
    return out


def save_sentence_translations(pairs: dict[str, str], lang: str, target: str) -> None:
    """Stores newly-translated text -> translation pairs. Empty translations
    are skipped — an empty answer means the source-language check upstream
    rejected the text as same-as-source, and caching that would make a later,
    successful translation attempt (e.g. after the source text is corrected)
    unreachable behind a cached "" that never gets overwritten."""
    rows = [
        (lang, target, _hash(text), text, translation)
        for text, translation in pairs.items()
        if translation
    ]
    if not rows:
        return
    conn = get_db()
    conn.executemany(
        """INSERT OR REPLACE INTO sentence_translations (lang, target, text_hash, text, translation)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()

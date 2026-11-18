"""Chat history of the Mac quick panel (#1584). The client owns the turns and
PUTs the whole conversation after every successful send; the server only
stores it. All SQL for this feature lives here.
"""
import json

from .core import get_db


def save_quick_chat(chat_id: int | None, lang: str, title: str, turns: list) -> dict | None:
    """Insert (chat_id None) or overwrite an existing chat. Returns
    {id, updated_at}, or None when chat_id was given but the row doesn't exist."""
    turns_json = json.dumps(turns, ensure_ascii=False)
    conn = get_db()
    try:
        if chat_id is None:
            cur = conn.execute(
                "INSERT INTO quick_chats (lang, title, turns_json) VALUES (?, ?, ?)",
                (lang, title, turns_json),
            )
            new_id = cur.lastrowid
        else:
            cur = conn.execute(
                """UPDATE quick_chats
                   SET lang = ?, title = ?, turns_json = ?,
                       updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (lang, title, turns_json, chat_id),
            )
            if cur.rowcount == 0:
                return None
            new_id = chat_id
        conn.commit()
        row = conn.execute(
            "SELECT id, updated_at FROM quick_chats WHERE id = ?", (new_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_quick_chat(chat_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM quick_chats WHERE id = ?", (chat_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["turns"] = json.loads(d.pop("turns_json"))
    return d


def list_quick_chats(limit: int = 50) -> list[dict]:
    """List view: no turns body, only the turn count."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, lang, title, updated_at, turns_json
           FROM quick_chats ORDER BY updated_at DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["turn_count"] = len(json.loads(d.pop("turns_json")))
        out.append(d)
    return out


def delete_quick_chat(chat_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM quick_chats WHERE id = ?", (chat_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

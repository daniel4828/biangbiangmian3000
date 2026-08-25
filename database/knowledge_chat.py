"""Chat-about-a-knowledge-item storage (#945): one saved conversation per
knowledge item, so a follow-up question asked today is still there next week.
All SQL for the feature lives here — routes/knowledge.py only calls in.

A turn is written with add_turn(), which stores the question and the answer
together in one transaction. That is deliberate: an AI call that fails must
leave nothing behind, and a stored question with no answer is worse than no
record at all (see routes/knowledge.py's chat endpoint).
"""
from .core import get_db


def get_chat(episode_id: int) -> dict | None:
    """The conversation for one knowledge item, messages included (oldest
    first). Returns None when nothing has been asked yet — the caller decides
    whether that is an empty panel or a 404."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM knowledge_chats WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    msgs = conn.execute(
        """SELECT id, role, content, model, created_at
           FROM knowledge_chat_messages WHERE chat_id = ? ORDER BY id""",
        (row["id"],),
    ).fetchall()
    conn.close()
    chat = dict(row)
    chat["messages"] = [dict(m) for m in msgs]
    return chat


def add_turn(episode_id: int, question: str, answer: str, model: str) -> dict:
    """Append one question+answer pair, creating the conversation row on
    first use, and return the two stored messages.

    Both messages go in under a single commit so a crash can never leave a
    dangling question in the history.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM knowledge_chats WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if row:
        chat_id = row["id"]
        conn.execute(
            "UPDATE knowledge_chats SET model = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (model, chat_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO knowledge_chats (episode_id, model) VALUES (?, ?)",
            (episode_id, model),
        )
        chat_id = cur.lastrowid
    ids = []
    for role, content, msg_model in (("user", question, None), ("assistant", answer, model)):
        cur = conn.execute(
            "INSERT INTO knowledge_chat_messages (chat_id, role, content, model) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, msg_model),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    msgs = conn.execute(
        """SELECT id, role, content, model, created_at
           FROM knowledge_chat_messages WHERE id IN (?, ?) ORDER BY id""",
        tuple(ids),
    ).fetchall()
    conn.close()
    return {"chat_id": chat_id, "messages": [dict(m) for m in msgs]}


def delete_chat(episode_id: int) -> bool:
    """Wipe the conversation for one item. Returns whether a row was actually
    deleted, so the route can 404 instead of pretending success on an item
    that was never chatted about."""
    conn = get_db()
    cur = conn.execute("DELETE FROM knowledge_chats WHERE episode_id = ?", (episode_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

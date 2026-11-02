"""Storage side of the conjugation pull-forward (#1395).

conj_patterns.ensure_pullforward() decides WHICH cards to pull; this module
loads the card/deck context it needs and writes the result in one transaction.
Only the due date of a card is changed - never its deck or scheduling state.
"""
import json
from datetime import datetime, time, timedelta

from .core import get_db, anki_today, get_day_cutoff_hour
from .daily_target import _blocked_decks, _read_setting
from .stories import get_disabled_langs


def load_pull_context(lang: str, batch_key: str) -> dict | None:
    """Everything the planner needs, read-only. None when `lang` is disabled.

    {"today", "tomorrow_cutoff", "batch_ids": set, "cards": [dict],
     "reviewed": [{"id","word_id","category"}]}
    Each card dict: id, word_id, word_zh, category, state, due, deck_id and
    "eligible" (not deleted/suspended/buried today, deck not blocked, not Saved,
    right language, category enabled in the deck's preset)."""
    if lang in get_disabled_langs():
        return None
    today_date = anki_today()
    today = today_date.isoformat()
    cutoff = get_day_cutoff_hour()
    start = datetime.combine(today_date, time(cutoff)).isoformat(' ')
    end_dt = datetime.combine(today_date + timedelta(days=1), time(cutoff))
    conn = get_db()
    try:
        decks = [dict(r) for r in conn.execute('SELECT * FROM decks')]
        blocked = _blocked_decks(decks, today_date)
        deck_map = {d['id']: d for d in decks}
        presets = {r['id']: dict(r) for r in conn.execute('SELECT * FROM deck_presets')}
        batch = json.loads(_read_setting(conn, batch_key, '{}') or '{}')
        batch_ids = set(batch.get('ids', [])) if batch.get('day') == today else set()
        cards = []
        for r in conn.execute(
                """SELECT c.id, c.word_id, c.category, c.state, c.due, c.deck_id,
                          c.buried_until, e.word_zh
                   FROM cards c JOIN entries e ON e.id = c.word_id
                   WHERE c.deleted_at IS NULL AND e.lang = ?""", (lang,)):
            c = dict(r)
            d = deck_map.get(c['deck_id'])
            ok = bool(d and d['id'] not in blocked and d['lang'] == lang
                      and d['name'] != 'Saved' and c['state'] != 'suspended'
                      and not (c['buried_until'] and c['buried_until'] >= today))
            if ok:
                p = presets.get(d['preset_id']) or {}
                ok = bool(p.get(c['category'] + '_enabled', 1))
            c['eligible'] = ok
            del c['buried_until']
            cards.append(c)
        reviewed = [dict(r) for r in conn.execute(
            """SELECT DISTINCT c.id, c.word_id, c.category FROM review_log rl
               JOIN cards c ON c.id = rl.card_id JOIN entries e ON e.id = c.word_id
               WHERE e.lang = ? AND rl.reviewed_at >= datetime(?, 'utc')
                     AND rl.reviewed_at < datetime(?, 'utc')""",
            (lang, start, end_dt.isoformat(' ')))]
        return {"today": today, "tomorrow_cutoff": end_dt.isoformat(),
                "batch_ids": batch_ids, "cards": cards, "reviewed": reviewed}
    finally:
        conn.close()


def commit_pulls(lang: str, batch_key: str, card_ids: list[int], limit: int) -> int:
    """Set due = today on `card_ids` and record them in today's batch, in ONE
    transaction. Re-reads the batch inside it: ids already pulled today are
    skipped and at most `limit` ids may be in the batch afterwards, so
    concurrent callers can never pull twice or exceed the daily budget.
    Returns the number of cards actually pulled."""
    if not card_ids:
        return 0
    today = anki_today().isoformat()
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        batch_ids = _batch_ids_for(conn, today, batch_key)
        room = max(0, limit - len(batch_ids))
        new = [i for i in dict.fromkeys(card_ids) if i not in batch_ids][:room]
        if not new:
            conn.rollback()
            return 0
        for cid in new:
            conn.execute("UPDATE cards SET due = ? WHERE id = ? AND deleted_at IS NULL",
                         (today, cid))
        conn.execute(
            'INSERT INTO app_settings(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (batch_key, json.dumps({'day': today, 'ids': sorted(batch_ids | set(new))})))
        conn.commit()
        return len(new)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _batch_ids_for(conn, today: str, batch_key: str) -> set[int]:
    batch = json.loads(_read_setting(conn, batch_key, '{}') or '{}')
    return set(batch.get('ids', [])) if batch.get('day') == today else set()

"""Fill the Chinese study day to 50 distinct cards without changing presets."""
import json
from collections import deque
from datetime import datetime, time, timedelta

from word_frequency import frequency_rank
from .core import anki_today, get_day_cutoff_hour, get_db, _ensure_default_preset
from .decks import parse_daily_deck_date, _invalidate_locked_deck_cache
from .stories import get_app_setting, set_app_setting

DAILY_TARGET = 50
_BATCH_KEY = 'daily_target_batch'


def get_daily_target_settings() -> dict:
    return {'enabled': get_app_setting('daily_target_enabled', '1') == '1', 'target': DAILY_TARGET}


def set_daily_target_enabled(enabled: bool) -> dict:
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    set_app_setting('daily_target_enabled', '1' if enabled else '0')
    return get_daily_target_settings()


def _read_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _batch_ids(conn, today):
    batch = json.loads(_read_setting(conn, _BATCH_KEY, '{}'))
    return set(batch.get('ids', [])) if batch.get('day') == today else set()


def daily_target_card_ids(conn=None) -> set[int]:
    """Today's explicitly added cards bypass the usual new-card limit only.

    State, deletion, burial, future-deck locks and category switches still apply.
    The reservation survives reloads, restarts and turning off future filling.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        return _batch_ids(conn, anki_today().isoformat())
    finally:
        if own_conn:
            conn.close()


def limit_new_cards(cards: list, remaining: int, auto_ids=None) -> list:
    """Keep the ordinary cap, plus every eligible card explicitly added today."""
    if auto_ids is None:
        auto_ids = daily_target_card_ids()
    normal = 0
    result = []
    for c in cards:
        if c['id'] in auto_ids:
            result.append(c)
        elif normal < remaining:
            result.append(c)
            normal += 1
    return result


def _blocked_decks(decks, today):
    by_id = {d['id']: d for d in decks}
    blocked = set()
    for d in decks:
        cur = d
        seen = set()
        while cur and cur['id'] not in seen:
            seen.add(cur['id'])
            parent = by_id.get(cur['parent_id'])
            future = (parent and parent['name'].strip().lower() == 'daily'
                      and (parse_daily_deck_date(cur['name'], today) or today) > today)
            if cur['deleted_at'] or future:
                blocked.add(d['id'])
                break
            cur = parent
    return blocked


def _daily_leaf(conn, today, category, preset_id):
    """Create the usual Daily/date/category path inside the fill transaction.

    A trashed destination is deliberately not revived by an automatic action.
    """
    root = conn.execute("SELECT id FROM decks WHERE name='All' AND parent_id IS NULL AND deleted_at IS NULL LIMIT 1").fetchone()
    parent_id = root['id'] if root else None
    for name, cat in [('Daily', None), (today, None),
                      (f'{today} · {category.title()}', category)]:
        row = conn.execute('SELECT id, deleted_at FROM decks WHERE name=? AND parent_id IS ?',
                           (name, parent_id)).fetchone()
        if row:
            if row['deleted_at']:
                return None
            parent_id = row['id']
        else:
            parent_id = conn.execute(
                'INSERT INTO decks(name,parent_id,preset_id,category,lang) VALUES(?,?,?,?,?)',
                (name, parent_id, preset_id, cat, 'zh')).lastrowid
    return parent_id


def ensure_daily_target() -> int:
    """Atomically reserve and activate only today's shortfall; return added count.

    Existing due cards and different cards already rated today form a union.
    Same-word candidates are skipped to respect the existing sibling-bury rule.
    Today's reservations also count after a manual bury/suspend, so refreshing
    cannot repeatedly pull a new batch as the user hides unwanted cards.
    """
    today_date = anki_today()
    today = today_date.isoformat()
    cutoff = get_day_cutoff_hour()
    start = datetime.combine(today_date, time(cutoff)).isoformat(' ')
    end_dt = datetime.combine(today_date + timedelta(days=1), time(cutoff))
    end = end_dt.isoformat(' ')
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (_read_setting(conn, 'daily_target_enabled', '1') != '1'
                or 'zh' in json.loads(_read_setting(conn, 'disabled_langs', '[]'))):
            return 0
        decks = [dict(r) for r in conn.execute('SELECT * FROM decks')]
        blocked = _blocked_decks(decks, today_date)
        presets = {r['id']: dict(r) for r in conn.execute('SELECT * FROM deck_presets')}
        deck_map = {d['id']: d for d in decks}
        auto_ids = _batch_ids(conn, today)
        rows = [dict(r) for r in conn.execute(
            """SELECT c.*, e.word_zh, e.lang FROM cards c
               JOIN entries e ON e.id=c.word_id
               WHERE c.deleted_at IS NULL AND e.lang='zh'""")]
        active = []
        candidates = {'leech': [], 'saved': []}
        for c in rows:
            d = deck_map.get(c['deck_id'])
            if not d or d['id'] in blocked or d['lang'] != 'zh':
                continue
            p = presets[d['preset_id']]
            if not p.get(c['category'] + '_enabled', 1):
                continue
            if c['buried_until'] and c['buried_until'] >= today:
                continue
            if c['is_leech']:
                candidates['leech'].append(c)
            elif d['name'] == 'Saved':
                candidates['saved'].append(c)
            if d['name'] != 'Saved' and c['state'] != 'suspended':
                due = c['due']
                if ((c['state'] in ('learning', 'relearn') and 'T' in due and due < end_dt.isoformat())
                        or ('T' not in due and due <= today)):
                    active.append(c)
        reviewed = conn.execute(
            """SELECT DISTINCT c.id, c.word_id FROM review_log rl
               JOIN cards c ON c.id=rl.card_id JOIN entries e ON e.id=c.word_id
               WHERE e.lang='zh' AND rl.reviewed_at >= datetime(?, 'utc')
                     AND rl.reviewed_at < datetime(?, 'utc')""", (start, end)).fetchall()
        done_ids = {r['id'] for r in reviewed}
        # Natural new cards are subject to the existing per-leaf and root caps.
        # Added cards are explicitly exempt, never by modifying a preset.
        new_done = {}
        for r in conn.execute(
            """SELECT DISTINCT c.id,c.deck_id,c.category FROM cards c
               JOIN entries e ON e.id=c.word_id
               WHERE e.lang='zh' AND EXISTS (SELECT 1 FROM review_log rl WHERE rl.card_id=c.id
                   AND rl.reviewed_at >= datetime(?, 'utc') AND rl.reviewed_at < datetime(?, 'utc'))
               AND NOT EXISTS (SELECT 1 FROM review_log rl WHERE rl.card_id=c.id
                   AND rl.reviewed_at < datetime(?, 'utc'))""", (start, end, start)):
            d = deck_map.get(r['deck_id'])
            if (r['id'] not in auto_ids and d and d['id'] not in blocked
                    and d['lang'] == 'zh'
                    and presets[d['preset_id']].get(r['category'] + '_enabled', 1)):
                key = (r['deck_id'], r['category'])
                new_done[key] = new_done.get(key, 0) + 1
        root = next((d for d in decks if d['name'] == 'All' and d['parent_id'] is None and not d['deleted_at']), None)
        used_leaf, used_cat = {}, {}
        eligible = []
        for c in sorted(active, key=lambda r: r['id']):
            if c['state'] != 'new' or c['id'] in auto_ids or c['id'] in done_ids:
                eligible.append(c)
                continue
            key = (c['deck_id'], c['category'])
            p = presets[deck_map[c['deck_id']]['preset_id']]
            leaf_limit = p['new_per_day']
            override = conn.execute('SELECT new_per_day FROM preset_category_overrides WHERE preset_id=? AND category=?',
                                    (p['id'], c['category'])).fetchone()
            if override and override['new_per_day'] is not None:
                leaf_limit = override['new_per_day']
            root_limit = presets[root['preset_id']]['new_per_day'] if root else leaf_limit
            if root:
                root_override = conn.execute(
                    'SELECT new_per_day FROM preset_category_overrides WHERE preset_id=? AND category=?',
                    (root['preset_id'], c['category'])).fetchone()
                if root_override and root_override['new_per_day'] is not None:
                    root_limit = root_override['new_per_day']
            cat_done = sum(n for (did, cat), n in new_done.items() if cat == c['category'])
            if (used_leaf.get(key, 0) < max(0, leaf_limit - new_done.get(key, 0))
                    and used_cat.get(c['category'], 0) < max(0, root_limit - cat_done)):
                eligible.append(c)
                used_leaf[key] = used_leaf.get(key, 0) + 1
                used_cat[c['category']] = used_cat.get(c['category'], 0) + 1
        # Count the cards that remain usable after normal sibling burial.
        # This changes the target estimate, never the scheduler's bury policy.
        from .cards import resolve_bury_flags
        suppressed_states = {}
        usable = []
        category_order = {'listening': 0, 'reading': 1, 'creating': 2}
        state_order = {'learning': 0, 'relearn': 0, 'review': 1, 'new': 2}
        for c in sorted(eligible, key=lambda r: (state_order.get(r['state'], 3), category_order.get(r['category'], 3), r['id'])):
            if c['state'] in suppressed_states.get(c['word_id'], set()):
                continue
            usable.append(c)
            p = presets[deck_map[c['deck_id']]['preset_id']]
            deck_bury = deck_map[c['deck_id']].get('bury_quick_mode')
            if deck_bury is not None:
                p = {**p, 'bury_quick_mode': deck_bury}
            bury_new, bury_review, bury_learning = resolve_bury_flags(p)
            states = suppressed_states.setdefault(c['word_id'], set())
            if bury_new:
                states.add('new')
            if bury_review:
                states.add('review')
            if bury_learning:
                states.update(('learning', 'relearn'))
        planned = auto_ids | done_ids | {c['id'] for c in usable}
        gap = max(0, DAILY_TARGET - len(planned))
        if not gap:
            return 0
        excluded_words = {r['word_id'] for r in reviewed} | {c['word_id'] for c in active}
        excluded_words.update(c['word_id'] for c in rows if c['id'] in auto_ids)
        for source, cs in candidates.items():
            cs.sort(key=lambda c: (frequency_rank(c['word_zh'], 'zh') or float('inf'), c['word_id'], c['id']))
            candidates[source] = deque(cs)
        next_source = _read_setting(conn, 'daily_target_next_source', 'leech')
        if next_source not in candidates:
            next_source = 'leech'
        default_preset = _ensure_default_preset(conn)
        added = 0
        while added < gap:
            selected = None
            for source in (next_source, 'saved' if next_source == 'leech' else 'leech'):
                while candidates[source]:
                    c = candidates[source].popleft()
                    if c['word_id'] not in excluded_words:
                        selected = c
                        break
                if selected:
                    break
            if not selected:
                break
            c = selected
            target = _daily_leaf(conn, today, c['category'], default_preset)
            if target is None:
                break
            target_preset = conn.execute('SELECT p.* FROM decks d JOIN deck_presets p ON p.id=d.preset_id WHERE d.id=?', (target,)).fetchone()
            if not target_preset[c['category'] + '_enabled']:
                excluded_words.add(c['word_id'])
                continue
            conn.execute(
                """UPDATE cards SET deck_id=?,state='new',due=?,step_index=0,interval=0,
                   ease=2.5,repetitions=0,lapses=0,stability=NULL,difficulty=NULL,
                   last_review=NULL,learning_again_count=0,is_leech=0,leeched_at=NULL,
                   probation=0,buried_until=NULL,pre_suspend_state=NULL WHERE id=?""",
                (target, today, c['id']))
            auto_ids.add(c['id'])
            excluded_words.add(c['word_id'])
            added += 1
            next_source = 'saved' if source == 'leech' else 'leech'
        if added:
            for key, value in ((_BATCH_KEY, json.dumps({'day': today, 'ids': sorted(auto_ids)})),
                               ('daily_target_next_source', next_source)):
                conn.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            conn.commit()
            _invalidate_locked_deck_cache()
        return added
    finally:
        conn.close()


def ordinary_new_sql(conn) -> str:
    """SQL suffix excluding today's reserved IDs from ordinary daily usage."""
    ids = sorted(i for i in daily_target_card_ids(conn) if type(i) is int)
    return ' AND c.id NOT IN (' + ','.join(map(str, ids)) + ')' if ids else ''


def daily_target_new_counts(conn=None) -> dict:
    """Eligible new reservations by leaf/category, for batched badge counts."""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        ids = daily_target_card_ids(conn)
        if not ids:
            return {}
        ph = ','.join('?' * len(ids))
        today = anki_today().isoformat()
        rows = conn.execute(
            f"""SELECT deck_id,category,COUNT(*) AS n FROM cards
                WHERE id IN ({ph}) AND state='new' AND deleted_at IS NULL
                AND due<=? AND (buried_until IS NULL OR buried_until<?)
                GROUP BY deck_id,category""", [*ids, today, today])
        decks = [dict(r) for r in conn.execute('SELECT * FROM decks')]
        blocked = _blocked_decks(decks, anki_today())
        deck_map = {d['id']: d for d in decks}
        presets = {r['id']: dict(r) for r in conn.execute('SELECT * FROM deck_presets')}
        return {(r['deck_id'], r['category']): r['n'] for r in rows
                if r['deck_id'] not in blocked and r['deck_id'] in deck_map
                and presets[deck_map[r['deck_id']]['preset_id']].get(r['category'] + '_enabled', 1)}
    finally:
        if own_conn:
            conn.close()

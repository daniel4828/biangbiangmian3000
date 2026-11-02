"""Fill the Chinese study day to 50 distinct cards without changing presets."""
import json
import logging
from collections import deque
from datetime import datetime, time, timedelta

import languages
from word_frequency import frequency_rank
from .core import (anki_today, get_day_cutoff_hour, get_db, _ensure_default_preset,
                   _ensure_lang_preset)
from .decks import parse_daily_deck_date, _invalidate_locked_deck_cache
from .stories import get_app_setting, set_app_setting

DAILY_TARGET = 50
MIN_TARGET, MAX_TARGET = 1, 500
_BATCH_KEY = 'daily_target_batch'
_COUNT_KEY = 'daily_target_count'
_CREATING_KEY = 'daily_target_creating_pct'
# Share of auto-filled cards that go to `creating` (#1390). None = no balancing
# (Chinese keeps its historical behaviour); Spanish defaults to half-half.
_CREATING_DEFAULT = {'zh': None}
_CREATING_FALLBACK = 50


def _key(base: str, lang: str = 'zh') -> str:
    """zh keeps the historical setting keys; other languages get a suffix (#1386)."""
    return base if lang == 'zh' else f'{base}_{lang}'


def _check_lang(lang: str) -> str:
    if not languages.is_valid_lang(lang):
        raise ValueError(f'unknown language: {lang!r}')
    return lang


def get_daily_target_settings(lang: str = 'zh') -> dict:
    _check_lang(lang)
    default = '1' if lang == 'zh' else '0'
    return {'enabled': get_app_setting(_key('daily_target_enabled', lang), default) == '1',
            'target': _parse_target(get_app_setting(_key(_COUNT_KEY, lang), None)),
            'creating_pct': _parse_creating(get_app_setting(_key(_CREATING_KEY, lang), None), lang)}


def _parse_creating(raw, lang):
    """Stored value: '' = explicitly unbalanced, integer 0–100 = share."""
    if raw is None:
        return _CREATING_DEFAULT.get(lang, _CREATING_FALLBACK)
    if raw == '':
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _CREATING_DEFAULT.get(lang, _CREATING_FALLBACK)
    return value if 0 <= value <= 100 else _CREATING_DEFAULT.get(lang, _CREATING_FALLBACK)


def _parse_target(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DAILY_TARGET
    return value if MIN_TARGET <= value <= MAX_TARGET else DAILY_TARGET


def _validate_target(target) -> int:
    if type(target) is not int or not MIN_TARGET <= target <= MAX_TARGET:
        raise ValueError(f'target must be an integer between {MIN_TARGET} and {MAX_TARGET}')
    return target


def set_daily_target_enabled(enabled: bool, lang: str = 'zh') -> dict:
    return set_daily_target_settings(lang, enabled=enabled)


def set_daily_target_count(target: int, lang: str = 'zh') -> dict:
    return set_daily_target_settings(lang, target=target)


_UNSET = object()


def set_daily_target_settings(lang: str = 'zh', enabled=None, target=None,
                              creating_pct=_UNSET) -> dict:
    """Validate everything first, then write; at least one value is required.
    creating_pct: integer 0–100, or None = no category balancing."""
    if enabled is None and target is None and creating_pct is _UNSET:
        raise ValueError('enabled, target or creating_pct is required')
    if creating_pct is not _UNSET and creating_pct is not None and (
            type(creating_pct) is not int or not 0 <= creating_pct <= 100):
        raise ValueError('creating_pct must be an integer between 0 and 100, or null')
    if enabled is not None and type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    if target is not None:
        _validate_target(target)
    _check_lang(lang)
    if enabled is not None:
        set_app_setting(_key('daily_target_enabled', lang), '1' if enabled else '0')
    if target is not None:
        set_app_setting(_key(_COUNT_KEY, lang), str(target))
    if creating_pct is not _UNSET:
        set_app_setting(_key(_CREATING_KEY, lang), '' if creating_pct is None else str(creating_pct))
    return get_daily_target_settings(lang)


def _read_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _batch_ids(conn, today, lang: str = 'zh'):
    batch = json.loads(_read_setting(conn, _key(_BATCH_KEY, lang), '{}'))
    ids = set(batch.get('ids', [])) if batch.get('day') == today else set()
    return ids


def daily_target_card_ids(conn=None) -> set[int]:
    """Today's explicitly added cards bypass the usual new-card limit only.

    State, deletion, burial, future-deck locks and category switches still apply.
    The reservation survives reloads, restarts and turning off future filling.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        today = anki_today().isoformat()
        ids = set()
        for lang in languages.LANGUAGES:
            ids |= _batch_ids(conn, today, lang)
        return ids
    finally:
        if own_conn:
            conn.close()


def limit_new_cards(cards: list, remaining: int, auto_ids=None) -> list:
    """Keep the ordinary cap, plus every eligible card explicitly added today."""
    # #1412: aufgefüllte Karten unterliegen immer dem normalen Limit.
    auto_ids = auto_ids or set()
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
    roots = {languages.deck_root(l).strip().lower() for l in languages.LANGUAGES}
    blocked = set()
    for d in decks:
        cur = d
        seen = set()
        while cur and cur['id'] not in seen:
            seen.add(cur['id'])
            parent = by_id.get(cur['parent_id'])
            future = (parent and parent['name'].strip().lower() in roots
                      and (parse_daily_deck_date(cur['name'], today) or today) > today)
            if cur['deleted_at'] or future:
                blocked.add(d['id'])
                break
            cur = parent
    return blocked


def _daily_leaf(conn, today, category, preset_id, lang='zh'):
    """Create the usual <root>/date/category path inside the fill transaction.

    A trashed destination is deliberately not revived by an automatic action.
    """
    root = conn.execute("SELECT id FROM decks WHERE name='All' AND parent_id IS NULL AND deleted_at IS NULL LIMIT 1").fetchone()
    parent_id = root['id'] if root else None
    for name, cat in [(languages.deck_root(lang), None), (today, None),
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
                (name, parent_id, preset_id, cat, lang)).lastrowid
    return parent_id


def ensure_daily_targets(lang: str | None) -> int:
    """Fill one registered language, or every language when `lang` is None or
    the multi-language sentinel. Disabled languages return 0 immediately."""
    if languages.is_valid_lang(lang):
        total = ensure_daily_target(lang)
    else:
        total = sum(ensure_daily_target(l) for l in languages.LANGUAGES)
    if lang == 'es' or not languages.is_valid_lang(lang):
        total += _pull_conj_cards()
    return total


def _pull_conj_cards() -> int:
    """Pull learned Spanish verb cards forward for due conjugation patterns
    (#1395). Must never break the daily-target fill."""
    try:
        import conj_patterns
        return conj_patterns.ensure_pullforward('es')
    except Exception as e:
        logging.getLogger(__name__).warning('conjugation pull-forward failed - %s', e)
        return 0


def ensure_daily_target(lang: str = 'zh') -> int:
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
    enabled_key = _key('daily_target_enabled', lang)
    enabled_default = '1' if lang == 'zh' else '0'
    lang_preset = None
    try:
        if lang != 'zh':
            # _ensure_lang_preset commits, so resolve it before the write
            # transaction opens; skipped entirely while the language is off.
            if _read_setting(conn, enabled_key, enabled_default) != '1':
                return 0
            lang_preset = _ensure_lang_preset(conn, lang)
        conn.execute('BEGIN IMMEDIATE')
        if (_read_setting(conn, enabled_key, enabled_default) != '1'
                or lang in json.loads(_read_setting(conn, 'disabled_langs', '[]'))):
            return 0
        decks = [dict(r) for r in conn.execute('SELECT * FROM decks')]
        blocked = _blocked_decks(decks, today_date)
        presets = {r['id']: dict(r) for r in conn.execute('SELECT * FROM deck_presets')}
        deck_map = {d['id']: d for d in decks}
        auto_ids = _batch_ids(conn, today, lang)
        rows = [dict(r) for r in conn.execute(
            """SELECT c.*, e.word_zh, e.lang FROM cards c
               JOIN entries e ON e.id=c.word_id
               WHERE c.deleted_at IS NULL AND e.lang=?""", (lang,))]
        active = []
        candidates = {'leech': [], 'saved': []}
        for c in rows:
            d = deck_map.get(c['deck_id'])
            if not d or d['id'] in blocked or d['lang'] != lang:
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
               WHERE e.lang=? AND rl.reviewed_at >= datetime(?, 'utc')
                     AND rl.reviewed_at < datetime(?, 'utc')""", (lang, start, end)).fetchall()
        done_ids = {r['id'] for r in reviewed}
        # Natural new cards are subject to the existing per-leaf and root caps.
        # Added cards are explicitly exempt, never by modifying a preset.
        new_done = {}
        for r in conn.execute(
            """SELECT DISTINCT c.id,c.deck_id,c.category FROM cards c
               JOIN entries e ON e.id=c.word_id
               WHERE e.lang=? AND EXISTS (SELECT 1 FROM review_log rl WHERE rl.card_id=c.id
                   AND rl.reviewed_at >= datetime(?, 'utc') AND rl.reviewed_at < datetime(?, 'utc'))
               AND NOT EXISTS (SELECT 1 FROM review_log rl WHERE rl.card_id=c.id
                   AND rl.reviewed_at < datetime(?, 'utc'))""", (lang, start, end, start)):
            d = deck_map.get(r['deck_id'])
            if (d and d['id'] not in blocked
                    and d['lang'] == lang
                    and presets[d['preset_id']].get(r['category'] + '_enabled', 1)):
                key = (r['deck_id'], r['category'])
                new_done[key] = new_done.get(key, 0) + 1
        root = next((d for d in decks if d['name'] == 'All' and d['parent_id'] is None and not d['deleted_at']), None)
        used_leaf, used_cat = {}, {}
        bindings = {(r['lang'], r['category']): r['preset_id']
                    for r in conn.execute('SELECT lang, category, preset_id FROM category_presets')}

        def _limit(pid, category):
            o = conn.execute('SELECT new_per_day FROM preset_category_overrides WHERE preset_id=? AND category=?',
                             (pid, category)).fetchone()
            if o and o['new_per_day'] is not None:
                return o['new_per_day']
            return presets[pid]['new_per_day']

        def _caps(deck_id, category):
            # Gleiche Limits wie die Warteschlange: Blatt-Deck (inkl. #1372-Bindung) + „All“.
            d = deck_map.get(deck_id) or dict(conn.execute('SELECT * FROM decks WHERE id=?', (deck_id,)).fetchone())
            pid = bindings.get((d.get('lang') or 'zh', category), d['preset_id'])
            leaf = _limit(pid, category)
            return leaf, (_limit(root['preset_id'], category) if root else leaf)

        def _has_room(deck_id, category):
            leaf_limit, root_limit = _caps(deck_id, category)
            cat_done = sum(n for (did, cat), n in new_done.items() if cat == category)
            key = (deck_id, category)
            if (used_leaf.get(key, 0) < max(0, leaf_limit - new_done.get(key, 0))
                    and used_cat.get(category, 0) < max(0, root_limit - cat_done)):
                used_leaf[key] = used_leaf.get(key, 0) + 1
                used_cat[category] = used_cat.get(category, 0) + 1
                return True
            return False

        eligible = []
        for c in sorted(active, key=lambda r: r['id']):
            if c['state'] != 'new' or c['id'] in done_ids:
                eligible.append(c)
                continue
            key = (c['deck_id'], c['category'])
            leaf_limit, root_limit = _caps(c['deck_id'], c['category'])
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
        # #1395: creating of an irregular Spanish verb is not buried by
        # reading/listening (same rule as bury_siblings / story suppression).
        from .cards import _irregular_verb_word_ids
        irregular_words = _irregular_verb_word_ids(
            conn, {c['word_id'] for c in eligible if c['category'] == 'creating'}) if lang == 'es' else set()
        for c in sorted(eligible, key=lambda r: (state_order.get(r['state'], 3), category_order.get(r['category'], 3), r['id'])):
            if (c['state'] in suppressed_states.get(c['word_id'], set())
                    and not (c['category'] == 'creating' and c['word_id'] in irregular_words)):
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
        creating_pct = _parse_creating(_read_setting(conn, _key(_CREATING_KEY, lang), None), lang)
        swapped = 0
        if creating_pct is not None and auto_ids:
            swapped = _rebalance_batch(conn, rows, auto_ids, done_ids, deck_map, presets,
                                       blocked, creating_pct, lang, today)
            if swapped:
                rows = [dict(r) for r in conn.execute(
                    """SELECT c.*, e.word_zh, e.lang FROM cards c
                       JOIN entries e ON e.id=c.word_id
                       WHERE c.deleted_at IS NULL AND e.lang=?""", (lang,))]
        planned = done_ids | {c['id'] for c in usable}
        target_count = _parse_target(_read_setting(conn, _key(_COUNT_KEY, lang), None))
        gap = max(0, target_count - len(planned))
        if not gap:
            if swapped:
                _save_batch(conn, lang, today, auto_ids)
                conn.commit()
                _invalidate_locked_deck_cache()
            return swapped
        row_by_id = {c['id']: c for c in rows}
        mix = {'creating': sum(1 for i in auto_ids if row_by_id.get(i, {}).get('category') == 'creating')}
        mix['other'] = len(auto_ids) - mix['creating']
        excluded_words = {r['word_id'] for r in reviewed} | {c['word_id'] for c in active}
        excluded_words.update(c['word_id'] for c in rows if c['id'] in auto_ids)
        for source, cs in candidates.items():
            cs.sort(key=lambda c: (frequency_rank(c['word_zh'], lang) or float('inf'), c['word_id'], c['id']))
            candidates[source] = deque(cs)
        source_key = _key('daily_target_next_source', lang)
        next_source = _read_setting(conn, source_key, 'leech')
        if next_source not in candidates:
            next_source = 'leech'
        default_preset = _ensure_default_preset(conn) if lang == 'zh' else lang_preset
        added = 0
        while added < gap:
            selected = None
            groups = [None]
            if creating_pct is not None:
                total = mix['creating'] + mix['other'] + 1
                want_creating = (creating_pct / 100 * total - mix['creating']
                                 >= (1 - creating_pct / 100) * total - mix['other'])
                if creating_pct in (0, 100):
                    want_creating = creating_pct == 100
                groups = ['creating', 'other'] if want_creating else ['other', 'creating']
                if creating_pct not in (0, 100):
                    groups.append(None)
            for group in groups:
                for source in (next_source, 'saved' if next_source == 'leech' else 'leech'):
                    pool = candidates[source]
                    for i, cand in enumerate(pool):
                        if cand['word_id'] in excluded_words:
                            continue
                        if group is not None and (cand['category'] == 'creating') != (group == 'creating'):
                            continue
                        selected = cand
                        del pool[i]
                        break
                    if selected:
                        break
                if selected:
                    break
            if not selected:
                break
            c = selected
            target = _daily_leaf(conn, today, c['category'], default_preset, lang)
            if target is None:
                break
            target_preset = conn.execute('SELECT p.* FROM decks d JOIN deck_presets p ON p.id=d.preset_id WHERE d.id=?', (target,)).fetchone()
            if not target_preset[c['category'] + '_enabled'] or not _has_room(target, c['category']):
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
            mix['creating' if c['category'] == 'creating' else 'other'] += 1
            added += 1
            next_source = 'saved' if source == 'leech' else 'leech'
        if added or swapped:
            _save_batch(conn, lang, today, auto_ids)
            conn.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (source_key, next_source))
            conn.commit()
            _invalidate_locked_deck_cache()
        return added + swapped
    finally:
        conn.close()


def _save_batch(conn, lang, today, auto_ids):
    conn.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (_key(_BATCH_KEY, lang), json.dumps({'day': today, 'ids': sorted(auto_ids)})))


def _rebalance_batch(conn, rows, auto_ids, done_ids, deck_map, presets, blocked,
                     creating_pct, lang, today) -> int:
    """Swap today's untouched auto cards to the other category (#1390).

    Applies a changed creating share to a batch already added today. Only an
    auto card that is still new and unreviewed is swapped, and only for a
    sibling card parked in the Saved deck (suspended, no progress to lose).
    The swapped-out card goes back to Saved, suspended. Returns swap count.
    """
    by_id = {c['id']: c for c in rows}
    batch = [by_id[i] for i in auto_ids if i in by_id]
    creating = [c for c in batch if c['category'] == 'creating']
    other = [c for c in batch if c['category'] != 'creating']
    want = round(len(batch) * creating_pct / 100)
    if len(creating) > want:
        movable, n, to_creating = creating, len(creating) - want, False
    elif len(creating) < want:
        movable, n, to_creating = other, want - len(creating), True
    else:
        return 0
    order = {'listening': 0, 'reading': 1}
    swaps = 0
    for c in sorted(movable, key=lambda r: -r['id']):
        if swaps >= n:
            break
        if c['state'] != 'new' or c['id'] in done_ids:
            continue
        siblings = sorted(
            (s for s in rows if s['word_id'] == c['word_id'] and s['id'] != c['id']
             and (s['category'] == 'creating') == to_creating
             and s['id'] not in auto_ids and s['state'] == 'suspended'
             and deck_map.get(s['deck_id'], {}).get('name') == 'Saved'
             and presets[deck_map[c['deck_id']]['preset_id']].get(s['category'] + '_enabled', 1)),
            key=lambda s: (order.get(s['category'], 2), s['id']))
        if not siblings:
            continue
        sib = siblings[0]
        target = _daily_leaf(conn, today, sib['category'], deck_map[c['deck_id']]['preset_id'], lang)
        if target is None or target in blocked:
            continue
        conn.execute(
            """UPDATE cards SET deck_id=?,state='new',due=?,step_index=0,interval=0,
               ease=2.5,repetitions=0,lapses=0,stability=NULL,difficulty=NULL,
               last_review=NULL,learning_again_count=0,is_leech=0,leeched_at=NULL,
               probation=0,buried_until=NULL,pre_suspend_state=NULL WHERE id=?""",
            (target, today, sib['id']))
        conn.execute("UPDATE cards SET deck_id=?,state='suspended' WHERE id=?",
                     (sib['deck_id'], c['id']))
        auto_ids.discard(c['id'])
        auto_ids.add(sib['id'])
        swaps += 1
    return swaps


def ordinary_new_sql(conn) -> str:
    """SQL suffix excluding today's reserved IDs from ordinary daily usage."""
    return ''  # #1412: keine Ausnahme mehr für aufgefüllte Karten


def daily_target_new_counts(conn=None) -> dict:
    """Eligible new reservations by leaf/category, for batched badge counts."""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        return {}  # #1412: aufgefüllte Karten zählen wie normale neue Karten
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

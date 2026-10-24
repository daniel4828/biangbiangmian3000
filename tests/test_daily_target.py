"""Daily target fills cards (not words or rating presses), in an isolated DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

import database
import main
from test_browse_lang import _entry


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'target.db'))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()


def card(word, source='active', category='listening', lang='zh', **fields):
    wid = _entry(word, lang)
    if source == 'saved':
        did = database.get_or_create_saved_deck(lang)
    else:
        parent = database.get_or_create_deck_path('Practice' if lang == 'zh' else 'Français::Practice', lang=lang)
        did = database.get_or_create_category_decks(parent, 'Practice')[category]
    cid = database.insert_card(wid, category, did, state='suspended' if source != 'active' else 'review',
                               due=database.anki_today().isoformat())
    if source == 'leech':
        fields.update(is_leech=1, pre_suspend_state='review', lapses=9, repetitions=20,
                      stability=30, difficulty=8, learning_again_count=10,
                      last_review='2020-01-01', leeched_at='2020-01-01')
    if fields:
        conn = database.get_db()
        conn.execute('UPDATE cards SET ' + ', '.join(k+'=?' for k in fields) + ' WHERE id=?',
                     [*fields.values(), cid])
        conn.commit()
        conn.close()
    return cid


def seed_active(n):
    return [card(f'已有卡{i}') for i in range(n)]


def test_fills_gap_by_frequency_alternating_and_resets_only_selected_card():
    seed_active(47)
    low_l = card('完全不存在的测试词语', 'leech')
    high_l = card('的', 'leech')
    high_s = card('我', 'saved')
    low_s = card('另外不存在的测试词语', 'saved')
    # Same word's other card must retain its scheduling.
    sibling = database.insert_card(database.get_card(high_l)['word_id'], 'creating',
                                   database.get_card(high_l)['deck_id'], state='review', due='2099-01-01')
    assert database.ensure_daily_target() == 3
    assert database.get_card(high_l)['state'] == 'new'
    assert database.get_card(high_s)['state'] == 'new'
    assert database.get_card(low_l)['state'] == 'new'
    assert database.get_card(low_s)['state'] == 'suspended'
    reset = database.get_card(high_l)
    for field in ('interval', 'repetitions', 'lapses', 'learning_again_count', 'is_leech', 'step_index', 'probation'):
        assert reset[field] == 0
    for field in ('stability', 'difficulty', 'last_review', 'leeched_at', 'pre_suspend_state', 'buried_until'):
        assert reset[field] is None
    assert database.get_card(sibling)['due'] == '2099-01-01'
    assert database.ensure_daily_target() == 0


def test_completed_cards_count_once_and_use_anki_day_boundary():
    active = seed_active(49)
    saved = card('我', 'saved')
    conn = database.get_db()
    reviewed = active[0]
    conn.execute("UPDATE cards SET due='2099-01-01' WHERE id=?", (reviewed,))
    boundary = datetime.combine(database.anki_today(), time(database.get_day_cutoff_hour()))
    # review_log is UTC; use the same local->UTC conversion as SQLite stats.
    for _ in range(3):
        conn.execute("INSERT INTO review_log(card_id, rating, reviewed_at) VALUES (?,3,datetime(?, 'utc'))",
                     (reviewed, (boundary + timedelta(hours=1)).isoformat(' ')))
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 1
    assert database.get_card(saved)['state'] == 'new'
    assert database.ensure_daily_target() == 0


def test_does_not_touch_ineligible_cards_and_handles_empty_source():
    seed_active(48)
    eligible = card('我', 'saved')
    disabled = card('的', 'leech', category='reading')
    buried = card('你', 'leech', buried_until=database.anki_today().isoformat())
    deleted = card('他', 'leech', deleted_at='2020-01-01')
    french = card('bonjour', 'saved', lang='fr')
    manual = card('她', state='suspended')
    assert database.ensure_daily_target() == 1
    assert database.get_card(eligible)['state'] == 'new'
    for cid in (disabled, buried, deleted, french, manual):
        assert database.get_card(cid)['state'] == 'suspended'


def test_fifty_cards_remain_available_through_leaf_and_root_caps():
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=2')
    conn.commit()
    conn.close()
    card('明天才学', due='2099-01-01')  # second listening leaf exercises root cap
    for i in range(60):
        card(f'待学词{i}', 'saved')
    assert database.ensure_daily_target() == 50
    root = database.get_all_deck_id()
    assert len(database.get_due_cards_any_cat(root, lang='zh')) == 50
    assert database.count_due_any_cat(root, lang='zh')['new'] == 50
    assert database.count_due_by_category(root, lang='zh')['listening']['new'] == 50
    cards = database.get_due_cards_any_cat(root, lang='zh')
    leaves = sorted({c['deck_id'] for c in cards})
    assert len(database.get_due_cards_multi(leaves, 'listening', root_deck_id=root)) == 50
    assert database.count_due_multi(leaves, 'listening', root_deck_id=root)['new'] == 50
    counts, _ = database.count_due_all_decks()
    assert sum(c['new'] for c in counts.values()) == 50
    assert sum(database.count_due(did, 'listening')['new'] for did in leaves) == 50


def test_disabled_setting_and_strict_api_boolean():
    candidate = card('我', 'saved')
    client = TestClient(main.app)
    assert client.get('/api/daily-target').json() == {'enabled': True, 'target': 50}
    assert client.put('/api/daily-target', json={'enabled': 'false'}).status_code == 400
    assert client.put('/api/daily-target', json={'enabled': False}).json() == {'enabled': False, 'target': 50}
    assert database.ensure_daily_target() == 0
    assert database.get_card(candidate)['state'] == 'suspended'
    assert client.put('/api/daily-target', json={'enabled': True}).status_code == 200
    client.get('/api/decks?lang=zh')
    assert database.get_card(candidate)['state'] == 'new'


def test_concurrent_fills_are_idempotent_and_preserve_alternation():
    seed_active(49)
    l1 = card('的', 'leech')
    s1 = card('我', 'saved')
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: database.ensure_daily_target(), range(2)))
    assert sum(result) == 1
    assert database.get_card(l1)['state'] == 'new'
    # Add a new gap without serving the same source again.
    conn = database.get_db()
    conn.execute("UPDATE cards SET due='2099-01-01' WHERE id=(SELECT MIN(id) FROM cards WHERE state='review')")
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 1
    assert database.get_card(s1)['state'] == 'new'


def test_candidates_do_not_repeat_same_word_or_words_already_planned():
    seed_active(48)
    selected = card('我', 'leech')
    sibling = database.insert_card(database.get_card(selected)['word_id'], 'creating',
                                   database.get_card(selected)['deck_id'], state='suspended')
    conn = database.get_db()
    conn.execute('UPDATE cards SET is_leech=1 WHERE id=?', (sibling,))
    conn.commit()
    conn.close()
    extra = card('你', 'saved')
    assert database.ensure_daily_target() == 2
    assert database.get_card(selected)['state'] == 'new'
    assert database.get_card(extra)['state'] == 'new'
    assert database.get_card(sibling)['state'] == 'suspended'


def test_future_and_trashed_ancestor_decks_are_not_candidates_or_due():
    seed_active(49)
    future = card('的', 'leech')
    trash = card('我', 'leech')
    eligible = card('你', 'saved')
    day = (database.anki_today() + timedelta(days=1)).isoformat()
    future_deck, _ = database.get_or_create_daily_deck(day)
    future_leaf = database.get_or_create_category_decks(future_deck, day)['listening']
    trash_parent = database.get_or_create_deck_path('TrashParent')
    trash_leaf = database.get_or_create_category_decks(trash_parent, 'TrashParent')['listening']
    conn = database.get_db()
    conn.execute('UPDATE cards SET deck_id=? WHERE id=?', (future_leaf, future))
    conn.execute('UPDATE cards SET deck_id=? WHERE id=?', (trash_leaf, trash))
    conn.execute("UPDATE decks SET deleted_at='2020-01-01' WHERE id=?", (trash_parent,))
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 1
    assert database.get_card(eligible)['state'] == 'new'
    assert database.get_card(future)['state'] == 'suspended'
    assert database.get_card(trash)['state'] == 'suspended'


def test_new_day_uses_persisted_alternate_source_and_only_new_shortfall(monkeypatch):
    seed_active(49)
    l = card('的', 'leech')
    s = card('我', 'saved')
    assert database.ensure_daily_target() == 1
    assert database.get_card(l)['state'] == 'new'
    tomorrow = database.anki_today() + timedelta(days=1)
    import database.daily_target as target
    monkeypatch.setattr(target, 'anki_today', lambda: tomorrow)
    # Yesterday's new card is still due, so no new card is added.
    assert database.ensure_daily_target() == 0
    conn = database.get_db()
    conn.execute("UPDATE cards SET due='2099-01-01' WHERE id=?", (l,))
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 1
    assert database.get_card(s)['state'] == 'new'


def test_auto_review_does_not_consume_ordinary_new_limit():
    seed_active(49)
    saved = card('我', 'saved')
    assert database.ensure_daily_target() == 1
    chosen = database.get_card(saved)
    wid = _entry('另一个普通词', 'zh')
    normal = database.insert_card(wid, 'listening', chosen['deck_id'], due=database.anki_today().isoformat())
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=1')
    conn.execute("UPDATE cards SET state='learning',due='2099-01-01' WHERE id=?", (saved,))
    conn.execute("INSERT INTO review_log(card_id,rating,reviewed_at) VALUES (?,3,?)",
                 (saved, database.anki_today().isoformat() + ' 12:00:00'))
    conn.commit()
    conn.close()
    assert [c['id'] for c in database.get_due_cards(chosen['deck_id'], 'listening')] == [normal]
    assert database.count_due(chosen['deck_id'], 'listening')['new'] == 1


def test_auto_reservation_still_respects_future_locks_and_disabled_categories():
    cid = card('我', 'saved')
    assert database.ensure_daily_target() == 1
    original = database.get_card(cid)
    tomorrow = (database.anki_today() + timedelta(days=1)).isoformat()
    future, _ = database.get_or_create_daily_deck(tomorrow)
    leaf = database.get_or_create_category_decks(future, tomorrow)['listening']
    conn = database.get_db()
    conn.execute('UPDATE cards SET deck_id=? WHERE id=?', (leaf, cid))
    conn.commit()
    conn.close()
    assert database.daily_target_new_counts() == {}
    assert database.count_due_multi([original['deck_id'], leaf], 'listening', root_deck_id=database.get_all_deck_id())['new'] == 0
    conn = database.get_db()
    conn.execute('UPDATE cards SET deck_id=? WHERE id=?', (original['deck_id'], cid))
    conn.execute('UPDATE deck_presets SET listening_enabled=0')
    conn.commit()
    conn.close()
    assert database.daily_target_new_counts() == {}


def test_root_category_override_does_not_hide_the_shortfall():
    root = database.get_all_deck_id()
    conn = database.get_db()
    root_preset = conn.execute("INSERT INTO deck_presets(name,new_per_day,listening_enabled) VALUES('Root target test',20,1)").lastrowid
    conn.execute('UPDATE decks SET preset_id=? WHERE id=?', (root_preset, root))
    conn.execute('INSERT INTO preset_category_overrides(preset_id,category,new_per_day) VALUES(?,?,?)', (root_preset, 'listening', 0))
    conn.commit()
    conn.close()
    for i in range(10):
        card(f'普通新卡{i}', state='new')
    for i in range(55):
        card(f'收藏词{i}', 'saved')
    assert database.ensure_daily_target() == 50
    assert len(database.get_due_cards_any_cat(root, lang='zh')) == 50
    assert database.ensure_daily_target() == 0


def test_natural_siblings_that_will_be_buried_do_not_inflate_target():
    for i in range(25):
        cid = card(f'原有兄弟词{i}')
        c = database.get_card(cid)
        did = database.get_or_create_category_decks(database.get_deck(c['deck_id'])['parent_id'], 'Practice')['creating']
        database.insert_card(c['word_id'], 'creating', did, state='review', due=database.anki_today().isoformat())
    for i in range(30):
        card(f'补充词{i}', 'saved')
    assert database.ensure_daily_target() == 25
    assert database.count_due_any_cat(database.get_all_deck_id(), lang='zh')['new'] == 25
    assert database.ensure_daily_target() == 0


def test_due_tagged_leech_already_counts_as_scheduled_without_reset():
    seed_active(49)
    tagged = card('我', 'leech')
    saved = card('你', 'saved')
    conn = database.get_db()
    conn.execute("UPDATE cards SET state='review' WHERE id=?", (tagged,))
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 0
    assert database.get_card(tagged)['stability'] == 30
    assert database.get_card(tagged)['is_leech'] == 1
    assert database.get_card(saved)['state'] == 'suspended'


def test_disabled_chinese_language_never_promotes_candidates():
    cid = card('我', 'saved')
    database.set_app_setting('disabled_langs', '["zh"]')
    assert database.ensure_daily_target() == 0
    assert database.get_card(cid)['state'] == 'suspended'


def test_french_new_reviews_do_not_consume_chinese_new_allowance():
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=20')
    conn.commit()
    conn.close()
    for i in range(10):
        card(f'自然中文新词{i}', state='new')
    fr = [card(f'french{i}', lang='fr', due='2099-01-01') for i in range(20)]
    conn = database.get_db()
    for cid in fr:
        conn.execute("INSERT INTO review_log(card_id,rating,reviewed_at) VALUES(?,3,?)", (cid, database.anki_today().isoformat() + ' 12:00:00'))
    conn.commit()
    conn.close()
    for i in range(55):
        card(f'中文收藏词{i}', 'saved')
    assert database.ensure_daily_target() == 40
    assert len(database.get_due_cards_any_cat(database.get_all_deck_id(), lang='zh')) == 50


def test_deck_bury_override_keeps_both_sibling_cards_in_daily_total():
    for i in range(25):
        cid = card(f'允许重复词{i}')
        c = database.get_card(cid)
        did = database.get_or_create_category_decks(database.get_deck(c['deck_id'])['parent_id'], 'Practice')['creating']
        database.insert_card(c['word_id'], 'creating', did, state='review', due=database.anki_today().isoformat())
    candidate = card('我', 'saved')
    conn = database.get_db()
    conn.execute("UPDATE decks SET bury_quick_mode='none' WHERE category IS NOT NULL")
    conn.commit()
    conn.close()
    assert database.ensure_daily_target() == 0
    assert database.get_card(candidate)['state'] == 'suspended'

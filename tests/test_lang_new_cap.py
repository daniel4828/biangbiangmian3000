"""#1599: Neu/Tag einer (Sprache, Kategorie)-Bindung ist ein Gesamtlimit.

Vorher galt das Limit pro datiertem Blatt (`Español::<Datum> · Listening`):
bei 17 Blättern zeigten Pille und Warteschlange 17 × 25 statt 25 Karten.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import database
import main
from test_browse_lang import _entry


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'langcap.db'))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()


def _find(nodes, name):
    for n in nodes:
        if n.get('name') == name:
            return n
        found = _find(n.get('children') or [], name)
        if found:
            return found
    return None


def _setup(lang='es', prefix='Español', n_decks=3, per_deck=5, word_prefix='palabra'):
    """Preset new_per_day=3 everywhere; n_decks dated listening leaves."""
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=3')
    conn.commit()
    conn.close()
    today = database.anki_today().isoformat()
    leaves, cards = [], []
    for d in range(n_decks):
        parent = database.get_or_create_deck_path(f'{prefix}::Tag{d}', lang=lang)
        leaf = database.get_or_create_category_decks(parent, f'Tag{d}')['listening']
        leaves.append(leaf)
        for i in range(per_deck):
            cid = database.insert_card(_entry(f'{word_prefix}{d}x{i}', lang), 'listening',
                                       leaf, state='new', due=today)
            cards.append(cid)
    return leaves, cards


def _bind(lang='es'):
    pid = database.list_presets()[0]['id']
    database.set_category_preset(lang, 'listening', pid)
    return pid


def _introduce_today(card_id):
    """Simulate a card first reviewed today (leaves the 'new' state)."""
    tomorrow = (database.anki_today() + timedelta(days=2)).isoformat()
    conn = database.get_db()
    conn.execute("UPDATE cards SET state='learning', due=? WHERE id=?",
                 (tomorrow + 'T10:00:00', card_id))
    conn.execute("INSERT INTO review_log(card_id, rating, reviewed_at) VALUES (?,3,?)",
                 (card_id, database.anki_today().isoformat() + ' 12:00:00'))
    conn.commit()
    conn.close()


def _new_in_queue(ids):
    return [c for c in database.get_due_cards_multi(ids, 'listening') if c['state'] == 'new']


def test_bound_language_cap_is_total_over_all_leaves():
    leaves, _ = _setup()
    _bind('es')

    assert database.count_due_multi(leaves, 'listening', root_deck_id=None)['new'] == 3
    assert len(_new_in_queue(leaves)) == 3

    all_counts, _susp = database.count_due_all_decks()
    for leaf in leaves:
        assert all_counts[(leaf, 'listening')]['new'] <= 3

    # Einzelnes Blatt und Gesamt-Zähler dürfen das Limit nie überschreiten.
    assert database.count_due(leaves[0], 'listening')['new'] <= 3
    assert len(database.get_due_cards(leaves[0], 'listening')) <= 3
    assert database.count_due_deduped([(l, 'listening') for l in leaves])['new'] == 3

    client = TestClient(main.app)
    tree = client.get('/api/decks?lang=es').json()
    es = _find(tree, 'Español')
    assert es['cat_counts']['listening']['new'] == 3
    assert es['counts']['new'] == 3

    tree_all = client.get('/api/decks').json()
    all_deck = _find(tree_all, 'All')
    assert all_deck['lang_cat_counts']['es']['listening']['new'] == 3


def test_remaining_shrinks_after_cards_introduced_today():
    leaves, cards = _setup()
    _bind('es')
    _introduce_today(cards[0])      # Blatt 0
    _introduce_today(cards[5])      # Blatt 1

    assert database.lang_new_remaining('listening') == {'es': 1}
    assert database.count_due_multi(leaves, 'listening', root_deck_id=None)['new'] == 1
    assert len(_new_in_queue(leaves)) == 1

    client = TestClient(main.app)
    es = _find(client.get('/api/decks?lang=es').json(), 'Español')
    assert es['cat_counts']['listening']['new'] == 1


def test_without_binding_limits_stay_per_leaf():
    leaves, _ = _setup()
    assert database.lang_new_remaining('listening') == {}
    # 3 Blätter × je 3 (Preset-Limit), kein Elterndeck-Limit bei root_deck_id=None
    assert database.count_due_multi(leaves, 'listening', root_deck_id=None)['new'] == 9
    assert len(_new_in_queue(leaves)) == 9

    _bind('es')
    database.set_category_preset('es', 'listening', None)
    assert database.count_due_multi(leaves, 'listening', root_deck_id=None)['new'] == 9


def test_other_language_unaffected_by_binding():
    _setup()                                   # es
    zh_leaves, _ = _setup(lang='zh', prefix='Daily', word_prefix='ci')
    _bind('es')
    assert database.count_due_multi(zh_leaves, 'listening', root_deck_id=None)['new'] == 9
    assert len(_new_in_queue(zh_leaves)) == 9
    assert 'zh' not in database.lang_new_remaining('listening')

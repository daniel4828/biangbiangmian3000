"""#1595: Die Kategorie-Pille eines Elterndecks respektiert dessen Neu/Tag-Limit.

Vorher wurde die Zahl der Pille im Frontend aus allen Blättern summiert; jedes
datierte Blatt (`Español::<Datum> · Listening`) hat sein eigenes Limit, also
zeigte die Pille 3 × 50 = 150, obwohl die Warteschlange nur 50 ausgibt.
"""
import pytest
from fastapi.testclient import TestClient

import database
import main
from test_browse_lang import _entry


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'cap.db'))
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


def test_parent_pill_new_count_is_capped_by_parent_limit():
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=3')
    conn.commit()
    conn.close()
    today = database.anki_today().isoformat()
    for d in range(3):
        parent = database.get_or_create_deck_path(f'Español::Tag{d}', lang='es')
        leaf = database.get_or_create_category_decks(parent, f'Tag{d}')['listening']
        for i in range(5):
            database.insert_card(_entry(f'palabra{d}x{i}', 'es'), 'listening', leaf,
                                 state='new', due=today)

    client = TestClient(main.app)
    tree = client.get('/api/decks?lang=es').json()
    es = _find(tree, 'Español')
    assert es is not None
    # Blätter einzeln: je min(5, 3) = 3 → Summe wäre 9; das Elterndeck erlaubt 3.
    assert es['cat_counts']['listening']['new'] == 3
    assert es['counts']['new'] == 3
    # Gleiche Zahl wie die Warteschlange.
    ids = [l for l, c in database.cards._leaf_decks_with_category(es['id'], lang='es') if c == 'listening']
    assert database.count_due_multi(ids, 'listening', root_deck_id=es['id'])['new'] == 3

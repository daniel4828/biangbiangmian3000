"""#1492: the mixed-review header "new" count must honour per-category preset
overrides (e.g. listening new_per_day=0), like the queue and count_due_by_category."""
import pytest

import database
from test_browse_lang import _entry


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'override.db'))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()


def _setup():
    root = database.get_or_create_deck_path('Español::Mixed', lang='es')
    leaves = database.get_or_create_category_decks(root, 'Mixed')
    preset = {**database.default_preset(), 'name': 'Override test', 'new_per_day': 9996}
    pid = database.insert_preset(preset)
    for did in (root, *leaves.values()):
        database.assign_preset_to_deck(did, pid)
    today = database.anki_today().isoformat()
    for cat, n in (('listening', 3), ('creating', 2)):
        for i in range(n):
            wid = _entry(f'palabra{cat}{i}', 'es')
            database.insert_card(wid, cat, leaves[cat], state='new', due=today)
    return root, pid


def test_mixed_new_count_respects_category_override():
    root, pid = _setup()
    # Without the override both categories' new cards count.
    assert database.count_due_any_cat(root)['new'] == 5

    database.set_category_override(pid, 'listening', {'new_per_day': 0})
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()

    assert database.count_due_any_cat(root)['new'] == 2
    by_cat = database.count_due_by_category(root)
    assert sum(v['new'] for v in by_cat.values()) == 2
    assert by_cat['listening']['new'] == 0

    database.delete_category_override(pid, 'listening')
    assert database.count_due_any_cat(root)['new'] == 5

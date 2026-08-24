"""Issue #918: the ⏸ suspension toggles must not cross language trees.

Both toggles descend recursively from the deck they are given. The home page's
'All' deck is a single row (parent_id IS NULL) that every language tab shows —
routes.decks._filter_tree_by_lang deliberately keeps aggregating parents alive
under each language — so an unscoped descent from it reaches the Français
subtree as well: pausing Creating under 中文 suspended Daniel's French cards.

Same family as #915 (the category switches), different code path.

Omitting `lang` must keep the old, unfiltered behaviour: a pure-Chinese install
never sends the parameter, and neither do scripts.
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import database
import main

client = TestClient(main.app)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """One Chinese and one French leaf deck, each with a Creating card."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()

    zh_deck = database.get_or_create_deck_path("Daily::2026-08-24 · Creating")
    fr_deck = database.get_or_create_deck_path(
        "Français::2026-08-24 · Creating", lang="fr")

    def card(word_zh, lang, deck_id):
        wid = database.insert_word({"word_zh": word_zh, "lang": lang})
        return database.insert_card(wid, "creating", deck_id)

    return {
        "root": database.get_all_deck_id(),
        "zh_card": card("生态", "zh", zh_deck),
        "fr_card": card("parler", "fr", fr_deck),
    }


def _state(card_id):
    conn = database.get_db()
    row = conn.execute("SELECT state FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return row["state"]


def test_category_toggle_under_zh_leaves_french_alone(tree):
    r = client.post(f"/api/decks/{tree['root']}/categories/creating/toggle-suspension",
                    params={"lang": "zh"})
    assert r.status_code == 200
    assert _state(tree["zh_card"]) == "suspended"
    assert _state(tree["fr_card"]) == "new"


def test_category_toggle_under_fr_leaves_chinese_alone(tree):
    r = client.post(f"/api/decks/{tree['root']}/categories/creating/toggle-suspension",
                    params={"lang": "fr"})
    assert r.status_code == 200
    assert _state(tree["fr_card"]) == "suspended"
    assert _state(tree["zh_card"]) == "new"


def test_unsuspending_is_scoped_too(tree):
    """The un-pause half reads the same scope — otherwise pausing 中文 and then
    un-pausing it would wake up French cards that were never suspended here."""
    client.post(f"/api/decks/{tree['root']}/categories/creating/toggle-suspension",
                params={"lang": "fr"})
    client.post(f"/api/decks/{tree['root']}/categories/creating/toggle-suspension",
                params={"lang": "zh"})
    assert _state(tree["fr_card"]) == "suspended"
    assert _state(tree["zh_card"]) == "suspended"

    client.post(f"/api/decks/{tree['root']}/categories/creating/toggle-suspension",
                params={"lang": "zh"})
    assert _state(tree["zh_card"]) == "new"
    assert _state(tree["fr_card"]) == "suspended"


def test_deck_all_toggle_is_scoped(tree):
    r = client.post(f"/api/decks/{tree['root']}/toggle-all-suspension",
                    params={"lang": "zh"})
    assert r.status_code == 200
    assert _state(tree["zh_card"]) == "suspended"
    assert _state(tree["fr_card"]) == "new"


def test_creating_shortcut_route_is_scoped(tree):
    r = client.post(f"/api/decks/{tree['root']}/creating/toggle-suspension",
                    params={"lang": "fr"})
    assert r.status_code == 200
    assert _state(tree["fr_card"]) == "suspended"
    assert _state(tree["zh_card"]) == "new"


def test_without_lang_both_trees_are_toggled(tree):
    """No language tab in play (pure-Chinese install, scripts): unchanged."""
    r = client.post(f"/api/decks/{tree['root']}/toggle-all-suspension")
    assert r.status_code == 200
    assert _state(tree["zh_card"]) == "suspended"
    assert _state(tree["fr_card"]) == "suspended"


def test_unknown_lang_is_rejected(tree):
    """Matching no deck would report success while suspending nothing."""
    r = client.post(f"/api/decks/{tree['root']}/toggle-all-suspension",
                    params={"lang": "xx"})
    assert r.status_code == 400
    assert _state(tree["zh_card"]) == "new"
    assert _state(tree["fr_card"]) == "new"

"""Issue #898: the three category switches must not be shared between languages.

The home page's 'All' deck is a single row (parent_id IS NULL) that every
language tab shows — routes.decks._filter_tree_by_lang deliberately keeps
aggregating parents alive under each language. It is bound to the default
preset (id=2 in production, Chinese's), so turning Creating on while the
Français tab was active turned it on for 中文 as well.

Only reading/listening/creating_enabled move to the language's own preset
(#806 gave every non-Chinese tree one). The scheduling fields stay on the
deck's own preset on purpose — see CLAUDE.md's #629 postmortem.
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import database
import main

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    # A French tree has to exist for the fr tab to be a real thing; creating it
    # is also what clones the 'Français' preset (#806).
    database.get_or_create_deck_path("Français::2026-08-23 · Listening", lang="fr")
    return database.get_all_deck_id()


def _switches(deck_id, lang):
    r = client.get(f"/api/decks/{deck_id}/preset", params={"lang": lang})
    assert r.status_code == 200
    return {k: r.json()[k] for k in
            ("reading_enabled", "listening_enabled", "creating_enabled")}


def test_switch_under_fr_tab_leaves_zh_untouched(tmp_db):
    root = tmp_db
    before_zh = _switches(root, "zh")

    r = client.put(f"/api/decks/{root}/preset", params={"lang": "fr"},
                   json={"creating_enabled": 0, "listening_enabled": 1,
                         "reading_enabled": 1})
    assert r.status_code == 200
    assert r.json()["creating_enabled"] == 0

    assert _switches(root, "fr")["creating_enabled"] == 0
    assert _switches(root, "zh") == before_zh


def test_switch_lands_on_the_language_preset_not_the_default(tmp_db):
    root = tmp_db
    default_id = database.get_lang_preset("zh")["id"]
    fr_id = database.get_lang_preset("fr")["id"]
    assert fr_id != default_id

    client.put(f"/api/decks/{root}/preset", params={"lang": "fr"},
               json={"creating_enabled": 0})

    assert database.get_preset(fr_id)["creating_enabled"] == 0
    assert database.get_preset(default_id)["creating_enabled"] == 1


def test_scheduling_fields_stay_on_the_decks_own_preset(tmp_db):
    """Only the switches are language-scoped — new_per_day is not (#629)."""
    root = tmp_db
    own_id = database.get_deck(root)["preset_id"]
    fr_id = database.get_lang_preset("fr")["id"]

    client.put(f"/api/decks/{root}/preset", params={"lang": "fr"},
               json={"new_per_day": 7, "creating_enabled": 0})

    assert database.get_preset(own_id)["new_per_day"] == 7
    assert database.get_preset(own_id)["creating_enabled"] == 1
    assert database.get_preset(fr_id)["creating_enabled"] == 0


def test_deck_tree_reports_the_language_switches(tmp_db):
    root = tmp_db
    client.put(f"/api/decks/{root}/preset", params={"lang": "fr"},
               json={"creating_enabled": 0})

    def root_node(lang):
        tree = client.get("/api/decks", params={"lang": lang}).json()
        return next(d for d in tree if d.get("id") == root)

    assert root_node("fr")["creating_enabled"] == 0
    assert root_node("zh")["creating_enabled"] == 1


def test_zh_tab_writes_the_default_preset_as_before(tmp_db):
    """No behaviour change for Chinese: its tab still edits its own preset."""
    root = tmp_db
    own_id = database.get_deck(root)["preset_id"]
    client.put(f"/api/decks/{root}/preset", params={"lang": "zh"},
               json={"creating_enabled": 0})
    assert database.get_preset(own_id)["creating_enabled"] == 0


# --- #915: decks *inside* the French tree, not just the shared root ----------
#
# _ensure_lang_preset (#806) only runs when a deck is created, so every French
# deck predating it is still bound to the default (Chinese) preset — that is
# the state of production: `Français` itself and its 2026-08-13 subtree sit on
# preset 2. #898 routed by "deck's language differs from the tab", which those
# rows fail, so they read and wrote Chinese's switches under the fr tab.

@pytest.fixture
def legacy_fr_deck(tmp_db):
    """A French deck bound to the default preset, like the pre-#806 rows."""
    deck_id = database.get_or_create_deck_path("Français", lang="fr")
    database.assign_preset_to_deck(deck_id, database.get_lang_preset("zh")["id"])
    return deck_id


def test_legacy_fr_deck_does_not_write_the_chinese_preset(legacy_fr_deck):
    default_id = database.get_lang_preset("zh")["id"]
    fr_id = database.get_lang_preset("fr")["id"]
    assert database.get_deck(legacy_fr_deck)["preset_id"] == default_id

    r = client.put(f"/api/decks/{legacy_fr_deck}/preset", params={"lang": "fr"},
                   json={"creating_enabled": 0})
    assert r.status_code == 200

    assert database.get_preset(fr_id)["creating_enabled"] == 0
    assert database.get_preset(default_id)["creating_enabled"] == 1


def test_legacy_fr_deck_reads_the_french_switches(legacy_fr_deck):
    client.put(f"/api/decks/{legacy_fr_deck}/preset", params={"lang": "fr"},
               json={"creating_enabled": 0})
    assert _switches(legacy_fr_deck, "fr")["creating_enabled"] == 0

    def node(lang):
        tree = client.get("/api/decks", params={"lang": lang}).json()
        return next(d for d in _flatten(tree) if d["id"] == legacy_fr_deck)

    assert node("fr")["creating_enabled"] == 0


def test_chinese_tab_unaffected_by_the_french_switches(legacy_fr_deck, tmp_db):
    """The reported symptom: flipping Creating under 中文 must stay in 中文."""
    client.put(f"/api/decks/{legacy_fr_deck}/preset", params={"lang": "fr"},
               json={"creating_enabled": 1})
    client.put(f"/api/decks/{tmp_db}/preset", params={"lang": "zh"},
               json={"creating_enabled": 0})

    assert _switches(legacy_fr_deck, "fr")["creating_enabled"] == 1
    assert _switches(tmp_db, "zh")["creating_enabled"] == 0


def _flatten(tree):
    for node in tree:
        yield node
        yield from _flatten(node.get("children") or [])

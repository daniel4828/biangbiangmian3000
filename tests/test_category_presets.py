"""Issue #1372: per-(language, category) scheduling presets.

A row in category_presets makes cards/decks of that category in that language
read the bound preset; decks.preset_id is never rewritten. Without a binding
everything behaves exactly as before.
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import database
import main
from database.cards import _get_presets_bulk

client = TestClient(main.app)

X_STEPS = "7m 9m"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    es_deck = database.get_or_create_deck_path("Español::Test", lang="es")
    zh_deck = database.get_or_create_deck_path("Daily::Test", lang="zh")
    preset_x = database.insert_preset(
        {**database.default_preset(), "name": "X", "learning_steps": X_STEPS}
    )

    def card(deck_id, word, category, lang):
        wid = database.insert_word({"word_zh": word, "lang": lang, "definition": word})
        return database.insert_card(wid, category, deck_id)

    return {
        "es_deck": es_deck,
        "zh_deck": zh_deck,
        "preset_x": preset_x,
        "es_listening": card(es_deck, "hablar", "listening", "es"),
        "es_creating": card(es_deck, "comer", "creating", "es"),
        "zh_listening": card(zh_deck, "你好", "listening", "zh"),
    }


def _deck_presets():
    conn = database.get_db()
    rows = conn.execute("SELECT id, preset_id FROM decks ORDER BY id").fetchall()
    conn.close()
    return {r["id"]: r["preset_id"] for r in rows}


def test_without_binding_nothing_changes(env):
    es_own = database.get_deck(env["es_deck"])["preset_id"]
    assert database.get_card(env["es_listening"])["preset_id"] == es_own
    assert database.get_preset_for_deck(env["es_deck"], "listening")["id"] == es_own
    assert _get_presets_bulk([env["es_deck"]], "listening")[env["es_deck"]]["id"] == es_own
    assert database.get_category_binding("es", "listening") is None


def test_binding_applies_to_matching_lang_and_category_only(env):
    database.set_category_preset("es", "listening", env["preset_x"])

    assert database.get_card(env["es_listening"])["learning_steps"] == X_STEPS
    assert database.get_card(env["es_listening"])["preset_id"] == env["preset_x"]
    # other category, same language
    assert database.get_card(env["es_creating"])["learning_steps"] != X_STEPS
    # same category, other language
    assert database.get_card(env["zh_listening"])["learning_steps"] != X_STEPS

    assert database.get_preset_for_deck(env["es_deck"], "listening")["id"] == env["preset_x"]
    assert database.get_preset_for_deck(env["es_deck"], "creating")["id"] != env["preset_x"]
    # no category -> deck's own preset, as before
    assert database.get_preset_for_deck(env["es_deck"])["id"] != env["preset_x"]
    assert _get_presets_bulk([env["es_deck"]], "listening")[env["es_deck"]]["id"] == env["preset_x"]
    assert _get_presets_bulk([env["es_deck"]], "creating")[env["es_deck"]]["id"] != env["preset_x"]
    assert _get_presets_bulk([env["es_deck"]])[env["es_deck"]]["id"] != env["preset_x"]


def test_binding_never_rewrites_deck_preset_ids(env):
    before = _deck_presets()
    database.set_category_preset("es", "listening", env["preset_x"])
    assert _deck_presets() == before


def test_unbinding_restores_previous_behaviour(env):
    es_own = database.get_deck(env["es_deck"])["preset_id"]
    database.set_category_preset("es", "listening", env["preset_x"])
    database.set_category_preset("es", "listening", None)
    assert database.get_category_binding("es", "listening") is None
    assert database.get_card(env["es_listening"])["preset_id"] == es_own


def test_set_category_preset_validates(env):
    with pytest.raises(ValueError):
        database.set_category_preset("es", "bogus", env["preset_x"])
    with pytest.raises(ValueError):
        database.set_category_preset("es", "listening", 99999)


def test_cannot_delete_a_bound_preset(env):
    database.set_category_preset("es", "listening", env["preset_x"])
    with pytest.raises(ValueError):
        database.delete_preset(env["preset_x"])
    database.set_category_preset("es", "listening", None)
    database.delete_preset(env["preset_x"])
    assert database.get_preset(env["preset_x"]) is None


def test_get_category_preset_shows_bound_or_language_default(env):
    preset, bound = database.get_category_preset("es", "listening")
    assert bound is False
    assert preset["id"] == database.get_lang_preset("es")["id"]
    database.set_category_preset("es", "listening", env["preset_x"])
    preset, bound = database.get_category_preset("es", "listening")
    assert bound is True and preset["id"] == env["preset_x"]


# --- API -------------------------------------------------------------------

def test_api_get_category_preset(env):
    r = client.get("/api/category-preset", params={"category": "listening", "lang": "es"})
    assert r.status_code == 200
    body = r.json()
    assert body["bound"] is False
    assert body["lang"] == "es" and body["category"] == "listening"
    assert "category_overrides" in body

    # lang falls back to the deck's language
    r = client.get("/api/category-preset",
                   params={"category": "listening", "deck_id": env["es_deck"]})
    assert r.json()["lang"] == "es"


def test_api_put_category_preset_binds_and_unbinds(env):
    r = client.put("/api/category-preset",
                   json={"lang": "es", "category": "listening", "preset_id": env["preset_x"]})
    assert r.status_code == 200
    assert r.json()["bound"] is True
    assert r.json()["id"] == env["preset_x"]
    assert database.get_card(env["es_listening"])["learning_steps"] == X_STEPS

    r = client.put("/api/category-preset",
                   json={"lang": "es", "category": "listening", "preset_id": None})
    assert r.status_code == 200
    assert r.json()["bound"] is False


def test_api_category_preset_errors(env):
    r = client.get("/api/category-preset", params={"category": "bogus", "lang": "es"})
    assert r.status_code == 400
    r = client.put("/api/category-preset",
                   json={"lang": "es", "category": "bogus", "preset_id": env["preset_x"]})
    assert r.status_code == 400
    r = client.put("/api/category-preset",
                   json={"lang": "es", "category": "listening", "preset_id": 99999})
    assert r.status_code == 404


def test_api_update_preset(env):
    r = client.put(f"/api/presets/{env['preset_x']}", json={"learning_steps": "5m", "name": "Y"})
    assert r.status_code == 200
    assert r.json()["learning_steps"] == "5m"
    assert r.json()["name"] == "Y"
    assert client.put("/api/presets/99999", json={"name": "Z"}).status_code == 404

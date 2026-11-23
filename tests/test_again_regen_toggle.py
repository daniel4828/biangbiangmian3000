"""Tests for the Again → regenerate switch (issue #714).

Rating Again has always regenerated the card's sentence in the background. The
switch makes that optional; what these tests pin down is the asymmetry that
makes it safe: only the *automatic* trigger is gated. The "New sentence" button
asks for a regeneration in so many words, so a global switch must never swallow
it — that would turn a button into a no-op with no explanation on screen.
"""

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import database
import database.core
import main
from routes import review as review_routes

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # database.core.DB_PATH，不是 database.DB_PATH —— 见 conftest.py（#615）。
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path / "test.db"


@pytest.fixture
def spawns(monkeypatch):
    """Record every background regeneration instead of running one."""
    calls = []
    monkeypatch.setattr(review_routes, "_spawn_again_regen",
                        lambda card: calls.append(card.get("word_zh")))
    return calls


def _card() -> int:
    deck_id = database.get_or_create_deck("RegenDeck")
    word_id = database.insert_word({"word_zh": "开关", "definition": "switch"})
    return database.insert_card(word_id, "listening", deck_id)


# --- the setting itself -----------------------------------------------------

def test_defaults_to_on(tmp_db):
    """An untouched install must behave exactly as it did before #714."""
    assert review_routes.again_regen_enabled() is True
    body = client.get("/api/again-regen-enabled").json()
    assert body["enabled"] is True
    assert body["langs"] == {"zh": True, "es": True, "fr": True}


def test_switch_round_trips(tmp_db):
    assert client.put("/api/again-regen-enabled", json={"enabled": False}).json()["enabled"] is False
    assert review_routes.again_regen_enabled() is False
    assert client.get("/api/again-regen-enabled").json()["enabled"] is False

    client.put("/api/again-regen-enabled", json={"enabled": True})
    assert review_routes.again_regen_enabled() is True


# --- what the switch gates --------------------------------------------------

def test_again_regenerates_while_on(tmp_db, spawns):
    card_id = _card()
    r = client.post(f"/api/review?card_id={card_id}&rating=1")
    assert r.status_code == 200
    assert spawns == ["开关"]


def test_again_does_not_regenerate_while_off(tmp_db, spawns):
    client.put("/api/again-regen-enabled", json={"enabled": False})
    card_id = _card()
    r = client.post(f"/api/review?card_id={card_id}&rating=1")
    assert r.status_code == 200
    assert spawns == []


def test_ratings_other_than_again_never_regenerate(tmp_db, spawns):
    card_id = _card()
    client.post(f"/api/review?card_id={card_id}&rating=3")
    assert spawns == []


def test_new_sentence_button_ignores_the_switch(tmp_db, spawns):
    """The requeue button is an explicit request — it regenerates either way."""
    client.put("/api/again-regen-enabled", json={"enabled": False})
    card_id = _card()
    r = client.post(f"/api/review/requeue?card_id={card_id}")
    assert r.status_code == 200
    assert spawns == ["开关"]


# --- per language ------------------------------------------------------------

def test_langs_follow_global_until_overridden(tmp_db):
    client.put("/api/again-regen-enabled", json={"enabled": False})
    langs = client.get("/api/again-regen-enabled").json()["langs"]
    assert langs["zh"] is False and langs["es"] is False

    client.put("/api/again-regen-enabled", json={"enabled": True, "lang": "es"})
    assert review_routes.again_regen_enabled("es") is True
    assert review_routes.again_regen_enabled("zh") is False
    assert client.get("/api/again-regen-enabled").json()["langs"]["es"] is True


def test_unknown_lang_is_rejected(tmp_db):
    r = client.put("/api/again-regen-enabled", json={"enabled": False, "lang": "xx"})
    assert r.status_code == 400


def test_again_only_regenerates_for_enabled_language(tmp_db, spawns):
    client.put("/api/again-regen-enabled", json={"enabled": False, "lang": "zh"})
    client.put("/api/again-regen-enabled", json={"enabled": True, "lang": "es"})
    zh_card = _card()
    client.post(f"/api/review?card_id={zh_card}&rating=1")
    assert spawns == []

    client.put("/api/again-regen-enabled", json={"enabled": True, "lang": "zh"})
    client.put("/api/again-regen-enabled", json={"enabled": False, "lang": "es"})
    client.post(f"/api/review?card_id={zh_card}&rating=1")
    assert spawns == ["开关"]


def test_requeue_ignores_language_switch(tmp_db, spawns):
    client.put("/api/again-regen-enabled", json={"enabled": False, "lang": "zh"})
    card_id = _card()
    assert client.post(f"/api/review/requeue?card_id={card_id}").status_code == 200
    assert spawns == ["开关"]

"""Tests for issue #1031: home page quick search (GET /api/quick-search).

Modeled on tests/test_browse_lang.py — same fixtures, same "patch
database.core.DB_PATH, never database.DB_PATH" rule (#615).
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
import main


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _entry(word: str, lang: str, definition: str = "", definition_de: str = "") -> int:
    return database.insert_word({
        "word_zh": word, "lang": lang, "pinyin": None, "definition": definition,
        "pos": None, "hsk_level": None, "traditional": None, "definition_zh": None,
        "source": "test", "note_type": "vocabulary", "notes": None, "date_yaml": None,
        "source_sentence": None, "grammar_notes": None, "register": None,
        "definition_de": definition_de, "definition_fr": None,
    })


@pytest.fixture
def client(tmp_db):
    _entry("生态", "zh", definition="ecology", definition_de="Ökologie")
    _entry("boire", "fr", definition="to drink")
    return TestClient(main.app)


def test_quick_search_matches_german_definition(client):
    rows = client.get("/api/quick-search?q=logie").json()
    assert {r["word_zh"] for r in rows} == {"生态"}


def test_quick_search_matches_english_definition(client):
    rows = client.get("/api/quick-search?q=drink").json()
    assert {r["word_zh"] for r in rows} == {"boire"}


def test_quick_search_matches_word_zh(client):
    rows = client.get("/api/quick-search?q=生态").json()
    assert {r["word_zh"] for r in rows} == {"生态"}


def test_quick_search_filters_by_lang(client):
    rows = client.get("/api/quick-search?q=o&lang=fr").json()
    assert {r["word_zh"] for r in rows} == {"boire"}


def test_quick_search_blank_query_returns_empty(client):
    assert client.get("/api/quick-search", params={"q": "   "}).json() == []


def test_quick_search_exact_match_ranks_first(tmp_db):
    _entry("学", "zh", definition="to study")
    _entry("学习", "zh", definition="to learn")
    client = TestClient(main.app)
    rows = client.get("/api/quick-search?q=学").json()
    assert rows[0]["word_zh"] == "学"


def test_quick_search_limit(tmp_db):
    for i in range(5):
        _entry(f"词{i}", "zh", definition="test word")
    client = TestClient(main.app)
    rows = client.get("/api/quick-search?q=test&limit=2").json()
    assert len(rows) == 2


def test_quick_search_reports_has_cards(client):
    word_id = database.get_word_by_zh("生态")["id"]
    deck_id = database.get_or_create_deck("Daily", lang="zh")
    database.insert_card(word_id, "reading", deck_id)

    r = TestClient(main.app).get("/api/quick-search?q=生态").json()
    row = next(x for x in r if x["word_zh"] == "生态")
    assert row["has_cards"] is True

    r2 = TestClient(main.app).get("/api/quick-search?q=boire").json()
    row2 = next(x for x in r2 if x["word_zh"] == "boire")
    assert row2["has_cards"] is False

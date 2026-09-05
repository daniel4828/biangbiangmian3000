"""Tests for the header search box's word lookup endpoint (issue #1055).

/api/word-search returns full entry rows (not just IDs like /api/search-words)
so the header box can render results without pulling /api/browse-words.
"""

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import database
import main

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path


def _insert(word_zh, lang="zh", pinyin=None, definition=None, definition_zh=None):
    return database.insert_word({
        "word_zh": word_zh,
        "lang": lang,
        "pinyin": pinyin,
        "definition": definition,
        "definition_zh": definition_zh,
    })


def test_exact_and_prefix_matches_rank_before_definition_matches(tmp_db):
    _insert("生态", pinyin="shēngtài", definition="ecology")
    _insert("生态学", pinyin="shēngtàixué", definition="ecology (as a discipline)")
    _insert("环境", pinyin="huánjìng", definition="environment", definition_zh="与生态相关的环境")

    r = client.get("/api/word-search", params={"q": "生态"})
    assert r.status_code == 200
    words = r.json()["words"]
    assert [w["word_zh"] for w in words] == ["生态", "生态学", "环境"]


def test_lang_filter_excludes_other_languages(tmp_db):
    _insert("chat", lang="fr", pinyin=None, definition="cat")

    r = client.get("/api/word-search", params={"q": "chat", "lang": "zh"})
    assert r.status_code == 200
    assert r.json()["words"] == []

    r = client.get("/api/word-search", params={"q": "chat", "lang": "fr"})
    assert r.status_code == 200
    assert [w["word_zh"] for w in r.json()["words"]] == ["chat"]


def test_limit_is_respected(tmp_db):
    for i in range(5):
        _insert(f"生态{i}", pinyin="shēngtài", definition="ecology")

    r = client.get("/api/word-search", params={"q": "生态", "limit": 2})
    assert r.status_code == 200
    assert len(r.json()["words"]) == 2


def test_empty_query_returns_empty_list_not_an_error(tmp_db):
    _insert("生态", pinyin="shēngtài", definition="ecology")

    r = client.get("/api/word-search", params={"q": ""})
    assert r.status_code == 200
    assert r.json()["words"] == []


def test_result_rows_include_pinyin_and_definition(tmp_db):
    _insert("生态", pinyin="shēngtài", definition="ecology")

    r = client.get("/api/word-search", params={"q": "生态"})
    assert r.status_code == 200
    word = r.json()["words"][0]
    assert word["pinyin"] == "shēngtài"
    assert word["definition"] == "ecology"

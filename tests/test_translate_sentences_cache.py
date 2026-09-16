"""Persistent cache for /api/translate-sentences (#1177).

The read-along full-screen reader's 译 toggle can re-request the same block
translation many times (reopening an article, two readers hitting the same
sentence from different tracks). translator.translate_batch is stubbed
throughout — what's under test is that the cache is actually consulted and
populated, not Google Translate itself.
"""
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
import database.core
import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """database.core.DB_PATH, never database.DB_PATH — the latter is a
    wildcard-import copy and patching it silently writes to data/srs.db
    (#615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _post(texts, lang="zh", target="de"):
    return client.post("/api/translate-sentences", json={"texts": texts, "lang": lang, "target": target})


# ---------------------------------------------------------------------------
# Endpoint-level behaviour
# ---------------------------------------------------------------------------

def test_second_request_hits_cache_not_translator():
    with patch("translator.translate_batch", side_effect=lambda texts, **kw: [f"DE:{t}" for t in texts]) as stub:
        r1 = _post(["你好", "再见"])
        assert r1.status_code == 200
        assert r1.json()["translations"] == ["DE:你好", "DE:再见"]
        assert stub.call_count == 1
        assert stub.call_args.args[0] == ["你好", "再见"]

        r2 = _post(["你好", "再见"])
        assert r2.status_code == 200
        assert r2.json()["translations"] == ["DE:你好", "DE:再见"]
        # No second call at all — everything was already cached.
        assert stub.call_count == 1


def test_partial_cache_hit_only_translates_the_miss():
    with patch("translator.translate_batch", side_effect=lambda texts, **kw: [f"DE:{t}" for t in texts]) as stub:
        _post(["你好"])
        assert stub.call_count == 1

        r = _post(["你好", "新句子"])
        assert r.status_code == 200
        assert r.json()["translations"] == ["DE:你好", "DE:新句子"]
        assert stub.call_count == 2
        # Only the uncached text was sent for translation.
        assert stub.call_args.args[0] == ["新句子"]


def test_same_as_source_result_is_not_cached():
    # translate_batch's failure contract is "return the input unchanged" —
    # the endpoint must turn that into "", and must not remember it, so a
    # later successful translation is reachable.
    with patch("translator.translate_batch", side_effect=lambda texts, **kw: list(texts)):
        r = _post(["你好"])
        assert r.status_code == 200
        assert r.json()["translations"] == [""]

    assert database.get_sentence_translations(["你好"], "zh", "de") == {}

    with patch("translator.translate_batch", side_effect=lambda texts, **kw: [f"DE:{t}" for t in texts]) as stub:
        r = _post(["你好"])
        assert r.json()["translations"] == ["DE:你好"]
        assert stub.call_count == 1  # not skipped by a bogus empty-string cache entry


def test_translator_exception_with_full_cache_hit_returns_no_error():
    with patch("translator.translate_batch", side_effect=lambda texts, **kw: [f"DE:{t}" for t in texts]):
        _post(["你好"])

    with patch("translator.translate_batch", side_effect=RuntimeError("boom")) as stub:
        r = _post(["你好"])
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body
        assert body["translations"] == ["DE:你好"]
        stub.assert_not_called()  # nothing needed translating — the stub must not even run


def test_translator_exception_with_partial_miss_still_reports_error():
    with patch("translator.translate_batch", side_effect=lambda texts, **kw: [f"DE:{t}" for t in texts]):
        _post(["你好"])

    with patch("translator.translate_batch", side_effect=RuntimeError("boom")):
        r = _post(["你好", "新句子"])
        assert r.status_code == 200
        body = r.json()
        assert body["translations"] == ["DE:你好", ""]
        assert "error" in body


# ---------------------------------------------------------------------------
# database.translations unit tests
# ---------------------------------------------------------------------------

def test_save_skips_empty_translations():
    database.save_sentence_translations({"你好": "", "再见": "Auf Wiedersehen"}, "zh", "de")
    result = database.get_sentence_translations(["你好", "再见"], "zh", "de")
    assert result == {"再见": "Auf Wiedersehen"}


def test_get_returns_only_cached_texts():
    database.save_sentence_translations({"你好": "Hallo"}, "zh", "de")
    result = database.get_sentence_translations(["你好", "从没存过的句子"], "zh", "de")
    assert result == {"你好": "Hallo"}


def test_cache_is_scoped_by_lang_and_target():
    database.save_sentence_translations({"bonjour": "Hallo"}, "fr", "de")
    assert database.get_sentence_translations(["bonjour"], "zh", "de") == {}
    assert database.get_sentence_translations(["bonjour"], "fr", "en") == {}
    assert database.get_sentence_translations(["bonjour"], "fr", "de") == {"bonjour": "Hallo"}

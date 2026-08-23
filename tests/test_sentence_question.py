"""Tests for asking AI about a review sentence (issue #853).

The AI is stubbed at ai._call_api — the single choke point every provider
goes through (see tests/test_add_word.py / tests/test_dictionary.py for why
not a provider client).
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
from unittest.mock import patch

import ai
import database
import main
import routes.review

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(routes.review, "ai_disabled", lambda: False)
    return tmp_path


def test_ask_about_sentence_returns_plain_text_answer(tmp_db):
    with patch.object(ai, "_call_api", return_value="这句话没问题，很自然。"):
        r = client.post("/api/sentence-question", json={
            "sentence_zh": "他每天去公司上班。",
            "question": "为什么用去不用来？",
            "word_zh": "上班",
        })
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "这句话没问题，很自然。"


def test_empty_question_defaults_to_quality_check(tmp_db):
    """No question typed → the prompt still asks "is anything wrong", never a
    400 for a missing question (only sentence_zh is required)."""
    captured = {}

    def _fake_call_api(model, messages, max_tokens, purpose, thinking=False):
        captured["prompt"] = messages[0]["content"]
        return "句子没问题。"

    with patch.object(ai, "_call_api", side_effect=_fake_call_api):
        r = client.post("/api/sentence-question", json={"sentence_zh": "他每天去公司上班。"})
    assert r.status_code == 200, r.text
    assert "这句话有没有问题" in captured["prompt"]


def test_empty_sentence_is_rejected(tmp_db):
    r = client.post("/api/sentence-question", json={"sentence_zh": "  "})
    assert r.status_code == 400


def test_missing_sentence_field_is_rejected(tmp_db):
    r = client.post("/api/sentence-question", json={})
    assert r.status_code == 400


def test_ai_disabled_is_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(routes.review, "ai_disabled", lambda: True)
    r = client.post("/api/sentence-question", json={"sentence_zh": "他每天去公司上班。"})
    assert r.status_code == 400


def test_ai_failure_surfaces_as_500_not_silent(tmp_db):
    """A raised exception from the AI layer must not be swallowed — the whole
    point of this feature is trustworthy feedback, never a silent failure."""
    with patch.object(ai, "_call_api", side_effect=RuntimeError("provider down")):
        r = client.post("/api/sentence-question", json={"sentence_zh": "他每天去公司上班。"})
    assert r.status_code == 500
    assert "provider down" in r.json()["detail"]


# ---------------------------------------------------------------------------
# ai.ask_about_sentence() itself
# ---------------------------------------------------------------------------

def test_ask_about_sentence_prompt_includes_sentence_and_word():
    captured = {}

    def _fake_call_api(model, messages, max_tokens, purpose, thinking=False):
        captured["prompt"] = messages[0]["content"]
        captured["purpose"] = purpose
        return "answer"

    with patch.object(ai, "_call_api", side_effect=_fake_call_api):
        result = ai.ask_about_sentence("他每天去公司上班。", question="为什么？", word_zh="上班")

    assert result == "answer"
    assert "他每天去公司上班。" in captured["prompt"]
    assert "上班" in captured["prompt"]
    assert "为什么？" in captured["prompt"]
    assert captured["purpose"] == "sentence_question"


def test_ask_about_sentence_default_question_when_blank():
    captured = {}

    def _fake_call_api(model, messages, max_tokens, purpose, thinking=False):
        captured["prompt"] = messages[0]["content"]
        return "answer"

    with patch.object(ai, "_call_api", side_effect=_fake_call_api):
        ai.ask_about_sentence("他每天去公司上班。")

    assert "这句话有没有问题" in captured["prompt"]

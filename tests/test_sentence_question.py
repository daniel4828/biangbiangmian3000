"""Tests for the sentence chat (issue #853, multi-turn + editable template #1362).

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

SENTENCE = "他每天去公司上班。"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(routes.review, "ai_disabled", lambda: False)
    return tmp_path


def _capture(captured, answer="answer"):
    def _fake(model, messages, max_tokens, purpose, thinking=False):
        captured["messages"] = messages
        captured["purpose"] = purpose
        return answer
    return _fake


def _post(**body):
    body.setdefault("sentence_zh", SENTENCE)
    return client.post("/api/sentence-question", json=body)


def test_single_question_uses_default_template(tmp_db):
    cap = {}
    with patch.object(ai, "_call_api", side_effect=_capture(cap, " 好的 ")):
        r = _post(question="为什么用去？", word_zh="上班")
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "好的"
    msgs = cap["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert content.startswith("Bitte hilf mir, Chinesisch zu lernen")
    assert SENTENCE in content
    assert content.endswith("为什么用去？")
    assert "natürlich" not in content.lower()
    assert cap["purpose"] == "sentence_question"


def test_spanish_language_name(tmp_db):
    cap = {}
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        r = _post(sentence_zh="Ayer fui al cine.", question="Warum?", lang="es")
    assert r.status_code == 200, r.text
    assert "Bitte hilf mir, Spanisch zu lernen" in cap["messages"][0]["content"]


def test_multi_turn_template_only_in_first_message(tmp_db):
    cap = {}
    history = [
        {"role": "user", "content": "Frage 1"},
        {"role": "assistant", "content": "Antwort 1"},
        {"role": "user", "content": "Frage 2"},
    ]
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        r = _post(messages=history)
    assert r.status_code == 200, r.text
    msgs = cap["messages"]
    assert len(msgs) == 3
    assert msgs[0]["content"].startswith("Bitte hilf mir")
    assert msgs[0]["content"].endswith("Frage 1")
    assert msgs[1] == {"role": "assistant", "content": "Antwort 1"}
    assert msgs[2] == {"role": "user", "content": "Frage 2"}
    # caller's list is not mutated
    assert history[0]["content"] == "Frage 1"


def test_no_question_and_no_messages_is_rejected(tmp_db):
    assert _post().status_code == 400
    assert _post(question="  ", messages=[]).status_code == 400


def test_empty_sentence_is_rejected(tmp_db):
    assert _post(sentence_zh="  ", question="x").status_code == 400


def test_missing_sentence_field_is_rejected(tmp_db):
    r = client.post("/api/sentence-question", json={})
    assert r.status_code == 400


def test_ai_disabled_is_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(routes.review, "ai_disabled", lambda: True)
    assert _post(question="x").status_code == 400


def test_ai_failure_surfaces_as_500_not_silent(tmp_db):
    with patch.object(ai, "_call_api", side_effect=RuntimeError("provider down")):
        r = _post(question="x")
    assert r.status_code == 500
    assert "provider down" in r.json()["detail"]


def test_invalid_messages_are_rejected(tmp_db):
    with patch.object(ai, "_call_api", side_effect=_capture({})) as m:
        r = _post(messages=[{"role": "user", "content": "a"},
                            {"role": "assistant", "content": "b"}])
        assert r.status_code == 400
        r = _post(messages=[{"role": "system", "content": "a"}])
        assert r.status_code == 400
        r = _post(messages=[{"role": "user", "content": "  "}])
        assert r.status_code == 400
    m.assert_not_called()


# ---------------------------------------------------------------------------
# Editable template
# ---------------------------------------------------------------------------

def test_prompt_get_default(tmp_db):
    r = client.get("/api/sentence-question/prompt")
    assert r.status_code == 200
    d = r.json()
    assert d["template"] == d["default"] == ai.DEFAULT_SENTENCE_QUESTION_TEMPLATE
    assert d["is_custom"] is False
    assert d["variables"] == ["{language}", "{sentence}", "{word}"]


def test_stored_template_is_used_and_reset(tmp_db):
    r = client.put("/api/sentence-question/prompt",
                   json={"template": "Lerne {language}: {sentence} / Wort: {word}"})
    assert r.status_code == 200
    assert r.json()["is_custom"] is True
    assert client.get("/api/sentence-question/prompt").json()["is_custom"] is True

    cap = {}
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        _post(question="Q", word_zh="上班")
    assert cap["messages"][0]["content"] == f"Lerne Chinesisch: {SENTENCE} / Wort: 上班\n\nQ"

    r = client.delete("/api/sentence-question/prompt")
    assert r.status_code == 200
    assert r.json()["is_custom"] is False
    assert r.json()["template"] == ai.DEFAULT_SENTENCE_QUESTION_TEMPLATE

    cap.clear()
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        _post(question="Q")
    assert cap["messages"][0]["content"].startswith("Bitte hilf mir")


def test_put_empty_template_is_rejected(tmp_db):
    assert client.put("/api/sentence-question/prompt", json={"template": "  "}).status_code == 400
    assert client.put("/api/sentence-question/prompt", json={}).status_code == 400


def test_body_template_overrides_stored(tmp_db):
    client.put("/api/sentence-question/prompt", json={"template": "STORED {sentence}"})
    cap = {}
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        r = _post(question="Q", template="TEST {sentence}")
    assert r.status_code == 200, r.text
    assert cap["messages"][0]["content"] == f"TEST {SENTENCE}\n\nQ"
    # and nothing was saved by the test
    assert client.get("/api/sentence-question/prompt").json()["template"] == "STORED {sentence}"


def test_template_with_other_braces_does_not_crash(tmp_db):
    cap = {}
    with patch.object(ai, "_call_api", side_effect=_capture(cap)):
        r = _post(question="Q", template="{foo} {sentence} {0} {")
    assert r.status_code == 200, r.text
    assert cap["messages"][0]["content"] == f"{{foo}} {SENTENCE} {{0}} {{\n\nQ"

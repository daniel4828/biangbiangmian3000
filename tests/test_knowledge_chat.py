"""Tests for the chat-about-a-knowledge-item feature (issue #945).

The AI is stubbed at ai._call_api — the one choke point every provider goes
through (see tests/test_add_word.py for why not a provider client).
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
from unittest.mock import patch

import ai
import database
import main
import routes.knowledge

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(routes.knowledge, "ai_disabled", lambda: False)
    return tmp_path


def _episode(transcript="今天我们聊生态保护。", summary_de="<p>Ein Text über Ökologie.</p>"):
    eid = database.create_pending_episode(
        "abc123", None, "Ökologie-Folge", "2026-08-25",
        "https://example.com/ep", kind="article")
    database.update_episode(eid, transcript_zh=transcript, summary_de=summary_de,
                            status="summarized")
    return eid


def test_empty_chat_is_not_a_404(tmp_db):
    """An item nobody has asked about yet renders an empty panel, not an error."""
    eid = _episode()
    r = client.get(f"/api/knowledge/{eid}/chat")
    assert r.status_code == 200, r.text
    assert r.json()["messages"] == []


def test_ask_stores_the_turn_and_survives_a_reload(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="Es geht um Ökologie.") as call:
        r = client.post(f"/api/knowledge/{eid}/chat",
                        json={"message": "Worum geht es?", "model": "glm-4.7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "glm-4.7"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["content"] == "Es geht um Ökologie."

    # The material rides in the first user message, and no system role is used
    # (Anthropic would reject one inside `messages`).
    sent = call.call_args[0][1]
    assert [m["role"] for m in sent] == ["user"]
    assert "今天我们聊生态保护。" in sent[0]["content"]

    stored = client.get(f"/api/knowledge/{eid}/chat").json()["messages"]
    assert [m["content"] for m in stored] == ["Worum geht es?", "Es geht um Ökologie."]


def test_second_turn_replays_the_history(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="Antwort 1"):
        client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage 1"})
    with patch.object(ai, "_call_api", return_value="Antwort 2") as call:
        client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage 2"})

    sent = call.call_args[0][1]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert "Frage 1" in sent[0]["content"]          # baked into the context message
    assert sent[1]["content"] == "Antwort 1"
    assert sent[2]["content"] == "Frage 2"


def test_failed_ai_call_stores_nothing(tmp_db):
    """A half turn — question stored, no answer — is a permanent hole in the
    conversation, so a failure must leave the history exactly as it was."""
    eid = _episode()
    with patch.object(ai, "_call_api", side_effect=RuntimeError("boom")):
        r = client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})
    assert r.status_code == 500
    assert client.get(f"/api/knowledge/{eid}/chat").json()["messages"] == []


def test_empty_answer_stores_nothing(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="   "):
        r = client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})
    assert r.status_code == 500
    assert client.get(f"/api/knowledge/{eid}/chat").json()["messages"] == []


def test_summary_is_used_when_there_is_no_transcript(tmp_db):
    eid = _episode(transcript="")
    with patch.object(ai, "_call_api", return_value="ok") as call:
        client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})
    assert "Ein Text über Ökologie." in call.call_args[0][1][0]["content"]


def test_item_without_material_is_400(tmp_db):
    eid = _episode(transcript="", summary_de="")
    with patch.object(ai, "_call_api", return_value="ok") as call:
        r = client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})
    assert r.status_code == 400
    call.assert_not_called()


def test_unknown_model_falls_back_instead_of_being_sent(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="ok") as call:
        body = client.post(f"/api/knowledge/{eid}/chat",
                           json={"message": "Frage", "model": "not-a-model"}).json()
    assert body["model"] == ai.DEFAULT_MODEL
    assert call.call_args[0][0] == ai.DEFAULT_MODEL


def test_missing_item_and_empty_message_are_rejected(tmp_db):
    eid = _episode()
    assert client.get("/api/knowledge/99999/chat").status_code == 404
    assert client.post("/api/knowledge/99999/chat", json={"message": "hi"}).status_code == 404
    assert client.post(f"/api/knowledge/{eid}/chat", json={"message": "  "}).status_code == 400


def test_ai_disabled_is_400(tmp_db, monkeypatch):
    eid = _episode()
    monkeypatch.setattr(routes.knowledge, "ai_disabled", lambda: True)
    with patch.object(ai, "_call_api", return_value="ok") as call:
        r = client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})
    assert r.status_code == 400
    call.assert_not_called()


def test_clear_chat_then_404_on_the_second_clear(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="Antwort"):
        client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})

    assert client.delete(f"/api/knowledge/{eid}/chat").status_code == 200
    assert client.get(f"/api/knowledge/{eid}/chat").json()["messages"] == []
    # Nothing left to delete: report the miss instead of pretending success.
    assert client.delete(f"/api/knowledge/{eid}/chat").status_code == 404


def test_deleting_the_item_takes_the_chat_with_it(tmp_db):
    eid = _episode()
    with patch.object(ai, "_call_api", return_value="Antwort"):
        client.post(f"/api/knowledge/{eid}/chat", json={"message": "Frage"})

    conn = database.get_db()
    conn.execute("DELETE FROM podcast_episodes WHERE id = ?", (eid,))
    conn.commit()
    left = conn.execute("SELECT COUNT(*) AS n FROM knowledge_chat_messages").fetchone()["n"]
    conn.close()
    assert left == 0

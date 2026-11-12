"""Tests for the /quick dictionary chat (#1510) and the one-word-one-entry
merge safety net. The AI is stubbed at ai._call_api (see test_dictionary.py)."""
import copy
import json

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
from unittest.mock import patch

import ai
import database
import main
import routes.dictionary

client = TestClient(main.app)


def _opt(zh, de, key="a", ex="Llamo a mi madre."):
    return {"key": key, "zh": zh, "de": de, "example_zh": ex, "example_de": ex + " (de)"}


LLAMAR = {
    "input_lang": "es",
    "kind": "chinese",
    "headline": "llamar",
    "headline_de": "rufen",
    "groups": [
        {"label": "1. rufen", "options": [_opt("llamar", "rufen", ex="Te llamo.")]},
        {"label": "2. nennen", "options": [_opt("Llamar", "nennen", ex="Lo llaman Juan.")]},
        {"label": "3. anrufen", "options": [_opt("llamar ", "anrufen", ex="Llamo a mi madre.")]},
    ],
}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(routes.dictionary, "ai_disabled", lambda: False)
    return tmp_path


def _chat(payload, messages=None, lang="es"):
    body = {"messages": messages or [{"role": "user", "content": "llamar"}], "lang": lang}
    with patch.object(ai, "_call_api", return_value=json.dumps(payload, ensure_ascii=False)) as m:
        r = client.post("/api/dict/chat", json=body)
    return r, m


def test_plain_reply(tmp_db):
    r, _ = _chat({"reply": "llamo, llamaste", "entry": None, "add": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "llamo, llamaste"
    assert body["entry"] is None and body["id"] is None and body["add"] == []
    assert body["history_text"] == "llamo, llamaste"
    assert database.list_dict_queries() == []


def test_entry_is_stored_and_id_returned(tmp_db):
    r, _ = _chat({"reply": "", "entry": copy.deepcopy(LLAMAR), "add": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] is not None
    assert len(body["entry"]["groups"]) == 1
    assert "[Eintrag: llamar" in body["history_text"]
    stored = client.get(f"/api/dict/history/{body['id']}").json()
    assert stored["query"] == "llamar" and stored["lang"] == "es"


def test_add_passthrough(tmp_db):
    r, _ = _chat({"reply": "ok", "entry": None, "add": ["llamar", " ", 3, "ir"]})
    body = r.json()
    assert body["add"] == ["llamar", "ir"]
    assert "[Hinzugefügt: llamar, ir]" in body["history_text"]


def test_empty_answer_is_500_and_nothing_stored(tmp_db):
    r, _ = _chat({"reply": "", "entry": None, "add": []})
    assert r.status_code == 500
    assert database.list_dict_queries() == []


def test_bad_last_role_is_400(tmp_db):
    r, m = _chat({"reply": "x"}, messages=[{"role": "assistant", "content": "hi"}])
    assert r.status_code == 400
    m.assert_not_called()


def test_empty_last_message_is_400(tmp_db):
    r, _ = _chat({"reply": "x"}, messages=[{"role": "user", "content": "  "}])
    assert r.status_code == 400


def test_bad_lang_is_400(tmp_db):
    r, _ = _chat({"reply": "x"}, lang="ja")
    assert r.status_code == 400


def test_merge_duplicate_options():
    merged = ai._merge_duplicate_options(copy.deepcopy(LLAMAR))
    assert len(merged["groups"]) == 1
    options = merged["groups"][0]["options"]
    assert len(options) == 1
    senses = options[0]["senses"]
    assert [s["de"] for s in senses] == ["rufen", "nennen", "anrufen"]
    assert senses[1]["example_zh"] == "Lo llaman Juan."


def test_merge_leaves_sentence_alone():
    data = copy.deepcopy(LLAMAR)
    data["sentence"] = {"zh": "Llamar llamar", "de": "x"}
    merged = ai._merge_duplicate_options(data)
    assert merged["sentence"] == {"zh": "Llamar llamar", "de": "x"}


def test_prompt_contains_prior_turns(tmp_db):
    msgs = [
        {"role": "user", "content": "llamar"},
        {"role": "assistant", "content": "[Eintrag: llamar — rufen]"},
        {"role": "user", "content": "3. Person Singular Präsens?"},
    ]
    r, m = _chat({"reply": "llama", "entry": None, "add": []}, messages=msgs)
    assert r.status_code == 200
    sent = m.call_args[0][1]
    assert len(sent) == 1 and sent[0]["role"] == "user"
    prompt = sent[0]["content"]
    assert "Daniel: llamar" in prompt
    assert "Assistant: [Eintrag: llamar — rufen]" in prompt
    assert "Daniel: 3. Person Singular Präsens?" in prompt
    assert m.call_args.kwargs["purpose"] == "dictionary_chat"


def test_lookup_prompts_unchanged_shape():
    assert "Input to look up: llamar" in ai._dictionary_prompt("llamar", "es")
    assert "Input to look up: 派" in ai._dictionary_prompt("派", "zh")

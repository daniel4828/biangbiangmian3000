"""Tests for the in-app AI dictionary (issue #746).

The AI is stubbed at ai._call_api — the single choke point every provider
goes through (see tests/test_add_word.py for why not a provider client).
"""
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


GOOD_RESULT = {
    "input_lang": "de",
    "kind": "phrase",
    "headline": "派任务",
    "headline_pinyin": "pài rènwu",
    "headline_de": "jemandem eine Aufgabe geben",
    "notes": "口语里最自然的说法是 派任务",
    "groups": [
        {
            "label": "assign (Verb)",
            "options": [
                {
                    "key": "a",
                    "zh": "派",
                    "pinyin": "pài",
                    "de": "beauftragen",
                    "usage": "sehr umgangssprachlich",
                    "register": "spoken_colloquial",
                    "recommended": True,
                    "example_zh": "老师又给我派任务了。",
                    "example_pinyin": "Lǎoshī yòu gěi wǒ pài rènwu le.",
                    "example_de": "Der Lehrer hat mir schon wieder eine Aufgabe gegeben.",
                }
            ],
        }
    ],
}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(routes.dictionary, "ai_disabled", lambda: False)
    return tmp_path


def _stub_response(result=GOOD_RESULT):
    return json.dumps(result, ensure_ascii=False)


def test_lookup_saves_and_returns_full_result(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        r = client.post("/api/dict/lookup", json={"query": "jemandem eine Aufgabe geben"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "jemandem eine Aufgabe geben"
    assert body["result"]["headline"] == "派任务"
    assert body["result"]["groups"][0]["options"][0]["zh"] == "派"
    assert "id" in body and "created_at" in body


def test_history_roundtrip_and_search(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        client.post("/api/dict/lookup", json={"query": "jemandem eine Aufgabe geben"})

    other = dict(GOOD_RESULT, headline="生态")
    with patch.object(ai, "_call_api", return_value=_stub_response(other)):
        client.post("/api/dict/lookup", json={"query": "ecology"})

    items = client.get("/api/dict/history").json()["items"]
    assert len(items) == 2
    assert {i["headline"] for i in items} == {"派任务", "生态"}

    # Search matches on query OR headline.
    by_query = client.get("/api/dict/history", params={"q": "Aufgabe"}).json()["items"]
    assert len(by_query) == 1 and by_query[0]["headline"] == "派任务"

    by_headline = client.get("/api/dict/history", params={"q": "生态"}).json()["items"]
    assert len(by_headline) == 1 and by_headline[0]["query"] == "ecology"

    no_match = client.get("/api/dict/history", params={"q": "nonexistent"}).json()["items"]
    assert no_match == []


def test_history_item_full_shape(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        created = client.post("/api/dict/lookup", json={"query": "test"}).json()

    r = client.get(f"/api/dict/history/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == GOOD_RESULT
    assert body["query"] == "test"


def test_history_item_missing_is_404(tmp_db):
    assert client.get("/api/dict/history/999").status_code == 404


def test_delete_history_item(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        created = client.post("/api/dict/lookup", json={"query": "test"}).json()

    r = client.delete(f"/api/dict/history/{created['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    assert client.get(f"/api/dict/history/{created['id']}").status_code == 404


def test_delete_missing_history_item_is_404(tmp_db):
    """Deleting a nonexistent row must not pretend success (project rule:
    never fake a successful result)."""
    assert client.delete("/api/dict/history/999").status_code == 404


# ---------------------------------------------------------------------------
# Repeat button (#777): replace_id
# ---------------------------------------------------------------------------

def test_repeat_overwrites_the_same_row(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        first = client.post("/api/dict/lookup", json={"query": "anordnen"}).json()

    better = dict(GOOD_RESULT, headline="安排")
    with patch.object(ai, "_call_api", return_value=_stub_response(better)):
        again = client.post(
            "/api/dict/lookup", json={"query": "anordnen", "replace_id": first["id"]}
        )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first["id"]
    assert again.json()["result"]["headline"] == "安排"

    # One row, not two — the history keeps only the latest answer.
    items = client.get("/api/dict/history").json()["items"]
    assert len(items) == 1
    assert items[0]["headline"] == "安排"


def test_repeat_on_missing_row_is_404_and_creates_nothing(tmp_db):
    """A stale id must not silently degrade into a fresh lookup."""
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        r = client.post("/api/dict/lookup", json={"query": "test", "replace_id": 999})
    assert r.status_code == 404
    assert client.get("/api/dict/history").json()["items"] == []


def test_failed_repeat_leaves_the_previous_answer_intact(tmp_db):
    """A bad retry must never destroy the answer it was meant to improve."""
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        first = client.post("/api/dict/lookup", json={"query": "anordnen"}).json()

    with patch.object(ai, "_call_api", return_value="Sorry, I cannot help with that."):
        r = client.post(
            "/api/dict/lookup", json={"query": "anordnen", "replace_id": first["id"]}
        )
    assert r.status_code == 500

    stored = client.get(f"/api/dict/history/{first['id']}").json()
    assert stored["result"] == GOOD_RESULT


def test_empty_query_is_rejected(tmp_db):
    assert client.post("/api/dict/lookup", json={"query": "   "}).status_code == 400


def test_ai_disabled_is_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(routes.dictionary, "ai_disabled", lambda: True)
    r = client.post("/api/dict/lookup", json={"query": "test"})
    assert r.status_code == 400


def test_garbage_ai_response_fails_loudly_and_writes_nothing(tmp_db):
    """A model that answers in prose (or otherwise unparseable JSON) must
    surface as a server error, and — critically — never reach the database:
    a blank/garbage dictionary entry is worse than an error."""
    with patch.object(ai, "_call_api", return_value="Sorry, I cannot help with that."):
        r = client.post("/api/dict/lookup", json={"query": "test"})
    assert r.status_code == 500
    assert client.get("/api/dict/history").json()["items"] == []


def test_ai_response_missing_groups_is_rejected(tmp_db):
    bad = {"input_lang": "de", "kind": "phrase", "headline": "x"}
    with patch.object(ai, "_call_api", return_value=json.dumps(bad)):
        r = client.post("/api/dict/lookup", json={"query": "test"})
    assert r.status_code == 500
    assert client.get("/api/dict/history").json()["items"] == []


def test_ai_response_with_empty_zh_option_is_rejected(tmp_db):
    bad = {
        "input_lang": "de", "kind": "phrase", "headline": "x",
        "groups": [{"label": "g", "options": [{"key": "a", "zh": ""}]}],
    }
    with patch.object(ai, "_call_api", return_value=json.dumps(bad)):
        r = client.post("/api/dict/lookup", json={"query": "test"})
    assert r.status_code == 500
    assert client.get("/api/dict/history").json()["items"] == []


# ---------------------------------------------------------------------------
# ai.dictionary_lookup() itself
# ---------------------------------------------------------------------------

def test_dictionary_lookup_strips_code_fence():
    fenced = "```json\n" + json.dumps(GOOD_RESULT) + "\n```"
    with patch.object(ai, "_call_api", return_value=fenced):
        result, model = ai.dictionary_lookup("test")
    assert result["headline"] == "派任务"
    assert model == ai.DEFAULT_MODEL


def test_dictionary_lookup_raises_on_unparseable_response():
    with patch.object(ai, "_call_api", return_value="not json at all"):
        with pytest.raises(ValueError):
            ai.dictionary_lookup("test")


# ---------------------------------------------------------------------------
# /dict standalone page
# ---------------------------------------------------------------------------

def test_dict_page_is_served():
    r = client.get("/dict")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Streaming lookup (#1381): POST /api/dict/lookup-stream
# ---------------------------------------------------------------------------

def _chunks(text, n=7):
    return [text[i:i + n] for i in range(0, len(text), n)]


def _fake_stream(text):
    def fake(*args, **kwargs):
        yield from _chunks(text)
    return fake


def _events(resp):
    out = []
    for block in resp.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


def _history_count():
    return len(client.get("/api/dict/history").json()["items"])


def test_stream_emits_deltas_then_done_and_stores(tmp_db):
    full = _stub_response()
    with patch.object(ai, "_stream_api", _fake_stream(full)):
        r = client.post("/api/dict/lookup-stream", json={"query": "anordnen"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _events(r)
    deltas = [e for e in events if e["type"] == "delta"]
    assert len(deltas) > 1
    assert "".join(e["text"] for e in deltas) == full
    assert events[-1]["type"] == "done"
    record = events[-1]["record"]
    assert record["result"] == GOOD_RESULT
    assert record["query"] == "anordnen"
    stored = database.get_dict_query(record["id"])
    assert stored is not None
    assert json.loads(stored["result_json"]) == GOOD_RESULT
    assert _history_count() == 1


def test_stream_invalid_json_emits_error_and_stores_nothing(tmp_db):
    with patch.object(ai, "_stream_api", _fake_stream("Sorry, I cannot help with that.")):
        r = client.post("/api/dict/lookup-stream", json={"query": "anordnen"})
    assert r.status_code == 200
    events = _events(r)
    assert events[-1]["type"] == "error"
    assert events[-1]["detail"]
    assert not any(e["type"] == "done" for e in events)
    assert _history_count() == 0


def test_stream_provider_exception_emits_error_and_stores_nothing(tmp_db):
    def boom(*args, **kwargs):
        yield '{"headline": "x'
        raise RuntimeError("provider down")
    with patch.object(ai, "_stream_api", boom):
        r = client.post("/api/dict/lookup-stream", json={"query": "anordnen"})
    events = _events(r)
    assert events[-1] == {"type": "error", "detail": "provider down"}
    assert _history_count() == 0


def test_stream_failed_repeat_leaves_old_row_untouched(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        first = client.post("/api/dict/lookup", json={"query": "anordnen"}).json()
    with patch.object(ai, "_stream_api", _fake_stream("not json")):
        r = client.post("/api/dict/lookup-stream",
                        json={"query": "anordnen", "replace_id": first["id"]})
    assert _events(r)[-1]["type"] == "error"
    assert client.get(f"/api/dict/history/{first['id']}").json()["result"] == GOOD_RESULT


def test_stream_repeat_success_updates_same_row(tmp_db):
    with patch.object(ai, "_call_api", return_value=_stub_response()):
        first = client.post("/api/dict/lookup", json={"query": "anordnen"}).json()
    better = dict(GOOD_RESULT, headline="安排")
    with patch.object(ai, "_stream_api", _fake_stream(_stub_response(better))):
        r = client.post("/api/dict/lookup-stream",
                        json={"query": "anordnen", "replace_id": first["id"]})
    done = _events(r)[-1]
    assert done["type"] == "done"
    assert done["record"]["id"] == first["id"]
    assert done["record"]["result"]["headline"] == "安排"
    assert _history_count() == 1


def test_stream_upfront_errors_are_plain_http_errors(tmp_db):
    with patch.object(ai, "_stream_api", _fake_stream(_stub_response())):
        assert client.post("/api/dict/lookup-stream", json={"query": "  "}).status_code == 400
        assert client.post("/api/dict/lookup-stream",
                           json={"query": "x", "lang": "xx"}).status_code == 400
        assert client.post("/api/dict/lookup-stream",
                           json={"query": "x", "replace_id": 999}).status_code == 404
    assert _history_count() == 0


def test_stream_ai_disabled_is_400(tmp_db, monkeypatch):
    monkeypatch.setattr(routes.dictionary, "ai_disabled", lambda: True)
    assert client.post("/api/dict/lookup-stream", json={"query": "x"}).status_code == 400


def test_parse_dictionary_response_matches_lookup_validation():
    assert ai.parse_dictionary_response(json.dumps(GOOD_RESULT))["headline"] == "派任务"
    with pytest.raises(ValueError):
        ai.parse_dictionary_response("nope")
    with pytest.raises(ValueError):
        ai.parse_dictionary_response(json.dumps({"headline": "x"}))

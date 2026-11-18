"""Quick panel chat history (#1584)."""
import pytest
from fastapi.testclient import TestClient

import database
import main

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path


def _turn(user, reply="ok"):
    return {"user": user, "reply": reply, "entry": None, "entry_id": None,
            "add": [], "history_text": reply, "lang": "es"}


def test_create_list_get_roundtrip(tmp_db):
    r = client.put("/api/quick-chats", json={"lang": "es", "turns": [_turn("hola")]})
    assert r.status_code == 200
    cid = r.json()["id"]
    items = client.get("/api/quick-chats").json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == cid and items[0]["title"] == "hola"
    assert items[0]["turn_count"] == 1 and "turns" not in items[0]
    chat = client.get(f"/api/quick-chats/{cid}").json()
    assert chat["lang"] == "es"
    assert chat["turns"][0]["user"] == "hola"


def test_update_replaces_turns(tmp_db):
    cid = client.put("/api/quick-chats", json={"lang": "es", "turns": [_turn("uno")]}).json()["id"]
    r = client.put("/api/quick-chats",
                   json={"id": cid, "lang": "fr", "turns": [_turn("uno"), _turn("dos")]})
    assert r.status_code == 200 and r.json()["id"] == cid
    chat = client.get(f"/api/quick-chats/{cid}").json()
    assert [t["user"] for t in chat["turns"]] == ["uno", "dos"]
    assert chat["lang"] == "fr"
    assert len(client.get("/api/quick-chats").json()["items"]) == 1


def test_put_missing_id_is_404(tmp_db):
    r = client.put("/api/quick-chats", json={"id": 999, "lang": "es", "turns": [_turn("x")]})
    assert r.status_code == 404


def test_empty_turns_is_400(tmp_db):
    assert client.put("/api/quick-chats", json={"lang": "es", "turns": []}).status_code == 400


def test_bad_lang_is_400(tmp_db):
    r = client.put("/api/quick-chats", json={"lang": "xx", "turns": [_turn("x")]})
    assert r.status_code == 400


def test_title_derived_and_truncated(tmp_db):
    long = "a" * 200
    cid = client.put("/api/quick-chats", json={"lang": "es", "turns": [_turn(long)]}).json()["id"]
    assert client.get(f"/api/quick-chats/{cid}").json()["title"] == "a" * 80


def test_delete_then_404(tmp_db):
    cid = client.put("/api/quick-chats", json={"lang": "es", "turns": [_turn("x")]}).json()["id"]
    assert client.delete(f"/api/quick-chats/{cid}").json() == {"ok": True}
    assert client.delete(f"/api/quick-chats/{cid}").status_code == 404
    assert client.get(f"/api/quick-chats/{cid}").status_code == 404


def test_get_missing_is_404(tmp_db):
    assert client.get("/api/quick-chats/12345").status_code == 404

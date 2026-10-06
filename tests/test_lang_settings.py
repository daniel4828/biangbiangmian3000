"""Tests for issue #1252: languages can be switched off in Settings (French is
off by default). Isolation: patch database.core.DB_PATH (see #615)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import database
import main

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "t.db"))
    database.init_db()
    # Root-level decks don't count as "in use" (get_available_langs skips
    # parent_id IS NULL), so nest it like the real Français tree.
    root = database.get_or_create_deck("Français", lang="fr")
    database.get_or_create_deck("Saved", parent_id=root, lang="fr")
    return tmp_path


def test_default_disables_french(tmp_db):
    assert database.get_disabled_langs() == ["fr"]
    assert "fr" not in client.get("/api/langs").json()
    s = client.get("/api/lang-settings").json()
    assert "fr" in s["in_use"]
    fr = next(l for l in s["langs"] if l["code"] == "fr")
    assert fr["enabled"] is False and fr["in_use"] is True


def test_enable_and_disable_roundtrip(tmp_db):
    r = client.put("/api/lang-settings", json={"lang": "fr", "enabled": True})
    assert r.status_code == 200
    assert "fr" in client.get("/api/langs").json()
    r = client.put("/api/lang-settings", json={"lang": "fr", "enabled": False})
    assert r.status_code == 200
    assert "fr" not in client.get("/api/langs").json()


def test_zh_cannot_be_disabled_and_unknown_rejected(tmp_db):
    assert client.put("/api/lang-settings",
                      json={"lang": "zh", "enabled": False}).status_code == 400
    assert client.put("/api/lang-settings",
                      json={"lang": "xx", "enabled": True}).status_code == 400


def test_available_unaffected(tmp_db):
    assert "fr" in client.get("/api/langs?available=1").json()

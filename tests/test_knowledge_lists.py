"""Tests for issue #940 (umbrella #934): Read Later, custom lists, archiving.

Ingesting and reading are two different moments — material is fetched and
summarized long before Daniel sits down with it — and until now nothing
recorded "I still want to read this" or "I'm done with it".

The rules pinned down here:

  * The built-in Read Later list can be renamed but never deleted: the swipe
    gesture has nowhere to put things without it, and it finds it by
    is_builtin, not by name.
  * Adding to a list is idempotent; removing something that isn't there is a
    404 rather than a pretend success.
  * Archived material drops out of the default list view and comes back with
    the filter toggle.

Isolation: monkeypatch database.core.DB_PATH, never database.DB_PATH (#615).
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


@pytest.fixture
def client(tmp_db):
    return TestClient(main.app)


@pytest.fixture
def episode(tmp_db):
    return database.create_pending_episode(
        video_id="e1", channel_id=None, title="Ein Artikel",
        published_at=None, youtube_url="https://example.com/a", kind="article")


def _read_later_id():
    return database.get_builtin_list()["id"]


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

def test_read_later_is_there_from_the_start(client):
    lists = client.get("/api/knowledge/lists").json()
    assert [l["name"] for l in lists] == ["Read Later"]
    assert lists[0]["is_builtin"] == 1


def test_create_rename_and_delete_a_list(client):
    created = client.post("/api/knowledge/lists", json={"name": "Recherche", "icon": "🔍"}).json()
    assert created["name"] == "Recherche"

    renamed = client.put(f"/api/knowledge/lists/{created['id']}",
                         json={"name": "Archivrecherche"}).json()
    assert renamed["name"] == "Archivrecherche"

    assert client.delete(f"/api/knowledge/lists/{created['id']}").status_code == 200
    assert [l["name"] for l in client.get("/api/knowledge/lists").json()] == ["Read Later"]


def test_duplicate_list_name_is_409(client):
    client.post("/api/knowledge/lists", json={"name": "Recherche"})
    assert client.post("/api/knowledge/lists", json={"name": "Recherche"}).status_code == 409


def test_empty_list_name_is_400(client):
    assert client.post("/api/knowledge/lists", json={"name": "   "}).status_code == 400


def test_builtin_list_can_be_renamed_but_not_deleted(client):
    list_id = _read_later_id()
    assert client.put(f"/api/knowledge/lists/{list_id}", json={"name": "Später lesen"}).status_code == 200
    resp = client.delete(f"/api/knowledge/lists/{list_id}")
    assert resp.status_code == 400
    assert database.get_builtin_list() is not None


def test_unknown_list_operations_are_404(client):
    assert client.put("/api/knowledge/lists/9999", json={"name": "X"}).status_code == 404
    assert client.delete("/api/knowledge/lists/9999").status_code == 404
    assert client.post("/api/knowledge/lists/9999/items", json={"episode_id": 1}).status_code == 404


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def test_add_and_remove_membership(client, episode):
    list_id = _read_later_id()
    assert client.post(f"/api/knowledge/lists/{list_id}/items",
                       json={"episode_id": episode}).status_code == 200
    assert database.list_episode_ids(list_id) == [episode]
    assert client.delete(f"/api/knowledge/lists/{list_id}/items/{episode}").status_code == 200
    assert database.list_episode_ids(list_id) == []


def test_adding_twice_is_idempotent(client, episode):
    list_id = _read_later_id()
    client.post(f"/api/knowledge/lists/{list_id}/items", json={"episode_id": episode})
    client.post(f"/api/knowledge/lists/{list_id}/items", json={"episode_id": episode})
    assert database.list_episode_ids(list_id) == [episode]


def test_removing_something_that_is_not_there_is_404(client, episode):
    """Not a pretend success — the UI would otherwise show an undo strip for a
    change that never happened."""
    list_id = _read_later_id()
    assert client.delete(f"/api/knowledge/lists/{list_id}/items/{episode}").status_code == 404


def test_adding_an_unknown_episode_is_404(client):
    assert client.post(f"/api/knowledge/lists/{_read_later_id()}/items",
                       json={"episode_id": 9999}).status_code == 404


def test_membership_comes_back_with_the_episode(client, episode):
    list_id = _read_later_id()
    client.post(f"/api/knowledge/lists/{list_id}/items", json={"episode_id": episode})
    row = client.get("/api/podcast/episodes").json()[0]
    assert row["list_ids"] == [list_id]


def test_list_filter_on_the_material_endpoint(client, episode):
    other = database.create_pending_episode(
        video_id="e2", channel_id=None, title="Anderer", published_at=None,
        youtube_url="https://example.com/b", kind="article")
    list_id = _read_later_id()
    client.post(f"/api/knowledge/lists/{list_id}/items", json={"episode_id": episode})

    rows = client.get(f"/api/podcast/episodes?list_id={list_id}").json()
    assert [r["id"] for r in rows] == [episode]
    assert other not in [r["id"] for r in rows]


def test_deleting_a_list_keeps_the_material(client, episode):
    created = client.post("/api/knowledge/lists", json={"name": "Recherche"}).json()
    client.post(f"/api/knowledge/lists/{created['id']}/items", json={"episode_id": episode})
    client.delete(f"/api/knowledge/lists/{created['id']}")
    assert database.get_episode(episode) is not None


# ---------------------------------------------------------------------------
# Archiving
# ---------------------------------------------------------------------------

def test_archive_hides_from_the_default_view(client, episode):
    assert client.post(f"/api/podcast/episodes/{episode}/archive").status_code == 200
    assert client.get("/api/podcast/episodes").json() == []
    shown = client.get("/api/podcast/episodes?include_archived=true").json()
    assert [r["id"] for r in shown] == [episode]
    assert shown[0]["archived_at"] is not None


def test_unarchive_brings_it_back(client, episode):
    client.post(f"/api/podcast/episodes/{episode}/archive")
    client.post(f"/api/podcast/episodes/{episode}/archive?archived=false")
    assert [r["id"] for r in client.get("/api/podcast/episodes").json()] == [episode]
    assert database.get_episode(episode)["archived_at"] is None


def test_archive_unknown_episode_is_404(client):
    assert client.post("/api/podcast/episodes/9999/archive").status_code == 404


def test_archived_count_shows_up_in_the_facets(client, episode):
    client.post(f"/api/podcast/episodes/{episode}/archive")
    assert client.get("/api/knowledge/facets").json()["archived_count"] == 1


# ---------------------------------------------------------------------------
# The frontend contract the swipe gesture depends on
# ---------------------------------------------------------------------------

APP_JS = None


def _app_js():
    global APP_JS
    if APP_JS is None:
        import pathlib
        APP_JS = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    return APP_JS


def test_swipe_locks_the_axis_before_it_takes_over():
    """Without an axis lock a vertical drag that starts with a pixel of
    horizontal noise is eaten as a swipe, and the page can no longer be
    scrolled past the list."""
    src = _app_js()
    assert "_SWIPE_DECIDE" in src
    assert "_swipe.axis" in src


def test_swipe_listeners_are_delegated_and_bound_once():
    """The list's innerHTML is replaced on every filter change, so per-row
    listeners would have to be re-attached forever."""
    src = _app_js()
    assert "dataset.swipeBound" in src
    assert src.count("addEventListener('touchstart'") == 1


def test_desktop_keeps_the_same_two_actions():
    """No touch screen must not mean no feature."""
    assert "knowledge-hover-actions" in _app_js()

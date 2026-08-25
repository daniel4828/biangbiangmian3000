"""Tests for issue #937 (umbrella #934): hand-editing an item's metadata.

Title/author/platform/published date arrive from RSS, yt-dlp or an AI
extraction, and all three get them wrong regularly — an article's "title" is
often the site's nav text, a Reel's is the first line of its description. This
is the escape hatch, and it comes with one rule the AI paths must obey:

  * Everything Daniel writes here lands in podcast_episodes.manual_fields, and
    no later AI pass may overwrite a field listed there. He was looking at the
    source; the model is guessing.
  * Tags saved here are source='user', so re-tagging (#938) can neither remove
    them nor be removed by them.

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
        video_id="e1", channel_id="https://feed.example/rss",
        title="Nav-Text der Website", published_at=None,
        youtube_url="https://example.com/a", kind="article", platform="web")


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_patch_updates_the_fields_it_is_given(client, episode):
    resp = client.patch(f"/api/podcast/episodes/{episode}", json={
        "title": "Der echte Titel", "author": "Jan Böhmermann",
        "platform": "paste", "published_at": "2026-08-01",
    })
    assert resp.status_code == 200
    row = database.get_episode(episode)
    assert row["title"] == "Der echte Titel"
    assert row["author"] == "Jan Böhmermann"
    assert row["platform"] == "paste"
    assert row["published_at"] == "2026-08-01"


def test_patch_leaves_absent_fields_alone(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}", json={"author": "Bach"})
    row = database.get_episode(episode)
    assert row["author"] == "Bach"
    assert row["title"] == "Nav-Text der Website"


def test_empty_string_clears_a_field(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}", json={"author": "Bach"})
    client.patch(f"/api/podcast/episodes/{episode}", json={"author": ""})
    assert database.get_episode(episode)["author"] is None


def test_bad_published_at_is_rejected(client, episode):
    resp = client.patch(f"/api/podcast/episodes/{episode}",
                        json={"published_at": "letzten Mittwoch"})
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.json()["detail"]
    # ... and nothing was written.
    assert database.get_episode(episode)["published_at"] is None


def test_unknown_episode_is_404(client):
    assert client.patch("/api/podcast/episodes/9999", json={"title": "x"}).status_code == 404


def test_unknown_field_is_ignored_not_fatal(client, episode):
    """A stray key is a frontend bug — it must not cost Daniel the edit that
    came with it."""
    resp = client.patch(f"/api/podcast/episodes/{episode}",
                        json={"title": "Guter Titel", "status": "summarized"})
    assert resp.status_code == 200
    row = database.get_episode(episode)
    assert row["title"] == "Guter Titel"
    assert row["status"] == "pending"   # not editable, not touched


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_patch_sets_user_tags(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}", json={"tags": ["Politik", "KI"]})
    tags = {t["name"]: t["source"] for t in database.item_tags(episode)}
    assert tags == {"Politik": "user", "KI": "user"}


def test_user_tags_survive_re_tagging(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}", json={"tags": ["Lieblingsfolge"]})
    database.set_item_tags(episode, ["Wirtschaft"], source="ai")
    names = {t["name"] for t in database.item_tags(episode)}
    assert names == {"Lieblingsfolge", "Wirtschaft"}


def test_editing_tags_does_not_delete_the_ai_ones(client, episode):
    database.set_item_tags(episode, ["Wirtschaft"], source="ai")
    client.patch(f"/api/podcast/episodes/{episode}", json={"tags": ["Lieblingsfolge"]})
    names = {t["name"] for t in database.item_tags(episode)}
    assert names == {"Lieblingsfolge", "Wirtschaft"}


def test_absent_tags_key_leaves_tags_alone(client, episode):
    database.set_item_tags(episode, ["Politik"], source="user")
    client.patch(f"/api/podcast/episodes/{episode}", json={"title": "Neu"})
    assert [t["name"] for t in database.item_tags(episode)] == ["Politik"]


def test_empty_tag_list_clears_the_user_tags(client, episode):
    database.set_item_tags(episode, ["Politik"], source="user")
    client.patch(f"/api/podcast/episodes/{episode}", json={"tags": []})
    assert database.item_tags(episode) == []


# ---------------------------------------------------------------------------
# manual_fields — the protection the AI paths read
# ---------------------------------------------------------------------------

def test_edited_fields_are_recorded_as_manual(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}",
                 json={"title": "Meiner", "author": "Bach"})
    assert database.manual_fields(episode) == {"title", "author"}
    assert database.is_manual(episode, "title") is True
    assert database.is_manual(episode, "platform") is False


def test_manual_fields_accumulate_across_edits(client, episode):
    client.patch(f"/api/podcast/episodes/{episode}", json={"title": "Meiner"})
    client.patch(f"/api/podcast/episodes/{episode}", json={"author": "Bach"})
    assert database.manual_fields(episode) == {"title", "author"}


def test_ai_source_never_overwrites_a_manual_field(tmp_db, episode):
    database.update_episode_metadata(episode, {"title": "Meiner"}, source="user")
    database.update_episode_metadata(
        episode, {"title": "Vom Modell geraten", "author": "Vom Modell geraten"}, source="ai")
    row = database.get_episode(episode)
    assert row["title"] == "Meiner"          # claimed — untouched
    assert row["author"] == "Vom Modell geraten"  # never claimed — filled in


def test_ai_source_does_not_claim_fields(tmp_db, episode):
    database.update_episode_metadata(episode, {"author": "Modell"}, source="ai")
    assert database.manual_fields(episode) == set()


def test_corrupt_manual_fields_does_not_break_editing(tmp_db, episode):
    """A broken marker must not make the item uneditable — worst case one AI
    pass overwrites one field."""
    conn = database.get_db()
    conn.execute("UPDATE podcast_episodes SET manual_fields = 'not json' WHERE id = ?", (episode,))
    conn.commit()
    conn.close()
    assert database.manual_fields(episode) == set()
    assert database.update_episode_metadata(episode, {"title": "Neu"}) is not None


def test_update_metadata_on_unknown_episode_returns_none(tmp_db):
    assert database.update_episode_metadata(9999, {"title": "x"}) is None


def test_regenerate_summary_does_not_replace_a_hand_edited_title(tmp_db, monkeypatch):
    """The concrete AI path #937 has to fence off: podcast.regenerate_summary's
    placeholder-title replacement (#781)."""
    import ai
    import podcast

    episode_id = database.create_pending_episode(
        video_id="reel1", channel_id=None, title="Video by thefreepress",
        published_at=None, youtube_url="https://instagram.com/reel/x", kind="video")
    database.update_episode(episode_id, status="summarized", transcript_zh="一些转录文本" * 40)
    # Daniel renames it — to something that still looks like a placeholder.
    database.update_episode_metadata(episode_id, {"title": "Video by thefreepress"})

    monkeypatch.setattr(podcast, "summarize", lambda *a, **kw: {
        "summary_de": "Zusammenfassung.", "summary_zh": "总结。",
        "words": [], "title_suggestion": "Ein KI-Titel"})
    monkeypatch.setattr(podcast, "filter_new_words", lambda words: words)
    monkeypatch.setattr(ai, "translate_title", lambda t: None)

    podcast.regenerate_summary(episode_id)
    assert database.get_episode(episode_id)["title"] == "Video by thefreepress"

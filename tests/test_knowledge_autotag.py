"""Tests for issue #938 (umbrella #934): AI topic tags on knowledge material.

Tags only become useful once they're on everything, and hand-tagging a dozen
items a day isn't going to happen — so the summarize step proposes them. Two
things have to hold for that to be safe:

  1. It is a convenience, not a requirement. Any failure (no summary, API
     error, garbage reply) leaves the item summarized and stored, just without
     tags — the same contract as ai.translate_title / extract_article_metadata.
  2. It may never touch a tag Daniel typed himself (#937).

And one thing has to hold for it to stay useful: the model is shown the
library's existing vocabulary and told to reuse it, otherwise it invents
'Klima' next to the 'Klimawandel' that is already there and the filter bar
degrades into near-duplicates.

AI is stubbed at ai._call_api, never at a provider client (#615).
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ai
import database
import main
import podcast


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def client(tmp_db):
    return TestClient(main.app)


@pytest.fixture
def episode(tmp_db):
    episode_id = database.create_pending_episode(
        video_id="e1", channel_id=None, title="Ein Artikel",
        published_at=None, youtube_url="https://example.com/a", kind="article")
    database.update_episode(episode_id, status="summarized",
                            summary_de="<p>Es geht um Klimapolitik in Europa.</p>")
    return episode_id


def _stub_ai(monkeypatch, reply, capture=None):
    def fake_call(model, messages, max_tokens, **kwargs):
        if capture is not None:
            capture.append(messages[-1]["content"])
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(ai, "_call_api", fake_call)


# ---------------------------------------------------------------------------
# ai.extract_knowledge_tags
# ---------------------------------------------------------------------------

def test_parses_a_json_array(monkeypatch):
    _stub_ai(monkeypatch, '["Politik", "Klimawandel", "Europa"]')
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == ["Politik", "Klimawandel", "Europa"]


def test_tolerates_markdown_fences_and_chatter(monkeypatch):
    _stub_ai(monkeypatch, 'Hier sind die Tags:\n```json\n["Politik"]\n```')
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == ["Politik"]


def test_existing_vocabulary_goes_into_the_prompt(monkeypatch):
    """The one thing keeping the tag list from degrading into near-synonyms."""
    prompts = []
    _stub_ai(monkeypatch, '["Klimawandel"]', capture=prompts)
    ai.extract_knowledge_tags("T", "Zusammenfassung", ["Klimawandel", "Politik"])
    assert "Klimawandel, Politik" in prompts[0]


def test_no_summary_means_no_ai_call(monkeypatch):
    called = []
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: called.append(1) or "[]")
    assert ai.extract_knowledge_tags("T", "   ") == []
    assert not called


@pytest.mark.parametrize("reply", ['not json at all', '', '"Politik"', '{"a": 1}'])
def test_unparseable_replies_return_empty(monkeypatch, reply):
    _stub_ai(monkeypatch, reply)
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == []


def test_array_is_recovered_from_a_wrapper_object(monkeypatch):
    """Models like answering {"tags": [...]} however plainly the prompt asks
    for a bare array. Taking the array out of it beats throwing the answer
    away — the tags are right there."""
    _stub_ai(monkeypatch, '{"tags": ["Politik", "Europa"]}')
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == ["Politik", "Europa"]


def test_api_error_returns_empty(monkeypatch):
    _stub_ai(monkeypatch, RuntimeError("provider down"))
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == []


def test_junk_entries_are_dropped(monkeypatch):
    """A sentence-length 'tag' is the model answering the wrong question — and
    it would wreck every list row it lands on."""
    _stub_ai(monkeypatch, '["Politik", 42, "", "  ", "#Klima", "' + "x" * 80 + '", "politik"]')
    assert ai.extract_knowledge_tags("T", "Zusammenfassung") == ["Politik", "Klima"]


def test_at_most_six_tags(monkeypatch):
    _stub_ai(monkeypatch, '["a","b","c","d","e","f","g","h"]')
    assert len(ai.extract_knowledge_tags("T", "Zusammenfassung")) == 6


# ---------------------------------------------------------------------------
# podcast.autotag_episode
# ---------------------------------------------------------------------------

def test_autotag_writes_ai_tags(tmp_db, episode, monkeypatch):
    _stub_ai(monkeypatch, '["Politik", "Klimawandel"]')
    podcast.autotag_episode(episode, "Ein Artikel", "Zusammenfassung")
    tags = {t["name"]: t["source"] for t in database.item_tags(episode)}
    assert tags == {"Politik": "ai", "Klimawandel": "ai"}


def test_autotag_skips_an_item_that_already_has_ai_tags(tmp_db, episode, monkeypatch):
    """Re-tagging on every summarize would spend money to churn the same six
    words, and would fight a list Daniel has already looked at."""
    database.set_item_tags(episode, ["Alt"], source="ai")
    calls = []
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: calls.append(1) or '["Neu"]')
    podcast.autotag_episode(episode, "T", "Zusammenfassung")
    assert not calls
    assert [t["name"] for t in database.item_tags(episode)] == ["Alt"]


def test_force_retags(tmp_db, episode, monkeypatch):
    database.set_item_tags(episode, ["Alt"], source="ai")
    _stub_ai(monkeypatch, '["Neu"]')
    podcast.autotag_episode(episode, "T", "Zusammenfassung", force=True)
    assert [t["name"] for t in database.item_tags(episode)] == ["Neu"]


def test_autotag_never_touches_hand_typed_tags(tmp_db, episode, monkeypatch):
    database.set_item_tags(episode, ["Lieblingsfolge"], source="user")
    _stub_ai(monkeypatch, '["Politik"]')
    podcast.autotag_episode(episode, "T", "Zusammenfassung", force=True)
    tags = {t["name"]: t["source"] for t in database.item_tags(episode)}
    assert tags == {"Lieblingsfolge": "user", "Politik": "ai"}


def test_autotag_failure_is_swallowed(tmp_db, episode, monkeypatch):
    _stub_ai(monkeypatch, RuntimeError("provider down"))
    assert podcast.autotag_episode(episode, "T", "Zusammenfassung") == []
    assert database.item_tags(episode) == []


def test_autotag_survives_a_database_error(tmp_db, episode, monkeypatch):
    """Nothing here may turn a successfully summarized item into a failure."""
    monkeypatch.setattr(database, "list_tags", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert podcast.autotag_episode(episode, "T", "Zusammenfassung") == []


# ---------------------------------------------------------------------------
# HTTP: retag + tag management
# ---------------------------------------------------------------------------

def test_retag_endpoint(client, episode, monkeypatch):
    database.set_item_tags(episode, ["Alt"], source="ai")
    database.set_item_tags(episode, ["Meiner"], source="user")
    _stub_ai(monkeypatch, '["Neu"]')

    resp = client.post(f"/api/podcast/episodes/{episode}/retag")
    assert resp.status_code == 200
    tags = {t["name"]: t["source"] for t in resp.json()["tags"]}
    assert tags == {"Meiner": "user", "Neu": "ai"}


def test_retag_unknown_episode_is_404(client):
    assert client.post("/api/podcast/episodes/9999/retag").status_code == 404


def test_retag_without_a_summary_is_400(client, tmp_db):
    episode_id = database.create_pending_episode(
        video_id="x", channel_id=None, title="T", published_at=None,
        youtube_url="https://e/x", kind="article")
    resp = client.post(f"/api/podcast/episodes/{episode_id}/retag")
    assert resp.status_code == 400


def test_tag_rename_merges(client, tmp_db):
    a = database.create_pending_episode(video_id="a", channel_id=None, title="A",
                                        published_at=None, youtube_url="https://e/a")
    b = database.create_pending_episode(video_id="b", channel_id=None, title="B",
                                        published_at=None, youtube_url="https://e/b")
    database.set_item_tags(a, ["KI"], source="user")
    database.set_item_tags(b, ["AI"], source="user")
    ai_tag = next(t for t in database.list_tags() if t["name"] == "AI")

    resp = client.put(f"/api/knowledge/tags/{ai_tag['id']}", json={"name": "KI"})
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()] == ["KI"]
    assert [t["name"] for t in database.item_tags(b)] == ["KI"]


def test_tag_rename_unknown_is_404(client):
    assert client.put("/api/knowledge/tags/9999", json={"name": "X"}).status_code == 404


def test_tag_rename_empty_is_400(client, tmp_db):
    tag_id = database.get_or_create_tag("Politik")
    assert client.put(f"/api/knowledge/tags/{tag_id}", json={"name": "  "}).status_code == 400


def test_tag_delete(client, tmp_db):
    tag_id = database.get_or_create_tag("Politik")
    assert client.delete(f"/api/knowledge/tags/{tag_id}").status_code == 200
    assert client.delete(f"/api/knowledge/tags/{tag_id}").status_code == 404

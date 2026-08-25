"""Tests for issue #935 (umbrella #934): the knowledge base's metadata layer.

#935 adds the columns and tables everything else in the overhaul stands on:
processed_at / author / platform / manual_fields / archived_at on
podcast_episodes, plus knowledge_tags, knowledge_item_tags, knowledge_lists
and knowledge_list_items.

The rules worth defending here (they are what later stages assume):

  1. Tag names are unique case-insensitively — near-duplicate tags would make
     the filter bar useless.
  2. knowledge_item_tags.source keeps the AI tagger (#938) and Daniel's own
     edits (#937) apart. Neither may ever delete the other's work.
  3. Deleting an episode takes its tags and list memberships with it.
  4. The init_db() backfill is idempotent: production re-runs it every ~2
     minutes (deploy/deploy.sh), so a second pass must change nothing.
  5. The built-in Read Later list cannot be deleted, and is not recreated
     under its old name after a rename.

Isolation follows the house pattern: monkeypatch database.core.DB_PATH, never
database.DB_PATH — the latter is only a wildcard-import copy (#615).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _episode(video_id: str = "ep1", **kwargs) -> int:
    params = dict(
        video_id=video_id,
        channel_id="https://feed.example/rss",
        title="Titel",
        published_at="2026-08-01",
        youtube_url="https://example.com/ep1",
    )
    params.update(kwargs)
    return database.create_pending_episode(**params)


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def test_new_columns_exist_and_default_to_null(tmp_db):
    ep = database.get_episode(_episode())
    for col in ("processed_at", "author", "platform", "manual_fields", "archived_at"):
        assert col in ep, f"{col} missing from the episode row"
    assert ep["processed_at"] is None
    assert ep["archived_at"] is None


def test_author_and_platform_are_stored_and_listed(tmp_db):
    _episode(author="Lage der Nation", platform="podcast")
    (row,) = database.list_episodes()
    assert row["author"] == "Lage der Nation"
    assert row["platform"] == "podcast"


def test_author_is_separate_from_channel_id(tmp_db):
    """The whole reason for the new column: channel_id already means four
    different things (feed URL / channel id / domain / pasted author), so it
    can't double as the thing an author filter reads."""
    ep = database.get_episode(_episode(author="Jan Böhmermann"))
    assert ep["channel_id"] == "https://feed.example/rss"
    assert ep["author"] == "Jan Böhmermann"


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_tag_names_are_case_insensitively_unique(tmp_db):
    first = database.get_or_create_tag("Politik")
    assert database.get_or_create_tag("politik") == first
    assert database.get_or_create_tag("  POLITIK  ") == first
    assert len(database.list_tags()) == 1


def test_empty_tag_name_is_rejected(tmp_db):
    with pytest.raises(ValueError):
        database.get_or_create_tag("   ")


def test_set_item_tags_replaces_only_its_own_source(tmp_db):
    ep = _episode()
    database.set_item_tags(ep, ["KI", "Politik"], source="ai")
    database.set_item_tags(ep, ["Lieblingsfolge"], source="user")

    # The AI runs again and proposes something else entirely.
    database.set_item_tags(ep, ["Wirtschaft"], source="ai")

    names = {t["name"]: t["source"] for t in database.item_tags(ep)}
    assert names == {"Lieblingsfolge": "user", "Wirtschaft": "ai"}


def test_user_tagging_upgrades_an_ai_tag_it_duplicates(tmp_db):
    """Typing a tag the AI had guessed means Daniel owns it now — a later
    re-tag must not be able to remove it."""
    ep = _episode()
    database.set_item_tags(ep, ["KI"], source="ai")
    database.add_item_tag(ep, "ki", source="user")

    database.set_item_tags(ep, [], source="ai")
    assert [t["name"] for t in database.item_tags(ep)] == ["KI"]


def test_set_item_tags_deduplicates_case_insensitively(tmp_db):
    ep = _episode()
    database.set_item_tags(ep, ["Politik", "politik", "  Politik "], source="user")
    assert len(database.item_tags(ep)) == 1


def test_rename_tag_merges_into_an_existing_one(tmp_db):
    ep_a, ep_b = _episode("a"), _episode("b")
    ki = database.get_or_create_tag("KI")
    ai_tag = database.get_or_create_tag("AI")
    database.add_item_tag(ep_a, "KI")
    database.add_item_tag(ep_b, "AI")
    database.add_item_tag(ep_b, "KI")  # already carries the merge target

    assert database.rename_tag(ai_tag, "KI") is True

    assert [t["name"] for t in database.list_tags()] == ["KI"]
    assert [t["id"] for t in database.item_tags(ep_b)] == [ki]


def test_rename_tag_on_unknown_id_returns_false(tmp_db):
    assert database.rename_tag(9999, "Neu") is False


def test_delete_tag_reports_whether_it_existed(tmp_db):
    tag_id = database.get_or_create_tag("Weg damit")
    assert database.delete_tag(tag_id) is True
    assert database.delete_tag(tag_id) is False


def test_tags_come_back_with_the_episode_list_in_bulk(tmp_db):
    ep_a, ep_b = _episode("a"), _episode("b")
    database.set_item_tags(ep_a, ["Politik"], source="user")
    by_id = {e["id"]: e for e in database.list_episodes()}
    assert [t["name"] for t in by_id[ep_a]["tags"]] == ["Politik"]
    assert by_id[ep_b]["tags"] == []


def test_deleting_an_episode_removes_its_tags_and_list_rows(tmp_db):
    ep = _episode()
    database.set_item_tags(ep, ["Politik"], source="user")
    read_later = database.get_builtin_list()
    database.add_to_list(read_later["id"], ep)

    conn = database.get_db()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM podcast_episodes WHERE id = ?", (ep,))
    conn.commit()
    left_tags = conn.execute(
        "SELECT COUNT(*) c FROM knowledge_item_tags WHERE episode_id = ?", (ep,)).fetchone()["c"]
    left_lists = conn.execute(
        "SELECT COUNT(*) c FROM knowledge_list_items WHERE episode_id = ?", (ep,)).fetchone()["c"]
    conn.close()

    assert left_tags == 0
    assert left_lists == 0
    # The tag itself survives — it may still be in use elsewhere.
    assert [t["name"] for t in database.list_tags()] == ["Politik"]


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

def test_read_later_exists_and_is_builtin(tmp_db):
    lst = database.get_builtin_list()
    assert lst is not None and lst["name"] == "Read Later"
    assert lst["is_builtin"] == 1


def test_builtin_list_cannot_be_deleted(tmp_db):
    with pytest.raises(ValueError):
        database.delete_list(database.get_builtin_list()["id"])


def test_renamed_builtin_list_is_not_recreated_on_restart(tmp_db):
    """Guarded on is_builtin, not on the name — otherwise every restart would
    resurrect a second 'Read Later' next to the renamed one."""
    lst = database.get_builtin_list()
    database.update_list(lst["id"], name="Später lesen")
    database.init_db()

    names = [l["name"] for l in database.list_lists()]
    assert names == ["Später lesen"]


def test_list_membership_add_remove_and_bulk_lookup(tmp_db):
    ep_a, ep_b = _episode("a"), _episode("b")
    list_id = database.create_list("Recherche", icon="🔍")

    database.add_to_list(list_id, ep_a)
    database.add_to_list(list_id, ep_a)  # idempotent

    assert database.list_episode_ids(list_id) == [ep_a]
    assert database.list_membership([ep_a, ep_b]) == {ep_a: [list_id]}
    assert database.list_episodes()[0]["list_ids"] is not None

    assert database.remove_from_list(list_id, ep_a) is True
    assert database.remove_from_list(list_id, ep_a) is False


def test_delete_list_reports_whether_it_existed(tmp_db):
    list_id = database.create_list("Temporär")
    assert database.delete_list(list_id) is True
    assert database.delete_list(list_id) is False


def test_list_counts(tmp_db):
    list_id = database.create_list("Recherche")
    database.add_to_list(list_id, _episode("a"))
    counts = {l["name"]: l["count"] for l in database.list_lists()}
    assert counts["Recherche"] == 1
    assert counts["Read Later"] == 0


# ---------------------------------------------------------------------------
# Migration / backfill
# ---------------------------------------------------------------------------

def _raw_update(episode_id: int, **fields):
    conn = database.get_db()
    conn.execute(
        "UPDATE podcast_episodes SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE id = ?",
        (*fields.values(), episode_id))
    conn.commit()
    conn.close()


def test_backfill_fills_processed_at_from_email_sent_at(tmp_db):
    ep = _episode()
    _raw_update(ep, status="summarized", email_sent_at="2026-01-02T03:04:05", processed_at=None)
    database.init_db()
    assert database.get_episode(ep)["processed_at"] == "2026-01-02T03:04:05"


def test_backfill_falls_back_to_created_at_when_never_mailed(tmp_db):
    ep = _episode()
    _raw_update(ep, status="summarized", email_sent_at=None, processed_at=None)
    database.init_db()
    row = database.get_episode(ep)
    assert row["processed_at"] == row["created_at"]


def test_backfill_leaves_unprocessed_rows_alone(tmp_db):
    ep = _episode()
    _raw_update(ep, status="pending", processed_at=None)
    database.init_db()
    assert database.get_episode(ep)["processed_at"] is None


def test_backfill_is_idempotent_and_never_overwrites(tmp_db):
    ep = _episode()
    _raw_update(ep, status="summarized", email_sent_at="2026-01-02T03:04:05",
                processed_at="2026-06-06T06:06:06", platform="web")

    database.init_db()
    database.init_db()

    row = database.get_episode(ep)
    assert row["processed_at"] == "2026-06-06T06:06:06"
    assert row["platform"] == "web"


@pytest.mark.parametrize("kind,url,transcript_source,expected", [
    ("podcast", "https://example.com/ep", None, "podcast"),
    ("video", "https://www.youtube.com/watch?v=abc", "youtube_captions", "youtube"),
    ("video", "https://www.instagram.com/reel/xyz/", "groq_whisper", "instagram"),
    ("article", "https://www.faz.net/artikel", "article", "web"),
    ("article", "", "pasted", "paste"),
])
def test_backfill_infers_platform(tmp_db, kind, url, transcript_source, expected):
    ep = _episode(kind=kind, youtube_url=url)
    _raw_update(ep, platform=None, transcript_source=transcript_source)
    database.init_db()
    assert database.get_episode(ep)["platform"] == expected

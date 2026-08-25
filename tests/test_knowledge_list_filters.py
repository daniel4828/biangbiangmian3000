"""Tests for issue #936 (umbrella #934): the unified material list.

The four kind sub-tabs are gone; kind is now one filter among several over a
single list. That moves real logic into the backend — sorting, six filter axes,
and the facet catalog the filter bar renders from — so this is where it gets
pinned down:

  * `sort`/`order` are a whitelist. They go into an ORDER BY clause, and an
    unknown value falls back to the default order rather than 400ing (a stale
    bookmark should still show the list).
  * Unprocessed material sorts FIRST under the default sort. Those are the rows
    waiting for Daniel; NULL processed_at sinking to the bottom of a DESC sort
    would make "you have things to process" invisible.
  * Filters are OR within an axis, AND across axes — how a filter bar is
    expected to behave.
  * Archived material (#940) is hidden by default at the HTTP layer.

Isolation follows the house pattern: monkeypatch database.core.DB_PATH, never
database.DB_PATH (#615).
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


def _ep(video_id, **kwargs):
    fields = {k: kwargs.pop(k) for k in list(kwargs)
              if k in ("status", "processed_at", "archived_at", "transcript_source")}
    params = dict(
        video_id=video_id,
        channel_id="https://feed.example/rss",
        title=kwargs.pop("title", "Titel " + video_id),
        published_at=kwargs.pop("published_at", None),
        youtube_url=kwargs.pop("youtube_url", "https://example.com/" + video_id),
    )
    params.update(kwargs)
    episode_id = database.create_pending_episode(**params)
    if fields:
        conn = database.get_db()
        conn.execute(
            "UPDATE podcast_episodes SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE id = ?",
            (*fields.values(), episode_id))
        conn.commit()
        conn.close()
    return episode_id


def _titles(episodes):
    return [e["title"] for e in episodes]


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def test_default_sort_is_processed_at_desc(tmp_db):
    _ep("a", title="alt", status="summarized", processed_at="2026-01-01T00:00:00")
    _ep("b", title="neu", status="summarized", processed_at="2026-08-01T00:00:00")
    assert _titles(database.list_episodes()) == ["neu", "alt"]


def test_unprocessed_material_sorts_first(tmp_db):
    """The whole point of the default sort: rows still waiting to be processed
    are the ones Daniel has to act on."""
    _ep("a", title="fertig", status="summarized", processed_at="2026-08-01T00:00:00")
    _ep("b", title="wartet", status="pending", processed_at=None)
    assert _titles(database.list_episodes()) == ["wartet", "fertig"]
    # ... and still first when the order is flipped: "unprocessed" is not a
    # date, so it doesn't belong at either end depending on direction.
    assert _titles(database.list_episodes(order="asc"))[0] == "wartet"


def test_sort_by_title_and_author(tmp_db):
    _ep("a", title="Zebra", author="Bach")
    _ep("b", title="Apfel", author="Zweig")
    assert _titles(database.list_episodes(sort="title", order="asc")) == ["Apfel", "Zebra"]
    assert _titles(database.list_episodes(sort="author", order="asc")) == ["Zebra", "Apfel"]


def test_unknown_sort_falls_back_to_the_default(tmp_db):
    """A stale bookmark or a typo must still render the list."""
    _ep("a", title="alt", status="summarized", processed_at="2026-01-01T00:00:00")
    _ep("b", title="neu", status="summarized", processed_at="2026-08-01T00:00:00")
    assert _titles(database.list_episodes(sort="'; DROP TABLE podcast_episodes; --")) == ["neu", "alt"]
    # The table is, in fact, still there.
    assert len(database.list_episodes()) == 2


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filters_are_or_within_an_axis(tmp_db):
    _ep("a", platform="youtube")
    _ep("b", platform="web")
    _ep("c", platform="paste")
    got = {e["platform"] for e in database.list_episodes(platform=["youtube", "web"])}
    assert got == {"youtube", "web"}


def test_filters_are_and_across_axes(tmp_db):
    _ep("a", title="treffer", kind="video", platform="youtube")
    _ep("b", title="falsche plattform", kind="video", platform="instagram")
    _ep("c", title="falsche art", kind="article", platform="youtube")
    assert _titles(database.list_episodes(kind=["video"], platform=["youtube"])) == ["treffer"]


def test_kind_still_accepts_a_bare_string(tmp_db):
    """Every pre-#936 caller and bookmark spells it ?kind=video."""
    _ep("a", kind="video")
    _ep("b", kind="article")
    assert len(database.list_episodes(kind="video")) == 1


def test_filter_by_tag_name(tmp_db):
    a, b = _ep("a", title="mit"), _ep("b", title="ohne")
    database.set_item_tags(a, ["Politik"], source="ai")
    assert _titles(database.list_episodes(tag=["politik"])) == ["mit"]   # case-insensitive
    assert _titles(database.list_episodes(tag=["Wirtschaft"])) == []


def test_filter_by_list_membership(tmp_db):
    a, _b = _ep("a", title="drin"), _ep("b", title="draußen")
    list_id = database.get_builtin_list()["id"]
    database.add_to_list(list_id, a)
    assert _titles(database.list_episodes(list_id=list_id)) == ["drin"]


def test_filter_by_status_and_author(tmp_db):
    _ep("a", title="treffer", author="Bach", status="summarized")
    _ep("b", title="anderer autor", author="Zweig", status="summarized")
    _ep("c", title="anderer status", author="Bach", status="error")
    assert _titles(database.list_episodes(author=["Bach"], status=["summarized"])) == ["treffer"]


def test_since_bounds_on_the_sorted_date(tmp_db):
    _ep("a", title="alt", status="summarized", processed_at="2026-01-01T00:00:00")
    _ep("b", title="neu", status="summarized", processed_at="2026-08-01T00:00:00")
    assert _titles(database.list_episodes(since="2026-06-01")) == ["neu"]


def test_include_archived_defaults_to_showing_everything_in_the_db_layer(tmp_db):
    """The DB function stays inclusive so existing callers are untouched; it is
    the HTTP layer that hides archived material by default."""
    _ep("a", title="normal")
    _ep("b", title="archiviert", archived_at="2026-08-01T00:00:00")
    assert len(database.list_episodes()) == 2
    assert _titles(database.list_episodes(include_archived=False)) == ["normal"]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_endpoint_hides_archived_by_default(client):
    _ep("a", title="normal")
    _ep("b", title="archiviert", archived_at="2026-08-01T00:00:00")

    resp = client.get("/api/podcast/episodes")
    assert resp.status_code == 200
    assert _titles(resp.json()) == ["normal"]

    resp = client.get("/api/podcast/episodes?include_archived=true")
    assert len(resp.json()) == 2


def test_endpoint_repeats_filter_params(client):
    _ep("a", platform="youtube")
    _ep("b", platform="web")
    _ep("c", platform="paste")
    resp = client.get("/api/podcast/episodes?platform=youtube&platform=web")
    assert {e["platform"] for e in resp.json()} == {"youtube", "web"}


def test_endpoint_single_kind_still_works(client):
    _ep("a", kind="video")
    _ep("b", kind="article")
    assert len(client.get("/api/podcast/episodes?kind=video").json()) == 1


def test_endpoint_bad_sort_does_not_400(client):
    _ep("a")
    assert client.get("/api/podcast/episodes?sort=nonsense&order=sideways").status_code == 200


def test_facets_endpoint_lists_only_what_occurs(client):
    a = _ep("a", kind="video", platform="youtube", author="Bach", status="summarized")
    _ep("b", kind="article", platform="web", author="Zweig")
    database.set_item_tags(a, ["Politik"], source="ai")

    facets = client.get("/api/knowledge/facets").json()
    assert {f["value"] for f in facets["kinds"]} == {"video", "article"}
    assert {f["value"] for f in facets["platforms"]} == {"youtube", "web"}
    assert {f["value"] for f in facets["authors"]} == {"Bach", "Zweig"}
    assert [t["name"] for t in facets["tags"]] == ["Politik"]
    assert [l["name"] for l in facets["lists"]] == ["Read Later"]
    assert facets["archived_count"] == 0
    # Feeds come from the feed table, not from what happens to be ingested.
    assert isinstance(facets["feeds"], list)


def test_facets_counts_archived(client):
    _ep("a", archived_at="2026-08-01T00:00:00")
    assert client.get("/api/knowledge/facets").json()["archived_count"] == 1

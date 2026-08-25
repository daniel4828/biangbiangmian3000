"""Tests for issue #939 (umbrella #934): full-text search over the knowledge base.

The text Daniel might remember is spread over three places — the source
transcript, the two AI summaries, and the per-language renditions (#804) — so
all of them are indexed. The parts most worth pinning down:

  * Chinese is findable at all. unicode61 doesn't segment CJK, so the index
    stores it character by character and queries it as a phrase; without that a
    whole paragraph is one token and nothing matches.
  * Snippets read like the original text, not like spaced-out characters.
  * The index follows the data: a new summary, a new rendition, a hand edit
    (#937) and a deletion all have to move it.
  * A user query can never become FTS5 syntax — quoting is what makes "AND" or
    a stray quote character a search term instead of an operator or an error.

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


def _material(tmp_db=None, **over):
    episode_id = database.create_pending_episode(
        video_id=over.pop("video_id", "e1"), channel_id=None,
        title=over.pop("title", "Klimapolitik in Europa"),
        published_at=None, youtube_url="https://example.com/a",
        kind="article", author=over.pop("author", "Zeit Online"))
    fields = dict(
        status="summarized",
        transcript_zh="这是一篇关于生态环境的长文章，讨论了气候变化的影响。",
        summary_de="<p><b>Zusammenfassung</b> über Klimawandel und Ökologie.</p>",
        summary_zh="<p>关于气候变化的总结。</p>",
    )
    fields.update(over)
    database.update_episode(episode_id, **fields)
    return episode_id


def _ids(results):
    return [r["episode_id"] for r in results]


# ---------------------------------------------------------------------------
# What is searchable
# ---------------------------------------------------------------------------

def test_finds_a_word_from_the_middle_of_a_transcript(tmp_db):
    episode_id = _material()
    assert _ids(database.search_knowledge("生态")) == [episode_id]


def test_finds_a_word_only_in_a_rendition(tmp_db):
    """The French reading version is text Daniel actually reads, so it has to
    be searchable — it is nowhere else in the row."""
    episode_id = _material()
    database.save_knowledge_rendition(
        episode_id, "fr", "<p>Résumé sur le changement climatique.</p>", [])
    results = database.search_knowledge("climatique")
    assert _ids(results) == [episode_id]
    assert any("fr" in f for f in results[0]["fields"])


def test_finds_words_in_titles_authors_and_both_summaries(tmp_db):
    episode_id = _material()
    for query in ("Klimapolitik", "Zeit", "Klimawandel", "气候变化"):
        assert _ids(database.search_knowledge(query)) == [episode_id], query


def test_html_markup_is_not_searchable(tmp_db):
    """Summaries are <p>/<b> markup. Indexing the tags would make a search for
    'b' return the entire library."""
    _material()
    assert database.search_knowledge("strong") == []


def test_chinese_is_matched_as_a_phrase_not_as_loose_characters(tmp_db):
    _material(video_id="a", transcript_zh="这是一篇关于生态环境的文章。")
    # The characters exist in the text, but not adjacent in this order.
    assert database.search_knowledge("环生") == []
    assert database.search_knowledge("生态环境") != []


def test_prefix_matching_on_latin_words(tmp_db):
    episode_id = _material()
    assert _ids(database.search_knowledge("Klimapol")) == [episode_id]


def test_multiple_words_are_anded(tmp_db):
    a = _material(video_id="a", title="Klimapolitik in Europa")
    _material(video_id="b", title="Klimapolitik in Asien",
              transcript_zh="别的内容", summary_de="<p>Anderes.</p>", summary_zh="<p>别的。</p>")
    assert _ids(database.search_knowledge("Klimapolitik Europa")) == [a]


def test_one_result_per_episode_even_with_many_matching_fields(tmp_db):
    """A word usually appears in the transcript AND both summaries AND every
    rendition; four rows for one article would push everything else off the
    screen."""
    episode_id = _material()
    database.save_knowledge_rendition(episode_id, "fr", "<p>Klimawandel.</p>", [])
    results = database.search_knowledge("Klimawandel")
    assert len(results) == 1
    assert len(results[0]["fields"]) >= 2


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------

def test_snippet_marks_the_match(tmp_db):
    _material()
    snippet = database.search_knowledge("Klimawandel")[0]["snippet"]
    assert "\x02Klimawandel\x03" in snippet


def test_chinese_snippet_reads_like_the_original_text(tmp_db):
    """The index stores '生 态'; a snippet showing that would be unreadable."""
    _material()
    snippet = database.search_knowledge("气候变化")[0]["snippet"]
    assert "\x02气候变化\x03" in snippet
    assert "气 候" not in snippet


# ---------------------------------------------------------------------------
# The index follows the data
# ---------------------------------------------------------------------------

def test_a_new_summary_updates_the_index(tmp_db):
    episode_id = _material()
    database.update_episode(episode_id, summary_de="<p>Jetzt geht es um Bienenzucht.</p>")
    assert database.search_knowledge("Klimawandel") == []
    assert _ids(database.search_knowledge("Bienenzucht")) == [episode_id]


def test_a_hand_edited_title_is_searchable(tmp_db):
    """#937's edit form writes through update_episode_metadata, which is not
    update_episode — its own reindex call is what keeps this working."""
    episode_id = _material()
    database.update_episode_metadata(episode_id, {"title": "Bienenzucht in Bayern"})
    assert _ids(database.search_knowledge("Bienenzucht")) == [episode_id]


def test_clearing_renditions_drops_them_from_the_index(tmp_db):
    episode_id = _material()
    database.save_knowledge_rendition(episode_id, "fr", "<p>changement climatique</p>", [])
    assert database.search_knowledge("climatique") != []
    database.delete_knowledge_renditions(episode_id)
    assert database.search_knowledge("climatique") == []


def test_deleting_an_episode_removes_its_index_rows(tmp_db):
    episode_id = _material()
    database.delete_episode_index(episode_id)
    assert database.search_knowledge("Klimawandel") == []


def test_reindex_all_rebuilds_from_scratch(tmp_db):
    episode_id = _material()
    conn = database.get_db()
    conn.execute("DELETE FROM knowledge_fts")
    conn.commit()
    conn.close()
    assert database.search_knowledge("Klimawandel") == []
    assert database.reindex_all() == 1
    assert _ids(database.search_knowledge("Klimawandel")) == [episode_id]


def test_init_db_builds_the_index_only_once(tmp_db, monkeypatch):
    """The full build reads every transcript in the library, and production
    re-runs init_db() every ~2 minutes (deploy/deploy.sh)."""
    calls = []
    import database.search as search
    real = search.reindex_all
    monkeypatch.setattr(search, "reindex_all", lambda: calls.append(1) or real())
    database.init_db()
    database.init_db()
    assert calls == []   # already built by the fixture's first init_db


# ---------------------------------------------------------------------------
# Query safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "AND", "OR", "NEAR", '"', 'foo"bar', "*", "^", "()", "a AND OR b", "NEAR(a b)",
])
def test_operator_like_queries_are_treated_as_text(tmp_db, query):
    """FTS5 syntax inside a user's query must be search text, not syntax — and
    must never raise."""
    _material()
    assert isinstance(database.search_knowledge(query), list)


def test_empty_query_returns_nothing(tmp_db):
    _material()
    assert database.search_knowledge("   ") == []


def test_build_fts_query_quotes_every_token():
    assert database.build_fts_query("klima wandel") == '"klima"* AND "wandel"*'
    assert database.build_fts_query('  ') is None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_search_endpoint(client):
    episode_id = _material()
    resp = client.get("/api/knowledge/search?q=Klimawandel")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["episode_id"] for r in body] == [episode_id]
    assert body[0]["title"] == "Klimapolitik in Europa"
    assert "fields" in body[0] and "snippet" in body[0]


def test_search_endpoint_requires_a_query(client):
    assert client.get("/api/knowledge/search?q=").status_code == 400
    assert client.get("/api/knowledge/search").status_code == 400


def test_reindex_endpoint(client):
    _material()
    assert client.post("/api/knowledge/reindex").json() == {"indexed": 1}

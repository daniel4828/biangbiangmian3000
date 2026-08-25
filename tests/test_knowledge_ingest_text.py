"""Tests for knowledge/ingest.py's ingest_text() (issue #668) and its HTTP
wrapper POST /api/knowledge/add-text (routes/knowledge.py).

Uses a real (throwaway, per-test) sqlite db via database.init_db() rather
than mocking database.* — ingest_text()/_store_article() touch several
database.podcast functions (get_episode_by_video_id, create_pending_episode,
update_episode) and the dedup behaviour is the whole point of these tests,
so exercising the real row-creation code is more honest than re-deriving
its contract in a stub. ai.translate_title is monkeypatched to avoid any
real AI call (CLAUDE.md: AI must be stubbed at ai._call_api / the public
function, tests never call out to a real provider).
"""
import re

import pytest

import ai
import database
import knowledge.ingest as ingest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


@pytest.fixture(autouse=True)
def _no_title_translation(monkeypatch):
    """translate_title is a real (best-effort) AI call in production —
    stub it so these tests never reach a network/AI provider."""
    monkeypatch.setattr(ai, "translate_title", lambda title: None)


@pytest.fixture(autouse=True)
def _no_metadata_extraction(monkeypatch):
    """extract_article_metadata (#833) is the second real AI call in this
    path — it fires whenever title/author/source_url isn't fully filled in,
    which is most of these tests. Default stub: "the model found nothing",
    so every pre-#833 test keeps exercising the fallbacks it was written
    for. Tests that care about extraction override it themselves."""
    monkeypatch.setattr(ai, "extract_article_metadata", lambda text: {})


LONG_ARTICLE = "这是一篇粘贴进来的付费墙文章正文，用来测试知识库粘贴入库功能。" * 8
assert len(LONG_ARTICLE) >= 200


# ---------------------------------------------------------------------------
# ingest_text()
# ---------------------------------------------------------------------------

def test_ingest_text_creates_article_row():
    result = ingest.ingest_text("测试标题", LONG_ARTICLE)
    assert "episode_id" in result

    episode = database.get_episode(result["episode_id"])
    assert episode["kind"] == "article"
    assert episode["transcript_source"] == "pasted"
    assert episode["transcript_zh"] == LONG_ARTICLE
    assert episode["title"] == "测试标题"
    assert episode["video_id"].startswith("pasted:")


def test_ingest_text_stores_source_url_when_given():
    result = ingest.ingest_text("标题", LONG_ARTICLE, source_url="https://example.com/paywalled")
    episode = database.get_episode(result["episode_id"])
    assert episode["youtube_url"] == "https://example.com/paywalled"


def test_ingest_text_source_url_optional():
    result = ingest.ingest_text("标题", LONG_ARTICLE)
    episode = database.get_episode(result["episode_id"])
    # youtube_url is NOT NULL in schema.sql; no source_url given -> "".
    assert not episode["youtube_url"]


def test_ingest_text_duplicate_body_deduped():
    first = ingest.ingest_text("标题一", LONG_ARTICLE)
    second = ingest.ingest_text("标题二（同一篇正文再投一次）", LONG_ARTICLE)
    assert second == {"status": "already_exists", "episode_id": first["episode_id"]}

    # Only one row was actually created.
    episodes = database.get_db().execute(
        "SELECT COUNT(*) AS c FROM podcast_episodes WHERE kind='article'"
    ).fetchone()
    assert episodes["c"] == 1


def test_ingest_text_whitespace_differences_still_dedupe():
    """#668 completion criterion: the same article pasted with different
    line-wrapping/blank lines must not create a second row — the hash is
    computed over whitespace-normalized text. Uses paragraphs that already
    have a single-space/newline boundary between them, so collapsing
    whitespace-runs-to-one-space leaves the actual words untouched (an
    earlier version of this test inserted newlines *inside* words, which
    changes the normalized content and was a bug in the test, not the code)."""
    paragraphs = [LONG_ARTICLE[i:i + 40] for i in range(0, len(LONG_ARTICLE), 40)]
    variant_a = " ".join(paragraphs)          # single spaces
    variant_b = "\n\n  \n".join(paragraphs)   # blank lines + stray indentation
    # Sanity: the two variants are literally different strings, but carry
    # the same content once whitespace runs are collapsed to one space.
    assert variant_a != variant_b
    assert re.sub(r"\s+", " ", variant_a) == re.sub(r"\s+", " ", variant_b)

    first = ingest.ingest_text("标题", variant_a)
    second = ingest.ingest_text("标题（换行方式不同）", variant_b)
    assert second == {"status": "already_exists", "episode_id": first["episode_id"]}


def test_ingest_text_too_short_raises():
    with pytest.raises(ingest.IngestError):
        ingest.ingest_text("标题", "太短了")


def test_ingest_text_exactly_at_threshold_succeeds():
    text = "字" * ingest._MIN_TEXT_CHARS
    result = ingest.ingest_text("标题", text)
    assert "episode_id" in result


def test_ingest_text_one_under_threshold_raises():
    text = "字" * (ingest._MIN_TEXT_CHARS - 1)
    with pytest.raises(ingest.IngestError):
        ingest.ingest_text("标题", text)


def test_ingest_text_truncates_long_body():
    text = "字" * 20000
    result = ingest.ingest_text("标题", text)
    episode = database.get_episode(result["episode_id"])
    assert len(episode["transcript_zh"]) == ingest._MAX_TEXT_CHARS


def test_ingest_text_untitled_falls_back():
    """No title given and the AI found none either -> the body's first line,
    capped so an unwrapped paste doesn't put the whole article in the title
    column (#833)."""
    result = ingest.ingest_text("", LONG_ARTICLE)
    episode = database.get_episode(result["episode_id"])
    assert episode["title"] == LONG_ARTICLE[:ingest._MAX_TITLE_CHARS].rstrip() + "…"


def test_ingest_text_blank_body_lines_fall_back_to_untitled():
    text = "\n\n" + "字" * 300
    result = ingest.ingest_text("", "   \n\n" + text.strip())
    episode = database.get_episode(result["episode_id"])
    assert episode["title"]  # never empty


# ---------------------------------------------------------------------------
# AI metadata extraction for blank fields (#833)
# ---------------------------------------------------------------------------

def test_metadata_fills_blank_title_author_and_url(monkeypatch):
    monkeypatch.setattr(ai, "extract_article_metadata", lambda text: {
        "title": "抽出来的标题", "author": "某作者",
        "source_url": "https://zeit.de/x", "published_at": "2026-08-01",
    })
    result = ingest.ingest_text(None, LONG_ARTICLE)
    episode = database.get_episode(result["episode_id"])
    assert episode["title"] == "抽出来的标题"
    assert episode["channel_id"] == "某作者"
    assert episode["youtube_url"] == "https://zeit.de/x"
    assert episode["published_at"] == "2026-08-01"


def test_metadata_never_overwrites_what_the_user_typed(monkeypatch):
    monkeypatch.setattr(ai, "extract_article_metadata", lambda text: {
        "title": "AI 猜的标题", "author": "AI 猜的作者",
        "source_url": "https://ai.example/guess",
    })
    result = ingest.ingest_text("我的标题", LONG_ARTICLE,
                                source_url="https://mine.example/x", author="我")
    episode = database.get_episode(result["episode_id"])
    assert episode["title"] == "我的标题"
    assert episode["channel_id"] == "我"
    assert episode["youtube_url"] == "https://mine.example/x"


def test_no_ai_call_when_every_field_is_filled(monkeypatch):
    """All three given -> the extraction call must not happen at all. It
    would cost money for a result that is thrown away."""
    def boom(text):
        raise AssertionError("extract_article_metadata must not be called")
    monkeypatch.setattr(ai, "extract_article_metadata", boom)
    ingest.ingest_text("标题", LONG_ARTICLE, source_url="https://x.example/a", author="作者")


def test_metadata_failure_still_ingests(monkeypatch):
    """The AI helper swallows its own errors, but even a hard raise here
    must not cost Daniel the article body he already pasted."""
    monkeypatch.setattr(ai, "extract_article_metadata", lambda text: {})
    result = ingest.ingest_text(None, LONG_ARTICLE)
    episode = database.get_episode(result["episode_id"])
    assert episode["transcript_zh"] == LONG_ARTICLE
    assert episode["title"]


def test_metadata_not_called_for_a_duplicate_body(monkeypatch):
    """Dedup happens before the AI call — re-pasting the same article must
    not pay for extraction a second time."""
    ingest.ingest_text("标题", LONG_ARTICLE, source_url="https://x.example/a", author="作者")

    def boom(text):
        raise AssertionError("extract_article_metadata must not be called for a duplicate")
    monkeypatch.setattr(ai, "extract_article_metadata", boom)
    second = ingest.ingest_text(None, LONG_ARTICLE)
    assert second["status"] == "already_exists"


def test_ingest_text_reuses_store_article_not_a_second_pipeline():
    """Structural guard for the #668 requirement that ingest_text() must
    not duplicate _ingest_article's row-building code: both should funnel
    through the same _store_article helper."""
    import inspect
    src = inspect.getsource(ingest.ingest_text)
    assert "_store_article(" in src


# ---------------------------------------------------------------------------
# POST /api/knowledge/add-text — same response contract as /api/knowledge/add
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    pytest.importorskip("fastapi", reason="fastapi not installed")
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_add_text_endpoint_returns_episode_id(client):
    resp = client.post("/api/knowledge/add-text", json={"title": "标题", "text": LONG_ARTICLE})
    assert resp.status_code == 200
    body = resp.json()
    assert "episode_id" in body


def test_add_text_endpoint_dedup_returns_already_exists(client):
    first = client.post("/api/knowledge/add-text", json={"title": "标题", "text": LONG_ARTICLE})
    second = client.post("/api/knowledge/add-text", json={"title": "标题2", "text": LONG_ARTICLE})
    assert second.status_code == 200
    body = second.json()
    assert body == {"status": "already_exists", "episode_id": first.json()["episode_id"]}


def test_add_text_endpoint_too_short_returns_400(client):
    resp = client.post("/api/knowledge/add-text", json={"title": "标题", "text": "太短"})
    assert resp.status_code == 400


def test_add_text_endpoint_accepts_optional_source_url(client):
    resp = client.post(
        "/api/knowledge/add-text",
        json={"title": "标题", "text": LONG_ARTICLE, "source_url": "https://example.com/x"},
    )
    assert resp.status_code == 200
    episode = database.get_episode(resp.json()["episode_id"])
    assert episode["youtube_url"] == "https://example.com/x"


def test_add_text_endpoint_accepts_optional_author(client):
    resp = client.post(
        "/api/knowledge/add-text",
        json={"title": "标题", "text": LONG_ARTICLE, "author": "Jan Böhmermann"},
    )
    assert resp.status_code == 200
    episode = database.get_episode(resp.json()["episode_id"])
    assert episode["channel_id"] == "Jan Böhmermann"


def test_add_text_endpoint_accepts_body_only(client):
    """#833: the title is no longer required — the body alone is a valid
    submission (the iOS-shortcut / phone case: paste and hit Add)."""
    resp = client.post("/api/knowledge/add-text", json={"text": LONG_ARTICLE})
    assert resp.status_code == 200
    assert "episode_id" in resp.json()


# ---------------------------------------------------------------------------
# Standalone /save page (#681)
# ---------------------------------------------------------------------------

def test_save_page_is_served_without_the_app_bundle():
    """Like /add (#668): the point is opening instantly on the phone when
    sharing an article, so it must not pull in the ~9000-line app.js."""
    from fastapi.testclient import TestClient
    import main
    body = TestClient(main.app).get("/save").text
    assert 'id="url"' in body and 'id="text"' in body
    assert "/static/shared.js" in body
    assert "/static/app.js" not in body


def test_knowledge_ingest_is_not_duplicated_in_app_js():
    """One client-side ingestion path shared by the app and /save — a second
    copy would drift and every fix would have to be made twice."""
    import pathlib
    app_js = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    shared_js = pathlib.Path("static/shared.js").read_text(encoding="utf-8")
    save_html = pathlib.Path("static/save.html").read_text(encoding="utf-8")

    assert "async function ingestKnowledge(" in shared_js
    assert "async function ingestKnowledge(" not in app_js
    # Neither caller may talk to the endpoints directly.
    for source in (app_js, save_html):
        assert "/api/knowledge/add" not in source
        assert "ingestKnowledge(" in source


# ---------------------------------------------------------------------------
# platform / author (#935)
# ---------------------------------------------------------------------------

def test_ingest_text_defaults_to_the_paste_platform():
    ep = database.get_episode(ingest.ingest_text("标题", LONG_ARTICLE)["episode_id"])
    assert ep["platform"] == "paste"


def test_ingest_text_platform_is_per_caller():
    """platform is NOT derivable from kind: an uploaded file, a newsletter and
    a Signal share all land as kind='article'/'newsletter' pasted bodies but
    arrive from three different places, and Daniel filters on that."""
    ep = database.get_episode(
        ingest.ingest_text("标题", LONG_ARTICLE, platform="upload")["episode_id"])
    assert ep["platform"] == "upload"


def test_ingest_text_stores_the_author_in_its_own_column():
    ep = database.get_episode(
        ingest.ingest_text("标题", LONG_ARTICLE, author="Jan Böhmermann")["episode_id"])
    assert ep["author"] == "Jan Böhmermann"
    # channel_id keeps its historical value too — nothing that reads it breaks.
    assert ep["channel_id"] == "Jan Böhmermann"

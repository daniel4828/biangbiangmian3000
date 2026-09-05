"""Tests for YouTube audiobook ingestion (issue #1054, final phase of the
#1047 read-along umbrella): a video with no caption track at all gets its
audio downloaded and queued for local ASR transcription (audio_jobs, picked
up later by scripts/audio_worker.py) instead of the ordinary captions-only
path.

CLAUDE.md's hard rules for this suite apply: never actually shell out to
yt-dlp (subprocess.run is stubbed wherever a test reaches for it, and most
tests here stub knowledge.youtube's own functions instead, one level up);
isolated db only via database.core.DB_PATH (never database.DB_PATH, a
wildcard-import copy, #615).
"""
import os
import subprocess

import pytest

import ai
import database
import database.core
import knowledge.ingest as ingest
import knowledge.youtube as yt


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def tmp_audiobook_dir(tmp_path, monkeypatch):
    """Redirect the downloaded-audio directory to a throwaway path — nothing
    here may ever write into the real data/audio/source/."""
    monkeypatch.setattr(ingest, "_AUDIOBOOK_AUDIO_DIR", str(tmp_path / "audio_source"))


@pytest.fixture(autouse=True)
def _no_title_translation(monkeypatch):
    monkeypatch.setattr(ai, "translate_title", lambda title: None)


@pytest.fixture(autouse=True)
def _no_real_metadata(request, monkeypatch):
    """Every ingest-level test in this file supplies its own
    fetch_metadata/fetch_duration/download_audio behaviour explicitly — this
    fixture just keeps a test that forgets to stub one of them from reaching
    a real network call or subprocess, failing loudly instead of hanging.

    Tests marked @pytest.mark.real_youtube_calls skip this tripwire: those
    tests exercise fetch_duration/download_audio THEMSELVES (they are the
    thing under test), stubbing subprocess.run one level lower instead — so
    they would trip over their own tripwire otherwise.
    """
    if "real_youtube_calls" in request.keywords:
        return

    def fail_metadata(video_id):
        raise AssertionError("fetch_metadata must be stubbed by the test")

    def fail_duration(video_id):
        raise AssertionError("fetch_duration must be stubbed by the test")

    def fail_download(url, dest_dir):
        raise AssertionError("download_audio must be stubbed by the test")

    monkeypatch.setattr(yt, "fetch_metadata", fail_metadata)
    monkeypatch.setattr(yt, "fetch_duration", fail_duration)
    monkeypatch.setattr(yt, "download_audio", fail_download)


def _stub_short_video(monkeypatch, video_id="abc123", duration=1800, downloaded_to=None):
    monkeypatch.setattr(yt, "fetch_metadata",
                        lambda vid: {"title": "Ein Hörbuch", "author_name": "Reader"})
    monkeypatch.setattr(yt, "fetch_duration", lambda vid: duration)
    calls = {"download": 0}

    def fake_download(url, dest_dir):
        calls["download"] += 1
        path = downloaded_to or os.path.join(dest_dir, "audio.mp3")
        os.makedirs(dest_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"fake mp3 bytes")
        return path

    monkeypatch.setattr(yt, "download_audio", fake_download)
    return calls


# ---------------------------------------------------------------------------
# 1. duplicate links never re-download
# ---------------------------------------------------------------------------

def test_duplicate_video_is_not_redownloaded(monkeypatch, tmp_path):
    calls = _stub_short_video(monkeypatch, video_id="dup1")

    url = "https://www.youtube.com/watch?v=dup1"
    first = ingest.ingest_url(url, as_audiobook=True)
    assert "episode_id" in first
    assert calls["download"] == 1

    second = ingest.ingest_url(url, as_audiobook=True)
    assert second == {"status": "already_exists", "episode_id": first["episode_id"]}
    assert calls["download"] == 1, "a duplicate link must never re-download the audio"


# ---------------------------------------------------------------------------
# 2. long/unconfirmed videos require confirm_long
# ---------------------------------------------------------------------------

def test_long_video_requires_confirmation_before_downloading(monkeypatch):
    calls = _stub_short_video(monkeypatch, video_id="long1", duration=4 * 3600)  # 4h > 3h guard

    url = "https://www.youtube.com/watch?v=long1"
    res = ingest.ingest_url(url, as_audiobook=True)

    assert res["status"] == "confirm_required"
    assert res["duration_seconds"] == 4 * 3600
    assert res["title"] == "Ein Hörbuch"
    assert calls["download"] == 0, "must not download before confirmation"
    assert database.get_episode_by_video_id("long1") is None, "must not create a row either"

    confirmed = ingest.ingest_url(url, as_audiobook=True, confirm_long=True)
    assert "episode_id" in confirmed
    assert calls["download"] == 1


def test_unknown_duration_also_requires_confirmation(monkeypatch):
    """yt-dlp succeeding but reporting no duration at all (e.g. a livestream)
    must be treated the same as "too long" — unknown is not safe."""
    calls = _stub_short_video(monkeypatch, video_id="unknown1", duration=None)

    url = "https://www.youtube.com/watch?v=unknown1"
    res = ingest.ingest_url(url, as_audiobook=True)
    assert res["status"] == "confirm_required"
    assert res["duration_seconds"] is None
    assert calls["download"] == 0

    confirmed = ingest.ingest_url(url, as_audiobook=True, confirm_long=True)
    assert "episode_id" in confirmed
    assert calls["download"] == 1


def test_short_video_downloads_without_confirmation(monkeypatch):
    calls = _stub_short_video(monkeypatch, video_id="short1", duration=1800)

    res = ingest.ingest_url("https://www.youtube.com/watch?v=short1", as_audiobook=True)
    assert "episode_id" in res
    assert calls["download"] == 1


# ---------------------------------------------------------------------------
# 3. a successful ingest queues an audio_job pointing at the downloaded file
# ---------------------------------------------------------------------------

def test_successful_ingest_enqueues_an_audio_job(monkeypatch):
    _stub_short_video(monkeypatch, video_id="queue1", duration=1800)

    res = ingest.ingest_url("https://www.youtube.com/watch?v=queue1", as_audiobook=True)
    episode_id = res["episode_id"]

    jobs = database.list_audio_jobs(statuses=("pending", "running"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["owner_kind"] == "episode"
    assert job["owner_id"] == episode_id
    assert job["status"] == "pending"

    episode = database.get_episode(episode_id)
    assert job["audio_path"] == episode["audio_url"]
    assert os.path.isfile(job["audio_path"])


def test_episode_row_reflects_kind_platform_and_duration(monkeypatch):
    _stub_short_video(monkeypatch, video_id="meta1", duration=600)

    res = ingest.ingest_url("https://www.youtube.com/watch?v=meta1", as_audiobook=True)
    episode = database.get_episode(res["episode_id"])
    assert episode["kind"] == "video"
    assert episode["platform"] == "youtube"
    assert episode["duration_seconds"] == 600
    assert episode["author"] == "Reader"
    assert episode["title"] == "Ein Hörbuch"


# ---------------------------------------------------------------------------
# 4. as_audiobook only applies to YouTube URLs (routes/knowledge.py enforces
#    this at the HTTP layer with a 400; this test is the routing-level
#    counterpart inside knowledge.ingest itself)
# ---------------------------------------------------------------------------

def test_non_youtube_url_is_not_routed_to_audiobook_ingestion(monkeypatch):
    """A non-YouTube URL passed with as_audiobook=True must not reach the
    audiobook path at all — parse_video_id returns None for it, so ingest_url
    falls through to the ordinary Instagram/article dispatch, same as if
    as_audiobook had never been set."""
    def fail_if_called(video_id):
        raise AssertionError("fetch_duration must never run for a non-YouTube URL")

    monkeypatch.setattr(yt, "fetch_duration", fail_if_called)

    import knowledge.article as article

    def fake_fetch_article(url):
        return {"title": "An article", "site": "example.com", "text": "x" * 300,
                "published_at": None}

    monkeypatch.setattr(article, "fetch_article", fake_fetch_article)

    res = ingest.ingest_url("https://example.com/some-article", as_audiobook=True)
    assert "episode_id" in res
    episode = database.get_episode(res["episode_id"])
    assert episode["kind"] == "article"


# ---------------------------------------------------------------------------
# 5. a download failure must not leave an orphan episode row
# ---------------------------------------------------------------------------

def test_download_failure_leaves_no_orphan_episode_row(monkeypatch):
    monkeypatch.setattr(yt, "fetch_metadata",
                        lambda vid: {"title": "Ein Hörbuch", "author_name": "Reader"})
    monkeypatch.setattr(yt, "fetch_duration", lambda vid: 1800)

    def fail_download(url, dest_dir):
        raise yt.AudiobookDownloadError("yt-dlp audio download failed: network error")

    monkeypatch.setattr(yt, "download_audio", fail_download)

    with pytest.raises(ingest.IngestError):
        ingest.ingest_url("https://www.youtube.com/watch?v=faildl", as_audiobook=True)

    assert database.get_episode_by_video_id("faildl") is None
    assert database.list_audio_jobs(statuses=("pending", "running", "done", "error")) == []


def test_metadata_failure_leaves_no_orphan_row(monkeypatch):
    def fail_metadata(vid):
        raise RuntimeError("oEmbed unreachable")

    monkeypatch.setattr(yt, "fetch_metadata", fail_metadata)

    with pytest.raises(ingest.IngestError):
        ingest.ingest_url("https://www.youtube.com/watch?v=failmeta", as_audiobook=True)

    assert database.get_episode_by_video_id("failmeta") is None


def test_duration_failure_leaves_no_orphan_row(monkeypatch):
    monkeypatch.setattr(yt, "fetch_metadata",
                        lambda vid: {"title": "Ein Hörbuch", "author_name": "Reader"})

    def fail_duration(vid):
        raise yt.AudiobookDownloadError("yt-dlp duration lookup failed: network error")

    monkeypatch.setattr(yt, "fetch_duration", fail_duration)

    with pytest.raises(ingest.IngestError):
        ingest.ingest_url("https://www.youtube.com/watch?v=faildur", as_audiobook=True)

    assert database.get_episode_by_video_id("faildur") is None


# ---------------------------------------------------------------------------
# knowledge.youtube.download_audio / fetch_duration — subprocess stubbed,
# exercising the shared knowledge/_ytdlp.py plumbing (also used by
# knowledge/instagram.py, see tests/test_instagram_ingest.py for that side).
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.real_youtube_calls
def test_download_audio_returns_mp3_path(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[0] == yt.yt_dlp_path()
        (tmp_path / "audio.mp3").write_bytes(b"fake mp3 bytes")
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    path = yt.download_audio("https://www.youtube.com/watch?v=abc", str(tmp_path))
    assert path == str(tmp_path / "audio.mp3")


@pytest.mark.real_youtube_calls
def test_download_audio_missing_output_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    with pytest.raises(yt.AudiobookDownloadError):
        yt.download_audio("https://www.youtube.com/watch?v=abc", str(tmp_path))


@pytest.mark.real_youtube_calls
def test_download_audio_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeCompleted(1, stderr="ERROR: network unreachable"))

    with pytest.raises(yt.AudiobookDownloadError):
        yt.download_audio("https://www.youtube.com/watch?v=abc", str(tmp_path))


@pytest.mark.real_youtube_calls
def test_fetch_duration_parses_duration_field(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, stdout='{"duration": 36000}'),
    )
    assert yt.fetch_duration("abc") == 36000


@pytest.mark.real_youtube_calls
def test_fetch_duration_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout='{}'))
    assert yt.fetch_duration("abc") is None


@pytest.mark.real_youtube_calls
def test_fetch_duration_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr="ERROR: video unavailable"),
    )
    with pytest.raises(yt.AudiobookDownloadError):
        yt.fetch_duration("abc")


@pytest.mark.real_youtube_calls
def test_fetch_duration_missing_binary_raises(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(yt.AudiobookDownloadError, match="yt-dlp not found"):
        yt.fetch_duration("abc")

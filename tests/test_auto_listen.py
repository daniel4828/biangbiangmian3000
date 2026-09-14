"""Tests for issue #1133 part 3: auto-building the "Listen" read-along track
(and its full-text rendition) for episodes from feeds that opted into full
automation (podcast_feeds.auto_process), plus knowledge/listen.py's own
build_and_save() — the shared download+align+save implementation extracted
out of routes/podcast.py's _listen_thread.

Nothing here downloads real audio, calls a real ASR provider, or talks to
the network: every test that reaches knowledge.listen.build_and_save or
knowledge.rendition.get_or_create_fulltext either stubs it directly (for the
podcast._maybe_prepare_listen tests) or stubs its own dependencies one level
down (audio.build_track / knowledge.audio_fetch.download_episode_audio, for
the build_and_save tests).

Each DB test patches database.core.DB_PATH — never database.DB_PATH, which
is only a wildcard-import copy (#615).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio
import database
import database.core
import knowledge.audio_fetch
import knowledge.listen
import knowledge.rendition
import podcast
from audio import Cue, Track


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _make_feed(url="https://example.com/feed.xml", auto_process=1):
    return database.create_feed(url, title="Test Feed", auto_process=auto_process)


def _make_episode(feed_url="https://example.com/feed.xml", audio_url="https://example.com/ep.mp3",
                  transcript="今天天气很好。", duration_seconds=600, status="summarized"):
    episode_id = database.create_pending_episode(
        video_id=f"auto-listen-{os.urandom(4).hex()}", channel_id=feed_url,
        title="Test episode", published_at=None,
        youtube_url="https://example.com/ep", audio_url=audio_url,
        duration_seconds=duration_seconds, kind="podcast",
    )
    fields = {"status": status}
    if transcript is not None:
        fields["transcript_zh"] = transcript
    database.update_episode(episode_id, **fields)
    return episode_id


# ---------------------------------------------------------------------------
# podcast._maybe_prepare_listen
# ---------------------------------------------------------------------------

@pytest.fixture
def _stub_listen_build(monkeypatch):
    """Records calls to the two things _maybe_prepare_listen is allowed to
    trigger, without touching a real download/ASR/translation."""
    calls = {"build_and_save": [], "fulltext": []}

    def fake_build_and_save(episode_id, **kwargs):
        calls["build_and_save"].append(episode_id)
        return {"cues": 1, "source": "anchored"}

    def fake_get_or_create_fulltext(episode_id, lang, generate=False, force=False):
        calls["fulltext"].append(episode_id)
        return {"lang": lang, "text": "x", "new_words": []}

    monkeypatch.setattr(knowledge.listen, "build_and_save", fake_build_and_save)
    monkeypatch.setattr(knowledge.rendition, "get_or_create_fulltext", fake_get_or_create_fulltext)
    return calls


def test_auto_listen_builds_when_feed_has_auto_process_on(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    episode_id = _make_episode()

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == [episode_id]
    assert _stub_listen_build["fulltext"] == [episode_id]


def test_auto_listen_skips_when_feed_auto_process_is_off(tmp_db, _stub_listen_build):
    _make_feed(auto_process=0)
    episode_id = _make_episode()

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_skips_when_feed_is_unknown(tmp_db, _stub_listen_build):
    # No podcast_feeds row at all for this channel_id — e.g. a manually
    # pasted/paste-ingested episode with no RSS source.
    episode_id = _make_episode(feed_url="https://nobody-subscribed-to-this.example/feed.xml")

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_skips_when_no_audio_url(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    episode_id = _make_episode(audio_url=None)

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_skips_when_no_transcript(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    episode_id = _make_episode(transcript=None)

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_skips_when_track_already_exists(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    episode_id = _make_episode()
    database.save_audio_track(
        "episode", episode_id, "zh", "fulltext", "data/audio/existing.mp3", 5000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "anchored", None, source_text="今天天气很好。")

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_skips_when_duration_exceeds_cap(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    over_cap_seconds = podcast._AUTO_LISTEN_MAX_MINUTES * 60 + 1
    episode_id = _make_episode(duration_seconds=over_cap_seconds)

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == []
    assert _stub_listen_build["fulltext"] == []


def test_auto_listen_builds_when_duration_is_exactly_at_cap(tmp_db, _stub_listen_build):
    _make_feed(auto_process=1)
    at_cap_seconds = podcast._AUTO_LISTEN_MAX_MINUTES * 60
    episode_id = _make_episode(duration_seconds=at_cap_seconds)

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == [episode_id]
    assert _stub_listen_build["fulltext"] == [episode_id]


def test_auto_listen_builds_when_duration_is_missing(tmp_db, _stub_listen_build):
    """Thin RSS metadata (no itunes:duration) must not silently disable
    auto-listen for that source — only a KNOWN long duration should skip."""
    _make_feed(auto_process=1)
    episode_id = _make_episode(duration_seconds=None)

    podcast._maybe_prepare_listen(episode_id)

    assert _stub_listen_build["build_and_save"] == [episode_id]
    assert _stub_listen_build["fulltext"] == [episode_id]


def test_auto_listen_swallows_build_and_save_failure(tmp_db, monkeypatch):
    _make_feed(auto_process=1)
    episode_id = _make_episode()

    def raising_build_and_save(episode_id, **kwargs):
        raise knowledge.listen.ListenError("simulated alignment failure")

    monkeypatch.setattr(knowledge.listen, "build_and_save", raising_build_and_save)
    monkeypatch.setattr(knowledge.rendition, "get_or_create_fulltext",
                        lambda *a, **kw: {"lang": "zh", "text": "x", "new_words": []})

    podcast._maybe_prepare_listen(episode_id)  # must not raise


def test_auto_listen_swallows_fulltext_failure_and_never_reaches_build_and_save(
        tmp_db, monkeypatch):
    """Full text is generated BEFORE the track (#1133's ordering: it's what's
    on screen, no point paying for audio+ASR if it's not there) — a failure
    there must stop before build_and_save is even attempted, and still not
    raise out of _maybe_prepare_listen."""
    _make_feed(auto_process=1)
    episode_id = _make_episode()

    build_calls = []

    def recording_build_and_save(episode_id, **kwargs):
        build_calls.append(episode_id)
        return {"cues": 1, "source": "anchored"}

    def raising_fulltext(*a, **kw):
        raise knowledge.rendition.RenditionError("simulated translation failure")

    monkeypatch.setattr(knowledge.listen, "build_and_save", recording_build_and_save)
    monkeypatch.setattr(knowledge.rendition, "get_or_create_fulltext", raising_fulltext)

    podcast._maybe_prepare_listen(episode_id)  # must not raise

    assert build_calls == []


# ---------------------------------------------------------------------------
# knowledge.listen.build_and_save
# ---------------------------------------------------------------------------

def test_build_and_save_raises_when_no_audio_url_and_writes_nothing(tmp_db):
    episode_id = _make_episode(audio_url=None)

    with pytest.raises(knowledge.listen.ListenError):
        knowledge.listen.build_and_save(episode_id)

    assert database.get_audio_track("episode", episode_id, "zh", "fulltext") is None


def test_build_and_save_raises_when_both_cloud_and_local_asr_fail_and_writes_nothing(
        tmp_db, tmp_path, monkeypatch):
    episode_id = _make_episode()
    downloaded_path = str(tmp_path / "downloaded.mp3")

    def fake_download(url, dest_dir, filename):
        with open(downloaded_path, "wb") as f:
            f.write(b"fake mp3 bytes")
        return downloaded_path

    def fake_build_track(*, text=None, audio_path=None, lang="zh",
                         prefer_local=False, should_abort=None, on_progress=None,
                         provider=None):
        raise audio.AudioTrackError(f"simulated failure (prefer_local={prefer_local})")

    monkeypatch.setattr(knowledge.audio_fetch, "download_episode_audio", fake_download)
    monkeypatch.setattr(audio, "build_track", fake_build_track)

    with pytest.raises(knowledge.listen.ListenError):
        knowledge.listen.build_and_save(episode_id)

    assert database.get_audio_track("episode", episode_id, "zh", "fulltext") is None

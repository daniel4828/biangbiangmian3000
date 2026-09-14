"""Tests for issue #1133 parts 1 & 2:

1. The frontend's cue<->DOM alignment (_raAlignSrcToDom, static/app.js) must
   skip our own inline vocabulary glosses ("生态（shēngtài - Ökologie）") when
   comparing source_text against the rendered Full text, instead of bailing
   out with "no highlight at all" the moment one appears. _raAlignSrcToDom is
   JS, and this repo's tests are Python-only (no node in CI) — so what's
   under test here is textual: that the skip logic actually exists in
   static/app.js and hasn't been deleted in a later refactor (same idiom
   tests/test_add_word.py already uses for shared.js/app.js duplication
   guards).

2. An episode with its own recording (audio_url) must never be offered — or
   allowed — a synthetic TTS voice for its 'fulltext' variant: both write to
   the same audio_tracks cache key, so whichever finished first would
   permanently shadow the other's Listen recording. This half is a real
   backend contract and gets an actual HTTP-level test.

Each DB test patches database.core.DB_PATH — never database.DB_PATH, which
is only a wildcard-import copy (#615).
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
import database.core
import main
import routes.audio as audio_routes
from audio import Track
from fastapi.testclient import TestClient

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


# ---------------------------------------------------------------------------
# 1. Frontend alignment skip logic — textual guards against static/app.js
# ---------------------------------------------------------------------------

def _app_js() -> str:
    return pathlib.Path("static/app.js").read_text(encoding="utf-8")


def test_alignment_helper_skips_inline_annotation_groups():
    """_raAnnotationSpanAt must exist and _raAlignSrcToDom must actually call
    it — a plain textual guard, since this repo's CI has no JS runtime, but
    still enough to catch the logic being silently deleted in a later
    refactor (same idiom test_add_word.py uses for app.js/shared.js)."""
    app_js = _app_js()
    assert "function _raAnnotationSpanAt(" in app_js
    assert "_raAnnotationSpanAt(domText, di)" in app_js


def test_alignment_gloss_detection_mirrors_backend_should_strip():
    """The frontend's bracket-group classifier must check the same two
    conditions audio/__init__.py's _should_strip() does (' - ' separator or
    any CJK char) — divergence here would mean the two sides disagree about
    what counts as "our own annotation" vs. a human-written parenthetical
    like "(ca. 12:30)"."""
    app_js = _app_js()
    assert "function _raShouldStripAnnotation(" in app_js
    assert "' - '" in app_js


def test_full_text_alignment_bar_hides_generate_button_for_episode_audio():
    """_raBarHtml must not offer 🎧 Generate read-along for an
    (episode, fulltext) owner that already has real audio — the button is
    the frontend half of #1133's "never synthesize over a real recording"
    rule; the backend half is the HTTP test below."""
    app_js = _app_js()
    assert "_knowledgeDetailEpisode.audio_url" in app_js


# ---------------------------------------------------------------------------
# 2. Backend door: POST /api/audio/track must refuse to synthesize a track
#    for an episode's 'fulltext' variant when the episode already has its
#    own recording.
# ---------------------------------------------------------------------------

def _make_episode(audio_url="https://example.com/ep.mp3") -> int:
    return database.create_pending_episode(
        video_id=f"align-test-{os.urandom(4).hex()}", channel_id=None,
        title="Test episode", published_at=None,
        youtube_url="https://example.com/ep", audio_url=audio_url, kind="podcast",
    )


def test_create_track_rejects_fulltext_when_episode_has_audio(tmp_db, monkeypatch):
    episode_id = _make_episode(audio_url="https://example.com/ep.mp3")
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "一些正文")

    def fail_if_called(**kwargs):
        raise AssertionError("must not synthesize TTS when the episode has its own audio")

    monkeypatch.setattr(audio_routes.audio, "build_track", fail_if_called)

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": episode_id, "lang": "zh", "variant": "fulltext",
    })

    assert resp.status_code == 400
    assert "listen" in resp.json()["detail"].lower()
    assert database.get_audio_track("episode", episode_id, "zh", "fulltext") is None


def test_create_track_still_returns_an_already_cached_fulltext_track(tmp_db):
    """An existing track must be returned as-is even if the episode has
    audio_url — the door only blocks NEW synthetic generation, it must not
    retroactively 400 something already built (e.g. before #1133, or the
    Listen recording itself, whose `source` is 'anchored' not 'tts')."""
    episode_id = _make_episode(audio_url="https://example.com/ep.mp3")
    database.save_audio_track(
        "episode", episode_id, "zh", "fulltext", "data/audio/existing.mp3", 5000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "anchored", None, source_text="一些正文")

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": episode_id, "lang": "zh", "variant": "fulltext",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_create_track_allows_fulltext_when_episode_has_no_audio(tmp_db, monkeypatch):
    episode_id = _make_episode(audio_url=None)
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "一些正文")
    monkeypatch.setattr(audio_routes.audio, "build_track",
                        lambda **kw: Track(audio_path="/fake/out.mp3", duration_ms=1000,
                                           cues=[], word_cues=[], source="tts",
                                           voice="zh-CN-XiaoxiaoNeural", source_text="一些正文"))

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": episode_id, "lang": "zh", "variant": "fulltext",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_create_track_allows_summary_variant_even_when_episode_has_audio(tmp_db, monkeypatch):
    """The door is scoped to variant='fulltext' only — a summary is AI prose
    with no corresponding recording, so synthesizing it is legitimate even
    when the episode itself has real audio for its transcript."""
    episode_id = _make_episode(audio_url="https://example.com/ep.mp3")
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "一些摘要")
    monkeypatch.setattr(audio_routes.audio, "build_track",
                        lambda **kw: Track(audio_path="/fake/out.mp3", duration_ms=1000,
                                           cues=[], word_cues=[], source="tts",
                                           voice="zh-CN-XiaoxiaoNeural", source_text="一些摘要"))

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": episode_id, "lang": "zh", "variant": "summary",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_create_track_allows_non_zh_fulltext_even_when_episode_has_audio(tmp_db, monkeypatch):
    """The door is scoped to lang='zh' — the only slot the Listen path ever
    writes (routes/podcast.py hardcodes it, since transcript_zh is the
    correct-text side of the alignment). A French full text is a translation
    of that transcript that nobody ever recorded, so a synthetic voice is the
    only way to hear it and must stay available; blocking it would remove the
    feature rather than protect the recording."""
    episode_id = _make_episode(audio_url="https://example.com/ep.mp3")
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "du texte")
    monkeypatch.setattr(audio_routes.audio, "build_track",
                        lambda **kw: Track(audio_path="/fake/out.mp3", duration_ms=1000,
                                           cues=[], word_cues=[], source="tts",
                                           voice="fr-FR-DeniseNeural", source_text="du texte"))

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": episode_id, "lang": "fr", "variant": "fulltext",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"
    # ...and it landed in its OWN slot, never touching the zh one Listen owns.
    assert database.get_audio_track("episode", episode_id, "zh", "fulltext") is None

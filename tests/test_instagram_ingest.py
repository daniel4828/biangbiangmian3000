"""Tests for Instagram Reel ingestion (issue #750):

- knowledge/instagram.py: parse_shortcode (pure), fetch_metadata/download_audio
  (subprocess stubbed — never actually shell out to yt-dlp)
- podcast.py: hallucination filtering (_filter_whisper_hallucinations),
  word counting (_word_count), the Groq/whisper-1 fallback chain
  (_transcribe_instagram), and build_transcript_de's bidirectional
  translation (#772: Chinese source -> German, non-Chinese source -> Chinese,
  with the "zh" slot always holding the Chinese side). The short-transcript
  AI-summary skip from #750 (SUMMARY_WORD_THRESHOLD / _zero_cost_summary) was
  removed in #772 — Daniel decided short items should get the same detailed
  AI summary as everything else.
- knowledge/ingest.py: ingest_url() dispatching Instagram URLs to
  _ingest_instagram (real throwaway sqlite db via database.init_db(), same
  pattern as test_knowledge_ingest_text.py).

CLAUDE.md's hard rules for this suite: never actually reach yt-dlp/Groq/
OpenAI/a network service; AI is stubbed at ai._call_api or the public
function, never at a provider client; isolated db only via
database.core.DB_PATH.
"""
import subprocess

import pytest

import ai
import database
import knowledge.instagram as ig
import podcast
import zh_annotate


# ---------------------------------------------------------------------------
# knowledge.instagram.parse_shortcode — pure, no I/O
# ---------------------------------------------------------------------------

def test_parse_reel_url():
    assert ig.parse_shortcode("https://www.instagram.com/reel/AbC123xyz/") == "AbC123xyz"


def test_parse_reels_plural_url():
    assert ig.parse_shortcode("https://www.instagram.com/reels/AbC123xyz/") == "AbC123xyz"


def test_parse_post_url():
    assert ig.parse_shortcode("https://instagram.com/p/AbC123xyz/") == "AbC123xyz"


def test_parse_tv_url():
    assert ig.parse_shortcode("https://www.instagram.com/tv/AbC123xyz/") == "AbC123xyz"


def test_parse_no_www_prefix():
    assert ig.parse_shortcode("https://instagram.com/reel/AbC123xyz/") == "AbC123xyz"


def test_parse_without_trailing_slash():
    assert ig.parse_shortcode("https://www.instagram.com/reel/AbC123xyz") == "AbC123xyz"


def test_parse_with_query_params():
    assert ig.parse_shortcode("https://www.instagram.com/reel/AbC123xyz/?igsh=abc123") == "AbC123xyz"


def test_parse_rejects_non_instagram_url():
    assert ig.parse_shortcode("https://example.com/reel/AbC123xyz/") is None


def test_parse_rejects_instagram_homepage():
    assert ig.parse_shortcode("https://www.instagram.com/") is None


def test_parse_rejects_profile_url():
    assert ig.parse_shortcode("https://www.instagram.com/someusername/") is None


def test_parse_rejects_empty_and_none():
    assert ig.parse_shortcode("") is None
    assert ig.parse_shortcode(None) is None


def test_parse_rejects_garbage_string():
    assert ig.parse_shortcode("not a url at all") is None


# ---------------------------------------------------------------------------
# knowledge.instagram.fetch_metadata / download_audio — subprocess stubbed
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_metadata_parses_title_uploader_duration(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[0] == "yt-dlp"
        assert "--dump-json" in cmd
        return _FakeCompleted(
            0, stdout='{"title": "Ein toller Reel", "uploader": "someuser", '
                      '"duration": 42, "webpage_url": "https://www.instagram.com/reel/AbC/"}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    meta = ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert meta["title"] == "Ein toller Reel"
    assert meta["uploader"] == "someuser"
    assert meta["duration"] == 42


def test_fetch_metadata_falls_back_to_description_first_line(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, stdout='{"title": "", "description": "Erste Zeile\\nZweite Zeile"}'),
    )
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    meta = ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert meta["title"] == "Erste Zeile"


def test_fetch_metadata_falls_back_to_shortcode(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout='{}'))
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    meta = ig.fetch_metadata("https://www.instagram.com/reel/AbC123/")
    assert meta["title"] == "AbC123"


def test_fetch_metadata_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr="ERROR: Requested content is not available, rate-limit reached"),
    )
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    with pytest.raises(ig.InstagramError) as excinfo:
        ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert "cookies" in str(excinfo.value).lower()


def test_fetch_metadata_raises_on_unparsable_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout="not json"))
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    with pytest.raises(ig.InstagramError):
        ig.fetch_metadata("https://www.instagram.com/reel/AbC/")


def test_fetch_metadata_missing_binary_raises_instagram_error(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    with pytest.raises(ig.InstagramError) as excinfo:
        ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert "yt-dlp not found" in str(excinfo.value)


def test_download_audio_returns_mp3_path(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout):
        # Simulate yt-dlp actually producing the file.
        (tmp_path / "audio.mp3").write_bytes(b"fake mp3 bytes")
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    path = ig.download_audio("https://www.instagram.com/reel/AbC/", str(tmp_path))
    assert path == str(tmp_path / "audio.mp3")


def test_download_audio_error_message_flags_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr="ERROR: [Instagram] login required to access this content"),
    )
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    with pytest.raises(ig.InstagramError) as excinfo:
        ig.download_audio("https://www.instagram.com/reel/AbC/", str(tmp_path))
    assert "cookies" in str(excinfo.value).lower()


def test_download_audio_missing_output_raises(monkeypatch, tmp_path):
    """yt-dlp exits 0 but somehow produced no file — must not silently
    return a path to nothing."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0))
    monkeypatch.setattr(ig, "_cookies_file", lambda: None)

    with pytest.raises(ig.InstagramError):
        ig.download_audio("https://www.instagram.com/reel/AbC/", str(tmp_path))


def test_cookies_file_used_when_present(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return _FakeCompleted(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("INSTAGRAM_COOKIES_FILE", str(cookies))

    ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert "--cookies" in captured["cmd"]
    assert str(cookies) in captured["cmd"]


def test_cookies_file_omitted_when_missing(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return _FakeCompleted(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("INSTAGRAM_COOKIES_FILE", str(tmp_path / "does-not-exist.txt"))

    ig.fetch_metadata("https://www.instagram.com/reel/AbC/")
    assert "--cookies" not in captured["cmd"]


# ---------------------------------------------------------------------------
# podcast._filter_whisper_hallucinations
# ---------------------------------------------------------------------------

def _segs(*texts_and_probs):
    """Build segment dicts from (text, no_speech_prob, avg_logprob) tuples;
    trailing fields default to None (missing metadata)."""
    out = []
    for t in texts_and_probs:
        text = t[0]
        no_speech = t[1] if len(t) > 1 else None
        avg_logprob = t[2] if len(t) > 2 else None
        out.append({"text": text, "no_speech_prob": no_speech, "avg_logprob": avg_logprob})
    return out


def _real_speech_segments(n=25):
    """n distinct, plausible segments — long enough to clear
    _HALLUCINATION_MIN_WORDS after joining."""
    return _segs(*[(f"this is spoken sentence number {i} with real content", 0.1, -0.2) for i in range(n)])


def test_normal_speech_passes_through():
    text = podcast._filter_whisper_hallucinations(_real_speech_segments())
    assert text
    assert "sentence number 0" in text
    assert "sentence number 24" in text


def test_high_no_speech_prob_segment_dropped():
    segs = _real_speech_segments() + _segs(("music noise", 0.95, -0.2))
    text = podcast._filter_whisper_hallucinations(segs)
    assert "music noise" not in text


def test_low_avg_logprob_segment_dropped():
    segs = _real_speech_segments() + _segs(("garbled guess", 0.1, -2.5))
    text = podcast._filter_whisper_hallucinations(segs)
    assert "garbled guess" not in text


def test_consecutive_repetition_voids_whole_transcript():
    """The classic music-hallucination shape: the same phrase repeated many
    times in a row. Must void the ENTIRE transcript, not just the repeats."""
    segs = _real_speech_segments(10) + _segs(
        ("subscribe now", 0.1, -0.2),
        ("subscribe now", 0.1, -0.2),
        ("subscribe now", 0.1, -0.2),
        ("subscribe now", 0.1, -0.2),
    )
    assert podcast._filter_whisper_hallucinations(segs) == ""


def test_two_repeats_not_enough_to_void():
    """Below the repeat threshold (3) — two repeats can be a real refrain,
    not a hallucination loop."""
    segs = _real_speech_segments(10) + _segs(("chorus line", 0.1, -0.2), ("chorus line", 0.1, -0.2))
    text = podcast._filter_whisper_hallucinations(segs)
    assert "chorus line" in text


def test_too_short_after_filtering_returns_empty():
    segs = _segs(("just a few words here", 0.1, -0.2))
    assert podcast._filter_whisper_hallucinations(segs) == ""


def test_pure_music_no_speech_returns_empty():
    """The headline scenario: a Reel with only background music. Every
    segment is high no_speech_prob -> nothing survives -> empty string, not
    fabricated text."""
    segs = _segs(
        ("thanks for watching", 0.9, -0.1),
        ("like and subscribe", 0.85, -0.1),
        ("don't forget to follow", 0.92, -0.1),
    )
    assert podcast._filter_whisper_hallucinations(segs) == ""


def test_missing_metadata_still_runs_repeat_and_length_checks():
    """A segment with no no_speech_prob/avg_logprob (degraded provider path)
    skips checks 1-2 but must still be subject to the repeat/min-length
    checks."""
    segs = [{"text": "same phrase over and over"} for _ in range(4)]
    assert podcast._filter_whisper_hallucinations(segs) == ""


def test_pydantic_like_segment_objects_supported():
    """Segment objects from the real SDK are attribute-access, not dicts —
    _seg_field must handle both."""
    class FakeSeg:
        def __init__(self, text, no_speech_prob, avg_logprob):
            self.text = text
            self.no_speech_prob = no_speech_prob
            self.avg_logprob = avg_logprob

    segs = [FakeSeg(f"real spoken content number {i} right here", 0.1, -0.2) for i in range(25)]
    text = podcast._filter_whisper_hallucinations(segs)
    assert "real spoken content number 0" in text


# ---------------------------------------------------------------------------
# podcast._word_count
# ---------------------------------------------------------------------------

def test_word_count_chinese_counts_characters():
    # 10 CJK characters, no whitespace.
    assert podcast._word_count("这是一个测试句子啊啊") == 10


def test_word_count_western_counts_whitespace_tokens():
    assert podcast._word_count("this is a test sentence") == 5


def test_word_count_mixed_sums_both():
    text = "hello 你好 world 世界"  # 2 western tokens + 2 western + 4 cjk chars
    # tokens: "hello", "world" = 2; cjk chars: 你好世界 = 4
    assert podcast._word_count(text) == 6


def test_word_count_empty_string():
    assert podcast._word_count("") == 0
    assert podcast._word_count(None) == 0


# ---------------------------------------------------------------------------
# podcast._is_chinese_text
# ---------------------------------------------------------------------------

def test_is_chinese_text_true_for_chinese():
    assert podcast._is_chinese_text("这是一段完全用中文写的话，内容随便什么都行。")


def test_is_chinese_text_false_for_german():
    assert not podcast._is_chinese_text(
        "Das ist ein ganz normaler deutscher Satz ohne jegliche chinesische Zeichen."
    )


def test_is_chinese_text_false_for_english():
    assert not podcast._is_chinese_text("This is a completely ordinary English sentence with no Chinese at all.")


def test_is_chinese_text_false_for_empty():
    assert not podcast._is_chinese_text("")
    assert not podcast._is_chinese_text(None)


def test_is_chinese_text_mostly_chinese_with_a_few_latin_words():
    assert podcast._is_chinese_text("这是一段中文，但是里面有一个 App 和一个 iPhone 这样的词。")


# ---------------------------------------------------------------------------
# podcast.build_transcript_de: bidirectional translation (#772)
# ---------------------------------------------------------------------------

def test_build_transcript_de_still_works_for_chinese(monkeypatch):
    """Chinese source -> German, unchanged since before #750/#772: same
    splitter, same _translate_segments_de call."""
    monkeypatch.setattr(podcast, "_translate_segments_de", lambda segs: [f"DE:{s}" for s in segs])
    pairs = podcast.build_transcript_de("你好世界。今天天气不错。")
    assert pairs
    assert all(p["de"].startswith("DE:") for p in pairs)
    assert all(not p["zh"].startswith("DE:") for p in pairs)


def test_build_transcript_de_translates_non_chinese_to_zh(monkeypatch):
    """Non-Chinese source (e.g. a German/English Instagram Reel, #772) gets
    translated INTO Chinese instead of being skipped — the "zh" slot must
    hold the (translated) Chinese side, "de" the original."""
    monkeypatch.setattr(podcast, "_translate_segments",
                         lambda segs, target, source: [f"ZH:{s}" for s in segs])
    pairs = podcast.build_transcript_de("This is an English transcript, not Chinese at all.")
    assert pairs
    assert all(p["zh"].startswith("ZH:") for p in pairs)
    assert all(not p["de"].startswith("ZH:") for p in pairs)
    # original English text is preserved untranslated in the "de" slot
    assert "This is an English transcript" in pairs[0]["de"]


def test_build_transcript_de_zh_slot_always_chinese_both_directions(monkeypatch):
    """Direction-agnostic sanity check on the contract every renderer relies
    on: whichever branch runs, "zh" ends up holding Chinese text."""
    monkeypatch.setattr(podcast, "_translate_segments_de", lambda segs: ["DE" for _ in segs])
    monkeypatch.setattr(podcast, "_translate_segments",
                         lambda segs, target, source: ["中文" for _ in segs])

    zh_source_pairs = podcast.build_transcript_de("你好世界。今天天气不错。")
    non_zh_source_pairs = podcast.build_transcript_de("Hello world. The weather is nice today.")

    for pairs in (zh_source_pairs, non_zh_source_pairs):
        assert pairs
        for p in pairs:
            assert podcast._is_chinese_text(p["zh"])


def test_build_transcript_de_non_chinese_translation_failure_returns_empty(monkeypatch):
    """Best-effort like the zh->de branch: a translation failure must not
    raise out of _process_episode's caller, just yield no bilingual view."""
    def boom(segs, target, source):
        raise RuntimeError("translate service down")

    monkeypatch.setattr(podcast, "_translate_segments", boom)
    assert podcast.build_transcript_de("This is an English transcript with real content.") == []


# ---------------------------------------------------------------------------
# _process_episode: every item now gets the full AI summary (#772)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


@pytest.fixture(autouse=True)
def _no_ai_disabled(monkeypatch):
    """_process_episode short-circuits under ai_disabled() (DISABLE_AI) —
    these tests exercise the summarize-vs-zero-cost branch, so AI must be
    "enabled" even though the actual AI call itself is stubbed below."""
    import routes.utils
    monkeypatch.setattr(routes.utils, "ai_disabled", lambda: False)


@pytest.fixture(autouse=True)
def _no_notifications(monkeypatch):
    """Email/Signal notifications are irrelevant to the summary-skip
    decision and must not try to actually send anything."""
    monkeypatch.setattr(podcast, "send_email", lambda episode: False)
    monkeypatch.setattr(podcast, "send_signal", lambda episode: None)
    monkeypatch.setattr(podcast, "find_spotify_url", lambda title: "https://open.spotify.com/search/x")


@pytest.fixture(autouse=True)
def _no_real_translation(monkeypatch):
    """_annotate_summary (both the AI-summary and zero-cost paths) calls
    zh_annotate for pinyin/gloss annotation, which calls Google Translate for
    each new word's German gloss — stub that one choke point (same pattern
    as tests/test_zh_annotate.py) so this suite never reaches a real network
    service."""
    monkeypatch.setattr(zh_annotate, "_gloss_de_many",
                        lambda words: {w: f"DE:{w}" for w in words})


def _make_video_episode(transcript: str) -> tuple[int, dict]:
    episode_id = database.create_pending_episode(
        video_id="ig:test123", channel_id="someuser", title="Test Reel",
        published_at=None, youtube_url="https://www.instagram.com/reel/test123/",
        audio_url=None, duration_seconds=30, kind="video",
    )
    database.update_episode(episode_id, transcript_zh=transcript, transcript_source="groq_whisper")
    video = {"video_id": "ig:test123", "title": "Test Reel", "audio_url": None, "duration_seconds": 30, "kind": "video"}
    return episode_id, video


def test_short_transcript_also_uses_ai_summary(monkeypatch):
    """#772: the #750 short-transcript AI-summary skip is gone — even a
    short (well under the old 1000-word threshold) transcript must still go
    through ai.summarize_podcast_transcript, same as any other episode."""
    called = {"ai": False}

    def fake_ai_summarize(*a, **k):
        called["ai"] = True
        return {"summary_zh": "中文摘要", "summary_de": "Deutsche Zusammenfassung", "words": []}

    monkeypatch.setattr(ai, "summarize_podcast_transcript", fake_ai_summarize)
    monkeypatch.setattr(database, "get_podcast_config", lambda: {"summarizer": "api"})
    monkeypatch.setattr(podcast, "build_transcript_de", lambda transcript: [])

    short_transcript = "这是一段很短的转录文本，内容不多，用来测试短文本也走 AI 摘要的功能。" * 2

    episode_id, video = _make_video_episode(short_transcript)
    summary = {"summarized": 0, "failed": 0, "emailed": 0}
    podcast._process_episode(episode_id, video, "medium", summary)

    assert called["ai"] is True
    episode = database.get_episode(episode_id)
    assert episode["status"] == "summarized"
    assert episode["summary_zh"]
    assert episode["summary_de"]


def test_long_transcript_still_uses_ai_summary(monkeypatch):
    """Long transcripts always went through the AI summarizer — unaffected
    by #772's removal of the short-transcript skip."""
    called = {"ai": False}

    def fake_ai_summarize(transcript, title, detail_level, china_critical=False):
        called["ai"] = True
        return {"summary_zh": "中文摘要", "summary_de": "Deutsche Zusammenfassung", "words": []}

    monkeypatch.setattr(ai, "summarize_podcast_transcript", fake_ai_summarize)
    monkeypatch.setattr(database, "get_podcast_config", lambda: {"summarizer": "api"})
    monkeypatch.setattr(podcast, "build_transcript_de", lambda transcript: [])

    long_transcript = "这是一句用来测试长文本的句子，句子会被重复很多次以便超过一千字的阈值。" * 30

    episode_id, video = _make_video_episode(long_transcript)
    summary = {"summarized": 0, "failed": 0, "emailed": 0}
    podcast._process_episode(episode_id, video, "medium", summary)

    assert called["ai"] is True
    episode = database.get_episode(episode_id)
    assert episode["status"] == "summarized"


# ---------------------------------------------------------------------------
# podcast._transcribe_instagram: Groq-first, whisper-1 fallback chain
# ---------------------------------------------------------------------------

def test_transcribe_instagram_uses_groq_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(ig, "download_audio", lambda url, dest_dir: str(tmp_path / "audio.mp3"))
    monkeypatch.setattr(podcast, "_probe_duration_seconds", lambda path: 30.0)
    monkeypatch.setattr(podcast, "_transcribe_via_groq", lambda mp3, dur, vid: "groq transcript text")

    def fail_if_called(*a, **k):
        raise AssertionError("whisper-1 fallback must not run when Groq succeeds")

    monkeypatch.setattr(podcast, "_transcribe_via_whisper", fail_if_called)

    video = {"video_id": "ig:test1", "youtube_url": "https://www.instagram.com/reel/test1/", "kind": "video"}
    text, meta = podcast._transcribe_instagram(video)
    assert text == "groq transcript text"
    assert meta["transcript_source"] == "groq_whisper"


def test_transcribe_instagram_falls_back_to_whisper_when_groq_unavailable(monkeypatch, tmp_path):
    """No GROQ_API_KEY (or Groq otherwise returns None) -> falls back to the
    existing OpenAI whisper-1 path."""
    monkeypatch.setattr(ig, "download_audio", lambda url, dest_dir: str(tmp_path / "audio.mp3"))
    monkeypatch.setattr(podcast, "_probe_duration_seconds", lambda path: 30.0)
    monkeypatch.setattr(podcast, "_transcribe_via_groq", lambda mp3, dur, vid: None)

    calls = {}

    def fake_whisper(mp3_path, duration, video_id, tmp_dir, *, language=None, model=None, filter_hallucinations=False):
        calls["model"] = model
        calls["language"] = language
        calls["filter_hallucinations"] = filter_hallucinations
        return "whisper fallback transcript"

    monkeypatch.setattr(podcast, "_transcribe_via_whisper", fake_whisper)

    video = {"video_id": "ig:test2", "youtube_url": "https://www.instagram.com/reel/test2/", "kind": "video"}
    text, meta = podcast._transcribe_instagram(video)
    assert text == "whisper fallback transcript"
    assert meta["transcript_source"] == "whisper"
    assert calls["model"] == "whisper-1"
    assert calls["language"] is None
    assert calls["filter_hallucinations"] is True


def test_transcribe_instagram_both_unavailable_returns_no_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(ig, "download_audio", lambda url, dest_dir: str(tmp_path / "audio.mp3"))
    monkeypatch.setattr(podcast, "_probe_duration_seconds", lambda path: 30.0)
    monkeypatch.setattr(podcast, "_transcribe_via_groq", lambda mp3, dur, vid: None)
    monkeypatch.setattr(podcast, "_transcribe_via_whisper", lambda *a, **k: None)

    video = {"video_id": "ig:test3", "youtube_url": "https://www.instagram.com/reel/test3/", "kind": "video"}
    text, meta = podcast._transcribe_instagram(video)
    assert text is None


def test_transcribe_instagram_download_failure_propagates(monkeypatch):
    """Cookie expiry / dead link etc: InstagramError must propagate (lands
    on status='error' with a readable message), never silently become
    no_transcript."""
    def fail_download(url, dest_dir):
        raise ig.InstagramError("yt-dlp audio download failed — possibly expired/missing Instagram cookies")

    monkeypatch.setattr(ig, "download_audio", fail_download)

    video = {"video_id": "ig:test4", "youtube_url": "https://www.instagram.com/reel/test4/", "kind": "video"}
    with pytest.raises(ig.InstagramError) as excinfo:
        podcast._transcribe_instagram(video)
    assert "cookies" in str(excinfo.value).lower()


def test_transcribe_via_groq_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert podcast._transcribe_via_groq("/fake/path.mp3", 30.0, "vid1") is None


# ---------------------------------------------------------------------------
# fetch_transcript dispatches Instagram URLs away from YouTube captions
# ---------------------------------------------------------------------------

def test_fetch_transcript_routes_instagram_kind_video_to_instagram_chain(monkeypatch):
    called = {"instagram": False, "youtube": False}
    monkeypatch.setattr(podcast, "_transcribe_instagram", lambda video: (called.__setitem__("instagram", True), ("x", {}))[1])

    import knowledge.youtube
    def fail_youtube(video_id):
        called["youtube"] = True
        return None, {}
    monkeypatch.setattr(knowledge.youtube, "fetch_captions", fail_youtube)

    video = {"video_id": "shortcode1", "title": "t", "audio_url": None, "duration_seconds": 0,
             "kind": "video", "youtube_url": "https://www.instagram.com/reel/shortcode1/"}
    podcast.fetch_transcript(video)
    assert called["instagram"] is True
    assert called["youtube"] is False


def test_fetch_transcript_still_routes_youtube_kind_video_to_youtube(monkeypatch):
    called = {"instagram": False, "youtube": False}
    monkeypatch.setattr(podcast, "_transcribe_instagram", lambda video: called.__setitem__("instagram", True))

    import knowledge.youtube
    def fake_youtube(video_id):
        called["youtube"] = True
        return "captions text", {"transcript_source": "youtube_captions"}
    monkeypatch.setattr(knowledge.youtube, "fetch_captions", fake_youtube)

    video = {"video_id": "abc123", "title": "t", "audio_url": None, "duration_seconds": 0,
             "kind": "video", "youtube_url": "https://www.youtube.com/watch?v=abc123"}
    text, meta = podcast.fetch_transcript(video)
    assert called["youtube"] is True
    assert called["instagram"] is False
    assert text == "captions text"


# ---------------------------------------------------------------------------
# knowledge.ingest.ingest_url() dispatches Instagram URLs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_title_translation(monkeypatch):
    monkeypatch.setattr(ai, "translate_title", lambda title: None)


def test_ingest_url_creates_instagram_episode(monkeypatch):
    monkeypatch.setattr(
        ig, "fetch_metadata",
        lambda url: {"title": "Ein Reel", "uploader": "someuser", "duration": 45,
                     "webpage_url": "https://www.instagram.com/reel/abc123/"},
    )

    import knowledge.ingest as ingest
    result = ingest.ingest_url("https://www.instagram.com/reel/abc123/")
    assert "episode_id" in result

    episode = database.get_episode(result["episode_id"])
    assert episode["kind"] == "video"
    assert episode["video_id"] == "abc123"
    assert episode["title"] == "Ein Reel"
    assert episode["channel_id"] == "someuser"
    assert episode["duration_seconds"] == 45


def test_ingest_url_instagram_dedup(monkeypatch):
    monkeypatch.setattr(
        ig, "fetch_metadata",
        lambda url: {"title": "Ein Reel", "uploader": "someuser", "duration": 45,
                     "webpage_url": url},
    )

    import knowledge.ingest as ingest
    first = ingest.ingest_url("https://www.instagram.com/reel/dup123/")
    second = ingest.ingest_url("https://www.instagram.com/reel/dup123/")
    assert second == {"status": "already_exists", "episode_id": first["episode_id"]}


def test_ingest_url_instagram_metadata_failure_raises_ingest_error(monkeypatch):
    def fail_metadata(url):
        raise ig.InstagramError("yt-dlp metadata lookup failed — possibly expired/missing Instagram cookies")

    monkeypatch.setattr(ig, "fetch_metadata", fail_metadata)

    import knowledge.ingest as ingest
    with pytest.raises(ingest.IngestError) as excinfo:
        ingest.ingest_url("https://www.instagram.com/reel/fail123/")
    assert "cookies" in str(excinfo.value).lower()


def test_ingest_url_does_not_treat_instagram_as_article(monkeypatch):
    """Regression guard: an Instagram URL must never fall through to
    _ingest_article (which would try trafilatura on an Instagram page and
    almost certainly fail or produce garbage)."""
    def fail_if_called(url):
        raise AssertionError("article extraction must not run for an Instagram URL")

    import knowledge.article
    monkeypatch.setattr(knowledge.article, "fetch_article", fail_if_called)
    monkeypatch.setattr(
        ig, "fetch_metadata",
        lambda url: {"title": "Ein Reel", "uploader": None, "duration": None, "webpage_url": url},
    )

    import knowledge.ingest as ingest
    result = ingest.ingest_url("https://www.instagram.com/reel/notanarticle/")
    episode = database.get_episode(result["episode_id"])
    assert episode["kind"] == "video"


# ---------------------------------------------------------------------------
# #766: the video dict handed to fetch_transcript must carry youtube_url
# ---------------------------------------------------------------------------

def test_episode_to_video_carries_youtube_url():
    """The Instagram-vs-YouTube branch in fetch_transcript is decided purely
    by youtube_url. retry_episode used to build its dict inline and omit the
    field, so every Reel was routed into the YouTube captions API and came
    back 'no_transcript' (#766)."""
    episode = {
        "video_id": "abc123", "title": "T", "audio_url": None,
        "duration_seconds": 12, "kind": "video",
        "youtube_url": "https://www.instagram.com/reel/abc123/",
    }
    video = podcast._episode_to_video(episode)
    assert video["youtube_url"] == "https://www.instagram.com/reel/abc123/"
    assert video["kind"] == "video"


def test_retry_episode_routes_a_reel_to_instagram_transcription(monkeypatch):
    """End-to-end for the actual caller: an Instagram row going through
    retry_episode() must reach _transcribe_instagram, never the YouTube
    captions path. This is the test that would have caught #766 — the
    existing ones fed fetch_transcript a hand-built dict and so never
    exercised how the real caller builds it."""
    episode_id = database.create_pending_episode(
        video_id="reelcode99", channel_id="someuser", title="Ein Reel",
        published_at=None, youtube_url="https://www.instagram.com/reel/reelcode99/",
        audio_url=None, duration_seconds=45, kind="video",
    )

    took = {"instagram": False, "youtube": False}

    def fake_instagram(video):
        took["instagram"] = True
        return "Das ist ein kurzer Text aus einem Reel.", {"transcript_source": "groq_whisper"}

    import knowledge.youtube

    def fail_youtube(*a, **k):
        took["youtube"] = True
        raise AssertionError("a Reel must never reach the YouTube captions API")

    monkeypatch.setattr(podcast, "_transcribe_instagram", fake_instagram)
    monkeypatch.setattr(knowledge.youtube, "fetch_captions", fail_youtube)
    monkeypatch.setattr(podcast, "build_transcript_de", lambda transcript: [])
    monkeypatch.setattr(podcast, "_translate_segments",
                        lambda segs, target, source: [f"[{target}]{s}" for s in segs])

    podcast.retry_episode(episode_id)

    assert took["instagram"] is True
    assert took["youtube"] is False

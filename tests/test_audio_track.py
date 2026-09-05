"""Tests for issue #1048 (phase 1 of the #1047 read-along umbrella):
plain text -> mp3 + word-level cues, using edge-tts's WordBoundary events.

edge_tts.Communicate is stubbed throughout (FakeCommunicate below) — nothing
here may ever open a real network connection. What's under test is the
wiring that is easy to get subtly wrong and hard to notice once wrong:

  1. Chunking a long text into several edge-tts calls still produces cues
     whose timestamps advance monotonically across the seam.
  2. Inline vocabulary glosses ("生态（shēngtài - Ökologie）") are stripped
     before the text is sent to be spoken.
  3. A cue's char_start/char_end point back into the ORIGINAL text (with the
     gloss still in it) — the frontend needs that to cut the *rendered* HTML
     at the right place, and a stripped gloss must be inside that span, not
     silently dropped.
  4. A failure partway through writes nothing — no audio_tracks row, no
     leftover .tmp file.
  5. fulltext and summary are stored (and read back) independently for the
     same owner.
  6. Sentence-level merging doesn't treat a period inside quotes as a
     sentence end.
  7. build_track(audio_path=...) is a deliberate NotImplementedError, not a
     half-written branch.

Each DB test patches database.core.DB_PATH — never database.DB_PATH, which
is only a wildcard-import copy (#615).
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio
import audio.anchored as anchored
import audio.asr_cloud as asr_cloud
import audio.tts_track as tts_track
import database
import database.core
import main
import podcast
from audio import Cue, Track
from audio.segment import to_sentences
from fastapi.testclient import TestClient

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def tmp_audio_dir(tmp_path, monkeypatch):
    """Redirect the mp3 cache to a throwaway directory — nothing here may
    ever write into the real data/audio/."""
    d = tmp_path / "audio_cache"
    monkeypatch.setattr(tts_track, "AUDIO_CACHE_DIR", str(d))
    return d


# A CJK character is tokenized on its own; any other run of non-space
# characters is tokenized as one piece. Close enough to real edge-tts word
# boundaries for what these tests check (character-level Chinese text).
_TOKEN_RE = re.compile(r"[一-鿿]|[^\s一-鿿]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


class FakeCommunicate:
    """Stand-in for edge_tts.Communicate. Emits one WordBoundary per token
    (100ms/char, 10ms gap between tokens) plus one audio chunk per call.

    `fail_at` (1-based call index, reset per test by the fixture below)
    makes that call's stream() raise instead — the way #4 is tested.
    """
    fail_at: int | None = None
    calls: list[str] = []

    def __init__(self, text: str, voice: str):
        self.text = text
        self.voice = voice

    async def stream(self):
        FakeCommunicate.calls.append(self.text)
        if FakeCommunicate.fail_at == len(FakeCommunicate.calls):
            raise RuntimeError("simulated edge-tts failure")
        yield {"type": "audio", "data": b"\x00" * 16}
        offset = 0
        for token in _tokenize(self.text):
            duration = len(token) * 1_000_000  # 100ms per char, in 100ns ticks
            yield {"type": "WordBoundary", "offset": offset, "duration": duration, "text": token}
            offset += duration + 100_000  # 10ms gap, in 100ns ticks


@pytest.fixture(autouse=True)
def fake_edge_tts(monkeypatch):
    FakeCommunicate.fail_at = None
    FakeCommunicate.calls = []
    monkeypatch.setattr(tts_track.edge_tts, "Communicate", FakeCommunicate)
    return FakeCommunicate


# ---------------------------------------------------------------------------
# 1. chunking + monotonic timestamps across the seam
# ---------------------------------------------------------------------------

def test_chunk_boundary_keeps_cue_timestamps_monotonic(monkeypatch):
    monkeypatch.setattr(tts_track, "_CHUNK_CHAR_BUDGET", 5)
    text = "一二三四五\n\n六七八九十"  # two paragraphs, each its own chunk

    track = tts_track.build(text, lang="zh")

    assert len(FakeCommunicate.calls) == 2  # confirms two edge-tts calls happened
    starts = [c.start_ms for c in track.word_cues]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)  # strictly increasing, no ties

    first_chunk_cues = [c for c in track.word_cues if c.text in "一二三四五"]
    second_chunk_cues = [c for c in track.word_cues if c.text in "六七八九十"]
    assert len(first_chunk_cues) == 5 and len(second_chunk_cues) == 5
    # The core correctness requirement: the second chunk's cues don't just
    # come after the first's in list order, their *time* is later too.
    assert second_chunk_cues[0].start_ms > first_chunk_cues[-1].end_ms


# ---------------------------------------------------------------------------
# 2 & 3. inline gloss stripped before synthesis, char offsets point back at it
# ---------------------------------------------------------------------------

_GLOSS_TEXT = "生态（shēngtài - Ökologie）很重要。"


def test_inline_gloss_is_not_sent_to_be_spoken():
    tts_track.build(_GLOSS_TEXT, lang="zh")

    sent = "".join(FakeCommunicate.calls)
    assert "（" not in sent and "）" not in sent
    assert "shēngtài" not in sent
    assert "Ökologie" not in sent


def test_char_offsets_of_the_glossed_word_span_the_gloss_in_the_original_text():
    track = tts_track.build(_GLOSS_TEXT, lang="zh")

    tai_cue = next(c for c in track.word_cues if c.text == "态")
    span = _GLOSS_TEXT[tai_cue.char_start:tai_cue.char_end]
    # "态" alone is one character; the span must reach past the stripped
    # gloss all the way to the next spoken word ("很") — otherwise the
    # frontend has no way to know the gloss belongs with this cue.
    assert span != "态"
    assert "Ökologie" in span
    assert span.startswith("态")


# ---------------------------------------------------------------------------
# 4. failure writes nothing — no db row, no leftover .tmp
# ---------------------------------------------------------------------------

def test_generation_failure_writes_nothing_to_db_or_disk(tmp_db, tmp_audio_dir, monkeypatch):
    import routes.audio as audio_routes
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "一二三\n\n四五六")
    monkeypatch.setattr(tts_track, "_CHUNK_CHAR_BUDGET", 3)
    FakeCommunicate.fail_at = 2  # fail on the second chunk

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": 999, "lang": "zh", "variant": "fulltext",
    })

    assert resp.status_code == 502
    assert database.get_audio_track("episode", 999, "zh", "fulltext") is None
    if tmp_audio_dir.exists():
        assert list(tmp_audio_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# 5. fulltext and summary don't clobber each other
# ---------------------------------------------------------------------------

def test_variants_are_stored_and_read_back_independently(tmp_db):
    database.save_audio_track(
        "episode", 42, "zh", "fulltext", "data/audio/full.mp3", 10_000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")
    database.save_audio_track(
        "episode", 42, "zh", "summary", "data/audio/summ.mp3", 2_000,
        [{"start_ms": 0, "end_ms": 50, "text": "y", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")

    full = database.get_audio_track("episode", 42, "zh", "fulltext")
    summ = database.get_audio_track("episode", 42, "zh", "summary")

    assert full["audio_path"] == "data/audio/full.mp3"
    assert summ["audio_path"] == "data/audio/summ.mp3"
    assert full["cues"][0]["text"] == "x"
    assert summ["cues"][0]["text"] == "y"
    assert full["duration_ms"] == 10_000 and summ["duration_ms"] == 2_000


# ---------------------------------------------------------------------------
# 6. sentence merge: a period inside quotes is not a sentence end
# ---------------------------------------------------------------------------

def test_sentence_merge_keeps_a_quoted_period_inside_the_sentence():
    text = "他说“好。”然后走了。"
    word_positions = [i for i, ch in enumerate(text) if ch not in "“”。"]
    word_cues = [
        Cue(start_ms=i * 100, end_ms=i * 100 + 100, text=text[i], char_start=i, char_end=i + 1)
        for i in word_positions
    ]

    sentences = to_sentences(word_cues, text)

    # The only sentence-final punctuation NOT inside quotes is the very last
    # 。— the one right after "好" sits inside “ ” and must not split there.
    assert len(sentences) == 1
    assert sentences[0].text == text


def test_sentence_merge_does_split_on_an_unquoted_period():
    text = "他好。他走了。"
    word_positions = [i for i, ch in enumerate(text) if ch != "。"]
    word_cues = [
        Cue(start_ms=i * 100, end_ms=i * 100 + 100, text=text[i], char_start=i, char_end=i + 1)
        for i in word_positions
    ]

    sentences = to_sentences(word_cues, text)

    assert len(sentences) == 2
    assert sentences[0].text == "他好。"
    assert sentences[1].text == "他走了。"


def test_sentence_merge_ignores_french_elision_apostrophes():
    """L'eau, Qu'il — elision apostrophes are extremely common in French
    (Daniel's other learning language) and must NOT toggle "inside a quote":
    if they did, parity would flip on every one of them and most periods in
    a French text would land "inside a quote" by accident, merging the whole
    text into one giant sentence cue."""
    text = "L'eau est froide. Qu'il vienne demain."
    spans = [(0, 5), (6, 9), (10, 16), (18, 23), (24, 30), (31, 37)]
    word_cues = [
        Cue(start_ms=i * 100, end_ms=i * 100 + 100,
            text=text[s:e], char_start=s, char_end=e)
        for i, (s, e) in enumerate(spans)
    ]

    sentences = to_sentences(word_cues, text)

    assert len(sentences) == 2
    assert sentences[0].text == "L'eau est froide."
    assert sentences[1].text == "Qu'il vienne demain."


# ---------------------------------------------------------------------------
# 7. the other three alignment paths are explicit NotImplementedError
# ---------------------------------------------------------------------------

def test_build_track_dispatches_text_and_audio_path_to_anchored(monkeypatch):
    # #1051 implements text+audio_path as text-anchored ASR alignment —
    # only local/offline ASR (#1053) remains an unimplemented combination.
    called = {}

    # **kwargs on purpose: build_track() grows keyword arguments over time
    # (prefer_local and should_abort arrived with #1053), and a stub with a
    # frozen signature turns every such addition into a failure here that
    # says nothing about the dispatch this test actually checks.
    def fake_build(text, audio_path, lang="zh", **kwargs):
        called["args"] = (text, audio_path, lang)
        return Track(audio_path=audio_path, duration_ms=1000, cues=[],
                     word_cues=[], source="anchored", voice=None)

    monkeypatch.setattr(anchored, "build", fake_build)
    track = audio.build_track(text="some text", audio_path="some/recording.mp3", lang="zh")
    assert track.source == "anchored"
    assert called["args"] == ("some text", "some/recording.mp3", "zh")


def test_build_track_with_neither_text_nor_audio_raises():
    with pytest.raises(audio.AudioTrackError):
        audio.build_track()


# ---------------------------------------------------------------------------
# 11. Cloud ASR (#1052): audio_path alone -> asr_cloud.build(), dispatched
#     through audio.build_track().
# ---------------------------------------------------------------------------

class FakeCompletedProcess:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _padded(idx: int, n: int = 25) -> str:
    """A segment text that's (a) long enough to clear
    _HALLUCINATION_MIN_WORDS once a few segments are joined and (b) unique
    per `idx` so consecutive segments never look like the same text
    repeated (which would trip the hallucination repeat-filter and void the
    whole transcript)."""
    return " ".join([f"w{idx}"] * n)


@pytest.fixture(autouse=True)
def _no_real_groq_cost_log(monkeypatch):
    """These tests exercise chunking/timestamp math, not cost accounting,
    and most of them don't use the tmp_db fixture — stub the one DB write
    asr_cloud.build() makes so it doesn't need a real database."""
    monkeypatch.setattr(asr_cloud.database, "log_api_call", lambda **kw: None)


@pytest.fixture(autouse=True)
def _groq_api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


def test_asr_cloud_chunk_offsets_are_added_to_cue_timestamps(monkeypatch):
    """The core correctness requirement (#1052): each Groq chunk's segment
    timestamps are relative to THAT CHUNK's start, not the whole recording —
    getting the offset addition wrong only shows up on audio long enough to
    need more than one chunk."""
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 1500.0)

    ffmpeg_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ffmpeg_calls.append(cmd)
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(asr_cloud.subprocess, "run", fake_run)

    groq_calls: list[str] = []

    def fake_call_groq(client, path):
        groq_calls.append(path)
        idx = len(groq_calls)
        return [{"text": _padded(idx), "start": 0.0, "end": 10.0}]

    monkeypatch.setattr(asr_cloud, "_call_groq", fake_call_groq)

    track = asr_cloud.build("/fake/input.mp3", lang="zh")

    # 1500s at 600s/chunk -> three chunks (600, 600, 300), three ffmpeg
    # invocations and three Groq calls.
    assert len(ffmpeg_calls) == 3
    assert len(groq_calls) == 3
    assert len(track.cues) == 3
    assert track.source == "asr_cloud"
    assert track.word_cues == []

    # Chunk 1 starts at t=0, chunk 2 at t=600s, chunk 3 at t=1200s — the
    # fake Groq response always reports "0..10s within this chunk", so the
    # absolute cue start must be exactly the chunk's own offset.
    assert track.cues[0].start_ms == 0
    assert track.cues[1].start_ms == 600_000
    assert track.cues[2].start_ms == 1_200_000
    assert track.cues[1].end_ms == 610_000
    assert track.cues[2].end_ms == 1_210_000


def test_asr_cloud_does_not_split_a_short_recording(monkeypatch):
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 300.0)

    def fail_if_called(cmd, **kwargs):
        raise AssertionError("ffmpeg must not be invoked when the audio fits in one chunk")

    monkeypatch.setattr(asr_cloud.subprocess, "run", fail_if_called)
    monkeypatch.setattr(asr_cloud, "_call_groq",
                        lambda client, path: [{"text": _padded(1), "start": 0.0, "end": 5.0}])

    track = asr_cloud.build("/fake/input.mp3", lang="zh")

    assert len(track.cues) == 1
    assert track.cues[0].start_ms == 0
    assert track.cues[0].end_ms == 5000


def test_asr_cloud_char_offsets_point_into_the_joined_transcript(monkeypatch):
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 300.0)
    monkeypatch.setattr(asr_cloud.subprocess, "run",
                        lambda cmd, **kw: FakeCompletedProcess(returncode=0))
    segments = [
        {"text": _padded(1), "start": 0.0, "end": 3.0},
        {"text": _padded(2), "start": 3.0, "end": 6.0},
    ]
    monkeypatch.setattr(asr_cloud, "_call_groq", lambda client, path: segments)

    track = asr_cloud.build("/fake/input.mp3", lang="zh")

    full_text = " ".join(c.text for c in track.cues)
    for cue in track.cues:
        assert full_text[cue.char_start:cue.char_end] == cue.text
    assert track.cues[0].char_start == 0
    assert track.cues[1].char_start == len(_padded(1)) + 1  # +1 for the join space


def test_asr_cloud_raises_when_transcript_is_filtered_as_hallucination(monkeypatch, tmp_db):
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 300.0)
    monkeypatch.setattr(asr_cloud.subprocess, "run",
                        lambda cmd, **kw: FakeCompletedProcess(returncode=0))
    # Same text 3x in a row trips _filter_whisper_segments' repeat check and
    # voids the whole transcript, regardless of word count.
    repeated = [{"text": "background noise", "start": float(i), "end": float(i + 1)}
                for i in range(3)]
    monkeypatch.setattr(asr_cloud, "_call_groq", lambda client, path: repeated)

    with pytest.raises(audio.AudioTrackError, match="hallucination"):
        asr_cloud.build("/fake/input.mp3", lang="zh")

    assert database.get_audio_track("episode", 99999, "zh", "fulltext") is None


def test_asr_cloud_without_groq_api_key_raises_not_silently_skips(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(audio.AudioTrackError, match="GROQ_API_KEY"):
        asr_cloud.build("/fake/input.mp3", lang="zh")


def test_asr_cloud_cleans_up_chunk_files_on_success(monkeypatch):
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 1500.0)
    created_paths: list[str] = []

    def fake_run(cmd, **kwargs):
        created_paths.append(cmd[-1])
        assert os.path.exists(cmd[-1])  # tempfile.mkstemp already created it
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(asr_cloud.subprocess, "run", fake_run)
    calls = []

    def fake_call_groq(client, path):
        calls.append(path)
        return [{"text": _padded(len(calls)), "start": 0.0, "end": 10.0}]

    monkeypatch.setattr(asr_cloud, "_call_groq", fake_call_groq)

    asr_cloud.build("/fake/input.mp3", lang="zh")

    assert len(created_paths) == 3
    for p in created_paths:
        assert not os.path.exists(p)


def test_asr_cloud_cleans_up_chunk_files_on_groq_failure(monkeypatch):
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 1500.0)
    created_paths: list[str] = []

    def fake_run(cmd, **kwargs):
        created_paths.append(cmd[-1])
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(asr_cloud.subprocess, "run", fake_run)

    def fake_call_groq_raises(client, path):
        raise RuntimeError("simulated Groq API failure")

    monkeypatch.setattr(asr_cloud, "_call_groq", fake_call_groq_raises)

    with pytest.raises(audio.AudioTrackError):
        asr_cloud.build("/fake/input.mp3", lang="zh")

    assert len(created_paths) == 3
    for p in created_paths:
        assert not os.path.exists(p)


# ---------------------------------------------------------------------------
# 12. _filter_whisper_hallucinations' behavior must not change across the
#     #1052 refactor that split it into _filter_whisper_segments (returns
#     segments, keeping timestamps) + this thin string-joining wrapper.
# ---------------------------------------------------------------------------

def test_filter_whisper_hallucinations_unchanged_normal_segments():
    segments = [
        {"text": "hello there this is a normal segment with plenty of words",
         "no_speech_prob": 0.1, "avg_logprob": -0.2},
        {"text": "and here is a second normal segment also with plenty of words",
         "no_speech_prob": 0.05, "avg_logprob": -0.3},
    ]
    assert podcast._filter_whisper_hallucinations(segments) == (
        "hello there this is a normal segment with plenty of words "
        "and here is a second normal segment also with plenty of words")


def test_filter_whisper_hallucinations_unchanged_drops_high_no_speech_prob():
    long_real_speech = ("one two three four five six seven eight nine ten "
                        "eleven twelve thirteen fourteen fifteen sixteen "
                        "seventeen eighteen nineteen twenty twentyone")
    segments = [
        {"text": long_real_speech, "no_speech_prob": 0.1, "avg_logprob": -0.2},
        {"text": "silence artifact", "no_speech_prob": 0.9, "avg_logprob": -0.1},
    ]
    assert podcast._filter_whisper_hallucinations(segments) == long_real_speech


def test_filter_whisper_hallucinations_unchanged_voids_on_repeat():
    segments = [{"text": "loop", "no_speech_prob": 0.1, "avg_logprob": -0.1} for _ in range(3)]
    assert podcast._filter_whisper_hallucinations(segments) == ""


def test_filter_whisper_hallucinations_unchanged_voids_too_short():
    segments = [{"text": "too short", "no_speech_prob": 0.1, "avg_logprob": -0.1}]
    assert podcast._filter_whisper_hallucinations(segments) == ""


def test_filter_whisper_segments_returns_segments_not_strings():
    segments = [
        {"text": "hello there this is a normal segment with plenty of words",
         "start": 0.0, "end": 5.0, "no_speech_prob": 0.1, "avg_logprob": -0.2},
        {"text": "and here is a second normal segment also with plenty of words",
         "start": 5.0, "end": 10.0, "no_speech_prob": 0.05, "avg_logprob": -0.3},
    ]
    kept = podcast._filter_whisper_segments(segments)
    assert [s["start"] for s in kept] == [0.0, 5.0]
    assert [s["end"] for s in kept] == [5.0, 10.0]


# ---------------------------------------------------------------------------
# 8. source_text (#1049 phase 2): the frontend's alignment anchor round-trips,
#    NULL rows from before #1049 don't blow up, and the migration is
#    idempotent (init_db() runs every ~2 minutes on the server).
# ---------------------------------------------------------------------------

def test_source_text_round_trips_through_save_and_get(tmp_db):
    database.save_audio_track(
        "episode", 7, "zh", "fulltext", "data/audio/a.mp3", 1_000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural", source_text="这是原文。",
    )

    by_owner = database.get_audio_track("episode", 7, "zh", "fulltext")
    assert by_owner["source_text"] == "这是原文。"

    by_id = database.get_audio_track_by_id(by_owner["id"])
    assert by_id["source_text"] == "这是原文。"


def test_pre_1049_rows_with_null_source_text_read_back_fine(tmp_db):
    # Simulates a row written before #1049 added the parameter — omitting it
    # entirely, the way every caller wrote this before this change.
    database.save_audio_track(
        "episode", 8, "zh", "fulltext", "data/audio/b.mp3", 500,
        [{"start_ms": 0, "end_ms": 50, "text": "y", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural",
    )

    track = database.get_audio_track("episode", 8, "zh", "fulltext")
    assert track["source_text"] is None


def test_create_track_endpoint_returns_source_text(tmp_db, monkeypatch):
    import routes.audio as audio_routes
    monkeypatch.setattr(audio_routes, "_resolve_text",
                        lambda owner_kind, owner_id, lang, variant: "一二三")

    resp = client.post("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": 321, "lang": "zh", "variant": "fulltext",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["source_text"] == "一二三"

    cached = client.get("/api/audio/track", params={
        "owner_kind": "episode", "owner_id": 321, "lang": "zh", "variant": "fulltext",
    })
    assert cached.json()["source_text"] == "一二三"


# ---------------------------------------------------------------------------
# 9. owner_kind='book_page' (#1050 phase 3): reads the RENDERED page (not
#    book_pages.source_text) and is keyed on book_pages.id, not page_no
#    (which repeats across every book).
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_book_render(monkeypatch):
    """Stand in for translate-then-annotate (same idea as test_books.py's
    fake_render) so these tests exercise routes/audio.py's book_page wiring
    without depending on the real translator/annotator or a real EPUB."""
    import routes.books as book_routes

    def _render(html, lang, source="de"):
        return html, []

    monkeypatch.setattr(book_routes, "render_html", _render)


def _make_book_page(source_lang="zh", source_text="<p>今天天气很好。</p>"):
    book_id = database.create_book("Testbuch", None, source_lang, "epub", None, 1200)
    database.add_pages(book_id, [{"source_text": source_text, "ref_label": None}])
    return database.get_page(book_id, 1)


def test_book_page_track_summary_variant_is_400(tmp_db, fake_book_render):
    page = _make_book_page()
    resp = client.post("/api/audio/track", params={
        "owner_kind": "book_page", "owner_id": page["id"], "lang": "zh", "variant": "summary",
    })
    assert resp.status_code == 400


def test_book_page_track_missing_page_is_404(tmp_db, fake_book_render):
    resp = client.post("/api/audio/track", params={
        "owner_kind": "book_page", "owner_id": 999999, "lang": "zh", "variant": "fulltext",
    })
    assert resp.status_code == 404


def test_book_page_track_is_keyed_on_book_pages_id_not_page_no(tmp_db, fake_book_render):
    """Two different books, each with a page_no=1 — book_pages.id differs
    (autoincrement across the whole table) even though page_no is the same,
    and audio_tracks must key on the id, not the page_no, or the two books'
    tracks would collide."""
    page_a = _make_book_page(source_text="<p>第一本书的内容。</p>")
    page_b = _make_book_page(source_text="<p>第二本书的内容。</p>")
    assert page_a["page_no"] == page_b["page_no"] == 1
    assert page_a["id"] != page_b["id"]

    resp_a = client.post("/api/audio/track", params={
        "owner_kind": "book_page", "owner_id": page_a["id"], "lang": "zh", "variant": "fulltext",
    })
    resp_b = client.post("/api/audio/track", params={
        "owner_kind": "book_page", "owner_id": page_b["id"], "lang": "zh", "variant": "fulltext",
    })
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["track_id"] != resp_b.json()["track_id"]
    assert database.get_audio_track("book_page", page_a["id"], "zh", "fulltext") is not None
    assert database.get_audio_track("book_page", page_b["id"], "zh", "fulltext") is not None


def test_book_page_track_reads_the_rendered_text_not_the_source(tmp_db, monkeypatch):
    """The audio must say what's on screen (the translated + annotated
    rendition), never book_pages.source_text — same contract _episode_text
    already has for episode summaries/full text."""
    import routes.books as book_routes

    monkeypatch.setattr(book_routes, "render_html",
                        lambda html, lang, source="de": ("<p>RENDERED TEXT</p>", []))
    page = _make_book_page(source_text="<p>SOURCE TEXT</p>")

    resp = client.post("/api/audio/track", params={
        "owner_kind": "book_page", "owner_id": page["id"], "lang": "zh", "variant": "fulltext",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_text"] == "RENDERED TEXT"


# ---------------------------------------------------------------------------
# 10. deleting the owner cleans up audio_tracks rows AND the mp3 on disk
#     (#1050 follow-up) — audio_path is content-addressed, so a file shared
#     by two different owners must survive deleting just one of them.
# ---------------------------------------------------------------------------

def test_delete_book_removes_its_audio_tracks_and_files(tmp_db, tmp_path):
    page = _make_book_page()
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"fake mp3")
    database.save_audio_track(
        "book_page", page["id"], "zh", "fulltext", str(mp3), 1000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")

    resp = client.delete(f"/api/books/{page['book_id']}")
    assert resp.status_code == 200, resp.text
    assert database.get_audio_track("book_page", page["id"], "zh", "fulltext") is None
    assert not mp3.exists()


def test_delete_book_does_not_remove_a_file_still_used_by_another_owner(tmp_db, tmp_path):
    """Two owners sharing one content-addressed mp3 (same voice+text hashed
    to the same path, audio/tts_track.py._cache_path) — deleting one owner
    must delete only its ROW, never the file the other owner still needs."""
    page = _make_book_page()
    mp3 = tmp_path / "shared.mp3"
    mp3.write_bytes(b"fake mp3")
    database.save_audio_track(
        "book_page", page["id"], "zh", "fulltext", str(mp3), 1000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")
    # A second owner (an episode — audio_tracks has no FK to podcast_episodes,
    # so no real episode row is needed) pointing at the exact same file.
    database.save_audio_track(
        "episode", 4242, "zh", "fulltext", str(mp3), 1000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")

    resp = client.delete(f"/api/books/{page['book_id']}")
    assert resp.status_code == 200, resp.text
    assert database.get_audio_track("book_page", page["id"], "zh", "fulltext") is None
    assert mp3.exists(), "the file is still referenced by the episode row and must survive"
    assert database.get_audio_track("episode", 4242, "zh", "fulltext")["audio_path"] == str(mp3)


def test_delete_book_with_already_missing_audio_file_still_succeeds(tmp_db, tmp_path):
    page = _make_book_page()
    missing = tmp_path / "gone.mp3"   # never written
    database.save_audio_track(
        "book_page", page["id"], "zh", "fulltext", str(missing), 1000,
        [{"start_ms": 0, "end_ms": 100, "text": "x", "char_start": 0, "char_end": 1}],
        "tts", "zh-CN-XiaoxiaoNeural")

    resp = client.delete(f"/api/books/{page['book_id']}")
    assert resp.status_code == 200, resp.text
    assert database.get_audio_track("book_page", page["id"], "zh", "fulltext") is None


def test_audio_tracks_source_text_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "idempotent.db"))
    database.init_db()
    database.init_db()  # must not raise "duplicate column name" on the 2nd pass

    cols = {r["name"] for r in database.get_db().execute(
        "PRAGMA table_info(audio_tracks)").fetchall()}
    assert "source_text" in cols


# ---------------------------------------------------------------------------
# 13. Text-anchored ASR alignment (#1051): audio.asr_cloud.build is stubbed
#     throughout — nothing here may ever call Groq. What's under test is
#     anchored.build()'s own logic: transferring ASR timestamps onto a
#     known-correct text via difflib.SequenceMatcher.
# ---------------------------------------------------------------------------

def _fake_asr_track(cues, duration_ms=None):
    return Track(audio_path="/fake/input.mp3", duration_ms=duration_ms,
                cues=cues, word_cues=[], source="asr_cloud", voice=None)


def test_anchored_alignment_corrects_asr_typos(monkeypatch):
    """The core promise of #1051: ASR misheard 浙江 ("Zhejiang") as 折江, but
    the cue text in the output must be the KNOWN-CORRECT text, not what the
    ASR actually said — and its timing must still fall within the ASR
    segment that covers that part of the recording."""
    asr_cues = [
        Cue(start_ms=0, end_ms=2000, text="今天天气很好。", char_start=0, char_end=7),
        Cue(start_ms=2000, end_ms=4000, text="我去了折江。", char_start=8, char_end=14),
    ]
    correct_text = "今天天气很好。我去了浙江。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=4000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    assert track.source == "anchored"
    assert len(track.cues) == 2
    assert track.cues[1].text == "我去了浙江。"  # the CORRECT text, typo fixed
    assert 2000 <= track.cues[1].start_ms <= 4000
    assert 2000 <= track.cues[1].end_ms <= 4000


def test_anchored_alignment_handles_asr_text_with_no_punctuation(monkeypatch):
    """Groq's ASR output has no punctuation; the target text does. Diffing
    the raw strings would never line up — normalization has to strip
    punctuation from both sides first."""
    asr_cues = [
        Cue(start_ms=0, end_ms=4000, text="今天天气很好我去了浙江", char_start=0, char_end=11),
    ]
    correct_text = "今天天气很好。我去了浙江。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=4000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    assert len(track.cues) == 2
    assert track.cues[0].text == "今天天气很好。"
    assert track.cues[1].text == "我去了浙江。"


def test_anchored_alignment_drops_a_sentence_with_no_match(monkeypatch):
    """A sentence with zero matched characters gets NO cue at all — never a
    guess borrowed from a neighboring sentence (#1048's rule)."""
    asr_cues = [
        Cue(start_ms=0, end_ms=2000, text="今天天气很好。", char_start=0, char_end=7),
        Cue(start_ms=2000, end_ms=4000, text="我去了浙江。", char_start=8, char_end=14),
    ]
    correct_text = "今天天气很好。我去了浙江。这句录音里完全没有出现过。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=4000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    cue_texts = [c.text for c in track.cues]
    assert "今天天气很好。" in cue_texts
    assert "我去了浙江。" in cue_texts
    assert "这句录音里完全没有出现过。" not in cue_texts
    assert len(track.cues) == 2


def test_anchored_alignment_raises_on_low_coverage_and_writes_nothing(tmp_db, monkeypatch):
    asr_cues = [
        Cue(start_ms=0, end_ms=2000, text="今天天气很好。", char_start=0, char_end=7),
    ]
    unrelated_text = "量子物理与相对论的历史发展从未被提及过任何相关内容。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=2000))

    with pytest.raises(audio.AudioTrackError):
        anchored.build(unrelated_text, "/fake/input.mp3", lang="zh")

    rows = database.get_db().execute("SELECT COUNT(*) AS n FROM audio_tracks").fetchone()
    assert rows["n"] == 0


def test_anchored_alignment_cue_start_ms_is_monotonic_despite_out_of_order_asr_times(monkeypatch):
    """ASR segments can come back with timestamps that don't monotonically
    increase relative to the sentences they end up matching. The output cue
    sequence must never go backwards in time regardless."""
    asr_cues = [
        # Appears first in the transcript, but is timed LATER than the
        # segment below it — a deliberately "weird" ASR ordering.
        Cue(start_ms=5000, end_ms=6000, text="今天天气很好。", char_start=0, char_end=7),
        Cue(start_ms=0, end_ms=1000, text="我去了浙江。", char_start=8, char_end=14),
    ]
    correct_text = "今天天气很好。我去了浙江。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=6000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    starts = [c.start_ms for c in track.cues]
    assert starts == sorted(starts)
    for c in track.cues:
        assert c.end_ms >= c.start_ms


def test_anchored_alignment_char_offsets_point_into_the_correct_text(monkeypatch):
    asr_cues = [
        Cue(start_ms=0, end_ms=2000, text="今天天气很好。", char_start=0, char_end=7),
        Cue(start_ms=2000, end_ms=4000, text="我去了浙江。", char_start=8, char_end=14),
    ]
    correct_text = "今天天气很好。我去了浙江。"
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=4000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    for cue in track.cues:
        assert correct_text[cue.char_start:cue.char_end] == cue.text


def test_anchored_alignment_propagates_asr_cloud_failure(monkeypatch):
    """asr_cloud.build's own AudioTrackError (missing GROQ_API_KEY, filtered
    hallucination, etc.) must propagate unchanged, never swallowed or
    replaced with a half-built result."""
    def fake_asr_build(audio_path, lang="zh"):
        raise audio.AudioTrackError("simulated: GROQ_API_KEY is not configured")

    monkeypatch.setattr(anchored.asr_cloud, "build", fake_asr_build)

    with pytest.raises(audio.AudioTrackError, match="GROQ_API_KEY"):
        anchored.build("一些正确的文本。", "/fake/input.mp3", lang="zh")


def test_anchored_alignment_autojunk_false_regression(monkeypatch):
    """Regression guard for the autojunk=False argument to
    difflib.SequenceMatcher in anchored.build(). The default autojunk
    heuristic treats any character occurring in more than 1% of a long
    sequence as "popular" and excludes it from matching — ordinary Chinese
    text blows past that threshold for its most common characters. This test
    uses a text where one character (的) is repeated far beyond 1% of the
    whole; if a future edit drops autojunk=False (or removes the argument
    entirely, reverting to the default True), coverage on this input would
    collapse and the assertion below would fail.
    """
    padding = "的" * 250
    correct_text = f"{padding}。你好世界。"
    # ASR transcript here is byte-identical (no typos) — the point of this
    # test isn't typo-correction, it's proving alignment doesn't fall apart
    # on a highly repetitive character even when autojunk would normally
    # kick in on stock difflib settings.
    asr_cues = [
        Cue(start_ms=0, end_ms=25000, text=padding, char_start=0, char_end=len(padding)),
        Cue(start_ms=25000, end_ms=27000, text="你好世界", char_start=len(padding) + 1,
            char_end=len(padding) + 5),
    ]
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=27000))

    track = anchored.build(correct_text, "/fake/input.mp3", lang="zh")

    assert len(track.cues) == 2
    assert track.cues[0].text == f"{padding}。"
    assert track.cues[1].text == "你好世界。"
    assert track.cues[0].end_ms <= track.cues[1].start_ms


def test_anchored_alignment_still_rejects_text_covering_only_part_of_a_long_recording(monkeypatch):
    """The absolute floor added to the duration-drift check (_MIN_DRIFT_FLOOR_MS)
    must not turn that check into dead code on longer audio: text that fully
    matches (100% sentence coverage) but only accounts for the first third of
    a 600s recording — the rest is unrelated content never mentioned in the
    text at all — must still fail. Coverage alone wouldn't catch this (the
    text's one sentence matches perfectly); only the duration check does."""
    matching_text = "这是一段只对应音频前一部分的文本"
    correct_text = f"{matching_text}。"
    asr_cues = [
        Cue(start_ms=0, end_ms=200_000, text=matching_text,
            char_start=0, char_end=len(matching_text)),
        # The remaining 400s of the recording talks about something the
        # target text never mentions at all — deliberately using no
        # characters in common with matching_text, so there is no way for
        # SequenceMatcher to accidentally borrow a "close enough" match from
        # this segment and mask the drift this test is checking for.
        Cue(start_ms=200_000, end_ms=600_000, text="后续为完全无关旁白内容重复叙述查证",
            char_start=len(matching_text) + 1, char_end=len(matching_text) + 18),
    ]
    monkeypatch.setattr(anchored.asr_cloud, "build",
                        lambda audio_path, lang="zh": _fake_asr_track(asr_cues, duration_ms=600_000))

    with pytest.raises(audio.AudioTrackError, match="duration"):
        anchored.build(correct_text, "/fake/input.mp3", lang="zh")

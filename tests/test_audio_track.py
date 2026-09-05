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
import audio.tts_track as tts_track
import database
import database.core
import main
from audio import Cue
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

def test_build_track_with_audio_path_is_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        audio.build_track(audio_path="some/recording.mp3")


def test_build_track_with_neither_text_nor_audio_raises():
    with pytest.raises(audio.AudioTrackError):
        audio.build_track()


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

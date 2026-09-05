"""Karaoke-style read-along audio (issue #1047 umbrella; #1048 phase 1,
book pages #1050 phase 3).

Turns a knowledge-base episode's summary/full text, or a book reader page,
into an mp3 + a sentence-level cue list the reading screen can highlight
against, using whichever of audio.build_track()'s alignment paths applies —
today only the text-only TTS path (audio/tts_track.py) is implemented.
"""
import logging
import os
import threading

import audio
import database
import knowledge.rendition
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from languages import DEFAULT_LANG, is_valid_lang

logger = logging.getLogger(__name__)
router = APIRouter()

_OWNER_KINDS = ("episode", "book_page")
_VARIANTS = ("fulltext", "summary")

# Guards a single synchronous generation per (owner_kind, owner_id, lang,
# variant) — same 409 idea as routes/podcast.py's _PROCESSING_IDS, just keyed
# on the track's own identity rather than an episode id.
_building: set[tuple] = set()
_building_lock = threading.Lock()


def _resolve_lang(lang: str | None) -> str:
    lang = lang or DEFAULT_LANG
    if not is_valid_lang(lang):
        raise HTTPException(status_code=400, detail=f"Unknown language: {lang}")
    return lang


def _validate(owner_kind: str | None, owner_id: int | None, lang: str | None,
             variant: str) -> tuple[str, int, str, str]:
    if owner_kind not in _OWNER_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"owner_kind must be one of {', '.join(_OWNER_KINDS)}")
    if owner_id is None:
        raise HTTPException(status_code=400, detail="owner_id is required")
    if variant not in _VARIANTS:
        raise HTTPException(
            status_code=400, detail=f"variant must be one of {', '.join(_VARIANTS)}")
    return owner_kind, owner_id, _resolve_lang(lang), variant


def _episode_text(episode_id: int, lang: str, variant: str) -> str:
    """Plain text for one episode's summary or full text.

    Deliberately reuses knowledge/rendition.py's get_or_create_rendition() /
    get_or_create_fulltext() — the exact functions routes/podcast.py's detail
    endpoint and its own .../fulltext endpoint call — rather than
    re-deriving "which text represents this language" a second time (#643's
    single-pipeline lesson, applied to audio).
    """
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    import podcast  # local import: same pattern routes/story.py already uses
                     # to reach podcast._summary_to_plain_text

    if variant == "summary":
        if lang == DEFAULT_LANG:
            html = episode.get("summary_zh") or ""
        else:
            try:
                rendition = knowledge.rendition.get_or_create_rendition(episode_id, lang)
            except knowledge.rendition.RenditionError as e:
                raise HTTPException(status_code=502, detail=str(e))
            html = rendition["summary"]
    else:  # fulltext
        try:
            result = knowledge.rendition.get_or_create_fulltext(episode_id, lang, generate=True)
        except knowledge.rendition.RenditionError as e:
            raise HTTPException(status_code=502, detail=str(e))
        html = (result or {}).get("text") or ""

    text = podcast._summary_to_plain_text(html)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No text available to read aloud")
    return text


def _book_page_text(page_id: int, lang: str, variant: str) -> str:
    """Plain text for one book reader page (#1050), in `lang`.

    A book page has no "summary" — only ever 'fulltext' is meaningful here —
    so 'summary' is a 400 rather than silently serving the fulltext under
    the wrong label.

    Reads (and, on a miss, generates) the RENDERED page — the translated and
    annotated text Daniel actually reads on screen — never book_pages'
    source_text: the audio has to say what's on screen, the same reason
    _episode_text above reads a rendition rather than the raw transcript.

    `page_id` is book_pages.id (not page_no, which repeats across every
    book) — see database.get_page_by_id()'s docstring.
    """
    if variant != "fulltext":
        raise HTTPException(
            status_code=400, detail="book pages only have a 'fulltext' audio variant")

    page = database.get_page_by_id(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    book = database.get_book(page["book_id"])
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    import podcast  # local import: same pattern _episode_text above uses to
                     # reach podcast._summary_to_plain_text
    import routes.books  # local import: avoids importing the books router
                          # module (and everything it pulls in) unless this
                          # branch actually runs, same spirit as the podcast
                          # import above

    try:
        html, _new_words, _cached = routes.books.render_book_page(book, page, lang)
    except knowledge.rendition.RenditionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    text = podcast._summary_to_plain_text(html)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No text available to read aloud")
    return text


def _resolve_text(owner_kind: str, owner_id: int, lang: str, variant: str) -> str:
    if owner_kind == "episode":
        return _episode_text(owner_id, lang, variant)
    # owner_kind == "book_page" (the only other entry in _OWNER_KINDS)
    return _book_page_text(owner_id, lang, variant)


def _track_payload(track: dict) -> dict:
    return {
        "status": "ready",
        "track_id": track["id"],
        "audio_url": f"/api/audio/file/{track['id']}",
        "cues": track["cues"],
        "source": track["source"],
        "duration_ms": track["duration_ms"],
        "voice": track["voice"],
        # NULL for rows written before #1049 — the frontend falls back to
        # audio-only playback (no highlight) rather than guessing at an
        # alignment anchor that was never stored.
        "source_text": track["source_text"],
    }


@router.get("/api/audio/track")
def get_track(owner_kind: str | None = None, owner_id: int | None = None,
             lang: str | None = None, variant: str = "fulltext"):
    """Look up an already-generated track. Never generates one — opening a
    detail page must not silently kick off a TTS run (same contract as
    GET .../fulltext)."""
    owner_kind, owner_id, lang, variant = _validate(owner_kind, owner_id, lang, variant)
    track = database.get_audio_track(owner_kind, owner_id, lang, variant)
    if not track:
        return {"status": "absent"}
    return _track_payload(track)


@router.post("/api/audio/track")
def create_track(owner_kind: str | None = None, owner_id: int | None = None,
                 lang: str | None = None, variant: str = "fulltext"):
    """Generate (or return the already-cached) track. Synchronous — a few
    seconds up to tens of seconds depending on text length — with a 409
    guard so two requests for the same track don't race each other into two
    edge-tts runs and two writes.
    """
    owner_kind, owner_id, lang, variant = _validate(owner_kind, owner_id, lang, variant)

    cached = database.get_audio_track(owner_kind, owner_id, lang, variant)
    if cached:
        return _track_payload(cached)

    key = (owner_kind, owner_id, lang, variant)
    with _building_lock:
        if key in _building:
            raise HTTPException(status_code=409, detail="This track is already being generated")
        _building.add(key)
    try:
        text = _resolve_text(owner_kind, owner_id, lang, variant)
        try:
            track = audio.build_track(text=text, lang=lang)
        except audio.AudioTrackError as e:
            raise HTTPException(status_code=502, detail=str(e))
        track_id = database.save_audio_track(
            owner_kind, owner_id, lang, variant,
            track.audio_path, track.duration_ms,
            [c.to_dict() for c in track.cues],
            track.source, track.voice,
            source_text=text,
        )
        logger.info("audio: built track %s for %s/%s lang=%s variant=%s (%d cues)",
                   track_id, owner_kind, owner_id, lang, variant, len(track.cues))
    finally:
        with _building_lock:
            _building.discard(key)

    return {
        "status": "ready",
        "track_id": track_id,
        "audio_url": f"/api/audio/file/{track_id}",
        "cues": [c.to_dict() for c in track.cues],
        "source": track.source,
        "duration_ms": track.duration_ms,
        "voice": track.voice,
        "source_text": text,
    }


@router.get("/api/audio/file/{track_id}")
def get_track_file(track_id: int):
    """Serve the mp3. FileResponse handles HTTP Range requests natively —
    needed so scrubbing the progress bar doesn't re-download the whole file
    (a knowledge-base full-text track can run tens of MB)."""
    track = database.get_audio_track_by_id(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not os.path.isfile(track["audio_path"]):
        logger.warning("audio: track %s has no file on disk at %s", track_id, track["audio_path"])
        raise HTTPException(status_code=404, detail="Audio file missing on disk")
    return FileResponse(track["audio_path"], media_type="audio/mpeg")

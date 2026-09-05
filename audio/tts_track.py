"""Phase 1 of #1048: plain text -> mp3 + word-level timing, using edge-tts's
WordBoundary events. See audio/__init__.py's module docstring for why this
is the first (and cheapest) of the four planned alignment paths — the time
axis is not detected after the fact, it's handed to us for free by the TTS
engine itself.

Communicate.stream() is used instead of Communicate.save() specifically
because .save() never surfaces WordBoundary events — that is the whole
reason this module exists rather than reusing tts.py, which only ever calls
.save().
"""
import asyncio
import hashlib
import logging
import os
import re

import edge_tts

import languages
from offline import is_offline

from . import AudioTrackError, Cue, Track, strip_annotations
from .segment import to_sentences

logger = logging.getLogger(__name__)

AUDIO_CACHE_DIR = "data/audio"

# edge-tts single requests get unreliable (and slow) on very long inputs, so
# long text is split into chunks before synthesis. There's no documented hard
# limit to point to — 2000 characters is a conservative budget in the same
# spirit as knowledge/rendition.py's 4500-char translator chunk being well
# under the 5000 the endpoint rejects past.
_CHUNK_CHAR_BUDGET = 2000

# edge-tts's WordBoundary stream has no event marking trailing silence at the
# end of a chunk, so the true gap between one chunk's audio ending and the
# next one's starting can't be measured from the stream alone. This is a
# fixed approximation added between chunks so cue timestamps keep advancing
# monotonically across the seam; the error it introduces is bounded to
# roughly this many milliseconds and only ever shows up right at a chunk
# boundary, never accumulating beyond that.
_CHUNK_GAP_MS = 50


def _cache_path(voice: str, text: str) -> str:
    # Keyed on the ORIGINAL text (with any inline glosses still in it, not
    # the cleaned-for-speech version) — same text with a different gloss
    # would otherwise share a cache entry despite meaning something different
    # if it were ever re-annotated.
    key = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()
    return os.path.join(AUDIO_CACHE_DIR, f"{key}.mp3")


def _split_chunks(text: str) -> list[str]:
    """Cut `text` into pieces under _CHUNK_CHAR_BUDGET, first at paragraph
    boundaries, falling back to sentence boundaries for a paragraph that's
    still too long by itself (same approach as books/paginate.py)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()] or [text]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= _CHUNK_CHAR_BUDGET:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= _CHUNK_CHAR_BUDGET:
            current = para
            continue
        piece = ""
        for sentence in re.split(r"(?<=[。！？.!?])", para):
            if len(piece) + len(sentence) <= _CHUNK_CHAR_BUDGET:
                piece += sentence
            else:
                if piece:
                    chunks.append(piece)
                piece = sentence
        current = piece
    if current:
        chunks.append(current)
    return chunks or [text]


async def _synthesize_chunk(text: str, voice: str) -> tuple[bytes, list[dict]]:
    """One edge-tts request. Returns (mp3 bytes, word events), each word
    event {"start_ms", "end_ms", "text"} measured relative to the START OF
    THIS CHUNK — the caller adds the running offset to make them absolute
    over the whole track."""
    communicate = edge_tts.Communicate(text, voice)
    raw_audio = bytearray()
    words: list[dict] = []
    async for msg in communicate.stream():
        if msg["type"] == "audio":
            raw_audio.extend(msg["data"])
        elif msg["type"] == "WordBoundary":
            # edge-tts reports offset/duration in 100-nanosecond ticks.
            words.append({
                "start_ms": msg["offset"] / 10000,
                "end_ms": (msg["offset"] + msg["duration"]) / 10000,
                "text": msg["text"],
            })
    if not raw_audio:
        raise AudioTrackError("edge-tts returned no audio for this chunk")
    return bytes(raw_audio), words


def build(text: str, lang: str = "zh", voice: str | None = None) -> Track:
    """text -> Track, source='tts'.

    Raises AudioTrackError on any failure and never leaves a partial file
    behind — same atomic-write contract as tts.py's _ensure_cached (write to
    a .tmp, os.replace into place), plus explicit cleanup on failure since
    this writes incrementally across several edge-tts calls instead of one.
    """
    if is_offline():
        # Offline mode can't fall back to "serve whatever's cached" here the
        # way tts.py does: a track for this exact (owner, lang, variant) is,
        # by construction, new content that has never been generated before.
        raise AudioTrackError("offline mode: cannot synthesize new audio")
    voice = voice or languages.get_lang_config(lang)["tts_voice"]

    orig_text = text or ""
    clean_text, offset_map = strip_annotations(orig_text)
    if not clean_text.strip():
        raise AudioTrackError("nothing to synthesize — text is empty after stripping inline glosses")

    chunks = _split_chunks(clean_text)
    path = _cache_path(voice, orig_text)
    tmp = path + ".tmp"
    os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

    word_cues: list[Cue] = []
    search_cursor = 0     # position in clean_text — words arrive in reading order
    time_offset_ms = 0.0  # cumulative duration of chunks already written

    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                audio_bytes, raw_words = asyncio.run(_synthesize_chunk(chunk, voice))
                f.write(audio_bytes)
                chunk_last_end = 0.0
                for w in raw_words:
                    idx = clean_text.find(w["text"], search_cursor)
                    if idx == -1:
                        # edge-tts's word text didn't match verbatim at or
                        # after the cursor (rare — e.g. it merged/split a
                        # token differently than expected). Skip this one
                        # cue rather than guess at its position in the
                        # source text; losing one word's highlight is far
                        # better than a cue pointing at the wrong character.
                        logger.warning(
                            "audio.tts_track: could not locate word %r in source text", w["text"])
                        continue
                    clean_start, clean_end = idx, idx + len(w["text"])
                    search_cursor = clean_end
                    char_start = offset_map[clean_start] if clean_start < len(offset_map) else len(orig_text)
                    char_end = offset_map[clean_end] if clean_end < len(offset_map) else len(orig_text)
                    word_cues.append(Cue(
                        start_ms=round(time_offset_ms + w["start_ms"]),
                        end_ms=round(time_offset_ms + w["end_ms"]),
                        text=w["text"], char_start=char_start, char_end=char_end,
                    ))
                    chunk_last_end = max(chunk_last_end, w["end_ms"])
                # See _CHUNK_GAP_MS above for why this is an approximation.
                time_offset_ms += chunk_last_end + _CHUNK_GAP_MS

        if not word_cues:
            raise AudioTrackError("edge-tts produced audio but no usable word timings")

        os.replace(tmp, path)   # atomic: no partial file visible to readers
    except Exception as e:
        # Only ever remove `tmp` here, never `path`. `path` is content-
        # addressed (sha256(voice|text)) — if this exact text was already
        # synthesized successfully before, `path` is the file some OTHER
        # audio_tracks row is currently pointing at. A failed regeneration
        # attempt must not delete a good file out from under a row that
        # never asked to be touched (#777's rule: a failed retry must never
        # destroy what it was trying to improve). os.replace() is atomic and
        # only ever runs after a fully successful build, so `path` is safe
        # to leave alone unconditionally on any failure, including "no
        # usable word timings" above.
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, AudioTrackError):
            raise
        raise AudioTrackError(f"tts synthesis failed: {e}") from e

    duration_ms = round(time_offset_ms - _CHUNK_GAP_MS)
    sentence_cues = to_sentences(word_cues, orig_text)
    return Track(audio_path=path, duration_ms=duration_ms, cues=sentence_cues,
                word_cues=word_cues, source="tts", voice=voice,
                # The text as handed in, glosses still in it: cue offsets are
                # original-text positions (see strip_annotations' offset_map).
                source_text=orig_text)

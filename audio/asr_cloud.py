"""Phase 3 of #1047 (issue #1052): a recording with no matching text is
transcribed by Groq's whisper-large-v3-turbo (response_format="verbose_json")
and its own segments become the cues directly — see audio/__init__.py's
module docstring for how this sits alongside the other three planned
alignment paths.

This deliberately does NOT call podcast._transcribe_via_groq: that function
joins segments into one string (throwing the timestamps away, which is the
whole point here) and hardcodes purpose="podcast-transcribe" for cost
accounting. This module makes its own Groq request but reuses the shared
hallucination filter (podcast._filter_whisper_segments, #1052's split of the
old text-only _filter_whisper_hallucinations) rather than duplicating those
four checks a second time — this codebase's rule against a second copy of
the same logic (#643, #836) applies just as much to a filter as to a
pipeline.
"""
import logging
import os
import subprocess
import tempfile

import database
import podcast
from offline import is_offline

from . import AudioTrackError, Cue, Track

logger = logging.getLogger(__name__)

_GROQ_MODEL = "whisper-large-v3-turbo"

# Groq's audio endpoint caps uploads around 25MB; a fixed 600s (10min) chunk
# keeps an mp3 well under that regardless of bitrate. Splitting at a fixed
# duration (not on silence) costs at most one word cut across a seam — the
# same tradeoff audio/tts_track.py makes for chunking TTS input, for the same
# reason: silence-aware splitting needs its own analysis pass and isn't worth
# it for a read-along that's tracking sentences, not syllables.
_CHUNK_SECONDS = 600


def _probe_duration_seconds(path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as e:
        raise AudioTrackError(
            "ffmpeg (ffprobe) is required for cloud ASR but was not found") from e
    if result.returncode != 0:
        raise AudioTrackError(
            f"ffprobe failed to read audio duration: {result.stderr.strip()[-500:]}")
    try:
        return float(result.stdout.strip())
    except ValueError as e:
        raise AudioTrackError(f"ffprobe returned no readable duration for {path}") from e


def _split_chunks(path: str, duration: float) -> list[tuple[str, float]]:
    """Cut `path` into <=_CHUNK_SECONDS pieces with ffmpeg stream-copy (no
    re-encode, so this is fast regardless of file size). Returns
    [(chunk_path, start_offset_seconds), ...].

    When the whole file already fits in one chunk, returns [(path, 0.0)]
    WITHOUT invoking ffmpeg at all — splitting a file that doesn't need it
    would just be a slower no-op copy.
    """
    if duration <= _CHUNK_SECONDS:
        return [(path, 0.0)]

    chunks: list[tuple[str, float]] = []
    try:
        start = 0.0
        while start < duration:
            length = min(_CHUNK_SECONDS, duration - start)
            fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start), "-t", str(length),
                     "-i", path, "-c", "copy", chunk_path],
                    capture_output=True, text=True, timeout=120,
                )
            except FileNotFoundError as e:
                os.remove(chunk_path)
                raise AudioTrackError(
                    "ffmpeg is required for cloud ASR chunking but was not found") from e
            if result.returncode != 0:
                try:
                    os.remove(chunk_path)
                except OSError:
                    pass
                raise AudioTrackError(
                    f"ffmpeg failed to split audio: {result.stderr.strip()[-500:]}")
            chunks.append((chunk_path, start))
            start += _CHUNK_SECONDS
    except Exception:
        # Clean up whatever chunks were already cut before this one failed —
        # a half-cut audiobook left in the temp dir is exactly the kind of
        # leak this module must never produce.
        for chunk_path, _offset in chunks:
            try:
                os.remove(chunk_path)
            except OSError:
                pass
        raise
    return chunks


def _call_groq(client, path: str) -> list:
    """One Groq request for a single (<=25MB) audio chunk. Returns the raw
    verbose_json segments — dicts or pydantic SDK objects, either is fine
    since podcast._seg_field (reused below) already accepts both (#750)."""
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model=_GROQ_MODEL, file=f, response_format="verbose_json",
        )
    seg_list = getattr(resp, "segments", None) or []
    if seg_list:
        return seg_list
    # Degraded fallback (same shape podcast._transcribe_via_whisper falls
    # back to): no per-segment metadata, but still something for the
    # min-length/repeat checks in _filter_whisper_segments to run on.
    return [{"text": (getattr(resp, "text", "") or "").strip(), "start": 0.0, "end": 0.0}]


def build(audio_path: str, lang: str = "zh") -> Track:
    """audio_path -> Track, source='asr_cloud' (#1052).

    Raises AudioTrackError on any failure — missing GROQ_API_KEY, missing
    ffmpeg, an API error, or the whole transcript getting filtered out as
    hallucination/silence — and always cleans up any temporary chunk files
    it created, on both the success and failure paths. Never writes to the
    database except the cost log entry, and only after a successful API
    call.
    """
    if is_offline():
        raise AudioTrackError("offline mode: cannot run cloud ASR")
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        # Unlike podcast.py's optional-credential transcribers (Tingwu,
        # NotebookLM, this same Groq call in _transcribe_instagram), there is
        # no fallback chain here yet (#1053 will add local whisper.cpp, and
        # that decision belongs to build_track()'s caller, not this
        # function) — so silently returning None here would look like
        # nothing happened at all. Raise instead.
        raise AudioTrackError("GROQ_API_KEY is not configured — cloud ASR (#1052) needs it")

    duration = _probe_duration_seconds(audio_path)
    chunks = _split_chunks(audio_path, duration)

    import openai  # lazy: same pattern podcast._transcribe_via_groq uses
    client = openai.OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")

    all_segments: list[dict] = []
    try:
        for chunk_path, offset_seconds in chunks:
            raw_segments = _call_groq(client, chunk_path)
            for seg in raw_segments:
                # Every timestamp Groq hands back is relative to the START
                # OF THIS CHUNK, not the whole recording — the same seam
                # audio/tts_track.py's chunking crosses via time_offset_ms.
                # Getting this offset wrong only shows up on audio long
                # enough to need more than one chunk, which is exactly why
                # tests/test_audio_track.py asserts it explicitly rather
                # than trusting it by inspection.
                shifted = {
                    "text": podcast._seg_field(seg, "text", "") or "",
                    "start": (podcast._seg_field(seg, "start", 0.0) or 0.0) + offset_seconds,
                    "end": (podcast._seg_field(seg, "end", 0.0) or 0.0) + offset_seconds,
                }
                no_speech = podcast._seg_field(seg, "no_speech_prob")
                if no_speech is not None:
                    shifted["no_speech_prob"] = no_speech
                avg_logprob = podcast._seg_field(seg, "avg_logprob")
                if avg_logprob is not None:
                    shifted["avg_logprob"] = avg_logprob
                all_segments.append(shifted)
    except AudioTrackError:
        raise
    except Exception as e:
        raise AudioTrackError(f"Groq ASR request failed: {e}") from e
    finally:
        # Only remove files _split_chunks actually created — when the audio
        # fit in a single chunk, chunk_path IS audio_path, and that file
        # belongs to whoever called build(), not to us.
        for chunk_path, _offset in chunks:
            if chunk_path != audio_path:
                try:
                    os.remove(chunk_path)
                except OSError:
                    pass

    # Billed per minute of audio, same accounting convention as every other
    # transcriber in podcast.py.
    database.log_api_call(
        model=_GROQ_MODEL, input_tokens=int(duration),
        output_tokens=0, purpose="audio_asr",
    )

    kept = podcast._filter_whisper_segments(all_segments)
    if not kept:
        raise AudioTrackError("transcript was filtered out as hallucination/silence")

    # Each surviving segment becomes exactly one cue — Groq's segments are
    # already sentence-grained, so unlike audio/tts_track.py's word cues,
    # these must NOT be run through audio/segment.py's to_sentences(): doing
    # that a second time would just merge pairs of already-whole sentences
    # into one cue.
    cues: list[Cue] = []
    cursor = 0
    for seg in kept:
        text = (podcast._seg_field(seg, "text") or "").strip()
        if not text:
            continue
        if cues:
            cursor += 1  # the join space between this segment and the last
        char_start = cursor
        char_end = cursor + len(text)
        cues.append(Cue(
            start_ms=round((podcast._seg_field(seg, "start", 0.0) or 0.0) * 1000),
            end_ms=round((podcast._seg_field(seg, "end", 0.0) or 0.0) * 1000),
            text=text, char_start=char_start, char_end=char_end,
        ))
        cursor = char_end

    if not cues:
        raise AudioTrackError("transcript was filtered out as hallucination/silence")

    # The transcript every cue's char_start/char_end indexes into. Built with
    # exactly the same single-space join the cursor above assumed, so the two
    # can't drift apart.
    source_text = " ".join(c.text for c in cues)

    return Track(audio_path=audio_path, duration_ms=round(duration * 1000),
                cues=cues, word_cues=[], source="asr_cloud", voice=None,
                source_text=source_text)

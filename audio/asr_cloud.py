"""Phase 3 of #1047 (issue #1052): a recording with no matching text is
transcribed by a cloud Whisper-family API (response_format="verbose_json")
and its own segments become the cues directly — see audio/__init__.py's
module docstring for how this sits alongside the other three planned
alignment paths.

Two providers (#1090), both spoken to through the `openai` SDK since Groq's
audio endpoint is OpenAI-compatible — only the base_url and model name
differ:
  - 'openai': whisper-1 @ $0.006/min, api.openai.com. Daniel's 2026-09-08
    default after a 107-minute audiobook failed local whisper.cpp twice (a
    6-hour timeout with large-v3, then an 8h14m run with medium that then
    got voided entirely by the hallucination filter — see podcast.py's
    _VOID_ALL_MAX_SECONDS for the other half of that fix). Minutes instead of
    hours, ~$0.64 for that book.
  - 'groq': whisper-large-v3-turbo @ ~$0.0007/min, api.groq.com. ~9x cheaper
    and ~10x faster than OpenAI's whisper-1 (#750), kept as the default when
    only GROQ_API_KEY is configured.
🔴 OpenAI's model MUST be whisper-1, not gpt-4o-mini-transcribe: only
whisper-1 accepts response_format="verbose_json" (gpt-4o-mini-transcribe
rejects it outright, so it can't give per-segment no_speech_prob/
avg_logprob) — podcast._transcribe_via_whisper's docstring already documents
this the hard way (#750), don't relearn it here.

This deliberately does NOT call podcast._transcribe_via_groq /
_transcribe_via_whisper: those join segments into one string (throwing the
timestamps away, which is the whole point here) and hardcode a
purpose="podcast-transcribe" cost-accounting label. This module makes its
own request but reuses the shared hallucination filter
(podcast._filter_whisper_segments, #1052's split of the old text-only
_filter_whisper_hallucinations) rather than duplicating those checks a
second time — this codebase's rule against a second copy of the same logic
(#643, #836) applies just as much to a filter as to a pipeline.
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
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# 🔴 Must stay whisper-1 — see the module docstring's callout. This is the
# only OpenAI model this module is allowed to request.
_OPENAI_MODEL = "whisper-1"

# Groq's audio endpoint caps uploads around 25MB; a fixed 600s (10min) chunk
# keeps an mp3 well under that regardless of bitrate. Splitting at a fixed
# duration (not on silence) costs at most one word cut across a seam — the
# same tradeoff audio/tts_track.py makes for chunking TTS input, for the same
# reason: silence-aware splitting needs its own analysis pass and isn't worth
# it for a read-along that's tracking sentences, not syllables. OpenAI's
# endpoint has the same ~25MB cap, so the same chunking serves both
# providers unchanged.
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


def _call_openai(client, path: str) -> list:
    """One OpenAI request for a single (<=25MB) audio chunk, using whisper-1
    — see the module docstring's 🔴 callout for why that model specifically.
    Same shape/degraded-fallback contract as _call_groq above."""
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model=_OPENAI_MODEL, file=f, response_format="verbose_json",
        )
    seg_list = getattr(resp, "segments", None) or []
    if seg_list:
        return seg_list
    return [{"text": (getattr(resp, "text", "") or "").strip(), "start": 0.0, "end": 0.0}]


def _resolve_provider(provider: str | None) -> str:
    """Decide which cloud ASR provider to call (#1090).

    An explicit `provider` argument wins. Otherwise AUDIO_ASR_PROVIDER, then
    whichever credential is actually configured — OpenAI first (Daniel's
    2026-09-08 decision, see module docstring), Groq if only that key is
    present. Raising when neither is configured follows the same reasoning
    this function's predecessor always used for a missing GROQ_API_KEY:
    silently returning None here would look like nothing happened at all.
    """
    chosen = provider or os.environ.get("AUDIO_ASR_PROVIDER")
    if chosen:
        return chosen
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    raise AudioTrackError(
        "cloud ASR (#1052/#1090) needs OPENAI_API_KEY or GROQ_API_KEY — neither is configured")


def build(audio_path: str, lang: str = "zh", provider: str | None = None,
         on_progress=None) -> Track:
    """audio_path -> Track, source='asr_cloud' (#1052).

    Raises AudioTrackError on any failure — no usable API key configured,
    missing ffmpeg, an API error, or the whole transcript getting filtered
    out as hallucination/silence — and always cleans up any temporary chunk
    files it created, on both the success and failure paths. Never writes to
    the database except the cost log entry, and only after a successful API
    call.

    `provider` (#1090): 'openai' or 'groq', see _resolve_provider for how the
    default is chosen when this is None.

    `on_progress` (#1100): optional `callable(str) -> None`, called once
    before each chunk is sent — "第 N/M 块" when the recording needed
    splitting, a plain "正在转录…" when it fit in one request. `_split_chunks`
    already knows the chunk count, so this costs nothing extra to report.
    """
    if is_offline():
        raise AudioTrackError("offline mode: cannot run cloud ASR")

    provider = _resolve_provider(provider)
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AudioTrackError("OPENAI_API_KEY is not configured — cloud ASR (#1090) needs it")
        model, base_url, call_chunk = _OPENAI_MODEL, None, _call_openai
    elif provider == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise AudioTrackError("GROQ_API_KEY is not configured — cloud ASR (#1052) needs it")
        model, base_url, call_chunk = _GROQ_MODEL, _GROQ_BASE_URL, _call_groq
    else:
        raise AudioTrackError(
            f"unknown ASR provider {provider!r} (AUDIO_ASR_PROVIDER must be 'openai' or 'groq')")

    duration = _probe_duration_seconds(audio_path)
    chunks = _split_chunks(audio_path, duration)

    import openai  # lazy: same pattern podcast._transcribe_via_groq uses
    client = openai.OpenAI(api_key=api_key, base_url=base_url) if base_url else openai.OpenAI(api_key=api_key)

    n_chunks = len(chunks)
    all_segments: list[dict] = []
    try:
        for idx, (chunk_path, offset_seconds) in enumerate(chunks, start=1):
            if on_progress is not None:
                on_progress(f"第 {idx}/{n_chunks} 块" if n_chunks > 1 else "正在转录…")
            raw_segments = call_chunk(client, chunk_path)
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
        raise AudioTrackError(f"{provider} ASR request failed: {e}") from e
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
    # transcriber in podcast.py. `model` must be the actual model called
    # (whisper-1 or whisper-large-v3-turbo) — database/stats.py._MODEL_PRICING
    # is keyed by exact model name, so logging the wrong one would silently
    # mis-price every call.
    database.log_api_call(
        model=model, input_tokens=int(duration),
        output_tokens=0, purpose="audio_asr",
    )

    # total_seconds=duration (#1090) lets the shared filter tell a genuine
    # short-clip hallucination (voids everything, unchanged since #750) apart
    # from a few repeated seconds buried in an hours-long recording (drops
    # only that run) — see podcast._filter_whisper_segments and
    # _VOID_ALL_MAX_SECONDS.
    kept = podcast._filter_whisper_segments(all_segments, total_seconds=duration)
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

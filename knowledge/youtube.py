"""YouTube ingestion (issue #651): turn a video URL into a transcript the
existing podcast pipeline (podcast.summarize / _process_episode) can consume
unchanged. Captions only for the ordinary path; a video with no caption
track at all falls to status='no_transcript' (see podcast.fetch_transcript).

Issue #1054 adds a second path, used only when the caller explicitly asks
for it (knowledge.ingest's as_audiobook flag): audiobook videos have no
caption track at all, so fetch_duration()/download_audio() below fetch the
audio itself via yt-dlp and hand it to local ASR (audio/asr_local.py,
queued through audio_jobs) instead — see that section further down for why
this is a deliberately separate, opt-in path rather than a fallback the
ordinary captions flow reaches for automatically.

Issue #681: YouTube blocks cloud-provider IPs, so on the production VPS the
caption API answers `RequestBlocked` for videos that do have captions. That
case now falls back to NotebookLM (which fetches the video from Google's own
network) and, failing that, raises CaptionsUnavailable — it must never be
reported as "this video has no captions".

`youtube-transcript-api` note (checked against 1.2.4, 2026-08): the library
moved from classmethods to instance methods in 1.0 — `YouTubeTranscriptApi()`
must be instantiated, then `.list(video_id)` / `.fetch(video_id, languages=)`
called on the instance. Most examples online still show the old
`YouTubeTranscriptApi.get_transcript(...)` classmethod, which no longer
exists.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from knowledge._ytdlp import format_error, run_yt_dlp, yt_dlp_path

logger = logging.getLogger(__name__)

# Caption language priority (issue #651): Chinese variants first (Daniel's
# primary study language), then German, then English as a last resort before
# falling back to "whatever track exists" — a German or English video still
# has *some* transcript, and the podcast summary prompt now tolerates any
# input language (ai.build_podcast_summary_prompt).
_LANGUAGE_PRIORITY = ("zh-Hans", "zh-CN", "zh", "zh-TW", "de", "en")

_OEMBED_URL = "https://www.youtube.com/oembed"


class CaptionsUnavailable(Exception):
    """YouTube refused to hand over the captions AND the NotebookLM fallback
    couldn't stand in (issue #681). Deliberately an exception, not a
    (None, meta) return: "YouTube blocked us" is a real error the episode must
    surface as status='error' with a readable message, while (None, meta)
    means the far more specific "this video genuinely has no caption track"
    and lands on status='no_transcript'. Reporting the first as the second is
    what made a video with a perfectly good Chinese caption track show up as
    'no captions' on the server, with no way to tell the two apart."""


class _CaptionsBlocked(Exception):
    """Internal marker: the caption request was refused for a reason that has
    nothing to do with whether captions exist (cloud-provider IP ban, PoToken
    requirement, age gate, ...) — worth retrying through NotebookLM."""


def _blocked_error_types() -> tuple:
    """The youtube_transcript_api errors that mean "refused", not "absent".

    All of these subclass CouldNotRetrieveTranscript, so they MUST be caught
    before the base class or they get swallowed as "no captions" — that was
    the bug. Resolved lazily and defensively (getattr): the library renames
    error classes across releases, and a missing name here must not crash
    ingestion."""
    from youtube_transcript_api import _errors as e
    names = ("RequestBlocked", "IpBlocked", "PoTokenRequired", "AgeRestricted",
             "VideoUnplayable", "YouTubeRequestFailed", "YouTubeDataUnparsable")
    return tuple(t for t in (getattr(e, n, None) for n in names) if t is not None)


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _http_get_json(url: str, timeout: int = 10) -> dict:
    """Mirrors podcast._http_get's plain-urllib style (no extra HTTP
    dependency) but decodes JSON — used for the oEmbed metadata lookup."""
    req = urllib.request.Request(url, headers={"User-Agent": "biangbiangmian3000/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def parse_video_id(url: str) -> str | None:
    """Extract a YouTube video id from any of the URL shapes Daniel might
    paste: youtube.com/watch?v=, youtu.be/, youtube.com/shorts/,
    m.youtube.com/watch?v=, with or without extra query params. Returns None
    for anything that isn't a recognizable YouTube video URL."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "m.youtube.com":
        host = "youtube.com"

    if host in ("youtube.com", "youtube-nocookie.com"):
        if parsed.path == "/watch":
            qs = urllib.parse.parse_qs(parsed.query)
            vid = (qs.get("v") or [None])[0]
            return vid or None
        m = re.match(r"^/(?:shorts|embed|live)/([A-Za-z0-9_-]+)", parsed.path)
        if m:
            return m.group(1)
        return None

    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid or None

    return None


def fetch_metadata(video_id: str) -> dict:
    """Video title + channel name via oEmbed (no API key needed). Raises on
    a network/HTTP failure — the caller (routes/knowledge.py) surfaces that
    as a 400/500, since without a title there's nothing sensible to store."""
    query = urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "format": "json",
    })
    data = _http_get_json(f"{_OEMBED_URL}?{query}", timeout=10)
    return {
        "title": data.get("title") or video_id,
        "author_name": data.get("author_name"),
    }


# ---------------------------------------------------------------------------
# Audiobook ingestion (#1054): YouTube offers no downloadable captions for
# these (that's the whole reason this feature is last in #1047's umbrella —
# see the module CLAUDE.md entry), so the pipeline downloads the audio
# itself and hands it to audio/asr_local.py (whisper.cpp, queued via
# audio_jobs) instead. yt-dlp is used here the same way knowledge/instagram.py
# already uses it — see knowledge/_ytdlp.py for the shared subprocess plumbing.
# ---------------------------------------------------------------------------

# A ten-hour audiobook's audio track is a few hundred MB — the download
# itself (bandwidth-bound, not duration-bound) takes minutes on any real
# connection, but this is given generous headroom anyway since it runs
# synchronously inside the add-a-URL request.
_AUDIO_DOWNLOAD_TIMEOUT = 1800

# Single lightweight metadata request, same budget as knowledge/instagram.py's
# equivalent lookup.
_DURATION_LOOKUP_TIMEOUT = 60


class AudiobookDownloadError(Exception):
    """A YouTube video's audio could not be downloaded, or its duration could
    not be determined, for the audiobook ingestion path (#1054). Callers
    (knowledge.ingest._ingest_audiobook) wrap this into IngestError so it
    reads the same as every other ingestion failure."""


def fetch_duration(video_id: str) -> int | None:
    """Video duration in seconds via `yt-dlp --dump-json` — oEmbed
    (fetch_metadata above) doesn't include it, and it's the audiobook
    download guard's only way to know "is this short enough to fetch without
    asking first" before paying for a multi-hundred-MB download.

    Raises AudiobookDownloadError if yt-dlp itself can't be run or reports a
    failure — the caller must not treat "couldn't check" as "must be fine".
    Returns None only when yt-dlp succeeds but the video genuinely has no
    duration field (e.g. an ongoing livestream) — the caller treats that as
    "unknown, ask first" too, since there's nothing here to say it's safe.
    """
    cmd = [yt_dlp_path(), "--dump-json", "--no-warnings", _watch_url(video_id)]
    result = run_yt_dlp(cmd, _DURATION_LOOKUP_TIMEOUT, "duration lookup", AudiobookDownloadError)
    if result.returncode != 0:
        raise AudiobookDownloadError(format_error(result.stderr, "duration lookup"))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AudiobookDownloadError(f"yt-dlp returned unparsable metadata for {video_id}: {e}") from e
    return data.get("duration")


def download_audio(url: str, dest_dir: str) -> str:
    """Download `url`'s audio track as an mp3 into dest_dir via yt-dlp,
    returning the mp3 path. Mirrors knowledge/instagram.py's download_audio —
    same structure, same yt-dlp invocation shape — just no cookie jar
    (public YouTube videos need none) and raises AudiobookDownloadError
    instead of InstagramError.
    """
    out_template = os.path.join(dest_dir, "audio.%(ext)s")
    cmd = [
        yt_dlp_path(), "-f", "bestaudio", "-x", "--audio-format", "mp3",
        "--no-warnings", "-o", out_template, url,
    ]
    result = run_yt_dlp(cmd, _AUDIO_DOWNLOAD_TIMEOUT, "audio download", AudiobookDownloadError)
    if result.returncode != 0:
        raise AudiobookDownloadError(format_error(result.stderr, "audio download"))

    mp3_path = os.path.join(dest_dir, "audio.mp3")
    if not os.path.isfile(mp3_path):
        raise AudiobookDownloadError(f"yt-dlp reported success but no mp3 was produced for {url}")
    return mp3_path


def _fetch_captions_via_api(video_id: str) -> tuple[str | None, str | None]:
    """The youtube-transcript-api half of fetch_captions: returns
    (text_or_None, language_code). None means this video genuinely has no
    usable caption track. Raises _CaptionsBlocked when YouTube refused the
    request instead (see _blocked_error_types)."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import CouldNotRetrieveTranscript

    blocked = _blocked_error_types()
    api = YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except blocked as e:
        raise _CaptionsBlocked(f"{type(e).__name__}") from e
    except CouldNotRetrieveTranscript as e:
        logger.info("knowledge.youtube: no caption list for %s: %s", video_id, e)
        return None, None

    transcript = None
    try:
        transcript = transcript_list.find_transcript(_LANGUAGE_PRIORITY)
    except CouldNotRetrieveTranscript:
        # None of the priority languages exist — fall back to any available
        # track rather than giving up (issue #651: "再退到任意可用语言").
        try:
            transcript = next(iter(transcript_list))
        except StopIteration:
            transcript = None

    if transcript is None:
        logger.info("knowledge.youtube: no transcript track at all for %s", video_id)
        return None, None

    try:
        fetched = transcript.fetch()
    except blocked as e:
        raise _CaptionsBlocked(f"{type(e).__name__}") from e
    except CouldNotRetrieveTranscript as e:
        logger.warning("knowledge.youtube: fetch failed for %s (%s): %s",
                        video_id, transcript.language_code, e)
        return None, None

    text = " ".join(s.text.strip() for s in fetched if (s.text or "").strip())
    if not text.strip():
        return None, None
    return text, transcript.language_code


def fetch_captions(video_id: str) -> tuple[str | None, dict]:
    """Fetch a video's caption text, signature-compatible with
    podcast.fetch_transcript's (text, meta) return.

    Two sources, in this order:
      1. youtube-transcript-api — free and instant, and the only one that
         works when the caller's IP isn't blocked (Daniel's laptop).
      2. NotebookLM (#681) — used only when YouTube *refused* the request.
         The production server runs on a Contabo VPS and YouTube blocks
         cloud-provider IPs wholesale (`RequestBlocked`), so on the server
         this is the normal path, not the exception. NotebookLM fetches the
         video itself, from Google's own network, so our IP never matters.

    Returns (None, meta) only for a video that genuinely has no caption
    track — the caller stores status='no_transcript' for that. When YouTube
    refuses AND NotebookLM can't stand in, raises CaptionsUnavailable so the
    episode lands on status='error' with a message that says what actually
    happened. This function does NOT download audio for Whisper (#651).
    """
    meta: dict = {"transcript_source": "youtube_captions", "language_code": None}

    try:
        text, language_code = _fetch_captions_via_api(video_id)
    except _CaptionsBlocked as e:
        logger.warning("knowledge.youtube: YouTube refused captions for %s (%s), "
                       "trying NotebookLM", video_id, e)
        # Local import: podcast.fetch_transcript dispatches into this module
        # for kind='video', so podcast can't be imported at module load time.
        import podcast
        text = podcast.transcribe_url_via_notebooklm(_watch_url(video_id), video_id)
        if not text or not text.strip():
            raise CaptionsUnavailable(
                f"YouTube refused the caption request ({e}) — the server's IP is "
                f"likely blocked — and the NotebookLM fallback returned nothing"
            ) from e
        meta["transcript_source"] = "notebooklm"
        return _normalize(text), meta

    if text is None:
        return None, meta

    meta["language_code"] = language_code
    return _normalize(text), meta


def _normalize(text: str) -> str:
    """Reuse podcast's ASR cleanup (CJK-spacing collapse + Traditional ->
    Simplified). Local import for the same import-cycle reason as above."""
    from podcast import _normalize_transcript
    return _normalize_transcript(text)

"""Instagram Reel/Post ingestion (issue #750): resolve an Instagram URL to
metadata (podcast._ingest_instagram / knowledge.ingest._ingest_instagram)
and, later, downloadable audio for transcription
(podcast._transcribe_instagram). Unlike knowledge/youtube.py there is no
captions API to try first — Instagram exposes none — so every Reel/Post goes
straight to "download the audio and transcribe it" (Groq whisper-large-v3-
turbo, falling back to OpenAI whisper-1, see podcast.py).

Uses yt-dlp — a system-level command-line tool, like ffmpeg, NOT a Python
dependency (see requirements.txt / scripts/README.md) — rather than a
hand-rolled scraper: Instagram's page markup changes often and yt-dlp
already tracks it, and it needs a login cookie jar for reliable access
anyway (see _cookies_file below).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse

from knowledge._ytdlp import format_error, run_yt_dlp, yt_dlp_path

logger = logging.getLogger(__name__)

# instagram.com/reel/<code>/, /reels/<code>/, /p/<code>/, /tv/<code>/ — all
# four path shapes Daniel might actually share (Reels, the older "post" and
# "IGTV" video shapes), each followed by the shortcode.
_SHORTCODE_RE = re.compile(r"^/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")

# yt-dlp timeouts: metadata is a single lightweight request; audio download
# (+ Instagram's own throttling) needs more headroom. Both are well under
# the outer HTTP request's own timeout budget for the add-a-URL flow.
_METADATA_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 300

_DEFAULT_COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "instagram_cookies.txt",
)


class InstagramError(Exception):
    """A Reel/Post URL could not be resolved to metadata, or its audio could
    not be downloaded. Callers (knowledge.ingest, podcast._transcribe_instagram)
    let this propagate so it lands on podcast_episodes.status='error' with a
    readable message — see _yt_dlp_error_message for why that message
    specifically calls out cookies when the failure looks cookie-shaped."""


def _yt_dlp_path() -> str:
    return yt_dlp_path()


def _cookies_file() -> str | None:
    """INSTAGRAM_COOKIES_FILE (default data/instagram_cookies.txt), only if
    it actually exists. Missing is NOT an error here — a public Reel
    sometimes works without a login cookie at all — but yt-dlp reporting a
    login/rate-limit-shaped failure without one gets the cookie hint in
    _yt_dlp_error_message, since that's overwhelmingly the real cause."""
    path = os.environ.get("INSTAGRAM_COOKIES_FILE", _DEFAULT_COOKIES_FILE)
    return path if path and os.path.isfile(path) else None


def parse_shortcode(url: str) -> str | None:
    """Extract the Reel/Post shortcode from any Instagram URL shape Daniel
    might share: /reel/<code>/, /reels/<code>/, /p/<code>/, /tv/<code>/,
    with or without www., a trailing slash, or query params. Returns None
    for anything that isn't a recognizable Instagram post/reel/video URL —
    including Instagram's own non-post pages (profile, home, etc.)."""
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
    if host != "instagram.com":
        return None

    m = _SHORTCODE_RE.match(parsed.path)
    return m.group(1) if m else None


def _yt_dlp_error_message(stderr: str, action: str) -> str:
    """Instagram cookies expire silently, and yt-dlp's failure for that looks
    like an ordinary login-wall/rate-limit error, not a clean "your cookies
    expired" status. Daniel only ever sees this via the Signal receipt
    (#749's error channel) — the message must spell out the likely cause
    rather than just relay yt-dlp's (often terse/cryptic) stderr verbatim."""
    hint = (
        " — possibly expired/missing Instagram cookies, see scripts/README.md"
        if re.search(r"login|rate.?limit|private|cookie", stderr, re.IGNORECASE)
        else ""
    )
    return format_error(stderr, action, hint)


def _run_yt_dlp(cmd: list[str], timeout: int, action: str):
    """Instagram-flavoured wrapper around knowledge._ytdlp.run_yt_dlp — same
    behaviour, just raises InstagramError (the type this module's callers
    already catch) instead of a generic one."""
    return run_yt_dlp(cmd, timeout, action, InstagramError)


def fetch_metadata(url: str) -> dict:
    """Reel/Post metadata via `yt-dlp --dump-json` (#750): {title, uploader,
    duration, webpage_url}. Instagram titles are frequently empty or just
    duplicate the full caption — title falls back to the first line of
    `description`, then to the URL's shortcode, so there's always something
    short and non-empty to store as podcast_episodes.title.

    Raises InstagramError on any yt-dlp/parsing failure — same contract as
    knowledge.youtube.fetch_metadata (nothing sensible to store without at
    least a title).
    """
    cmd = [_yt_dlp_path(), "--dump-json", "--no-warnings"]
    cookies = _cookies_file()
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)

    result = _run_yt_dlp(cmd, _METADATA_TIMEOUT, "metadata lookup")
    if result.returncode != 0:
        raise InstagramError(_yt_dlp_error_message(result.stderr, "metadata lookup"))

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise InstagramError(f"yt-dlp returned unparsable metadata for {url}: {e}") from e

    title = (data.get("title") or "").strip()
    if not title:
        description = (data.get("description") or "").strip()
        title = description.splitlines()[0].strip() if description else ""
    if not title:
        title = parse_shortcode(url) or url

    return {
        "title": title,
        "uploader": data.get("uploader"),
        "duration": data.get("duration"),
        "webpage_url": data.get("webpage_url") or url,
    }


def download_audio(url: str, dest_dir: str) -> str:
    """Download `url`'s audio track as an mp3 into dest_dir via yt-dlp,
    returning the mp3 path. Raises InstagramError on failure — see
    _yt_dlp_error_message for the cookie-expiry hint
    podcast._transcribe_instagram relies on to surface a useful error to
    Daniel (via the Signal receipt, #749) rather than a bare "download
    failed"."""
    out_template = os.path.join(dest_dir, "audio.%(ext)s")
    cmd = [
        _yt_dlp_path(), "-f", "bestaudio", "-x", "--audio-format", "mp3",
        "--no-warnings", "-o", out_template,
    ]
    cookies = _cookies_file()
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)

    result = _run_yt_dlp(cmd, _DOWNLOAD_TIMEOUT, "audio download")
    if result.returncode != 0:
        raise InstagramError(_yt_dlp_error_message(result.stderr, "audio download"))

    mp3_path = os.path.join(dest_dir, "audio.mp3")
    if not os.path.isfile(mp3_path):
        raise InstagramError(f"yt-dlp reported success but no mp3 was produced for {url}")
    return mp3_path

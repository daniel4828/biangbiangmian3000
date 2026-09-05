"""Podcast crawler (issue #479): discover new episodes from podcast RSS
feeds, transcribe them, summarize into German + HSK5+ vocabulary via AI,
find a Spotify link, and email a notification.

Mostly-pure functions + logger, one module for
the whole pipeline.

Source (#497, feeds moved to the podcast_feeds table in #502): plain public
RSS feeds — the original YouTube-channel source (#479) was retired because
YouTube started bot-verifying the server's datacenter IP with no durable
Cookie fix (#491). RSS gives a direct MP3 enclosure link, so there is no
audio *download* step for the primary transcription path (Tingwu, #498)
at all — only the paid/optional fallbacks (Whisper #485, NotebookLM #486)
still need the audio downloaded+transcoded locally, via plain urllib (no
more yt-dlp).
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import ai
import database
import translator
import zh_annotate

logger = logging.getLogger(__name__)

# On a feed's very first crawl (no episodes from that feed in the DB yet)
# only backfill this many of its most recent episodes — otherwise
# subscribing to an established feed would try to transcribe/summarize its
# entire back catalog in one cycle (#497). Backfilled episodes are never
# auto-processed (#502, see _run_check_locked's is_backfill check) — they're
# metadata-only rows the UI lists for manual transcription — so this can be
# generous (10, was 3 pre-#502) without risking a burst of paid/slow
# transcription work on a freshly-added source.
FIRST_RUN_BACKFILL = 10

# "Load more" (#559) pulls back-catalog older than the initial backfill in
# on-demand pages of this size, metadata-only. Kept modest so one click on a
# huge feed (声动早咖啡 ~1000 eps) adds a manageable chunk, not the whole backlog.
LOAD_MORE_PAGE = 20

# itunes:duration namespace, used to read episode duration straight from the
# RSS feed (#497) — this is what lets duration-based guardrails/gates run
# *before* any download.
_ITUNES_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

# Audio transcription (#485 Whisper, #486 NotebookLM, #498 Tingwu) cost/time
# guardrails. Whisper/NotebookLM segments stay small; this guardrail applies
# to the shared download/transcode step, so it protects both paid/optional
# fallback paths. Tingwu (primary) is submitted as a direct URL — Alibaba
# does its own duration limiting server-side — but the same 3h check is
# applied up front (before any transcriber runs) since RSS gives us the
# duration for free.
_AUDIO_MAX_SECONDS = 3 * 60 * 60  # 3h — guards against a mislabeled/huge episode
_WHISPER_SEGMENT_SECONDS = 20 * 60  # 20min segments stay well under OpenAI's 25MB upload cap

# Instagram Reel transcription (#750): Groq's OpenAI-compatible audio
# endpoint running whisper-large-v3-turbo — ~9x cheaper and ~10x faster than
# the OpenAI whisper-1 fallback below. GROQ_API_KEY is optional (same
# contract as Tingwu/NotebookLM's credential checks): unset just means
# "fall back to whisper-1", not a failure.
_GROQ_MODEL = "whisper-large-v3-turbo"

# Instagram Reel hallucination guardrails (#750): a Reel that's pure
# background music with no speech makes Whisper-family models *invent*
# plausible-sounding text rather than admit silence. Both transcription
# providers in the Instagram chain (_transcribe_via_groq,
# _transcribe_via_whisper with filter_hallucinations=True) are asked for
# response_format="verbose_json" specifically so these checks — run by
# _filter_whisper_hallucinations — have real per-segment metadata to work
# with regardless of which provider actually ran.
_HALLUCINATION_NO_SPEECH_PROB = 0.6    # Whisper's own "this might be silence" signal
_HALLUCINATION_MIN_AVG_LOGPROB = -1.0  # the model's own token-confidence for the segment
_HALLUCINATION_REPEAT_COUNT = 3        # same sentence N times in a row = looping on nothing
_HALLUCINATION_MIN_WORDS = 20          # too short to be worth summarizing/carding either way

# NotebookLM (#486) settings. The notebook is created once and its id cached
# in podcast_config so every episode's audio lands in the same place; the
# source is deleted right after we read its fulltext so the notebook never
# grows unbounded. notebooklm-py is unofficial (undocumented Google RPCs) and
# has no documented per-source size cap; our mp3s are 16kHz/mono/32kbps, so
# even a full 3h episode (the guardrail above) is only ~43MB — the ceiling
# below is a generous safety net, not a known Google limit.
NOTEBOOKLM_NOTEBOOK_TITLE = "biangbiangmian3000 Transcripts"
_NOTEBOOKLM_INDEX_TIMEOUT = 10 * 60  # 10min cap for source indexing to finish
# Hard ceiling for a whole NotebookLM round (upload + indexing + fulltext/ask).
# wait_until_ready only bounds the indexing wait — the other RPCs have no
# timeout of their own, and one stalled call froze a check for 14h holding the
# run lock (#565). On expiry asyncio.wait_for cancels the round and the caller
# falls back down its chain (Tingwu/Whisper, API summarizers).
_NOTEBOOKLM_RUN_TIMEOUT = _NOTEBOOKLM_INDEX_TIMEOUT + 15 * 60
_NOTEBOOKLM_MAX_UPLOAD_BYTES = 190 * 1024 * 1024

# chat.ask silently returns an empty stream above roughly 4900 prompt characters
# (binary-searched against production, 2026-09-04, #1040) — notebooklm-py then
# raises ChatResponseParseError with a misleading "wire format may have changed"
# message. The cap below leaves headroom because the real limit may be counted
# in tokens, where a Chinese prompt buys far fewer characters.
_NOTEBOOKLM_MAX_PROMPT_CHARS = 4500

# Whisper (#485) is real money, so it only runs for short episodes (#495):
# duration <= whisper_max_minutes. The earlier title filter ("早咖啡", #486)
# never matched real episode titles and is retired (the config key is
# ignored). NotebookLM (free) is not subject to this gate. Tingwu (#498,
# primary) isn't subject to it either — it's cheaper than Whisper per hour.
_DEFAULT_WHISPER_MAX_MINUTES = 30

# Tongyi Tingwu (通义听悟, #498) offline transcription task polling. Tasks
# for a 15-90min podcast episode typically finish in a few minutes; 20min at
# 15s intervals is a generous ceiling before giving up and falling back.
_TINGWU_ENDPOINT = "tingwu.cn-beijing.aliyuncs.com"
_TINGWU_REGION = "cn-beijing"
_TINGWU_POLL_INTERVAL_SECONDS = 15
_TINGWU_POLL_TIMEOUT_SECONDS = 20 * 60


def _http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "biangbiangmian3000/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Repo root (this file's directory) — kept around for path resolution
# (e.g. the run-lock file below).
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Episode discovery (RSS, #497)
# ---------------------------------------------------------------------------

def _parse_itunes_duration(raw: str | None) -> int | None:
    """Parse an itunes:duration value into whole seconds. The iTunes podcast
    spec allows plain seconds, MM:SS or H:MM:SS — real feeds use all three
    (Daniel's two feeds alone mix MM:SS and H:MM:SS)."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _parse_rss_pubdate(raw: str | None) -> str | None:
    """RSS <pubDate> (RFC 822) -> ISO 8601, matching the format previously
    stored from YouTube's Atom <published> (which was already ISO)."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return raw  # best-effort: keep the raw string rather than lose it


def _parse_feed_item(item, feed_url: str) -> dict | None:
    """Extract one RSS <item> into an episode dict, or None if it has no
    stable id. Shared by fetch_new_videos() and load_more_episodes() so the
    two stay in lockstep on how a feed item maps to an episode row."""
    enclosure_el = item.find("enclosure")
    audio_url = enclosure_el.get("url") if enclosure_el is not None else None
    guid_el = item.find("guid")
    guid = guid_el.text.strip() if guid_el is not None and guid_el.text else None
    # Fall back to the enclosure URL as the unique id when a feed omits <guid>.
    video_id = guid or audio_url
    if not video_id:
        return None
    title_el = item.find("title")
    link_el = item.find("link")
    pubdate_el = item.find("pubDate")
    duration_el = item.find("itunes:duration", _ITUNES_NS)
    return {
        "video_id": video_id,
        "channel_id": feed_url,
        "title": (title_el.text.strip() if title_el is not None and title_el.text else video_id),
        "published_at": _parse_rss_pubdate(pubdate_el.text if pubdate_el is not None else None),
        "youtube_url": (link_el.text.strip() if link_el is not None and link_el.text else feed_url),
        "audio_url": audio_url,
        "duration_seconds": _parse_itunes_duration(duration_el.text if duration_el is not None else None),
    }


def fetch_new_videos() -> list[dict]:
    """Return episodes from the configured podcast RSS feeds
    (podcast_feeds table, #502 — one row per source with its own
    auto_process flag) that aren't in the DB yet. Zero API keys needed —
    plain public RSS/XML, no bot walls.

    Per feed: if that specific feed has zero episodes in the DB yet, only
    its latest FIRST_RUN_BACKFILL episodes are returned (backfill mode,
    marked `is_backfill: True`, see below). Otherwise, RSS items are
    newest-first by convention, so items are walked from the top and
    collection *stops at the first already-known guid* — everything older
    than that was either already ingested or deliberately left out of the
    initial backfill, and must stay left out forever (else every subsequent
    crawl of a long-running feed like 声动早咖啡, which has ~1000
    back-catalog episodes, would dump the *entire* backlog as "new" in one
    shot the very next cycle — this was caught by a real backfill+re-run
    test against both of Daniel's feeds during #497's implementation, not
    just theorized). A feed that fails to fetch/parse is logged and
    skipped — one broken feed must not block the others.

    Each returned dict carries `auto_process` (bool, copied from the feed
    row) and `is_backfill` (bool, True for episodes discovered on a feed's
    very first crawl) — `_run_check_locked` uses both to decide whether to
    immediately transcribe+summarize a newly-discovered episode (#502):
    only non-backfill episodes from an auto_process feed are processed
    automatically; everything else is stored metadata-only for manual
    transcription from the UI.

    If a feed's `title` is still unset (freshly added via the UI, no
    network request made at add-time), it's backfilled here from the RSS
    channel's own <title> element.
    """
    feeds = database.list_feeds()
    if not feeds:
        logger.warning("podcast: no feeds configured (podcast_feeds table)")
        return []

    known = database.get_known_video_ids()

    videos: list[dict] = []
    for feed in feeds:
        feed_url = feed["url"]
        try:
            root = ElementTree.fromstring(_http_get(feed_url, timeout=30))
        except (urllib.error.URLError, ElementTree.ParseError) as e:
            logger.warning("podcast: failed to fetch/parse feed %s: %s", feed_url, e)
            continue

        feed_title = feed.get("title")
        if not feed_title:
            channel_title_el = root.find("channel/title")
            if channel_title_el is not None and channel_title_el.text:
                feed_title = channel_title_el.text.strip()
                database.update_feed(feed["id"], title=feed_title)

        auto_process = bool(feed.get("auto_process"))
        is_first_run = not database.has_any_episode_for_feed(feed_url)
        feed_videos: list[dict] = []
        for item in root.findall(".//item"):
            video = _parse_feed_item(item, feed_url)
            if video is None:
                continue
            if video["video_id"] in known:
                if is_first_run:
                    continue  # shouldn't happen (nothing's known yet), but harmless
                break  # newest-first feed: everything from here on is old backlog

            video["auto_process"] = auto_process
            video["is_backfill"] = is_first_run
            # #935: the show's own name is the author of every one of its
            # episodes. channel_id already holds the feed URL, which is not
            # something anyone wants to see in an author filter.
            video["feed_title"] = feed_title
            feed_videos.append(video)
            if is_first_run and len(feed_videos) >= FIRST_RUN_BACKFILL:
                break

        videos.extend(feed_videos)

    return videos


def ingest_feed_episodes(feed_id: int, limit: int, root=None) -> int:
    """Store up to `limit` not-yet-known episodes of one feed as pending
    (metadata-only, never auto-processed), newest first. Shared by
    load_more_episodes (back-catalog paging) and create_feed (immediate
    backfill of the latest episodes right when a feed is added, #593).

    `root` may be a pre-parsed RSS ElementTree to avoid a redundant fetch
    (create_feed already fetched it to validate the URL); when None the feed
    is fetched here. Skips episodes already in the DB. Returns the count
    added. Raises ValueError (unknown feed) / RuntimeError (fetch/parse
    failure) for callers/routes to map to 404/500."""
    feed = database.get_feed(feed_id)
    if not feed:
        raise ValueError("feed not found")
    feed_url = feed["url"]
    if root is None:
        try:
            root = ElementTree.fromstring(_http_get(feed_url, timeout=30))
        except (urllib.error.URLError, ElementTree.ParseError) as e:
            raise RuntimeError(f"failed to fetch/parse feed: {e}")

    known = database.get_known_video_ids()
    added = 0
    for item in root.findall(".//item"):
        video = _parse_feed_item(item, feed_url)
        if video is None or video["video_id"] in known:
            continue
        database.create_pending_episode(
            video["video_id"], video["channel_id"], video["title"],
            video["published_at"], video["youtube_url"],
            video.get("audio_url"), video.get("duration_seconds"),
            author=feed.get("title"), platform="podcast",
        )
        added += 1
        if added >= limit:
            break
    return added


def load_more_episodes(feed_id: int) -> dict:
    """Ingest the next page of older back-catalog episodes for one feed,
    metadata-only (never auto-processed). Regular check() only walks the
    newest items and stops at the first already-known guid, so a feed's
    back-catalog older than the initial backfill is otherwise unreachable;
    this pulls it in on demand, LOAD_MORE_PAGE at a time, skipping anything
    already stored. Returns {"added": N}. Raises ValueError (unknown feed) /
    RuntimeError (fetch/parse failure) for the route to map to 404/500."""
    return {"added": ingest_feed_episodes(feed_id, LOAD_MORE_PAGE)}


# ---------------------------------------------------------------------------
# Audio download (#497: plain urllib from the RSS enclosure URL, no yt-dlp)
# ---------------------------------------------------------------------------

def _download_audio(audio_url: str, video_id: str, tmp_dir: str) -> str:
    """Download the RSS enclosure mp3 directly and transcode it to a single
    16kHz mono ~32kbps mp3 with ffmpeg. Shared by the Whisper (#485) and
    NotebookLM (#486) transcription paths (Tingwu, #498, is primary and
    needs no download at all — it's submitted the audio_url directly) so
    the (slow) download+transcode only ever happens once per episode.

    Returns mp3_path inside tmp_dir (the caller owns tmp_dir's lifetime).
    Duration is not re-derived here — the RSS itunes:duration guardrail
    check already happened in fetch_transcript *before* this is called, per
    issue #497 ("guardrails before download"). Raises RuntimeError on
    download/ffmpeg failures.
    """
    src_path = os.path.join(tmp_dir, "src_audio")
    req = urllib.request.Request(audio_url, headers={"User-Agent": "biangbiangmian3000/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(src_path, "wb") as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as e:
        raise RuntimeError(f"podcast: failed to download audio for {video_id}: {e}")

    mp3_path = os.path.join(tmp_dir, "full.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-ar", "16000", "-ac", "1", "-b:a", "32k",
        mp3_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"podcast: ffmpeg failed for {video_id}: {result.stderr[-500:]}")

    return mp3_path


def _split_audio_segments(mp3_path: str, tmp_dir: str, duration: float) -> list[str]:
    """Split the already-transcoded mp3 into <=_WHISPER_SEGMENT_SECONDS chunks
    for Whisper's per-request upload cap. Uses stream copy (-c copy, no
    re-encode) since the source is already 16kHz/mono/32kbps. Returns the
    single mp3_path unchanged if it's already short enough."""
    if duration <= _WHISPER_SEGMENT_SECONDS:
        return [mp3_path]

    cmd = [
        "ffmpeg", "-y", "-i", mp3_path,
        "-c", "copy", "-f", "segment", "-segment_time", str(_WHISPER_SEGMENT_SECONDS),
        os.path.join(tmp_dir, "seg_%03d.mp3"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"podcast: ffmpeg segmenting failed: {result.stderr[-500:]}")

    segments = sorted(f for f in os.listdir(tmp_dir) if f.startswith("seg_") and f.endswith(".mp3"))
    if not segments:
        raise RuntimeError("podcast: ffmpeg produced no audio segments")
    return [os.path.join(tmp_dir, s) for s in segments]


def _transcribe_via_whisper(mp3_path: str, duration: float, video_id: str, tmp_dir: str,
                            *, language: str | None = "zh",
                            model: str = "gpt-4o-mini-transcribe",
                            filter_hallucinations: bool = False) -> str | None:
    """Paid fallback (#485): segment the shared mp3 and transcribe each
    segment via OpenAI's audio.transcriptions endpoint.

    `language`/`model`/`filter_hallucinations` (#750): defaults reproduce the
    original Chinese-podcast-only behavior unchanged. The Instagram Reel
    chain (_transcribe_instagram) instead calls this with
    model="whisper-1" (the only OpenAI transcription model that accepts
    response_format="verbose_json" — gpt-4o-mini-transcribe rejects that
    value outright, so it can't give per-segment no_speech_prob/avg_logprob),
    language=None (Reels are German/English, not Chinese — forcing "zh"
    would corrupt the transcript), and filter_hallucinations=True so a
    music-only Reel's invented text gets caught by
    _filter_whisper_hallucinations exactly like the Groq path does.

    Returns None when OPENAI_API_KEY is simply missing (config choice, not a
    failure), or — filter_hallucinations=True only — when everything
    transcribed is filtered out as hallucination/silence. Raises on actual
    transcription failure; callers log and treat that the same as no
    transcript.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning("podcast: OPENAI_API_KEY not set, skipping Whisper for %s", video_id)
        return None

    segments = _split_audio_segments(mp3_path, tmp_dir, duration)

    import openai
    client = openai.OpenAI()
    texts: list[str] = []
    raw_segments: list = []
    for seg_path in segments:
        seg_name = os.path.basename(seg_path)
        result = None
        for attempt in range(2):  # one retry on transient failure
            try:
                with open(seg_path, "rb") as f:
                    kwargs = {"model": model, "file": f}
                    if language:
                        kwargs["language"] = language
                    if filter_hallucinations:
                        kwargs["response_format"] = "verbose_json"
                    result = client.audio.transcriptions.create(**kwargs)
                break
            except Exception as e:
                logger.warning(
                    "podcast: Whisper transcription failed (attempt %d) for %s/%s: %s",
                    attempt + 1, video_id, seg_name, e,
                )
        if result is None:
            raise RuntimeError(f"podcast: Whisper transcription failed twice for {video_id}/{seg_name}")

        if filter_hallucinations:
            seg_list = getattr(result, "segments", None) or []
            if seg_list:
                raw_segments.extend(seg_list)
            else:
                # This model/response_format combination didn't return
                # per-segment metadata after all — degrade gracefully to a
                # single pseudo-segment so the repeat/min-length checks in
                # _filter_whisper_hallucinations still run, just without the
                # no_speech_prob/avg_logprob checks (both skip cleanly on a
                # segment missing those keys).
                raw_segments.append({"text": (getattr(result, "text", "") or "").strip()})
        else:
            texts.append((result.text or "").strip())

    # Whisper is billed per minute, not per token. Log the audio duration (in
    # seconds) as input_tokens so database.stats._row_cost can price it via
    # the "per_minute" pricing entry — only on success, since a failed call
    # above already raised before reaching here.
    database.log_api_call(
        model=model, input_tokens=int(duration),
        output_tokens=0, purpose="podcast-transcribe",
    )

    if filter_hallucinations:
        transcript = _filter_whisper_hallucinations(raw_segments)
        if not transcript:
            logger.info("podcast: Whisper (%s) transcript for %s filtered out as hallucination/silence",
                        model, video_id)
            return None
        logger.info("podcast: Whisper (%s) transcribed %s (%d segment(s), %.0fs audio)",
                    model, video_id, len(segments), duration)
        return transcript

    transcript = " ".join(t for t in texts if t)
    logger.info(
        "podcast: Whisper transcribed %s (%d segment(s), %.0fmin)",
        video_id, len(segments), duration / 60,
    )
    return transcript or None


def _seg_field(seg, name: str, default=None):
    """Segment objects come back as SDK pydantic models (OpenAI/Groq clients,
    production) but as plain dicts (tests, and the degraded pseudo-segment
    fallback in _transcribe_via_whisper) — accept either (#750)."""
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def _filter_whisper_segments(segments: list) -> list:
    """Drop hallucinated segments from a Whisper/Groq verbose_json response
    and return the *segments themselves* (not just their text) that survive
    (#750, split from _filter_whisper_hallucinations in #1052 so audio ASR
    timestamps can be recovered — see that function's thin wrapper below). A
    Reel with no speech (background music only) makes Whisper-family models
    invent text with real confidence in the *words* they pick but strong
    internal signals that they're making it up:

    1. no_speech_prob > _HALLUCINATION_NO_SPEECH_PROB — the model's own guess
       that this segment is silence/non-speech, worth ignoring the emitted
       text for even though it went ahead and emitted some anyway.
    2. avg_logprob < _HALLUCINATION_MIN_AVG_LOGPROB — the model's own
       token-confidence for the segment.
    3. the exact same segment text repeated >= _HALLUCINATION_REPEAT_COUNT
       times in a row — a stronger signal than either probability alone, and
       catches cases they miss. This voids the WHOLE transcript, not just
       the repeats: a model confabulating anywhere in a clip this short
       isn't trustworthy anywhere else in it either.
    4. fewer than _HALLUCINATION_MIN_WORDS words survive checks 1-3 — too
       short to be worth summarizing/carding regardless of confidence.

    A segment missing no_speech_prob/avg_logprob (see
    _transcribe_via_whisper's degraded-fallback comment) simply skips checks
    1-2 for that segment; checks 3-4 still run on whatever text came back.
    """
    kept: list = []
    for seg in segments:
        text = (_seg_field(seg, "text") or "").strip()
        if not text:
            continue
        no_speech = _seg_field(seg, "no_speech_prob")
        if no_speech is not None and no_speech > _HALLUCINATION_NO_SPEECH_PROB:
            continue
        avg_logprob = _seg_field(seg, "avg_logprob")
        if avg_logprob is not None and avg_logprob < _HALLUCINATION_MIN_AVG_LOGPROB:
            continue
        kept.append(seg)

    if not kept:
        return []

    run_text, run_len = None, 0
    for seg in kept:
        text = (_seg_field(seg, "text") or "").strip()
        if text == run_text:
            run_len += 1
        else:
            run_text, run_len = text, 1
        if run_len >= _HALLUCINATION_REPEAT_COUNT:
            return []

    joined = " ".join((_seg_field(seg, "text") or "").strip() for seg in kept)
    if _word_count(joined) < _HALLUCINATION_MIN_WORDS:
        return []
    return kept


def _filter_whisper_hallucinations(segments: list) -> str:
    """Thin wrapper around _filter_whisper_segments (#1052) for the existing
    text-only callers (_transcribe_via_whisper, _transcribe_via_groq,
    _transcribe_instagram) — joins the surviving segments' text exactly as
    this function always has. See _filter_whisper_segments for the four
    hallucination checks themselves."""
    kept = _filter_whisper_segments(segments)
    return " ".join((_seg_field(seg, "text") or "").strip() for seg in kept)


_CJK_CHAR_RE = re.compile(r"[一-鿿]")
_NON_CJK_TOKEN_RE = re.compile(r"[^\s一-鿿]+")


def _word_count(text: str) -> int:
    """Estimate a word count for mixed Chinese/Western text (#750), used to
    decide whether a hallucination-filtered transcript is still worth keeping
    (_HALLUCINATION_MIN_WORDS). Chinese has no spaces
    between words, so this counts CJK *characters* (roughly one word/morpheme
    each — close enough for a threshold check) and adds the count of
    whitespace-delimited non-CJK tokens — a mixed transcript (e.g. a German
    Reel with an occasional Chinese aside) gets a reasonable combined
    estimate instead of one language's counting rule misapplied to the
    other."""
    if not text:
        return 0
    cjk_chars = len(_CJK_CHAR_RE.findall(text))
    western_tokens = len(_NON_CJK_TOKEN_RE.findall(text))
    return cjk_chars + western_tokens


def _is_chinese_text(text: str, threshold: float = 0.2) -> bool:
    """True when at least `threshold` fraction of `text`'s non-whitespace
    characters are CJK (#750). Decides build_transcript_de's translation
    direction (#772: zh->de for a Chinese transcript, ->zh for anything else)
    so a German/English Instagram Reel transcript — stored in the same
    transcript_zh column, see podcast_episodes' schema.sql docstring — isn't
    treated as Chinese and run through a zh->de translator that would mangle
    it. 0.2 is deliberately low: real Chinese text (even mixed with English
    loanwords/numbers) sits well above it, while non-Chinese text with an
    occasional Chinese aside (e.g. one word said in Mandarin) stays well
    below.

    The measurement itself lives in zh_annotate.cjk_ratio (#904) — ai.py and
    knowledge/rendition.py need the same primitive to reject a summary_de
    that was written in Chinese, and three copies of one ratio would drift."""
    return zh_annotate.is_chinese_text(text, threshold)


def _probe_duration_seconds(path: str) -> float:
    """ffprobe wrapper for an audio file's duration in seconds (#750) — used
    for accurate per-minute Whisper/Groq cost logging when there's no RSS
    itunes:duration to read (Instagram Reels have no RSS feed at all).
    Best-effort: returns 0.0 (logged) on any failure, which only means the
    cost log for that call undercounts — it must never block transcription."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning("podcast: ffprobe duration probe failed for %s: %s", path, e)
        return 0.0


def _transcribe_via_groq(mp3_path: str, duration: float, video_id: str) -> str | None:
    """Instagram Reel transcription, primary path (#750): Groq's
    OpenAI-compatible audio endpoint running whisper-large-v3-turbo — ~9x
    cheaper and ~10x faster than the OpenAI whisper-1 fallback
    (_transcribe_via_whisper). Returns None (not a failure, same contract as
    every other optional-credential transcriber in this module — Tingwu,
    NotebookLM) when GROQ_API_KEY isn't configured, or when the whole
    transcript is filtered out as hallucination/silence
    (_filter_whisper_hallucinations). Raises on an actual API failure; the
    caller (_transcribe_instagram) logs and falls through to whisper-1."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.info("podcast: GROQ_API_KEY not set, skipping Groq for %s", video_id)
        return None

    import openai
    client = openai.OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    with open(mp3_path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model=_GROQ_MODEL, file=f, response_format="verbose_json",
        )

    # Billed per minute of audio, same accounting convention as Whisper (see
    # _transcribe_via_whisper) — only reached after a successful API call.
    database.log_api_call(
        model=_GROQ_MODEL, input_tokens=int(duration),
        output_tokens=0, purpose="podcast-transcribe",
    )

    seg_list = getattr(resp, "segments", None) or []
    raw_segments = seg_list if seg_list else [{"text": (getattr(resp, "text", "") or "").strip()}]
    transcript = _filter_whisper_hallucinations(raw_segments)
    if not transcript:
        logger.info("podcast: Groq transcript for %s filtered out as hallucination/silence", video_id)
        return None
    logger.info("podcast: Groq transcribed %s (%.0fs audio)", video_id, duration)
    return transcript


def _is_instagram_url(url: str) -> bool:
    return "instagram.com" in (url or "").lower()


def _transcribe_instagram(video: dict) -> tuple[str | None, dict]:
    """Instagram Reel transcription chain (#750): download the audio once,
    then try Groq's whisper-large-v3-turbo first (cheap/fast), falling back
    to OpenAI's whisper-1 when GROQ_API_KEY isn't configured or Groq itself
    errors. Both requests ask for response_format="verbose_json" so
    _filter_whisper_hallucinations runs at full fidelity regardless of which
    provider actually ran — Reels are very often pure background music with
    no speech at all, and that filter is the entire reason this function
    exists rather than just calling _transcribe_via_whisper the way the RSS
    podcast path does.

    The audio *download* failing (dead link, expired/missing Instagram
    cookies, private post) is allowed to raise knowledge.instagram.
    InstagramError straight through to _process_episode's outer except,
    landing on status='error' with a message that names cookies as the
    likely cause — that's the only diagnostic signal Daniel gets, via the
    Signal receipt (#749). A clip that downloads fine but has nothing
    (hallucination-filtered) or both transcribers unavailable instead
    returns (None, meta), landing on status='no_transcript' — "downloaded
    but nothing to transcribe" is not an error.
    """
    import knowledge.instagram as ig

    video_id = video["video_id"]
    url = video.get("youtube_url") or ""
    meta = {"transcript_source": None}

    with tempfile.TemporaryDirectory() as tmp_dir:
        mp3_path = ig.download_audio(url, tmp_dir)  # InstagramError propagates on purpose
        duration = _probe_duration_seconds(mp3_path)

        transcript = None
        try:
            transcript = _transcribe_via_groq(mp3_path, duration, video_id)
        except Exception as e:
            logger.warning("podcast: Groq step raised for %s: %s", video_id, e)
        if transcript:
            meta["transcript_source"] = "groq_whisper"
            return transcript, meta

        try:
            transcript = _transcribe_via_whisper(
                mp3_path, duration, video_id, tmp_dir,
                language=None, model="whisper-1", filter_hallucinations=True,
            )
        except Exception as e:
            logger.warning("podcast: Whisper step raised for %s: %s", video_id, e)
            transcript = None
        if transcript:
            meta["transcript_source"] = "whisper"
            return transcript, meta

    logger.info("podcast: no usable transcript for Instagram Reel %s "
                "(both transcribers unavailable/filtered)", video_id)
    return None, meta


async def _get_or_create_notebooklm_notebook(client) -> str:
    """Return the id of the dedicated 'biangbiangmian3000 Transcripts' notebook,
    reusing the id cached in podcast_config when it still resolves; creates
    (and re-caches) a fresh one otherwise (first run, or the cached notebook
    was deleted server-side)."""
    cfg = database.get_podcast_config()
    cached_id = cfg.get("notebooklm_notebook_id")
    if cached_id:
        notebook = await client.notebooks.get_or_none(cached_id)
        if notebook is not None:
            return notebook.id

    notebook = await client.notebooks.create(NOTEBOOKLM_NOTEBOOK_TITLE)
    database.set_podcast_config("notebooklm_notebook_id", notebook.id)
    return notebook.id


async def _run_notebooklm_transcription(audio_path: str, video_id: str) -> str | None:
    import notebooklm

    async with notebooklm.NotebookLMClient.from_storage() as client:
        notebook_id = await _get_or_create_notebooklm_notebook(client)
        source = await client.sources.add_file(
            notebook_id, audio_path, title=f"podcast-{video_id}",
        )
        try:
            await client.sources.wait_until_ready(
                notebook_id, source.id, timeout=_NOTEBOOKLM_INDEX_TIMEOUT,
            )
            fulltext = await client.sources.get_fulltext(notebook_id, source.id)
            return fulltext.content or None
        finally:
            # Always drop the source afterwards so the notebook doesn't grow
            # unbounded — a delete failure here must not mask a successful
            # transcription, just log and move on.
            try:
                await client.sources.delete(notebook_id, source.id)
            except Exception as e:
                logger.warning("podcast: failed to delete NotebookLM source for %s: %s", video_id, e)


async def _run_notebooklm_url_source(url: str, video_id: str) -> str | None:
    """Same round-trip as _run_notebooklm_transcription, but the source is a
    URL NotebookLM fetches itself instead of a file we upload. add_url()
    detects YouTube links and adds them as video sources (notebooklm-py
    _sources.py), so the captions come back through Google's own access to
    YouTube — which is the entire point for #681."""
    import notebooklm

    async with notebooklm.NotebookLMClient.from_storage() as client:
        notebook_id = await _get_or_create_notebooklm_notebook(client)
        source = await client.sources.add_url(
            notebook_id, url, wait=True, wait_timeout=_NOTEBOOKLM_INDEX_TIMEOUT,
        )
        try:
            fulltext = await client.sources.get_fulltext(notebook_id, source.id)
            return fulltext.content or None
        finally:
            # Same cleanup contract as the file-upload path: the notebook must
            # not grow unbounded, and a delete failure must not mask a
            # successful fetch.
            try:
                await client.sources.delete(notebook_id, source.id)
            except Exception as e:
                logger.warning("podcast: failed to delete NotebookLM source for %s: %s", video_id, e)


def transcribe_url_via_notebooklm(url: str, video_id: str) -> str | None:
    """Free transcript path for a URL NotebookLM can open itself (#681):
    used as the YouTube-captions fallback when YouTube blocks our server's
    (cloud provider) IP — see knowledge/youtube.py.

    Failure contract is deliberately identical to _transcribe_via_notebooklm:
    an unofficial, undocumented API that can break at any time logs and
    returns None rather than raising. The *caller* decides what a None means
    here — knowledge.youtube turns it into a hard error rather than a silent
    'no captions', which is the whole bug #681 fixes.
    """
    try:
        import notebooklm  # noqa: F401 (import-only availability check)
    except ImportError:
        logger.info("podcast: notebooklm-py not installed, skipping NotebookLM URL source for %s", video_id)
        return None

    try:
        text = asyncio.run(asyncio.wait_for(
            _run_notebooklm_url_source(url, video_id),
            timeout=_NOTEBOOKLM_RUN_TIMEOUT,
        ))
    except FileNotFoundError:
        # No credentials file — `notebooklm login` was never run here.
        logger.info("podcast: NotebookLM not authenticated, skipping URL source for %s", video_id)
        return None
    except Exception as e:
        logger.warning("podcast: NotebookLM URL source failed for %s: %s", video_id, e)
        return None

    if text:
        logger.info("podcast: NotebookLM returned %d chars for %s", len(text), video_id)
    return text


def _normalize_transcript(text: str) -> str:
    """Clean up ASR output before storing/summarizing (#500). NotebookLM's
    speech recognition emits Traditional Chinese with a space between every
    character ("用 聲 音  生 動 活 潑") — that breaks jieba segmentation in
    the podcast review mode, HSK word matching against entries.word_zh, and
    wastes prompt tokens. Two steps:
      1. drop whitespace adjacent to any CJK character or full-width
         punctuation (keeps spacing inside pure-Latin runs; "2026 年" ->
         "2026年", "AI 记忆" -> "AI记忆", "活泼。 2026" -> "活泼。2026")
      2. Traditional -> Simplified via zhconv (pure-Python, requirements.txt)
    Tingwu/Whisper output is already Simplified without spacing — running it
    through here is a harmless no-op, so every source is normalized uniformly.
    """
    if not text:
        return text
    text = re.sub(r"(?<=[一-鿿　-〿＀-￯])\s+|\s+(?=[一-鿿　-〿＀-￯])", "", text)
    try:
        from zhconv import convert
        text = convert(text, "zh-cn")
    except ImportError:  # dependency missing (old venv) — spacing fix still applies
        logger.warning("podcast: zhconv not installed, skipping Traditional->Simplified conversion")
    return text


def _notebooklm_credentials_available() -> bool:
    """Best-effort presence check for NotebookLM login credentials (#510),
    used to decide whether the 'auto' transcriber/summarizer chains should
    even attempt the free NotebookLM path first. Not a hard gate — the
    actual calls (_transcribe_via_notebooklm, _summarize_via_notebooklm)
    still handle FileNotFoundError from notebooklm-py itself (e.g.
    credentials revoked after this check ran), same as before #510.

    Mirrors notebooklm-py's own storage_state.json lookup (see
    scripts/README.md): $NOTEBOOKLM_HOME/storage_state.json, or (if a
    profile was used at login) $NOTEBOOKLM_HOME/profiles/<profile>/
    storage_state.json. Split into its own function so tests can monkeypatch
    it directly instead of poking os.path.
    """
    try:
        import notebooklm  # noqa: F401 (import-only availability check)
    except ImportError:
        return False

    home = os.environ.get("NOTEBOOKLM_HOME") or os.path.expanduser("~/.notebooklm")
    if os.path.isfile(os.path.join(home, "storage_state.json")):
        return True
    profiles_dir = os.path.join(home, "profiles")
    if os.path.isdir(profiles_dir):
        for name in os.listdir(profiles_dir):
            if os.path.isfile(os.path.join(profiles_dir, name, "storage_state.json")):
                return True
    return False


def _transcribe_via_notebooklm(audio_path: str, video_id: str) -> str | None:
    """Free primary transcription path (#486): upload the shared mp3 to a
    dedicated NotebookLM notebook, wait for indexing, read the source's
    fulltext, then delete the source. Uses the unofficial notebooklm-py
    client (one-time browser login on Daniel's machine, credentials copied to
    the server — see scripts/README.md).

    This is an undocumented, unofficial API that can break at any time, so
    every failure mode (package not installed, not authenticated, RPC error,
    indexing timeout, ...) logs and returns None instead of raising —
    fetch_transcript then falls back to Whisper (or gives up, per
    podcast_config.transcriber).
    """
    try:
        import notebooklm  # noqa: F401 (import-only availability check)
    except ImportError:
        logger.info("podcast: notebooklm-py not installed, skipping NotebookLM for %s", video_id)
        return None

    size = os.path.getsize(audio_path)
    if size > _NOTEBOOKLM_MAX_UPLOAD_BYTES:
        logger.warning(
            "podcast: audio for %s is %.0fMB, exceeds the NotebookLM upload guardrail, skipping",
            video_id, size / 1024 / 1024,
        )
        return None

    try:
        transcript = asyncio.run(asyncio.wait_for(
            _run_notebooklm_transcription(audio_path, video_id),
            timeout=_NOTEBOOKLM_RUN_TIMEOUT,
        ))
    except FileNotFoundError:
        # AuthTokens.from_storage() raises this when no credentials file
        # exists yet — i.e. `notebooklm login` was never run. Not an error.
        logger.info("podcast: NotebookLM not authenticated (no credentials file), skipping for %s", video_id)
        return None
    except Exception as e:
        logger.warning("podcast: NotebookLM transcription failed for %s: %s", video_id, e)
        return None

    if transcript:
        logger.info("podcast: NotebookLM transcribed %s (%d chars)", video_id, len(transcript))
    return transcript


# NotebookLM sources have no documented per-source size cap; a transcript is
# plain text (much denser than audio), so this defensive ceiling is far more
# generous than any real episode transcript will hit — it just prevents an
# unbounded upload if something ever feeds this a huge string.
_NOTEBOOKLM_SUMMARY_TEXT_MAX_CHARS = 200_000


async def _run_notebooklm_summary(transcript: str, title: str, detail_level: str) -> str | None:
    """Async body of _summarize_via_notebooklm (#510): upload the transcript
    as a text source, wait for indexing, ask the short NotebookLM-specific
    summary question (ai.build_podcast_summary_prompt(..., for_notebooklm=True),
    #1040 — same JSON contract as the API path, but without the transcript
    inlined a second time and under the ~4900-char prompt cap) restricted to
    that one source, then delete the source. Returns the raw answer text
    (still needs ai.parse_podcast_summary_json) or None on any handled
    failure, including an over-length prompt (checked before any network
    call is made)."""
    import notebooklm

    # Build (and length-check) the question before touching the network at
    # all: if it's already over the limit, sending it would still burn a full
    # upload-plus-indexing round only to get an empty stream back (#1040).
    question = ai.build_podcast_summary_prompt(transcript, title, detail_level, for_notebooklm=True)
    if len(question) > _NOTEBOOKLM_MAX_PROMPT_CHARS:
        logger.warning(
            "podcast: NotebookLM summary prompt for %r is %d chars, over the %d cap — "
            "skipping chat.ask (would return an empty stream), falling back to the API chain",
            title, len(question), _NOTEBOOKLM_MAX_PROMPT_CHARS,
        )
        return None

    async with notebooklm.NotebookLMClient.from_storage() as client:
        notebook_id = await _get_or_create_notebooklm_notebook(client)
        source = await client.sources.add_text(
            notebook_id, f"podcast-transcript-{title}",
            transcript[:_NOTEBOOKLM_SUMMARY_TEXT_MAX_CHARS],
        )
        try:
            await client.sources.wait_until_ready(
                notebook_id, source.id, timeout=_NOTEBOOKLM_INDEX_TIMEOUT,
            )
            result = await client.chat.ask(notebook_id, question, source_ids=[source.id])
            return result.answer or None
        finally:
            # Same reasoning as _run_notebooklm_transcription: never let a
            # delete failure mask a successful summary, just log and move on.
            try:
                await client.sources.delete(notebook_id, source.id)
            except Exception as e:
                logger.warning("podcast: failed to delete NotebookLM summary source for %r: %s", title, e)


def _summarize_via_notebooklm(transcript: str, title: str, detail_level: str) -> dict | None:
    """Free summary path (#510): ask NotebookLM's chat interface to summarize
    the transcript (already-uploaded-and-deleted per episode, so this
    uploads its own throwaway text source) using the exact same prompt/JSON
    contract as the paid API path (ai.summarize_podcast_transcript), so
    downstream code (podcast._process_episode) doesn't care which path ran.

    Same unofficial-API failure posture as _transcribe_via_notebooklm: every
    failure mode (package missing, not authenticated, RPC error, indexing
    timeout, empty/unparseable answer, ...) logs and returns None instead of
    raising — summarize() then falls back to the API chain.
    """
    try:
        import notebooklm  # noqa: F401 (import-only availability check)
    except ImportError:
        logger.info("podcast: notebooklm-py not installed, skipping NotebookLM summary for %r", title)
        return None

    try:
        answer = asyncio.run(asyncio.wait_for(
            _run_notebooklm_summary(transcript, title, detail_level),
            timeout=_NOTEBOOKLM_RUN_TIMEOUT,
        ))
    except FileNotFoundError:
        logger.info("podcast: NotebookLM not authenticated (no credentials file), skipping summary for %r", title)
        return None
    except Exception as e:
        logger.warning("podcast: NotebookLM summary failed for %r: %s", title, e)
        return None

    if not answer:
        logger.warning("podcast: NotebookLM summary returned no answer for %r", title)
        return None

    result = ai.parse_podcast_summary_json(answer)
    if not result.get("summary_de"):
        logger.warning("podcast: NotebookLM summary answer was unparseable/empty for %r", title)
        return None
    if not ai.summary_de_is_german(result["summary_de"]):
        # Chinese where German was asked for (#904). Returning None hands the
        # episode to the paid API chain, which is the right trade: a summary
        # in the wrong language breaks every non-Chinese rendition downstream.
        logger.warning("podcast: NotebookLM summary_de is not German for %r, falling back", title)
        return None

    logger.info("podcast: NotebookLM summarized %r (%d word(s))", title, len(result.get("words") or []))
    return result


def _fmt_timestamp(ms: float) -> str:
    """Milliseconds -> "[MM:SS]" (or "[H:MM:SS]" past the hour) for prefixing
    a transcript paragraph (#543), so the summary AI can cite roughly when a
    topic was discussed."""
    total = int(ms // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


def _paragraph_start_ms(p: dict) -> int | None:
    """Extract a paragraph's start time in milliseconds from a Tingwu
    paragraph (#543). Tingwu's exact key isn't verified against a real
    response (see _parse_tingwu_transcript), so try the plausible paragraph-
    level keys, then fall back to the first word's start. Returns None when no
    timing is present — the caller then emits that paragraph without a prefix."""
    for key in ("Start", "BeginTime", "StartTime"):
        v = p.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    for w in p.get("Words") or []:
        for key in ("Start", "BeginTime", "StartTime"):
            v = w.get(key)
            if isinstance(v, (int, float)):
                return int(v)
    return None


def _parse_tingwu_transcript(result_json: dict) -> str:
    """Best-effort flatten of the Tingwu offline transcription result JSON
    (fetched from the URL in Result.Transcription once the task completes)
    into plain text, each paragraph prefixed with its start timestamp when
    available (#543).

    The documented shape is Transcription.Paragraphs[], each either carrying
    a Text field directly or a Words[] list of {Text: ...} tokens to join —
    walk both. Falls back to recursively collecting every "Text" string
    found anywhere in the payload if neither matches, so an undocumented/
    changed shape degrades to "some text" instead of an empty transcript
    (this fallback is exercised by the unit tests; the primary shape is
    unverified against a real response since #498 shipped without
    credentials to test with — see CLAUDE.md/scripts/README.md).
    """
    paragraphs = (
        (result_json.get("Transcription") or {}).get("Paragraphs")
        or result_json.get("Paragraphs")
        or []
    )
    lines: list[str] = []
    for p in paragraphs:
        text = p.get("Text")
        if not text:
            words = p.get("Words") or []
            text = "".join(w.get("Text", "") for w in words)
        if not text:
            continue
        start_ms = _paragraph_start_ms(p)
        lines.append(f"{_fmt_timestamp(start_ms)} {text}" if start_ms is not None else text)
    if lines:
        return " ".join(lines)

    collected: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "Text" and isinstance(v, str) and v.strip():
                    collected.append(v.strip())
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(result_json)
    return " ".join(collected)


def _transcribe_via_tingwu(audio_url: str, video_id: str) -> str | None:
    """Primary transcription path (#498): submit the RSS enclosure mp3 URL
    directly to Alibaba Cloud's Tongyi Tingwu (通义听悟) offline
    transcription API — no audio download needed at all, official API,
    ~¥0.6/hour (vs Whisper's ~¥1.3/hour), 90-day free tier for new accounts.

    Requires ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET
    (the alibabacloud SDK's standard env var names) plus TINGWU_APP_KEY (an
    application created once in the Tingwu console — see scripts/README.md).
    Missing config or any failure (create/poll/timeout/download/parse) logs
    and returns None; fetch_transcript falls back to Whisper/NotebookLM.
    """
    access_key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    app_key = os.environ.get("TINGWU_APP_KEY")
    if not access_key_id or not access_key_secret or not app_key:
        logger.info("podcast: Tingwu credentials not configured, skipping for %s", video_id)
        return None

    try:
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_tingwu20230930 import models as tingwu_models
        from alibabacloud_tingwu20230930.client import Client as TingwuClient
    except ImportError:
        logger.info("podcast: alibabacloud_tingwu20230930 not installed, skipping Tingwu for %s", video_id)
        return None

    try:
        client = TingwuClient(open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=_TINGWU_ENDPOINT,
            region_id=_TINGWU_REGION,
        ))
        create_response = client.create_task(tingwu_models.CreateTaskRequest(
            app_key=app_key,
            type="offline",
            input=tingwu_models.CreateTaskRequestInput(
                file_url=audio_url,
                source_language="cn",
            ),
        ))
        task_id = create_response.body.data.task_id if create_response.body and create_response.body.data else None
        if not task_id:
            logger.warning("podcast: Tingwu CreateTask returned no task_id for %s", video_id)
            return None

        result_url = None
        elapsed = 0
        while elapsed < _TINGWU_POLL_TIMEOUT_SECONDS:
            time.sleep(_TINGWU_POLL_INTERVAL_SECONDS)
            elapsed += _TINGWU_POLL_INTERVAL_SECONDS
            info = client.get_task_info(task_id)
            data = info.body.data if info.body else None
            status = data.task_status if data else None
            if status == "COMPLETED":
                result_url = data.result.transcription if data.result else None
                break
            if status == "FAILED":
                logger.warning(
                    "podcast: Tingwu task failed for %s: %s",
                    video_id, data.error_message if data else "unknown error",
                )
                return None
        else:
            logger.warning(
                "podcast: Tingwu task timed out after %ds for %s",
                _TINGWU_POLL_TIMEOUT_SECONDS, video_id,
            )
            return None

        if not result_url:
            logger.warning("podcast: Tingwu task completed with no transcription result for %s", video_id)
            return None

        transcript = _parse_tingwu_transcript(json.loads(_http_get(result_url, timeout=30)))
    except Exception as e:
        logger.warning("podcast: Tingwu transcription failed for %s: %s", video_id, e)
        return None

    if transcript:
        logger.info("podcast: Tingwu transcribed %s (%d chars)", video_id, len(transcript))
    return transcript or None


def _resolve_transcriber(cfg: dict) -> str:
    """Normalize podcast_config into one of auto|tingwu|whisper|notebooklm|off.

    Reads the current `transcriber` key when set to a legal value; otherwise
    falls back to the legacy `whisper_fallback` key (#485) so old installs
    keep behaving the same way without a data migration: whisper_fallback=0
    -> off, anything else -> auto (NotebookLM #486 -> Tingwu #498 -> Whisper
    #485, per fetch_transcript's ordering, reordered free-first in #510).
    """
    val = cfg.get("transcriber")
    if val in ("auto", "tingwu", "notebooklm", "whisper", "off"):
        return val
    if cfg.get("whisper_fallback", "1") not in ("1", "true", "True"):
        return "off"
    return "auto"


def _whisper_duration_allowed(duration: float, cfg: dict) -> bool:
    """Whisper costs real money, so it's gated to short episodes:
    duration <= podcast_config.whisper_max_minutes (default 30). Daniel's
    早咖啡-style daily episodes run 10-15 minutes; the long shows he doesn't
    want to pay for run 60-90. Duration replaces the earlier title filter
    (#486) because real episode titles never contain "早咖啡" (issue #495).
    0/empty disables the gate. Tingwu/NotebookLM are never subject to it."""
    raw = cfg.get("whisper_max_minutes", str(_DEFAULT_WHISPER_MAX_MINUTES))
    try:
        max_minutes = float(raw)
    except (TypeError, ValueError):
        max_minutes = _DEFAULT_WHISPER_MAX_MINUTES
    if max_minutes <= 0:
        return True
    return duration <= max_minutes * 60


def _episode_to_video(episode: dict) -> dict:
    """Build the `video` dict that _process_episode/fetch_transcript expect
    from a stored podcast_episodes row.

    Exists because three call sites used to build this dict inline, and one
    dropped field silently broke a whole ingestion path (#766): the Instagram
    branch in fetch_transcript keys off `youtube_url`, which retry_episode's
    hand-rolled dict never carried — so every Reel fell through to the
    YouTube captions path and tried to look up its Instagram shortcode as a
    YouTube video id (they're both 11 base64-ish characters, so it looks
    plausible right up until YouTube says "no such video"). One builder, so
    the next field added here reaches every caller.
    """
    return {
        "video_id": episode["video_id"],
        "title": episode["title"],
        "audio_url": episode.get("audio_url"),
        "duration_seconds": episode.get("duration_seconds"),
        "kind": episode.get("kind") or "podcast",
        # The Reel-vs-YouTube discriminator. Not optional despite the name:
        # since #650 this column holds article and Instagram URLs too.
        "youtube_url": episode.get("youtube_url"),
    }


def fetch_transcript(video: dict) -> tuple[str | None, dict]:
    """Get the Chinese transcript for one RSS episode. `video` needs
    video_id/title/audio_url/duration_seconds (an episode row or a
    fetch_new_videos() entry both satisfy this).

    Returns (transcript_text_or_None, meta) — meta always has at least
    'title' and 'transcript_source' (one of 'notebooklm'/'tingwu'/'whisper'/
    None). Tries the transcription chain in order — NotebookLM (#486, free
    but unofficial/optional, first per #510 since it's free) -> Tingwu (#498,
    cheap, submitted the RSS mp3 URL directly, no download) -> Whisper (#485,
    paid, gated to duration <= whisper_max_minutes) — per
    podcast_config.transcriber ('auto' tries all three in that order; a
    specific value tries only that one). transcript is None when the chain
    is disabled/unavailable/fails entirely for this episode (caller stores
    status='no_transcript' in that case).

    Each step is wrapped in its own try/except (#510): an exception raised
    by one transcriber (e.g. a 429 from OpenAI mid-Whisper-call) must not
    abort the whole chain and skip the remaining steps — that bug is exactly
    what stranded short episodes with no transcript at all on 2026-07-12,
    since the (paid, duration-gated) Whisper step used to run inside the
    same try/except as the NotebookLM step that would otherwise have caught
    them.

    NotebookLM and Whisper both need the audio downloaded+transcoded first;
    Tingwu doesn't (it's submitted the RSS URL directly). The download is
    attempted at most once and its result reused by whichever of
    NotebookLM/Whisper actually runs — a download failure only rules out
    those two, Tingwu can still be tried.
    """
    video_id = video["video_id"]
    title = video.get("title") or video_id
    audio_url = video.get("audio_url")
    duration = video.get("duration_seconds") or 0
    meta = {"title": title, "transcript_source": None}

    if video.get("kind") == "video":
        if "youtube_url" not in video:
            # #766: the Instagram branch below is decided purely by this
            # field, so a caller that forgot it doesn't get a wrong answer —
            # it gets a Reel silently routed into the YouTube captions API.
            # Loud, because the symptom (status='no_transcript') otherwise
            # looks exactly like "this video genuinely has no captions".
            logger.warning(
                "podcast: video episode %s has no 'youtube_url' key — cannot tell a "
                "Reel from a YouTube video, assuming YouTube (see _episode_to_video)",
                video_id)
        if _is_instagram_url(video.get("youtube_url") or ""):
            # Instagram Reel ingestion (#750): no captions API exists for
            # Instagram at all, so this never touches knowledge.youtube —
            # straight to download-audio + Groq/Whisper (_transcribe_instagram).
            # Checked via the URL (kind='video' alone doesn't distinguish a
            # Reel from a YouTube video) rather than transcript_source, which
            # is only set *after* a transcription attempt succeeds.
            return _transcribe_instagram(video)
        # YouTube ingestion (#651): captions only, no audio download/Whisper.
        # Dispatches out of the podcast RSS/Tingwu/NotebookLM/Whisper chain
        # entirely — that chain (below) is for kind='podcast' only.
        import knowledge.youtube
        return knowledge.youtube.fetch_captions(video_id)

    if video.get("kind") == "article":
        # Article ingestion (#652): body extraction only, no transcription
        # chain at all. routes.knowledge.add_knowledge already fetches +
        # stores transcript_zh eagerly at add time (no cheap article-only
        # metadata endpoint the way YouTube has oEmbed, see
        # knowledge/article.py's module docstring), so this branch is
        # normally never reached — _process_episode's "reuse existing
        # transcript" fast path (above) wins first. It exists as the
        # retry/reprocess fallback for a row without a stored transcript.
        import knowledge.article
        return knowledge.article.fetch_transcript(video)

    if not audio_url:
        logger.warning("podcast: no audio_url for %s, cannot transcribe", video_id)
        return None, meta

    if duration and duration > _AUDIO_MAX_SECONDS:
        logger.warning(
            "podcast: %s is %.1fh, exceeds the %.0fh audio transcription cost guardrail, skipping",
            video_id, duration / 3600, _AUDIO_MAX_SECONDS / 3600,
        )
        return None, meta

    cfg = database.get_podcast_config()
    transcriber = _resolve_transcriber(cfg)
    if transcriber == "off":
        logger.info("podcast: transcriber=off, skipping transcription for %s", video_id)
        return None, meta

    from routes.utils import ai_disabled
    if ai_disabled():
        # Dev mode must never trigger transcription (Tingwu/Whisper cost
        # money; NotebookLM is free but still an external side effect).
        logger.info("podcast: DISABLE_AI set, skipping transcription for %s", video_id)
        return None, meta

    with tempfile.TemporaryDirectory() as tmp_dir:
        download_state = {"attempted": False, "path": None}

        def get_mp3_path() -> str | None:
            """Lazily download+transcode the audio once, reused by both the
            NotebookLM and Whisper steps below. Returns None (logged) on
            missing ffmpeg or a download/transcode failure — callers treat
            that the same as "this step can't run", not a chain-abort."""
            if download_state["attempted"]:
                return download_state["path"]
            download_state["attempted"] = True
            if not shutil.which("ffmpeg"):
                logger.warning("podcast: ffmpeg not found, skipping audio download for %s", video_id)
                return None
            try:
                download_state["path"] = _download_audio(audio_url, video_id, tmp_dir)
            except Exception as e:
                logger.warning("podcast: audio download failed for %s: %s", video_id, e)
            return download_state["path"]

        # 1. NotebookLM (#486, free, tried first per #510) — only attempted
        # when credentials are present, so 'auto' doesn't waste a download
        # on it when it can't possibly work.
        if transcriber in ("auto", "notebooklm"):
            if _notebooklm_credentials_available():
                transcript = None
                try:
                    mp3_path = get_mp3_path()
                    if mp3_path:
                        transcript = _transcribe_via_notebooklm(mp3_path, video_id)
                except Exception as e:
                    logger.warning("podcast: NotebookLM step raised for %s: %s", video_id, e)
                if transcript:
                    meta["transcript_source"] = "notebooklm"
                    logger.info("podcast: transcript source for %s = notebooklm", video_id)
                    return transcript, meta
            if transcriber == "notebooklm":
                return None, meta

        # 2. Tingwu (#498, cheap, no download needed — can still run even if
        # the download above failed/was skipped).
        if transcriber in ("auto", "tingwu"):
            transcript = None
            try:
                transcript = _transcribe_via_tingwu(audio_url, video_id)
            except Exception as e:
                logger.warning("podcast: Tingwu step raised for %s: %s", video_id, e)
            if transcript:
                meta["transcript_source"] = "tingwu"
                logger.info("podcast: transcript source for %s = tingwu", video_id)
                return transcript, meta
            if transcriber == "tingwu":
                logger.info("podcast: transcriber=tingwu and Tingwu failed, not trying further for %s", video_id)
                return None, meta

        # 3. Whisper (#485, paid, last resort — gated to short episodes).
        if transcriber in ("auto", "whisper"):
            if _whisper_duration_allowed(duration, cfg):
                transcript = None
                try:
                    mp3_path = get_mp3_path()
                    if mp3_path:
                        transcript = _transcribe_via_whisper(mp3_path, duration, video_id, tmp_dir)
                except Exception as e:
                    logger.warning("podcast: Whisper step raised for %s: %s", video_id, e)
                if transcript:
                    meta["transcript_source"] = "whisper"
                    logger.info("podcast: transcript source for %s = whisper", video_id)
                    return transcript, meta
            else:
                logger.info(
                    "podcast: %s is %.0fmin, over whisper_max_minutes — skipping Whisper",
                    video_id, duration / 60,
                )
            if transcriber == "whisper":
                return None, meta

    logger.info("podcast: transcript source for %s = none (all paths exhausted)", video_id)
    return None, meta


# ---------------------------------------------------------------------------
# AI summary
# ---------------------------------------------------------------------------

def summarize(transcript: str, title: str, detail_level: str,
              china_critical: bool = False) -> dict:
    """Summarize a transcript into {"summary_de", "words"} (#479, NotebookLM
    path added in #510). When podcast_config.summarizer is 'auto' (default)
    and NotebookLM credentials are present, tries the free
    _summarize_via_notebooklm path first; any failure (or summarizer='api')
    falls back to the paid/quota-limited API chain in
    ai.summarize_podcast_transcript so the pipeline never breaks over it.

    `china_critical` (#731) only affects that API fallback — it picks OpenAI
    over DeepSeek there. NotebookLM stays first regardless: it is Google's,
    so it has no reason to censor the topic, and it is free."""
    cfg = database.get_podcast_config()
    summarizer = cfg.get("summarizer") or "auto"
    if summarizer == "auto" and _notebooklm_credentials_available():
        result = _summarize_via_notebooklm(transcript, title, detail_level)
        if result:
            return _annotate_summary(result)
    return _annotate_summary(ai.summarize_podcast_transcript(
        transcript, title, detail_level, china_critical=china_critical))


def _annotate_summary(result: dict) -> dict:
    """Add the AI-free vocabulary annotations (#638) to a fresh summary. Done
    here, at the single choke point both summarizer paths and both callers
    (_process_episode, regenerate_summary) pass through, so the annotated text
    is what gets stored — email, Signal and the detail page then all show it
    without each re-running Google Translate.

    zh_annotate never raises: on any failure the untouched text comes back, so
    a missing pinyin table can't cost Daniel the episode.

    The German summary is NOT annotated (#979): it is German prose Daniel reads
    to understand the content, and the pinyin/汉字 asides #631 put in there
    buried it. Whatever the model still writes in Chinese is stripped on the
    read path (database.podcast._hydrate), which also cleans up every summary
    stored before #979.

    Since #1001 the Chinese summary is not annotated inline either: every new
    word in the text is tappable (#967) and can show its gloss under the hanzi
    on demand (#996), so the parentheses only broke up the prose. The word
    SCAN below stays — it is what feeds both the word table and those taps.
    Summaries written before #1001 are cleaned on the same read path.

    Also replaces result["words"] (#650) — the AI's own pick from the summary
    prompt, which regularly misses words and includes ones Daniel already
    knows — with a deterministic scan of the Chinese summary using
    zh_annotate.extract_new_words(). The word table and the tappable words in
    the text are the same list because they come from this one scan. An empty
    scan (extraction failed, or genuinely no new words) keeps the AI's list
    as a fallback rather than wiping the table."""
    try:
        scanned = zh_annotate.extract_new_words(result.get("summary_zh") or "")
        if scanned:
            result["words"] = scanned
    except Exception as e:
        logger.warning("podcast: word-list extraction failed, keeping AI list — %s", e)
    return result


_PLACEHOLDER_TITLE_RE = re.compile(r"^(video|reel|post|photo|clip)\s+by\s+\S", re.IGNORECASE)
_SHORTCODE_TITLE_RE = re.compile(r"^[A-Za-z0-9_-]{8,20}$")


def _is_placeholder_title(title: str) -> bool:
    """True if `title` looks like an auto-generated non-title rather than
    real content (#781) — the only gate that decides whether the AI's
    title_suggestion is allowed to overwrite an episode's stored title.

    Instagram Reel/Post metadata (knowledge/instagram.py) almost always
    yields "Video by <uploader>" as yt-dlp's `title` field — informative
    about who posted it, not what it's about, so every Reel ends up with
    the same handful of indistinguishable list entries. Bare Instagram
    shortcodes show up too when even that fallback is missing.

    This must be conservative: a false positive here would let an AI
    guess silently overwrite a perfectly good real title (a podcast/
    YouTube/article title fetched from RSS/oEmbed/trafilatura) — false
    negatives just mean a placeholder title survives one summary cycle,
    which is harmless (it can still be fixed via regenerate_summary).
    """
    title = (title or "").strip()
    if not title or title.lower() == "(untitled)":
        return True
    if _PLACEHOLDER_TITLE_RE.match(title):
        return True
    if " " not in title and _SHORTCODE_TITLE_RE.match(title):
        return True
    return False


def filter_new_words(words: list[dict]) -> list[dict]:
    """Drop words the AI picked that are already in entries.word_zh — Daniel
    already has those in his SRS deck, no need to flag them again."""
    if not words:
        return []
    existing = database.word_zh_exists([w["word"] for w in words])
    return [w for w in words if w["word"] not in existing]


# ---------------------------------------------------------------------------
# Bilingual transcript (#553)
# ---------------------------------------------------------------------------

def _split_transcript_segments(text: str) -> list[str]:
    """Split a Chinese transcript into roughly sentence-sized segments for the
    parallel zh/de view (#553). Splits after each sentence-ending punctuation
    (。！？…), keeping any leading Tingwu timestamp attached to the sentence
    that follows. Blank pieces are dropped."""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？…])\s*", text)
    return [p.strip() for p in parts if p.strip()]


_SENTENCE_SPLIT_ANY_RE = re.compile(r"(?<=[。！？…])\s*|(?<=[.!?])\s+")


def _split_segments_any(text: str) -> list[str]:
    """Sentence-ish split usable for Chinese OR Western text (#750). Unlike
    _split_transcript_segments (CJK sentence-final punctuation only), this
    also breaks after '.', '!', '?' followed by whitespace — needed for
    build_transcript_de's non-Chinese ->zh branch (#772), where the source
    language isn't known ahead of time (Instagram Reels are commonly German
    or English, not Chinese)."""
    if not text:
        return []
    parts = _SENTENCE_SPLIT_ANY_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _translate_segments(segs: list[str], target: str, source: str) -> list[str]:
    """Translate `segs` to `target` from `source`. Returns a list aligned 1:1
    with segs (translate_batch falls back to the source text for any segment it
    can't translate, and since #756 does the chunking under Google Translate's
    ~5000-char free-endpoint request limit itself — this function used to carry
    that batching loop). Generalized (#750) from the zh->de-only original
    (_translate_segments_de, kept below as a thin wrapper)."""
    return translator.translate_batch(segs, target=target, source=source)


def _translate_segments_de(zh_segments: list[str]) -> list[str]:
    """zh->de translation — build_transcript_de's original narrow entry
    point, now a thin wrapper over _translate_segments (#750)."""
    return _translate_segments(zh_segments, target="de", source="zh-CN")


def build_transcript_de(transcript_zh: str) -> list[dict]:
    """Build the bilingual segment-pair list [{"zh","de"}] for a transcript
    (#553). The `transcript_zh` column (and this function's parameter name)
    stores whatever language the source material actually was — a Chinese
    podcast, but since #750/#772 also a German/English Instagram Reel; see
    podcast_episodes' schema.sql docstring for that precedent (same as
    `word_zh` doubling for French word forms).

    Direction depends on _is_chinese_text(transcript_zh):
    - Chinese source -> translated to German (#553's original behavior,
      unchanged byte-for-byte since #750/#772: same split function
      (_split_transcript_segments), same translate call (_translate_segments_de)).
    - Non-Chinese source (#772: Daniel decided short items should get a full
      bilingual transcript instead of the AI-summary skip that used to cover
      them) -> translated to Chinese instead, using the direction-agnostic
      splitter (_split_segments_any) since the source language isn't known
      ahead of time.

    Either way the returned dicts keep the SAME two keys, "zh" and "de" —
    the "zh" slot always holds the Chinese-language side of the pair and
    "de" always holds the other side, regardless of which one was the
    original. This is the contract every renderer (email's
    _bilingual_transcript_html, the detail page's transcript block in
    static/app.js) relies on: they just print p.zh above p.de, so as long as
    this invariant holds neither renderer needs to know or care which side
    was the source.

    Best-effort: returns [] if the transcript is empty, has no segments, or
    translation is unavailable/fails."""
    if _is_chinese_text(transcript_zh):
        segs = _split_transcript_segments(transcript_zh)
        if not segs:
            return []
        try:
            de = _translate_segments_de(segs)
        except Exception as e:
            logger.warning("podcast: transcript translation failed: %s", e)
            return []
        return [{"zh": z, "de": (d or "")} for z, d in zip(segs, de)]

    segs = _split_segments_any(transcript_zh)
    if not segs:
        return []
    try:
        zh = _translate_segments(segs, target="zh-CN", source="auto")
    except Exception as e:
        logger.warning("podcast: transcript translation failed: %s", e)
        return []
    return [{"zh": (z or ""), "de": d} for d, z in zip(segs, zh)]


# ---------------------------------------------------------------------------
# Spotify link
# ---------------------------------------------------------------------------

def _spotify_search_fallback(title: str) -> str:
    return f"https://open.spotify.com/search/{urllib.parse.quote(title)}"


def find_spotify_url(title: str) -> str:
    """Look up a Spotify episode link for `title` via the Web API's
    client-credentials flow when SPOTIFY_CLIENT_ID/SECRET are configured;
    otherwise (or on any failure) fall back to a search link."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return _spotify_search_fallback(title)

    try:
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        token_req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token = json.loads(resp.read())["access_token"]

        q = urllib.parse.urlencode({"type": "episode", "market": "DE", "q": title, "limit": 1})
        search_req = urllib.request.Request(
            f"https://api.spotify.com/v1/search?{q}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(search_req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = (data.get("episodes") or {}).get("items") or []
        if items:
            url = items[0].get("external_urls", {}).get("spotify")
            if url:
                return url
    except Exception as e:
        logger.warning("podcast: Spotify lookup failed for %r: %s", title, e)

    return _spotify_search_fallback(title)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _words_table_html(words: list[dict]) -> str:
    if not words:
        return "<p><em>Keine neuen HSK5+ Vokabeln gefunden.</em></p>"
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'>{w['word']}</td>"
        f"<td style='padding:4px 12px 4px 0;color:#666'>{w.get('pinyin', '')}</td>"
        f"<td style='padding:4px 0'>{w.get('definition_de', '')}</td></tr>"
        for w in words
    )
    return (
        "<table style='border-collapse:collapse'>"
        "<tr><th align='left'>Wort</th><th align='left'>Pinyin</th><th align='left'>Bedeutung</th></tr>"
        f"{rows}</table>"
    )


def _bilingual_transcript_html(pairs: list[dict]) -> str:
    """Render the bilingual (zh/de) transcript block appended to the end of
    the notification email (#553). Plain-text segments are HTML-escaped."""
    if not pairs:
        return ""
    rows = "".join(
        "<div style='margin:0 0 8px'>"
        f"<div>{html.escape(p.get('zh', ''))}</div>"
        f"<div style='color:#666'>{html.escape(p.get('de', ''))}</div></div>"
        for p in pairs
    )
    return f"<h3>Transkript (中德对照)</h3><div style='font-size:14px'>{rows}</div>"


def autotag_episode(episode_id: int, title: str, summary_de: str, *, force: bool = False) -> list[str]:
    """Give one item its AI topic tags (#938). Returns the tags written (possibly
    empty).

    Only runs when the item has no AI tags yet, unless `force` (the detail
    page's ↻ Retag button). Re-tagging on every summarize would spend money to
    churn the same six words, and would fight with a tag list Daniel has
    already looked at.

    Hand-typed tags are never at risk: set_item_tags(source='ai') replaces only
    the rows this function wrote (#935).

    Best-effort throughout. A tag list is a convenience on top of an item that
    is already summarized and stored — nothing here may turn a successful
    episode into a failed one.
    """
    try:
        if not force and any(t["source"] == "ai" for t in database.item_tags(episode_id)):
            return []
        vocabulary = [t["name"] for t in database.list_tags()]
        tags = ai.extract_knowledge_tags(title, summary_de, vocabulary)
        if tags:
            database.set_item_tags(episode_id, tags, source="ai")
        return tags
    except Exception as e:
        logger.warning("podcast: auto-tagging failed for episode %s: %s", episode_id, e)
        return []


def _feed_title(episode: dict) -> str | None:
    """The podcast's own name, looked up from podcast_feeds via the episode's
    channel_id (which holds the feed URL). Shared by the email subject line
    and the Signal header (#631). Returns None when the feed row is gone or
    unnamed — callers fall back to showing just the episode title."""
    channel_id = episode.get("channel_id")
    if not channel_id:
        return None
    feed = database.get_feed_by_url(channel_id)
    return (feed.get("title") or None) if feed else None


# Structural tags the Chinese summary is allowed to carry (#708). Everything
# else stays escaped: the model writes this text, and a stray <script> or
# <style> must never reach the mail client or the detail page.
_SUMMARY_ZH_ALLOWED_TAGS = re.compile(r"&lt;(/?(?:p|b|strong|em|i)|br\s*/?)&gt;")


def _summary_zh_html(summary_zh: str) -> str:
    """Render the Chinese summary as HTML (#631, #708).

    Since #708 the Chinese summary is a full translation of the German one and
    carries the same markup: <p> paragraphs, each opening with a <b> lead
    sentence. So instead of escaping everything, escape first and then let just
    that handful of structural tags back through — a model that ignores the
    contract still cannot inject markup.

    Episodes summarized before #708 hold plain text with blank lines between
    paragraphs; those get wrapped into real <p> tags here, because relying on
    white-space:pre-wrap would be a gamble across mail clients."""
    if not summary_zh:
        return ""
    escaped = _SUMMARY_ZH_ALLOWED_TAGS.sub(r"<\1>", html.escape(summary_zh))
    if "<p>" in escaped:
        body = escaped
    else:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", escaped) if p.strip()]
        body = "".join(f"<p style='margin:0 0 10px'>{p}</p>" for p in paragraphs)
    return (
        "<div style='background:#f5f5f5;border-left:3px solid #999;"
        "padding:10px 14px;margin:0 0 16px;font-size:15px;line-height:1.7'>"
        f"{body}</div>"
    )


def _rendition_fr_html(episode: dict) -> str | None:
    """通讯（#925）的通知里额外带一份法语阅读版。只对 kind='newsletter'
    生效——播客/视频/文章的邮件与 Signal 消息一个字节都不变。
    生成失败只记日志、返回 None：为一份锦上添花的译文丢掉整封通知是荒唐的。

    延迟到函数内部 import knowledge.rendition，避免模块级循环导入——
    knowledge.ingest.py 里 `import ai` 也是同样的写法（见其 docstring）：
    podcast.py 目前不会被 knowledge 包在模块加载时 import，但没必要为了
    这一个可选功能冒险在顶部建立一条新的模块间依赖。"""
    if episode.get("kind") != "newsletter":
        return None
    try:
        import knowledge.rendition
        rendition = knowledge.rendition.get_or_create_rendition(episode["id"], "fr")
        return rendition.get("summary")
    except Exception as e:
        logger.warning("podcast: 法语 rendition 生成失败 (episode %s): %s", episode.get("id"), e)
        return None


def send_mail(subject: str, body_html: str, *, context: str = "mail") -> bool:
    """Send one HTML mail to the configured recipient. Returns True if sent,
    False if skipped because SMTP isn't configured — skipping is not an error,
    callers just don't record a send. `context` only labels the log line.

    Shared by the podcast/knowledge notifications and the review reminder
    (#701) so there is one place that knows how this mailbox is reached."""
    host = os.environ.get("SMTP_HOST")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM") or username
    if not host or not username or not password or not from_addr:
        logger.info("podcast: SMTP not configured, skipping email for %s", context)
        return False

    port = int(os.environ.get("SMTP_PORT", "587"))
    cfg = database.get_podcast_config()
    to_addr = cfg.get("email_to") or "u82g@outlook.com"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    logger.info("podcast: email sent for %s to %s", context, to_addr)
    return True


def send_email(episode: dict) -> bool:
    """Send the HTML notification email for a freshly-summarized episode.
    Returns True if sent, False if skipped (SMTP not configured) — skipping
    is not an error, callers just don't set email_sent_at."""
    public_base = os.environ.get("PUBLIC_BASE_URL", "https://powerdaniel3000.duckdns.org")

    transcript_link = f"{public_base}/#podcast-{episode['id']}"
    words_html = _words_table_html(episode.get("hsk_words") or [])
    transcript_html = _bilingual_transcript_html(episode.get("transcript_de") or [])

    # 通讯（#925）额外附一份法语阅读版，放在德语总结之后；对其它 kind
    # 恒为 None，下面这一段 HTML 就不出现——播客/视频/文章的邮件字节不变。
    fr_html = _rendition_fr_html(episode)
    fr_block = f"<h3>Français</h3><div>{fr_html}</div>" if fr_html else ""

    body_html = f"""
    <html><body style="font-family:sans-serif;max-width:640px">
      <h2>{episode['title']}</h2>
      <p style="margin:0 0 14px">
        <a href="{transcript_link}" style="font-weight:bold">▸ Auf der Website öffnen</a>
      </p>
      {_summary_zh_html(episode.get('summary_zh') or '')}
      <div>{episode.get('summary_de') or ''}</div>
      {fr_block}
      <h3>Neue HSK5+ Vokabeln</h3>
      {words_html}
      <p>
        <a href="{transcript_link}">Transkript ansehen</a> ·
        <a href="{episode.get('spotify_url') or ''}">Spotify</a> ·
        <a href="{episode['youtube_url']}">Folge</a>
      </p>
      {transcript_html}
    </body></html>
    """

    # Subject = "<podcast name> - <episode title>" (#631). The old
    # "Neue Podcast-Folge:" prefix was identical on every mail and ate the
    # first 20 characters of the inbox list — the podcast name is what
    # actually lets Daniel tell one mail from another at a glance. With no
    # feed name on record the episode title stands alone; the dead prefix
    # does not come back.
    feed_title = _feed_title(episode)
    subject = f"{feed_title} - {episode['title']}" if feed_title else episode["title"]
    return send_mail(subject, body_html, context=episode["video_id"])


def _summary_to_plain_text(summary: str | None) -> str:
    """Flatten an HTML summary into plain text for the Signal message.

    Paragraph boundaries must survive as blank lines (#567: summaries are
    <p>-structured, and a plain tag-strip would glue paragraphs together), so
    </p> and <br> become newlines before the remaining tags are dropped.
    Plain-text summaries (Chinese ones written before #708) pass through
    unchanged.

    Sends the FULL summary (#541) — Daniel reads it directly in Signal and the
    old 1500-char cap cut it off mid-sentence. The cap kept here only stops a
    pathologically long summary from producing a runaway message; a normal
    "detailed" summary (~900-1300 words ≈ up to ~9000 chars) fits well under it.
    """
    text = summary or ""
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 12000:
        text = text[:12000].rstrip() + "…"
    return text


def send_signal_text(text: str, *, context: str = "signal") -> bool:
    """Send one plain-text Signal "Note to Self" message via a linked-device
    signal-cli install (#521, extracted #749 so knowledge/signal_inbox.py's
    receipts can share this instead of re-implementing the subprocess call).
    Returns True if sent, False if skipped (SIGNAL_ACCOUNT not configured) —
    skipping is not an error, mirrors send_mail(). Never raises.

    `context` is only used in log lines to identify the caller (an episode's
    video_id for send_signal(), "signal-inbox-receipt" for the inbox script).
    """
    account = os.environ.get("SIGNAL_ACCOUNT")
    if not account:
        logger.info("podcast: SIGNAL_ACCOUNT not configured, skipping Signal message (%s)", context)
        return False

    cli_path = os.environ.get("SIGNAL_CLI_PATH", "signal-cli")
    try:
        result = subprocess.run(
            [cli_path, "-a", account, "send", "--note-to-self", "-m", text],
            capture_output=True, timeout=60,
        )
    except Exception as e:
        logger.warning("podcast: signal-cli invocation failed for %s: %s", context, e)
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
        logger.warning("podcast: signal-cli exited %s for %s: %s", result.returncode, context, stderr)
        return False

    logger.info("podcast: Signal message sent (%s)", context)
    return True


def send_signal(episode: dict) -> bool:
    """Send a plain-text Signal "Note to Self" notification for a freshly-
    summarized episode via a linked-device signal-cli install (#521).
    Returns True if sent, False if skipped (SIGNAL_ACCOUNT not configured)
    — skipping is not an error, mirrors send_email. Never raises."""
    account = os.environ.get("SIGNAL_ACCOUNT")
    if not account:
        logger.info("podcast: SIGNAL_ACCOUNT not configured, skipping Signal notification for %s",
                     episode["video_id"])
        return False

    public_base = os.environ.get("PUBLIC_BASE_URL", "https://powerdaniel3000.duckdns.org")
    transcript_link = f"{public_base}/#podcast-{episode['id']}"

    # Both summaries are HTML (the Chinese one since #708) and may be None;
    # strip the tags for the plain-text Signal message.
    summary_de = _summary_to_plain_text(episode.get("summary_de"))
    summary_zh = _summary_to_plain_text(episode.get("summary_zh"))

    # hsk_words comes back as a list from database.get_episode (_hydrate
    # parses the stored JSON), but be defensive in case a raw row or a
    # pre-hydration dict is ever passed in.
    hsk_words = episode.get("hsk_words") or []
    if isinstance(hsk_words, str):
        try:
            hsk_words = json.loads(hsk_words) if hsk_words else []
        except (ValueError, TypeError):
            hsk_words = []

    word_lines = "\n".join(
        f"- {w.get('word', '')} ({w.get('pinyin', '')}) – {w.get('definition_de', '')}"
        for w in hsk_words[:10]
    )

    # 抬头行：播客名 · 星期几（德语） · 日期（#532）。播客名从 channel_id
    # （feed 的 url）反查 podcast_feeds；查不到就省略播客名部分，只留星期+日期。
    feed_title = _feed_title(episode)

    weekday_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    date_part = None
    raw_date = episode.get("published_at") or episode.get("created_at")
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date).astimezone(ZoneInfo("Europe/Berlin"))
            date_part = f"{weekday_de[dt.weekday()]} · {dt.strftime('%d.%m.%Y')}"
        except (ValueError, TypeError):
            date_part = None

    header_parts = [p for p in (feed_title, date_part) if p]
    lines = [" · ".join(header_parts)] if header_parts else []
    lines.append(f"🎙 {episode['title']}")
    # 中文总结先行（#631）：Daniel 想先用中文读完整集内容，再读德语细节。
    if summary_zh:
        lines.append("")
        lines.append(summary_zh)
    if summary_de:
        lines.append("")
        lines.append(summary_de)
    # 通讯（#925）额外附一份法语版；对其它 kind 恒为 None，不改变行为。
    fr_html = _rendition_fr_html(episode)
    if fr_html:
        lines.append("")
        lines.append(_summary_to_plain_text(fr_html))
    if word_lines:
        lines.append("")
        lines.append("Neue HSK5+ Vokabeln:")
        lines.append(word_lines)
    lines.append("")
    lines.append(f"🔗 {transcript_link}")
    if episode.get("spotify_url"):
        lines.append(episode["spotify_url"])
    text = "\n".join(lines)

    return send_signal_text(text, context=episode["video_id"])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# How far back run_check's automatic retry pass (#491) looks: episodes with
# status='error' created within this many days get one re-attempt per cycle.
# The window keeps a permanently-broken video from burning transcription
# money forever (the Whisper duration gate is the other cost guardrail).
_AUTO_RETRY_MAX_AGE_DAYS = 7

# At most this many auto-retries per cycle (#495): a large error backlog
# (15 episodes after the cookie outage) times NotebookLM's 10-minute indexing
# ceiling would make one cycle run for hours; capping it lets the hourly cron
# chew through the backlog a few episodes at a time. Oldest first.
_AUTO_RETRY_PER_CYCLE = 3

# Cross-process lock (#495) so a slow run (audio downloads + transcription
# can exceed an hour) never overlaps the next hourly cron or a manual
# POST /api/podcast/check. fcntl is POSIX-only — fine, prod is Linux and
# dev is macOS.
_RUN_LOCK_PATH = os.path.join(_BASE_DIR, "data", "podcast_check.lock")


def _maybe_prepare_fulltext(episode_id: int, kind: str) -> None:
    """Pre-build the full-text reading version for newsletters (#972).

    Only newsletters: Daniel reads one every morning and wants it ready, not
    a button to press first. Every other kind generates on request — a
    podcast transcript is an hour of speech, and most items he only ever
    reads the summary of.

    Failure is logged and swallowed, exactly like summary_zh (#708): the
    full text is an extra, and an episode whose summary succeeded must not
    be marked failed because a translation round hiccupped. The detail page
    can always generate it later.
    """
    if kind != "newsletter":
        return
    try:
        import knowledge.rendition
        from languages import DEFAULT_LANG
        knowledge.rendition.get_or_create_fulltext(episode_id, DEFAULT_LANG, generate=True)
    except Exception as e:
        logger.warning("podcast: full text for episode %s failed (skipped): %s",
                       episode_id, e)


def _process_episode(episode_id: int, video: dict, detail_level: str, summary: dict) -> None:
    """Process one already-inserted episode end-to-end: transcript -> AI
    summary -> HSK word filter -> Spotify link -> store -> email. Shared by
    run_check (new episodes + the auto-retry pass) and the manual retry
    endpoint (#491). `video` needs 'video_id', 'title', 'audio_url' and
    'duration_seconds' (#497 — fetch_transcript needs the last two now that
    there's no yt-dlp metadata lookup to fall back on).

    Never raises — any unexpected failure marks the episode 'error' and bumps
    summary['failed'] (a missing transcript is 'no_transcript', not a
    failure); success bumps summary['summarized'] / summary['emailed'].
    """
    from routes.utils import ai_disabled

    label = f"Transcribe & Summarize: {video['title'][:30]}"
    with database.action_context(label):
        # Stamp the episode as actively processing (#598): the finally below
        # clears this on every normal exit (success/error/no_transcript/dev),
        # so a still-set timestamp means the process was killed mid-transcription
        # (restart/crash) and recover_orphaned_podcast_episodes() will flip the
        # episode to 'error' at next startup for auto-retry. Backfilled episodes
        # never reach here (they rest at 'pending' until manually processed), so
        # this reliably marks "actively working" without a 'processing' status
        # (the DB CHECK constraint forbids one).
        database.update_episode(episode_id, processing_started_at=datetime.now().isoformat())
        try:
            # Reuse an already-stored transcript (#500): after e.g. an OpenAI-quota
            # failure in the summary step, the retry must not re-run the whole
            # transcription (a NotebookLM upload+indexing round takes ~10 minutes
            # and Tingwu/Whisper cost money) — the transcript is already good.
            existing = database.get_episode(episode_id) or {}
            stored = (existing.get("transcript_zh") or "").strip()
            if stored:
                transcript = _normalize_transcript(stored)
                meta = {"transcript_source": existing.get("transcript_source")}
                logger.info("podcast: reusing existing transcript for %s (%d chars)",
                            video["video_id"], len(transcript))
                if transcript != stored:
                    database.update_episode(episode_id, transcript_zh=transcript)
            else:
                transcript, meta = fetch_transcript(video)
                transcript = _normalize_transcript(transcript) if transcript else transcript
                if not transcript:
                    database.update_episode(episode_id, status="no_transcript")
                    return
                database.update_episode(
                    episode_id, transcript_zh=transcript,
                    transcript_source=meta.get("transcript_source"),
                )

            if ai_disabled():
                # Dev mode: stop at pending with the transcript stored, no AI
                # call, no email — matches DISABLE_AI's behavior for stories.
                return

            # Bilingual transcript (#553): translate the transcript segment-by-
            # segment for the parallel view + email — Chinese source ->
            # German, non-Chinese source (e.g. a German/English Instagram
            # Reel, #772) -> Chinese; see build_transcript_de's docstring for
            # the direction rule and the zh/de slot contract. Skip if already
            # built (retry path). Best-effort — must not fail the episode.
            if not (database.get_episode(episode_id) or {}).get("transcript_de"):
                try:
                    pairs = build_transcript_de(transcript)
                    if pairs:
                        database.update_episode(episode_id, transcript_de=pairs)
                except Exception as e:
                    logger.warning("podcast: transcript_de build failed for %s: %s",
                                   video["video_id"], e)

            # china_critical (#731) is read from the row, not from `video`:
            # `video` is an RSS item for the crawler path and has no such
            # field — only the stored row knows what was ticked at paste time.
            china_critical = bool((database.get_episode(episode_id) or {}).get("china_critical"))
            # #772: every item, short or long, gets the full AI summary now —
            # Daniel decided the #750 short-transcript skip made Reels less
            # useful, not more. The full transcript translation moved to the
            # bilingual transcript block above instead.
            result = summarize(transcript, video["title"], detail_level,
                               china_critical=china_critical)
            if not result.get("summary_de"):
                database.update_episode(episode_id, status="error",
                                        error="AI summary failed or empty")
                summary["failed"] += 1
                return

            words = filter_new_words(result.get("words") or [])
            # find_spotify_url intentionally keeps using the OLD title, even
            # when it's about to be replaced below — Reels never need a
            # Spotify search link, and podcasts/YouTube never have a
            # placeholder title in the first place, so this never matters.
            spotify_url = find_spotify_url(video["title"])
            update_fields = dict(
                summary_zh=result.get("summary_zh") or "",
                summary_de=result["summary_de"],
                hsk_words=words,
                detail_level=detail_level,
                spotify_url=spotify_url,
                status="summarized",
                # #935: "Bearbeitungsdatum" — when this material actually
                # became readable. The unified list sorts on it by default.
                processed_at=datetime.now().isoformat(),
            )
            title_suggestion = (result.get("title_suggestion") or "").strip()
            # #937: a title Daniel typed himself is never replaced, even if it
            # happens to look like a placeholder. He was looking at the source.
            if (title_suggestion and _is_placeholder_title(video["title"])
                    and not database.is_manual(episode_id, "title")):
                update_fields["title"] = title_suggestion
                try:
                    title_en = ai.translate_title(title_suggestion)
                except Exception as e:
                    # translate_title already swallows its own errors and
                    # returns None — this is just an extra safety net so a
                    # totally unexpected exception here can't fail the whole
                    # episode over a nice-to-have English title.
                    logger.warning("podcast: translate_title failed for %r: %s",
                                    title_suggestion, e)
                    title_en = None
                if title_en:
                    update_fields["title_en"] = title_en
                logger.info("podcast: replaced placeholder title %r -> %r for %s",
                            video["title"], title_suggestion, video["video_id"])
            database.update_episode(episode_id, **update_fields)
            autotag_episode(episode_id, update_fields.get("title") or video["title"],
                            result["summary_de"])
            summary["summarized"] += 1
            _maybe_prepare_fulltext(episode_id, video.get("kind"))

            episode = database.get_episode(episode_id)
            try:
                sent = send_email(episode)
            except Exception as e:
                # An SMTP hiccup must not downgrade a successfully summarized
                # episode to 'error' — the summary is stored and viewable on
                # the website regardless; only email_sent_at stays unset.
                logger.warning("podcast: email failed for %s: %s", video["video_id"], e)
                sent = False
            if sent:
                database.update_episode(episode_id, email_sent_at=datetime.now().isoformat())
                summary["emailed"] += 1

            try:
                send_signal(episode)
            except Exception as e:
                # Signal and email are independent, best-effort channels — a
                # signal-cli hiccup must not downgrade a successfully
                # summarized episode to 'error' either.
                logger.warning("podcast: Signal notification failed for %s: %s", video["video_id"], e)
        except Exception as e:
            logger.error("podcast: episode %s failed: %s", video["video_id"], e)
            database.update_episode(episode_id, status="error", error=str(e))
            summary["failed"] += 1
        finally:
            # Clear the "actively processing" stamp on every normal exit (#598).
            # Only a hard kill (SIGKILL) skips this, leaving the timestamp set so
            # startup recovery can spot the orphan.
            database.update_episode(episode_id, processing_started_at=None)


def regenerate_summary(episode_id: int) -> dict:
    """Re-run ONLY the summary step for an already-summarized episode (#567)
    — used by POST /api/podcast/episodes/{id}/regenerate-summary after e.g. a
    prompt/style change, reusing the stored transcript (no re-transcription,
    no notifications). Unlike _process_episode, a failure must NOT downgrade
    the episode: status stays 'summarized' and the existing summary_de /
    hsk_words are left untouched, so the worst case is "nothing changed".

    The caller (the route) validates that the episode exists, is summarized
    and has a transcript. Returns {"regenerated": bool, "error": str|None}.
    """
    episode = database.get_episode(episode_id)
    if not episode:
        raise ValueError(f"podcast: episode {episode_id} not found")
    transcript = (episode.get("transcript_zh") or "").strip()
    if not transcript:
        raise ValueError(f"podcast: episode {episode_id} has no transcript")

    cfg = database.get_podcast_config()
    detail_level = cfg.get("detail_level") or "detailed"

    with database.action_context(f"Regenerate summary: {episode['title'][:30]}"):
        try:
            result = summarize(_normalize_transcript(transcript), episode["title"], detail_level,
                               china_critical=bool(episode.get("china_critical")))
        except Exception as e:
            logger.error("podcast: summary regeneration raised for episode %s: %s", episode_id, e)
            return {"regenerated": False, "error": str(e)}

    if not result.get("summary_de"):
        logger.warning("podcast: summary regeneration returned empty for episode %s", episode_id)
        return {"regenerated": False, "error": "AI summary failed or empty"}

    words = filter_new_words(result.get("words") or [])
    update_fields = dict(
        summary_zh=result.get("summary_zh") or "",
        summary_de=result["summary_de"],
        hsk_words=words,
        detail_level=detail_level,
        # #935: regenerating really is processing the item again, so the
        # material list should float it back to the top.
        processed_at=datetime.now().isoformat(),
    )
    # Same placeholder-title gate as _process_episode (#781) — this is the
    # only path that can retroactively fix the existing backlog of Reels
    # stuck with "Video by <uploader>" titles, since it's the one Daniel can
    # trigger by hand from the detail page's "Regenerate summary" button.
    title_suggestion = (result.get("title_suggestion") or "").strip()
    # Same #937 guard as _process_episode: hand-edited titles are off limits.
    if (title_suggestion and _is_placeholder_title(episode["title"])
            and not database.is_manual(episode_id, "title")):
        update_fields["title"] = title_suggestion
        try:
            title_en = ai.translate_title(title_suggestion)
        except Exception as e:
            logger.warning("podcast: translate_title failed for %r: %s", title_suggestion, e)
            title_en = None
        if title_en:
            update_fields["title_en"] = title_en
        logger.info("podcast: replaced placeholder title %r -> %r for episode %s",
                    episode["title"], title_suggestion, episode_id)
    database.update_episode(episode_id, **update_fields)
    autotag_episode(episode_id, update_fields.get("title") or episode["title"],
                    result["summary_de"])
    # #804: the German summary just changed, so any cached French/Spanish/etc.
    # rendition was translated from the OLD text and is now stale — drop them
    # all, the next per-language detail view regenerates lazily. Best-effort:
    # the summary itself already saved successfully above, so a hiccup
    # invalidating a secondary cache must not turn that into a failure.
    try:
        database.delete_knowledge_renditions(episode_id)
    except Exception as e:
        logger.warning("podcast: failed to invalidate renditions for episode %s — %s",
                       episode_id, e)
    logger.info("podcast: summary regenerated for episode %s (%d word(s))", episode_id, len(words))
    return {"regenerated": True, "error": None}


def retry_episode(episode_id: int) -> dict:
    """Re-run the full processing pipeline for one existing episode (#491) —
    used by POST /api/podcast/episodes/{id}/retry after e.g. a failed
    transcription attempt. Reuses the existing row (video_id is UNIQUE — no
    second INSERT) after resetting its status/error, so a retry that fails
    again just lands back on 'error' with the fresh message.

    The caller (the route) validates that the episode exists and its status
    is retryable. Returns {status, transcript_source, error, emailed} read
    back from the row after processing.
    """
    episode = database.get_episode(episode_id)
    if not episode:
        raise ValueError(f"podcast: episode {episode_id} not found")

    cfg = database.get_podcast_config()
    detail_level = cfg.get("detail_level") or "detailed"
    database.update_episode(episode_id, status="pending", error=None)

    summary = {"summarized": 0, "emailed": 0, "failed": 0}
    _process_episode(episode_id, _episode_to_video(episode), detail_level, summary)

    fresh = database.get_episode(episode_id)
    return {
        "status": fresh["status"],
        "transcript_source": fresh.get("transcript_source"),
        "error": fresh.get("error"),
        "emailed": summary["emailed"] > 0,
    }


def run_check() -> dict:
    """Run one full crawl cycle: discover new videos, fetch transcripts,
    summarize, email. Never lets one episode's failure abort the rest — each
    is wrapped so a bad transcript/AI hiccup just marks that episode 'error'.
    At the end, recent failures (status='error', created within the last
    _AUTO_RETRY_MAX_AGE_DAYS days) each get one automatic re-attempt (#491),
    so a fixed cookie/network issue heals old failures without manual work.

    Returns a summary dict: {new, summarized, emailed, failed, retried, skipped}.
    """
    import fcntl

    cfg = database.get_podcast_config()
    if cfg.get("enabled", "1") not in ("1", "true", "True"):
        logger.info("podcast: disabled via config, skipping check")
        return {"new": 0, "summarized": 0, "emailed": 0, "failed": 0,
                "retried": 0, "skipped": True, "reason": "disabled"}

    # Non-blocking cross-process lock (#495): if another run is still going
    # (transcribing a backlog can take over an hour), skip this cycle instead
    # of processing the same episodes twice in parallel. Opened "a+" (not
    # "w") so a failed acquisition doesn't truncate the holder's start
    # timestamp (#565); the holder writes its own start time below so the
    # busy branch can report how long the other run has been going.
    os.makedirs(os.path.dirname(_RUN_LOCK_PATH), exist_ok=True)
    lock_file = open(_RUN_LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        held_minutes = _lock_age_minutes(lock_file)
        lock_file.close()
        logger.info(
            "podcast: another check is already running%s, skipping this cycle",
            f" (started {held_minutes:.0f} min ago)" if held_minutes is not None else "",
        )
        return {"new": 0, "summarized": 0, "emailed": 0, "failed": 0,
                "retried": 0, "skipped": True, "reason": "busy",
                "held_minutes": held_minutes}
    try:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(datetime.now().isoformat(timespec="seconds"))
        lock_file.flush()
        return _run_check_locked(cfg)
    finally:
        lock_file.close()


def _lock_age_minutes(lock_file) -> float | None:
    """How long ago the current holder of the run lock started, in minutes,
    read from the ISO timestamp it wrote into the lock file — None if the
    file is empty/unparseable (e.g. a holder from before #565)."""
    try:
        lock_file.seek(0)
        started = datetime.fromisoformat(lock_file.read().strip())
        return max((datetime.now() - started).total_seconds() / 60, 0.0)
    except (OSError, ValueError):
        return None


def _run_check_locked(cfg: dict) -> dict:

    detail_level = cfg.get("detail_level") or "detailed"
    summary = {"new": 0, "summarized": 0, "emailed": 0, "failed": 0,
               "retried": 0, "skipped": False}

    try:
        new_videos = fetch_new_videos()
    except Exception as e:
        logger.error("podcast: fetch_new_videos failed: %s", e)
        summary["failed"] += 1
        summary["error"] = str(e)
        return summary

    processed_ids: set[int] = set()
    for video in new_videos:
        episode_id = database.create_pending_episode(
            video["video_id"], video["channel_id"], video["title"],
            video["published_at"], video["youtube_url"],
            video.get("audio_url"), video.get("duration_seconds"),
            author=video.get("feed_title"), platform="podcast",
        )
        summary["new"] += 1
        # Auto-processing (#502): only immediately transcribe+summarize when
        # the source feed has auto_process=1 *and* this isn't part of a
        # feed's first-run backfill — a freshly-subscribed feed's back
        # catalog is stored metadata-only, transcribed on demand from the UI.
        if video.get("auto_process") and not video.get("is_backfill"):
            processed_ids.add(episode_id)
            _process_episode(episode_id, video, detail_level, summary)

    # Auto-retry pass (#491): give recent failures one more chance per cycle,
    # capped at _AUTO_RETRY_PER_CYCLE oldest-first (#495) so a big backlog is
    # chewed through gradually instead of one multi-hour run. Episodes that
    # just failed above are skipped — retrying immediately in the same cycle
    # would almost certainly fail the same way (and double the transcription
    # cost); they'll be picked up on the next cron run instead.
    retryable = [ep for ep in database.list_recent_error_episodes(max_age_days=_AUTO_RETRY_MAX_AGE_DAYS)
                 if ep["id"] not in processed_ids]
    for ep in retryable[:_AUTO_RETRY_PER_CYCLE]:
        logger.info("podcast: auto-retrying failed episode %s (%s)", ep["id"], ep["video_id"])
        summary["retried"] += 1
        database.update_episode(ep["id"], status="pending", error=None)
        _process_episode(ep["id"], _episode_to_video(ep), detail_level, summary)

    return summary

"""Podcast crawler API (issue #479, RSS source #497, Tingwu transcriber
#498, per-feed manager #502).
"""
import json
import logging
import threading
from xml.etree import ElementTree

import database
import knowledge.rendition
import podcast
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# Episode ids currently being manually processed (#502, POST
# /api/podcast/episodes/{id}/process) — guards against double-submitting the
# same episode (e.g. a double click) and lets the episode-list endpoints
# report a "processing" status without writing anything to the DB for it.
_PROCESSING_IDS: set[int] = set()
_PROCESSING_LOCK = threading.Lock()


@router.post("/api/podcast/check")
def check_podcast():
    """Run one crawl cycle synchronously and return a summary. Called by
    scripts/podcast_check.py (cron) or manually for testing."""
    try:
        return podcast.run_check()
    except Exception as e:
        logger.error("podcast check failed: %s", e)
        raise HTTPException(500, str(e))


def _overlay_processing_status(episode: dict) -> dict:
    """Cosmetic-only: episodes currently being manually processed (#502) show
    status='processing' in API responses without that ever being written to
    the DB row (the DB status stays 'pending' until the background thread
    finishes and updates it for real)."""
    if episode["id"] in _PROCESSING_IDS:
        episode = {**episode, "status": "processing"}
    return episode


@router.get("/api/podcast/episodes")
def list_episodes(
    limit: int = 100,
    feed_id: int | None = None,
    kind: list[str] | None = Query(None),
    sort: str | None = None,
    order: str | None = None,
    platform: list[str] | None = Query(None),
    author: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    tag: list[str] | None = Query(None),
    since: str | None = None,
    list_id: int | None = None,
    include_archived: bool = False,
):
    """The material list (#936). Every filter axis repeats (?tag=a&tag=b = OR
    within the axis, AND across axes); `kind` keeps working as a single value
    because that is how every pre-#936 caller and bookmark spells it.

    `sort`/`order` are validated against database.EPISODE_SORTS — an unknown
    value falls back to the default order instead of 400ing, because a stale
    bookmark should still show the list.

    `include_archived` defaults to False: archived material (#940) is out of
    the way by default, and a filter toggle brings it back.
    """
    feed_url = None
    if feed_id is not None:
        feed = database.get_feed(feed_id)
        if not feed:
            raise HTTPException(404, "Feed not found")
        feed_url = feed["url"]
    episodes = database.list_episodes(
        limit=limit, feed_url=feed_url, kind=kind, sort=sort, order=order,
        platform=platform, author=author, status=status, tag=tag,
        since=since, list_id=list_id, include_archived=include_archived,
    )
    return [_overlay_processing_status(e) for e in episodes]


@router.get("/api/knowledge/facets")
def knowledge_facets():
    """Everything the filter bar's dropdowns need, in one request (#936):
    the kinds/platforms/authors/statuses that actually occur in the library,
    plus the feed, tag and list catalogs. One call rather than one per
    dropdown — the bar renders as a unit, and five parallel requests on every
    list load is five times the chance of a half-drawn filter bar."""
    return database.knowledge_facets()


@router.get("/api/podcast/episodes/{episode_id}")
def get_episode(episode_id: int, lang: str = "zh"):
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    # Lazy bilingual-transcript backfill (#553): episodes summarized before this
    # feature have transcript_zh but no transcript_de — build it on first detail
    # view so old episodes also get the parallel view. Best-effort, one-time.
    if episode.get("transcript_zh") and not episode.get("transcript_de"):
        try:
            pairs = podcast.build_transcript_de(episode["transcript_zh"])
            if pairs:
                database.update_episode(episode_id, transcript_de=pairs)
                episode["transcript_de"] = pairs
        except Exception:
            logger.warning("podcast: lazy transcript_de backfill failed for episode %s",
                           episode_id, exc_info=True)
    # Per-language reading rendition (#804): zh reads summary_zh/hsk_words
    # directly (unchanged, see docstring on knowledge/rendition.py), every
    # other language gets a lazily generated + cached translated summary.
    # Never lets a translation failure turn the whole detail view into a
    # 500 — the frontend gets rendition=None + rendition_error and falls
    # back to showing the German summary.
    if lang != "zh":
        episode = dict(episode)
        try:
            rendition = knowledge.rendition.get_or_create_rendition(episode_id, lang)
            episode["rendition"] = rendition
            episode["rendition_error"] = None
        except knowledge.rendition.RenditionError as e:
            episode["rendition"] = None
            episode["rendition_error"] = str(e)
    return _overlay_processing_status(episode)


@router.post("/api/podcast/episodes/{episode_id}/retry")
def retry_episode(episode_id: int):
    """Re-run the full processing pipeline for one failed episode (#491) —
    the manual per-episode recovery path after e.g. an expired YouTube cookie
    failed a whole batch. Only error/no_transcript episodes are retryable;
    summarized episodes are done and pending ones are still being worked on."""
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] == "summarized":
        raise HTTPException(
            400, "Episode is already summarized — nothing to retry"
        )
    return podcast.retry_episode(episode_id)


def _process_episode_thread(episode_id: int) -> None:
    """Background thread body for POST /episodes/{id}/process (#502) — runs
    the full pipeline via podcast.retry_episode (which itself never raises,
    it stores status='error' on failure), but is wrapped again here anyway
    since a raise inside a bare thread would otherwise vanish silently."""
    try:
        podcast.retry_episode(episode_id)
    except Exception as e:
        logger.error("podcast: manual processing failed for episode %s: %s", episode_id, e)
    finally:
        with _PROCESSING_LOCK:
            _PROCESSING_IDS.discard(episode_id)


@router.post("/api/podcast/episodes/{episode_id}/process")
def process_episode(episode_id: int):
    """Manually trigger transcription+summary for one episode (#502) — used
    by the podcast manager UI for episodes from a non-auto-process feed (or
    a feed's backfilled back catalog), which are stored metadata-only until
    Daniel picks them to transcribe. Runs in a background thread; the
    response returns immediately so the UI can start polling."""
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] == "summarized":
        raise HTTPException(400, "Episode is already summarized — nothing to process")
    with _PROCESSING_LOCK:
        if episode_id in _PROCESSING_IDS:
            raise HTTPException(409, "Episode is already being processed")
        _PROCESSING_IDS.add(episode_id)
    threading.Thread(target=_process_episode_thread, args=(episode_id,), daemon=True).start()
    return {"status": "processing"}


def _regenerate_summary_thread(episode_id: int) -> None:
    """Background thread body for POST /episodes/{id}/regenerate-summary
    (#567) — podcast.regenerate_summary never downgrades the episode on
    failure, this wrapper just keeps a stray raise from vanishing silently
    and releases the processing guard."""
    try:
        podcast.regenerate_summary(episode_id)
    except Exception as e:
        logger.error("podcast: summary regeneration failed for episode %s: %s", episode_id, e)
    finally:
        with _PROCESSING_LOCK:
            _PROCESSING_IDS.discard(episode_id)


@router.post("/api/podcast/episodes/{episode_id}/regenerate-summary")
def regenerate_summary(episode_id: int):
    """Regenerate ONLY the summary of an already-summarized episode (#567),
    reusing the stored transcript — for re-styling old episodes after a
    prompt change or redoing an unsatisfying summary. Runs in a background
    thread (a NotebookLM summary round can take ~10 min); while it runs the
    episode shows status='processing' via _overlay_processing_status, and on
    failure the existing summary stays untouched."""
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] != "summarized":
        raise HTTPException(400, "Only summarized episodes can regenerate their summary — use process/retry instead")
    if not (episode.get("transcript_zh") or "").strip():
        raise HTTPException(400, "Episode has no stored transcript")
    with _PROCESSING_LOCK:
        if episode_id in _PROCESSING_IDS:
            raise HTTPException(409, "Episode is already being processed")
        _PROCESSING_IDS.add(episode_id)
    threading.Thread(target=_regenerate_summary_thread, args=(episode_id,), daemon=True).start()
    return {"status": "processing"}


class NotifyBody(BaseModel):
    channel: str


@router.post("/api/podcast/episodes/{episode_id}/notify")
def notify_episode(episode_id: int, body: NotifyBody):
    """Manually (re-)send the notification for an already-summarized episode
    (#530) — Daniel wants an on-demand "Send to Signal"/"Send Email" button
    on the episode detail page, independent of whether it was already sent
    automatically. Runs synchronously (sending itself only takes a few
    seconds). email_sent_at is intentionally left untouched on resend — that
    column means "first automatic send time", not "last sent"."""
    if body.channel not in ("signal", "email"):
        raise HTTPException(400, "channel must be 'signal' or 'email'")
    episode = database.get_episode(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] != "summarized":
        raise HTTPException(400, "Episode is not summarized yet — nothing to send")

    if body.channel == "signal":
        sent = podcast.send_signal(episode)
    else:
        sent = podcast.send_email(episode)

    if sent:
        return {"sent": True}
    return {
        "sent": False,
        "detail": "SIGNAL_ACCOUNT/SMTP not configured or send failed — check server logs",
    }


@router.get("/api/podcast/feeds")
def list_feeds():
    return database.list_feeds()


class FeedCreate(BaseModel):
    url: str


@router.post("/api/podcast/feeds")
def create_feed(body: FeedCreate):
    """Add a new RSS feed subscription. Validates the URL is a parseable RSS
    feed (fetches it once, synchronously) and pulls the channel <title> for
    display before storing — new feeds default to auto_process=0 (manual),
    matching #502's "opt in to automation per-source" design.

    Immediately backfills the feed's latest FIRST_RUN_BACKFILL episodes as
    metadata-only pending rows (#593), reusing the RSS root already fetched
    for validation — so the newest episodes show up in the list right away
    instead of waiting for the next crawl cycle. Nothing is transcribed."""
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        root = ElementTree.fromstring(podcast._http_get(url, timeout=30))
    except Exception as e:
        raise HTTPException(400, f"Not a valid RSS feed: {e}")
    title_el = root.find("channel/title")
    title = title_el.text.strip() if title_el is not None and title_el.text else None
    try:
        feed_id = database.create_feed(url, title=title, auto_process=0)
    except Exception:
        # sqlite3.IntegrityError on the UNIQUE(url) constraint — anything
        # else here would be unexpected, but still just a 400 (bad input),
        # not a 500 (server bug).
        raise HTTPException(400, "Feed already exists")
    # Backfill the latest episodes (metadata-only). A failure here must not
    # undo the successful subscription — the next crawl cycle backfills too.
    try:
        podcast.ingest_feed_episodes(feed_id, podcast.FIRST_RUN_BACKFILL, root=root)
    except Exception as e:
        logger.warning("podcast: initial backfill failed for feed %s: %s", feed_id, e)
    return database.get_feed(feed_id)


class FeedUpdate(BaseModel):
    auto_process: bool | None = None
    title: str | None = None


@router.put("/api/podcast/feeds/{feed_id}")
def update_feed(feed_id: int, body: FeedUpdate):
    if not database.get_feed(feed_id):
        raise HTTPException(404, "Feed not found")
    updates = body.model_dump(exclude_none=True)
    if "auto_process" in updates:
        updates["auto_process"] = int(updates["auto_process"])
    if updates:
        database.update_feed(feed_id, **updates)
    return database.get_feed(feed_id)


@router.delete("/api/podcast/feeds/{feed_id}")
def delete_feed(feed_id: int):
    if not database.get_feed(feed_id):
        raise HTTPException(404, "Feed not found")
    database.delete_feed(feed_id)
    return {"deleted": True}


@router.post("/api/podcast/feeds/{feed_id}/load-more")
def load_more_feed(feed_id: int):
    """Pull in the next page of older back-catalog episodes for this feed,
    metadata-only (transcribe on demand). See podcast.load_more_episodes."""
    try:
        return podcast.load_more_episodes(feed_id)
    except ValueError:
        raise HTTPException(404, "Feed not found")
    except Exception as e:
        logger.error("podcast load-more failed for feed %s: %s", feed_id, e)
        raise HTTPException(500, str(e))


class PodcastConfigUpdate(BaseModel):
    detail_level: str | None = None
    enabled: str | None = None
    email_to: str | None = None
    feeds: list[str] | None = None  # deprecated (#502) — feeds now live in podcast_feeds; kept only so old clients/scripts don't 422
    whisper_fallback: str | None = None  # deprecated (#485), superseded by transcriber (#486)
    transcriber: str | None = None
    whisper_max_minutes: str | None = None
    summarizer: str | None = None


@router.get("/api/podcast/config")
def get_config():
    cfg = database.get_podcast_config()
    if "feeds" in cfg:
        try:
            cfg["feeds"] = json.loads(cfg["feeds"])
        except (TypeError, ValueError):
            cfg["feeds"] = []
    return cfg


@router.put("/api/podcast/config")
def update_config(body: PodcastConfigUpdate):
    updates = body.model_dump(exclude_none=True)
    if "detail_level" in updates and updates["detail_level"] not in ("short", "medium", "detailed"):
        raise HTTPException(400, "detail_level must be short, medium or detailed")
    if "transcriber" in updates and updates["transcriber"] not in ("auto", "tingwu", "notebooklm", "whisper", "off"):
        raise HTTPException(400, "transcriber must be auto, tingwu, notebooklm, whisper or off")
    if "whisper_max_minutes" in updates:
        try:
            float(updates["whisper_max_minutes"])
        except (TypeError, ValueError):
            raise HTTPException(400, "whisper_max_minutes must be a number (0 = no limit)")
    if "summarizer" in updates and updates["summarizer"] not in ("auto", "api"):
        raise HTTPException(400, "summarizer must be auto or api")
    if "feeds" in updates:
        if not updates["feeds"] or not all(isinstance(u, str) and u.strip() for u in updates["feeds"]):
            raise HTTPException(400, "feeds must be a non-empty list of RSS feed URLs")
        updates["feeds"] = json.dumps(updates["feeds"])
    for key, value in updates.items():
        database.set_podcast_config(key, value)
    return get_config()

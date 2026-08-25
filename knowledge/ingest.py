"""Shared ingestion core for the knowledge base "add a URL" / "paste text"
pipeline (issue #651/#652, extracted issue #655, extended #668).

`ingest_url()` is the ONE place that turns an arbitrary URL into a
podcast_episodes row (kind='video' for YouTube, kind='article' for
everything else trafilatura can extract a body from). `ingest_text()` is
the equivalent for a pasted article body (paywalled articles trafilatura
can't reach — #668) — same kind='article' row, same "build the row"
helper (`_store_article`), just a different source for the body text and
a different dedup key (no URL to hash, so the body itself is hashed
instead). routes/knowledge.py (POST /api/knowledge/add and
/api/knowledge/add-text, the paste boxes in the UI) and
knowledge/mailbox.py (IMAP mailbox polling, #655) call these functions
directly — no HTTP-calls-itself loop, no second parallel pipeline. This
repo has been burned by that exact mistake before (#643: two add-word
entry points, the bug fixed in one silently came back in the other) so
there must only ever be one ingestion path here too.

Deliberately framework-free: raises plain `IngestError` instead of
fastapi.HTTPException so non-HTTP callers (the mailbox script) don't need
to import fastapi just to catch a failure.
"""
import hashlib
import logging
import re

import database
import knowledge.article
import knowledge.instagram
import knowledge.youtube

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """A URL or pasted text could not be turned into a podcast_episodes row
    (bad/unrecognized URL, metadata fetch failed, article extraction
    failed, pasted text too short, ...)."""


def ingest_url(url: str, china_critical: bool = False) -> dict:
    """Resolve `url` to a podcast_episodes row and return either
    {"episode_id": int} (newly created) or {"status": "already_exists",
    "episode_id": int} (deduped). Raises IngestError on failure.

    `china_critical` (#731) is stored on the row and only read much later,
    at summarize time (podcast.summarize) — the summary happens in the
    separate .../process call when nobody is watching, so the flag has to be
    captured here, at paste time, which is the only moment Daniel knows what
    he is adding. A deduped row keeps whatever flag it already had."""
    url = (url or "").strip()
    if not url:
        raise IngestError("url is required")

    video_id = knowledge.youtube.parse_video_id(url)
    if video_id:
        return _ingest_video(url, video_id, china_critical=china_critical)

    shortcode = knowledge.instagram.parse_shortcode(url)
    if shortcode:
        return _ingest_instagram(url, shortcode, china_critical=china_critical)

    return _ingest_article(url, china_critical=china_critical)


def _ingest_video(url: str, video_id: str, china_critical: bool = False) -> dict:
    existing = database.get_episode_by_video_id(video_id)
    if existing:
        return {"status": "already_exists", "episode_id": existing["id"]}

    try:
        meta = knowledge.youtube.fetch_metadata(video_id)
    except Exception as e:
        logger.warning("knowledge.ingest: oEmbed metadata lookup failed for %s: %s", video_id, e)
        raise IngestError(f"Could not fetch video metadata: {e}")

    title = meta.get("title") or video_id
    # translate_title (#651) is one cheap AI call — best-effort, must not
    # block/fail the ingest if it errors (translate_title itself already
    # swallows exceptions and returns None in that case).
    import ai
    title_en = ai.translate_title(title)

    episode_id = database.create_pending_episode(
        video_id=video_id,
        channel_id=meta.get("author_name"),
        title=title,
        published_at=None,
        youtube_url=url,
        audio_url=None,
        duration_seconds=None,
        kind="video",
        china_critical=china_critical,
        author=meta.get("author_name"),
        platform="youtube",
    )
    if title_en:
        database.update_episode(episode_id, title_en=title_en)

    return {"episode_id": episode_id}


def _ingest_instagram(url: str, shortcode: str, china_critical: bool = False) -> dict:
    """Instagram Reel/Post ingestion (#750), metadata-only at add time —
    mirrors _ingest_video: the audio download + transcription (Groq/OpenAI
    Whisper, podcast._transcribe_instagram) happens later, in the
    .../process call, not here. video_id is the Instagram shortcode
    (Instagram's own unique-per-post id), the same dedup key _ingest_video
    uses the YouTube video id for."""
    existing = database.get_episode_by_video_id(shortcode)
    if existing:
        return {"status": "already_exists", "episode_id": existing["id"]}

    try:
        meta = knowledge.instagram.fetch_metadata(url)
    except knowledge.instagram.InstagramError as e:
        logger.warning("knowledge.ingest: Instagram metadata lookup failed for %s: %s", shortcode, e)
        raise IngestError(str(e))
    except Exception as e:
        logger.warning("knowledge.ingest: Instagram metadata lookup failed for %s: %s", shortcode, e)
        raise IngestError(f"Could not fetch Instagram metadata: {e}")

    title = meta.get("title") or shortcode
    # translate_title (#651) is one cheap AI call — best-effort, must not
    # block/fail the ingest if it errors (translate_title itself already
    # swallows exceptions and returns None in that case).
    import ai
    title_en = ai.translate_title(title)

    episode_id = database.create_pending_episode(
        video_id=shortcode,
        channel_id=meta.get("uploader"),
        title=title,
        published_at=None,
        youtube_url=meta.get("webpage_url") or url,
        audio_url=None,
        duration_seconds=meta.get("duration"),
        kind="video",
        china_critical=china_critical,
        author=meta.get("uploader"),
        platform="instagram",
    )
    if title_en:
        database.update_episode(episode_id, title_en=title_en)

    return {"episode_id": episode_id}


def _existing_episode(video_id: str) -> dict | None:
    """Dedup lookup shared by every ingestion path that lands on
    podcast_episodes.video_id (article-by-URL, article-by-pasted-text).
    Returns the already_exists response shape, or None if this is new."""
    existing = database.get_episode_by_video_id(video_id)
    if existing:
        return {"status": "already_exists", "episode_id": existing["id"]}
    return None


def _store_article(*, video_id: str, title: str, site: str | None, published_at,
                    source_url: str | None, text: str, transcript_source: str,
                    china_critical: bool = False, kind: str = "article",
                    author: str | None = None, platform: str | None = None) -> dict:
    """Create the kind='article' (or kind='newsletter', #925) podcast_episodes
    row and store `text` as transcript_zh immediately — this is the ONE
    row-building code path for _ingest_article (URL), ingest_text (pasted
    body, #668) and newsletter.ingest_newsletter() (#925); they differ only
    in where `text`/`title`/`site`/`published_at`/`kind` come from. Landing
    transcript_zh here (rather than deferring the fetch to process time)
    puts both paths straight into _process_episode's "reuse existing
    transcript" fast path when the frontend later calls
    POST /api/podcast/episodes/{id}/process — see article.py's docstring.

    `author`/`platform` (#935) feed the unified material list's filters. Note
    that `site` and `author` are NOT the same thing even though both paths
    historically shared the channel_id column: a fetched article's `site` is a
    domain, which is not an author — so _ingest_article passes no author and
    lets #937/#938 fill it in, while the paste path passes what Daniel typed.

    Caller must already have deduped via `_existing_episode()`."""
    title = title or video_id
    # translate_title (#651) is one cheap AI call — best-effort, must not
    # block/fail the ingest if it errors (translate_title itself already
    # swallows exceptions and returns None in that case).
    import ai
    title_en = ai.translate_title(title)

    episode_id = database.create_pending_episode(
        video_id=video_id,
        channel_id=site,
        title=title,
        published_at=published_at,
        # youtube_url is NOT NULL in schema.sql — pasted text (#668) may
        # have no source_url at all, so fall back to "" rather than None.
        youtube_url=source_url or "",
        audio_url=None,
        duration_seconds=None,
        kind=kind,
        china_critical=china_critical,
        author=author,
        platform=platform,
    )
    updates = {"transcript_zh": text, "transcript_source": transcript_source}
    if title_en:
        updates["title_en"] = title_en
    database.update_episode(episode_id, **updates)

    return {"episode_id": episode_id}


def _ingest_article(url: str, china_critical: bool = False) -> dict:
    """Article ingestion (#652): anything that isn't a recognized YouTube
    URL is treated as an article. normalize_url() (strips utm_*/fbclid/...)
    is the dedup key, so the same article shared via different links lands
    on the same row instead of duplicating.

    The dedup check happens BEFORE fetch_article() (a real network
    download) so an already-ingested URL never pays that cost again."""
    normalized = knowledge.article.normalize_url(url)
    dup = _existing_episode(normalized)
    if dup:
        return dup

    try:
        article = knowledge.article.fetch_article(url)
    except knowledge.article.ArticleExtractionError as e:
        raise IngestError(str(e))
    except Exception as e:
        logger.warning("knowledge.ingest: article extraction failed for %s: %s", url, e)
        raise IngestError(f"Could not fetch article: {e}")

    return _store_article(
        video_id=normalized,
        title=article["title"],
        site=article["site"],
        platform="web",
        published_at=article.get("published_at"),
        source_url=url,
        text=article["text"],
        transcript_source="article",
        china_critical=china_critical,
    )


# Same paywall-stub / context-window guards as knowledge.article (#652) —
# a pasted body is stored exactly like a fetched one from here on, so the
# same thresholds apply for the same reasons (see that module's docstring).
_MIN_TEXT_CHARS = knowledge.article._MIN_ARTICLE_CHARS
_MAX_TEXT_CHARS = knowledge.article._MAX_ARTICLE_CHARS

# Ceiling for the first-line-of-the-body title fallback (#833).
_MAX_TITLE_CHARS = 120


def _fill_missing_metadata(text: str, title: str, author: str | None,
                           source_url: str | None) -> tuple[str, str | None, str | None, str | None]:
    """Ask the AI for whatever of title/author/source_url Daniel left blank
    (#833), and return (title, author, source_url, published_at).

    Two rules this must never break:
      - No AI call at all when all three are already filled in. The whole
        point is convenience for the blank ones; paying for a call whose
        result would be discarded is pure waste.
      - Values the user typed are authoritative and are NEVER overwritten.
        He was looking at the article; the model is guessing from its first
        3000 chars.

    published_at is a pure bonus (only ever comes from the AI, there is no
    form field for it) and is None whenever the model didn't produce a
    parseable date.
    """
    if title and author and source_url:
        return title, author, source_url, None

    import ai
    meta = ai.extract_article_metadata(text)   # {} on any failure — never raises
    return (
        title or (meta.get("title") or "").strip(),
        author or meta.get("author"),
        source_url or meta.get("source_url"),
        meta.get("published_at"),
    )


def ingest_text(title: str | None, text: str, source_url: str | None = None,
                author: str | None = None, china_critical: bool = False,
                fallback_title: str | None = None, kind: str = "article",
                platform: str = "paste") -> dict:
    """Ingest a pasted article body (#668) — for paywalled articles
    (Spiegel+, FAZ, ...) the server can't fetch, but the user can read in
    their browser and paste the text in directly. Same row-building path
    and transcript_zh storage as _ingest_article(), via _store_article();
    only the dedup key and body source differ (there's no URL to hash, so
    the body itself is hashed instead — see below).

    `kind` defaults to "article" (every existing caller's behaviour is
    unchanged) — knowledge/newsletter.py (#925) passes kind="newsletter"
    for known newsletter senders so their rows are distinguishable in the
    UI's Newsletter tab without adding a second ingestion path.

    `title`, `author` and `source_url` are all optional (#833): whichever
    Daniel left blank is filled in by one cheap AI call over the head of the
    body (_fill_missing_metadata), falling back to the body's first line for
    the title. `author` lands in the same column a fetched article's site
    name does (channel_id) — it's the "who is this from" slot — and, since
    #935, in the dedicated `author` column the material list filters on.

    `platform` (#935) says where the body arrived from: 'paste' (the default,
    i.e. the paste box), 'upload' (a file), 'email' (mailbox/newsletter) or
    'signal'. It is NOT derivable from `kind`, so every caller passes its own.

    Raises IngestError if `text` is under 200 chars (same threshold as
    knowledge.article._MIN_ARTICLE_CHARS — too short to be a real article,
    not just a snippet/teaser). Text over 15000 chars is truncated (same
    ceiling as knowledge.article._MAX_ARTICLE_CHARS).
    """
    text = (text or "").strip()
    if len(text) < _MIN_TEXT_CHARS:
        raise IngestError(
            f"pasted text too short ({len(text)} chars, need >= {_MIN_TEXT_CHARS})"
        )
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]

    title = (title or "").strip()
    author = (author or "").strip() or None
    source_url = (source_url or "").strip() or None

    # Whitespace must be normalized BEFORE hashing: the same article pasted
    # twice with different line-wrapping/blank-line whitespace must still
    # hash to the same dedup key, or every re-paste creates a new row.
    # Only the body is hashed — re-pasting the same article under a
    # different title must still land on the existing row.
    normalized = re.sub(r"\s+", " ", text).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    video_id = f"pasted:{digest}"

    # Dedup BEFORE the metadata AI call, for the same reason _ingest_article
    # dedups before downloading: an already-ingested body must never pay
    # that cost a second time.
    dup = _existing_episode(video_id)
    if dup:
        return dup

    title, author, source_url, published_at = _fill_missing_metadata(
        text, title, author, source_url)
    if not title:
        # A caller that has a better guess than "first line of the body"
        # supplies one (#835: an upload passes the filename). Still ranked
        # below the AI's reading of the actual text.
        title = (fallback_title or "").strip()
    if not title:
        # Last resort: the body's first non-blank line. Often a fine
        # headline, sometimes navigation debris — which is exactly why the
        # AI gets to try first now. Truncated because an unwrapped paste is
        # one single "line": without the cap the whole article ends up in
        # the title column and every list view is unreadable.
        title = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if len(title) > _MAX_TITLE_CHARS:
            title = title[:_MAX_TITLE_CHARS].rstrip() + "…"

    return _store_article(
        video_id=video_id,
        title=title or "(untitled)",
        site=author,
        published_at=published_at,
        source_url=source_url,
        text=text,
        transcript_source="pasted",
        china_critical=china_critical,
        kind=kind,
        author=author,
        platform=platform,
    )

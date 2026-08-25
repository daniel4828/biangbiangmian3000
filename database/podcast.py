"""Podcast crawler storage (issues #479, #497, #498, #502): episodes
discovered from podcast RSS feeds (podcast_feeds, one row per source, #502)
+ a small key-value config table (notification email, summary detail level,
enabled flag; the legacy `feeds` key is unused since #502 but kept for
backward compat, see database/core.py's one-time migration).

All SQL for the podcast feature lives here — podcast.py (the crawler logic)
and routes/podcast.py only call into this module.
"""
import json
from .core import get_db
from .knowledge import tags_for_items, list_membership


# ---------------------------------------------------------------------------
# Config (key-value)
# ---------------------------------------------------------------------------

# Keys the crawler/UI is allowed to read or write. Kept in one place so
# routes/podcast.py's PUT endpoint can validate against the same whitelist.
# `whisper_fallback` (#485) is kept for backward compat, normalized into the
# newer `transcriber` key (#486) by podcast._resolve_transcriber.
# `notebooklm_notebook_id` is a crawler-internal cache, not meant to be set
# directly via the PUT endpoint. `channel_url`/`channel_id`/
# `whisper_title_filter` are retired (#497) — kept so old rows/installs don't
# break, no longer read by the crawler. `feeds` (#497, JSON array of RSS
# feed URLs) replaces `channel_url` as the source list.
CONFIG_KEYS = (
    "feeds", "email_to", "detail_level", "enabled", "channel_url", "channel_id",
    "whisper_fallback", "transcriber", "whisper_title_filter", "whisper_max_minutes",
    "notebooklm_notebook_id", "summarizer",
)


def get_podcast_config() -> dict:
    """All podcast_config rows as a flat {key: value} dict."""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM podcast_config").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_podcast_config(key: str, value: str) -> None:
    """Upsert one config key. Used both by the crawler (caching channel_id)
    and the settings API (detail_level/enabled/email_to/channel_url)."""
    conn = get_db()
    conn.execute(
        "INSERT INTO podcast_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Feeds (issue #502)
# ---------------------------------------------------------------------------

def list_feeds() -> list[dict]:
    """All configured RSS feeds, oldest-added first (created_at ASC) so the
    list order stays stable as new feeds are appended, with each feed's
    stored episode count attached."""
    conn = get_db()
    rows = conn.execute(
        """SELECT f.*, COUNT(e.id) AS episode_count
           FROM podcast_feeds f
           LEFT JOIN podcast_episodes e ON e.channel_id = f.url
           GROUP BY f.id
           ORDER BY f.created_at, f.id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_feed(feed_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM podcast_feeds WHERE id = ?", (feed_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_feed_by_url(url: str) -> dict | None:
    """Look up a feed by its RSS URL — episodes store the feed's url as
    channel_id (#532: Signal notification needs the podcast title)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM podcast_feeds WHERE url = ?", (url,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_feed(url: str, title: str | None = None, auto_process: int = 0) -> int:
    """Insert a new feed row. Raises sqlite3.IntegrityError (caller/route
    turns it into a 400) if `url` is already subscribed (UNIQUE constraint)."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO podcast_feeds (url, title, auto_process) VALUES (?, ?, ?)",
        (url, title, int(auto_process)),
    )
    conn.commit()
    feed_id = cur.lastrowid
    conn.close()
    return feed_id


def update_feed(feed_id: int, **fields) -> None:
    """Generic column update for a feed (title/auto_process)."""
    if not fields:
        return
    conn = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE podcast_feeds SET {set_clause} WHERE id = ?",
        (*fields.values(), feed_id),
    )
    conn.commit()
    conn.close()


def delete_feed(feed_id: int) -> None:
    """Remove a feed's subscription row. Episodes already ingested from it
    are left in place as history (channel_id keeps pointing at the feed URL,
    which no longer resolves to a podcast_feeds row)."""
    conn = get_db()
    conn.execute("DELETE FROM podcast_feeds WHERE id = ?", (feed_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

def get_episode_by_video_id(video_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM podcast_episodes WHERE video_id = ?", (video_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_known_video_ids() -> set[str]:
    """Used to filter the RSS feed down to genuinely new videos."""
    conn = get_db()
    rows = conn.execute("SELECT video_id FROM podcast_episodes").fetchall()
    conn.close()
    return {r["video_id"] for r in rows}


def has_any_episode_for_feed(feed_url: str) -> bool:
    """True once at least one episode from this specific RSS feed (stored in
    the `channel_id` column, #497) has ever been stored — used to detect a
    feed's first crawl, which only backfills its latest FIRST_RUN_BACKFILL
    episodes instead of its entire back catalog."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM podcast_episodes WHERE channel_id = ? LIMIT 1", (feed_url,)
    ).fetchone()
    conn.close()
    return row is not None


def create_pending_episode(video_id: str, channel_id: str | None, title: str,
                           published_at: str | None, youtube_url: str,
                           audio_url: str | None = None,
                           duration_seconds: int | None = None,
                           kind: str = 'podcast',
                           china_critical: bool = False,
                           author: str | None = None,
                           platform: str | None = None) -> int:
    """Insert a new episode row with status=pending. Returns the new id.

    `channel_id` stores the source RSS feed URL (#497, was a YouTube channel
    id pre-#497). `youtube_url` stores the episode's webpage link (item
    <link>; name kept for backward compat with existing rows/column).
    `audio_url`/`duration_seconds` (#497) come from the RSS enclosure and
    itunes:duration. `kind` (#650) is 'podcast' | 'video' | 'article' — see
    docs/knowledge-base.md for what the generic columns mean per kind.
    `china_critical` (#731) makes the API summary fallback skip DeepSeek and
    use OpenAI directly — see podcast.summarize().
    `author`/`platform` (#935) are what the unified material list filters on.
    They are deliberately separate from `channel_id`, which already means four
    different things depending on kind and so can't be filtered on. Callers
    pass an author only when they genuinely know one (channel name, uploader,
    what Daniel typed) — a site domain is not an author, and a wrong one is
    worse than none.
    """
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO podcast_episodes
           (video_id, channel_id, title, published_at, youtube_url, audio_url, duration_seconds,
            status, kind, china_critical, author, platform)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (video_id, channel_id, title, published_at, youtube_url, audio_url, duration_seconds,
         kind, 1 if china_critical else 0, author, platform),
    )
    conn.commit()
    episode_id = cur.lastrowid
    conn.close()
    return episode_id


# Columns whose contents are in the search index (#939). Writing any of them
# has to rebuild that episode's index rows — this is the choke point every
# transcript and summary already goes through, which is why the hook lives
# here rather than being sprinkled over the six call sites that would each
# have to remember it.
_SEARCHABLE_COLUMNS = frozenset({
    "title", "title_en", "author", "transcript_zh", "transcript_de",
    "summary_de", "summary_zh",
})


def update_episode(episode_id: int, **fields) -> None:
    """Generic column update for an episode. hsk_words and transcript_de (if
    present) are serialized to JSON automatically."""
    if not fields:
        return
    for _jcol in ("hsk_words", "transcript_de"):
        if _jcol in fields and not isinstance(fields[_jcol], str):
            fields[_jcol] = json.dumps(fields[_jcol], ensure_ascii=False)
    conn = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE podcast_episodes SET {set_clause} WHERE id = ?",
        (*fields.values(), episode_id),
    )
    conn.commit()
    conn.close()
    if _SEARCHABLE_COLUMNS & set(fields):
        from .search import reindex_episode
        reindex_episode(episode_id)


def recover_orphaned_podcast_episodes() -> int:
    """Reset episodes orphaned mid-transcription back to 'error' so run_check's
    auto-retry (#491) reprocesses them, reusing any stored transcript (#500).

    _process_episode stamps processing_started_at while it works and clears it
    (via finally) on every normal exit, so at process startup — when nothing is
    running yet — any row with processing_started_at still set was left by a
    restart/crash that killed the process mid-transcription (#598). Backfilled
    episodes never reach _process_episode, so their stamp stays NULL and they
    are untouched. 'summarized' rows are left alone defensively (a stray stamp
    there should not clobber a finished episode). Returns the number recovered."""
    conn = get_db()
    cur = conn.execute(
        """UPDATE podcast_episodes
           SET status = 'error',
               error = 'Interrupted by a restart — will auto-retry',
               processing_started_at = NULL
           WHERE processing_started_at IS NOT NULL
             AND status != 'summarized'""",
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def _hydrate(row: dict) -> dict:
    d = dict(row)
    raw = d.get("hsk_words")
    try:
        d["hsk_words"] = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        d["hsk_words"] = []
    raw_de = d.get("transcript_de")
    try:
        d["transcript_de"] = json.loads(raw_de) if raw_de else []
    except (ValueError, TypeError):
        d["transcript_de"] = []
    return d


def get_episode(episode_id: int) -> dict | None:
    """Full episode row (including transcript_zh) for the detail endpoint."""
    conn = get_db()
    row = conn.execute("SELECT * FROM podcast_episodes WHERE id = ?", (episode_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = _hydrate(row)
    d["tags"] = tags_for_items([episode_id]).get(episode_id, [])
    d["list_ids"] = list_membership([episode_id]).get(episode_id, [])
    return d


# Columns the unified material list (#936) may sort on, mapped to the SQL
# ordering they mean. A whitelist, not string interpolation of whatever the
# query string says: `sort` goes straight into an ORDER BY clause.
#
# processed_at is the default and gets special treatment: rows that have NOT
# been processed yet sort FIRST, because those are the ones waiting for Daniel
# to do something. NULLs would otherwise sink to the bottom of a DESC sort and
# the "you have unprocessed material" signal would be invisible. That leading
# term is hard-coded ASC and does NOT follow `order`: "not processed yet" is
# not a date, so flipping the direction must not bury it at the far end.
EPISODE_SORTS = {
    "processed_at": "processed_at IS NOT NULL ASC, processed_at {dir}",
    "published_at": "COALESCE(published_at, created_at) {dir}",
    "created_at":   "created_at {dir}",
    "title":        "title COLLATE NOCASE {dir}",
    "duration":     "COALESCE(duration_seconds, 0) {dir}",
    "author":       "author COLLATE NOCASE {dir}",
}


def _order_by(sort: str | None, order: str | None) -> str:
    """ORDER BY body for list_episodes(). Unknown values fall back to the
    default instead of raising: a stale bookmark or a typo in a query string
    should still show the list, just in the default order."""
    spec = EPISODE_SORTS.get(sort or "", EPISODE_SORTS["processed_at"])
    direction = "ASC" if (order or "").lower() == "asc" else "DESC"
    return spec.format(dir=direction) + ", id DESC"


def list_episodes(limit: int = 100, feed_url: str | None = None, kind=None,
                  *, sort: str | None = None, order: str | None = None,
                  platform=None, author=None, status=None, tag=None,
                  since: str | None = None, list_id: int | None = None,
                  include_archived: bool = True) -> list[dict]:
    """Episode list without the transcript full text (kept out for payload
    size).

    `feed_url` (#502, the podcast_feeds.url / episode's channel_id) restricts
    the list to one source. `kind` (#650) restricts to 'podcast' | 'video' |
    'article' | 'newsletter'; it accepts a list too (#936, the unified list
    filters on several at once). None means "all kinds".

    The rest are the #936 filter axes, each accepting a string or a list of
    strings (OR within one axis, AND across axes — the way filter bars are
    expected to behave):

      platform  where the material came from (podcast_episodes.platform)
      author    exact author match
      status    pending | summarized | no_transcript | error
      tag       tag NAME (case-insensitive); an item matches if it carries ANY
                of the given tags
      since     ISO date/datetime lower bound on the active sort's date
      list_id   only members of one knowledge_list (#940)
      include_archived  False hides rows with archived_at set

    `sort`/`order` are validated against EPISODE_SORTS — never interpolated
    raw."""
    conn = get_db()
    query = """SELECT id, video_id, channel_id, title, title_en, kind, published_at, youtube_url, spotify_url,
                      audio_url, duration_seconds,
                      summary_de, hsk_words, detail_level, status, error, email_sent_at, created_at,
                      transcript_source, china_critical,
                      processed_at, author, platform, archived_at,
                      (transcript_zh IS NOT NULL AND transcript_zh != '') AS has_transcript
               FROM podcast_episodes"""
    clauses: list = []
    params: list = []
    if feed_url:
        clauses.append("channel_id = ?")
        params.append(feed_url)

    def _in(column: str, value) -> None:
        """OR-within-an-axis: one value or many, always as an IN clause."""
        values = [v for v in ([value] if isinstance(value, str) else (value or [])) if v]
        if not values:
            return
        clauses.append(f"{column} IN ({','.join('?' * len(values))})")
        params.extend(values)

    _in("kind", kind)
    _in("platform", platform)
    _in("author", author)
    _in("status", status)

    tags = [t for t in ([tag] if isinstance(tag, str) else (tag or [])) if t]
    if tags:
        # Matching on the tag NAME, not its id: the filter bar round-trips
        # names, and a name survives a merge (rename_tag) while an id doesn't.
        clauses.append(
            "id IN (SELECT it.episode_id FROM knowledge_item_tags it "
            "JOIN knowledge_tags t ON t.id = it.tag_id "
            f"WHERE t.name COLLATE NOCASE IN ({','.join('?' * len(tags))}))")
        params.extend(tags)

    if list_id is not None:
        clauses.append("id IN (SELECT episode_id FROM knowledge_list_items WHERE list_id = ?)")
        params.append(list_id)

    if since:
        # Bounded on the same date the current sort uses, so "last 7 days"
        # means the same thing the list is ordered by rather than silently
        # switching to a different clock.
        date_col = {
            "published_at": "COALESCE(published_at, created_at)",
            "created_at":   "created_at",
        }.get(sort or "", "COALESCE(processed_at, created_at)")
        clauses.append(f"{date_col} >= ?")
        params.append(since)

    if not include_archived:
        clauses.append("archived_at IS NULL")

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY " + _order_by(sort, order) + " LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    episodes = [_hydrate(r) for r in rows]
    # Tags and list membership for the whole page in two queries, not two per
    # row — this list is pulled 1000 rows at a time by the material view.
    ids = [e["id"] for e in episodes]
    tags = tags_for_items(ids)
    lists = list_membership(ids)
    for e in episodes:
        e["tags"] = tags.get(e["id"], [])
        e["list_ids"] = lists.get(e["id"], [])
    return episodes


def list_recent_error_episodes(max_age_days: int = 7) -> list[dict]:
    """Episodes with status='error' created within the last `max_age_days`
    days — run_check's automatic retry window (#491). Older failures are left
    alone so a permanently-broken video can't be retried (and billed) forever.
    created_at is stored via datetime('now') (UTC), so the comparison uses
    the same clock."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, video_id, title, audio_url, duration_seconds, kind FROM podcast_episodes
           WHERE status = 'error' AND created_at >= datetime('now', ?)
           ORDER BY id""",
        (f"-{int(max_age_days)} days",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def word_zh_exists(words: list[str]) -> set[str]:
    """Given candidate HSK words, return the subset already present in
    entries.word_zh — used to filter the AI's word list down to genuinely
    new vocabulary before it's shown to Daniel."""
    if not words:
        return set()
    conn = get_db()
    placeholders = ",".join("?" for _ in words)
    rows = conn.execute(
        f"SELECT word_zh FROM entries WHERE word_zh IN ({placeholders})", words
    ).fetchall()
    conn.close()
    return {r["word_zh"] for r in rows}


def known_words_exists(words: list[str], lang: str = "zh") -> set[str]:
    """Which of these words Daniel has marked as already known (#710).

    The counterpart of word_zh_exists: a word can be known without ever having
    been studied here. zh_annotate unions the two — see its _known_words().
    `lang` defaults to 'zh' so existing Chinese call sites are unaffected
    (#803: known_words is now keyed per language, same reasoning as
    entries.word_zh — French/Spanish share surface forms)."""
    if not words:
        return set()
    conn = get_db()
    placeholders = ",".join("?" for _ in words)
    rows = conn.execute(
        f"SELECT word_zh FROM known_words WHERE lang = ? AND word_zh IN ({placeholders})",
        (lang, *words),
    ).fetchall()
    conn.close()
    return {r["word_zh"] for r in rows}


def add_known_word(word_zh: str, lang: str = "zh") -> None:
    """Mark a word as already known (#710). Idempotent: marking a word twice
    is exactly what happens when Daniel meets it in a second episode."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO known_words (word_zh, lang) VALUES (?, ?)",
        (word_zh, lang),
    )
    conn.commit()
    conn.close()


def remove_known_word(word_zh: str, lang: str = "zh") -> bool:
    """Undo add_known_word. Returns whether the word was actually on the list
    — the caller reports a miss rather than pretending it removed something."""
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM known_words WHERE word_zh = ? AND lang = ?", (word_zh, lang)
    )
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


def list_known_words(lang: str | None = None) -> list[dict]:
    """Words marked as known, newest first. `lang=None` (default) returns all
    languages, matching the pre-#803 behavior of this endpoint."""
    conn = get_db()
    if lang is not None:
        rows = conn.execute(
            "SELECT word_zh, lang, added_at FROM known_words "
            "WHERE lang = ? ORDER BY added_at DESC, word_zh",
            (lang,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT word_zh, lang, added_at FROM known_words ORDER BY added_at DESC, word_zh"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-language knowledge-base renditions (#804)
# ---------------------------------------------------------------------------
# One episode, one AI-generated summary_de — every other language's reading
# view is a translated-and-annotated derivative, generated lazily and cached
# here so repeat views don't re-translate. Chinese has no rendition row: its
# summary_zh is AI-native and annotated by zh_annotate.py already.

def get_knowledge_rendition(episode_id: int, lang: str) -> dict | None:
    """The cached rendition for (episode_id, lang), or None if it hasn't been
    generated yet."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM knowledge_renditions WHERE episode_id = ? AND lang = ?",
        (episode_id, lang),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["new_words"] = json.loads(d["new_words"]) if d.get("new_words") else []
    return d


def save_knowledge_rendition(episode_id: int, lang: str, summary: str,
                             new_words: list[dict] | None = None) -> None:
    """Store (or overwrite) the rendition for (episode_id, lang). Only ever
    called after a translation succeeds — see knowledge/rendition.py; a
    failed translation must never reach here (#804: no half-finished or
    untranslated text may be stored pretending to be a lang's summary)."""
    conn = get_db()
    conn.execute(
        """INSERT INTO knowledge_renditions (episode_id, lang, summary, new_words)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(episode_id, lang) DO UPDATE SET
               summary = excluded.summary,
               new_words = excluded.new_words,
               created_at = datetime('now')""",
        (episode_id, lang, summary, json.dumps(new_words or [], ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    # #939: the per-language reading version is searchable too — Daniel is just
    # as likely to remember a word from the French rendition as from the German
    # original.
    from .search import reindex_episode
    reindex_episode(episode_id)


def delete_knowledge_renditions(episode_id: int) -> None:
    """Wipe every cached rendition for an episode (#804) — called whenever
    summary_de is regenerated, so a stale French/Spanish translation of the
    OLD summary can't keep being served next to a freshly regenerated German
    one. The next detail-page view for that language regenerates it lazily."""
    conn = get_db()
    conn.execute("DELETE FROM knowledge_renditions WHERE episode_id = ?", (episode_id,))
    conn.commit()
    conn.close()
    from .search import reindex_episode
    reindex_episode(episode_id)

"""Full-text search over the knowledge base (#939, umbrella #934).

Daniel's searchable text is spread over three places: the source material
(podcast_episodes.transcript_zh / transcript_de), the AI summaries
(summary_de / summary_zh) and the per-language reading versions
(knowledge_renditions.summary, #804). A search that only covered titles would
miss the thing he actually remembers — a sentence from the middle of a
transcript, or a word from the French rendition.

Design notes worth keeping:

  * ONE FTS5 row per (episode, field, lang), not one per episode. That is what
    lets a hit say WHERE it matched, which is most of what makes a result list
    readable.
  * CJK is indexed character by character, and a CJK query becomes a PHRASE of
    those characters. unicode61 does not segment Chinese, so without this a
    whole paragraph collapses into one token and nothing is findable. Using
    jieba instead was the obvious alternative and is worse: the segmenter would
    have to make exactly the same choice for the query as it did for the
    document ("生态" vs "生 态学"), and when it doesn't, the match silently
    disappears. Single characters + phrase search cannot disagree with itself.
  * Indexing is explicit (reindex_episode), not trigger-driven. Triggers living
    in schema.sql are invisible to everyone who later edits these columns, and
    two of the sources need JSON parsing that SQL can't do anyway.
"""
import json
import re

from .core import get_db

# The character class treated as "Chinese" for indexing. Includes CJK
# punctuation and fullwidth forms, not just the ideographs: they have to be
# spread and collapsed by the SAME rule, or a snippet comes back as "总结 。".
_CJK = "\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
_CJK_RE = re.compile(f"[{_CJK}]")

# snippet() wraps matches in these two sentinels. They can't occur in real text,
# so the frontend can find them and wrap the match in <mark> without ever
# parsing HTML out of material an AI wrote.
MATCH_START, MATCH_END = "\x02", "\x03"


def _spread_cjk(text: str) -> str:
    """Put a space around every CJK character so unicode61 makes each one its
    own token. Latin text is untouched — it already tokenizes correctly."""
    return _CJK_RE.sub(lambda m: f" {m.group(0)} ", text or "")


# Undo _spread_cjk for display: the index stores "生 态" so unicode61 tokenizes
# it, but a snippet has to read as the text Daniel wrote. Only whitespace
# BETWEEN two CJK characters goes; the space between a Chinese and a Latin word
# stays. The match sentinels count as transparent — they sit exactly where the
# injected spaces are.
_CJK_GAP_RE = re.compile(f"(?<=[{_CJK}\x02\x03])[ \t]+(?=[{_CJK}\x02\x03])")


def _collapse_cjk(text: str) -> str:
    # Twice: single-character gaps overlap, so one pass leaves every other one
    # ("生 态 环" -> "生态 环").
    text = _CJK_GAP_RE.sub("", _CJK_GAP_RE.sub("", text or ""))
    # _strip_tags turned every tag into a space, so Latin snippets come back
    # with double gaps.
    return re.sub(r"[ \t]{2,}", " ", text)


def _strip_tags(html: str) -> str:
    """Summaries and renditions are HTML (<p>/<b>). Index the text, not the
    markup — otherwise a search for "b" matches every summary in the library."""
    return re.sub(r"<[^>]+>", " ", html or "")


def build_fts_query(query: str) -> str | None:
    """Turn what Daniel typed into an FTS5 MATCH expression, or None if there
    is nothing to search for.

    Every token becomes a quoted phrase prefix, and tokens are ANDed: typing
    two words means "both of these", which is what every search box on earth
    does. Quoting is what makes this safe — FTS5 syntax characters (AND, OR,
    NEAR, ^, *, ") inside a quoted phrase are literal text, so a query can't
    turn into an operator soup or an error.
    """
    tokens = [t for t in (query or "").split() if t.strip()]
    if not tokens:
        return None
    parts = []
    for token in tokens:
        phrase = _spread_cjk(token).strip()
        if not phrase:
            continue
        parts.append('"' + phrase.replace('"', '""') + '"*')
    return " AND ".join(parts) or None


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _episode_documents(conn, episode_id: int) -> list[tuple]:
    """(field, lang, body) rows to index for one episode."""
    row = conn.execute(
        """SELECT title, title_en, author, transcript_zh, transcript_de,
                  summary_de, summary_zh
           FROM podcast_episodes WHERE id = ?""", (episode_id,)).fetchone()
    if row is None:
        return []

    docs: list[tuple] = []

    def add(field, lang, text):
        text = (text or "").strip()
        if text:
            docs.append((field, lang, _spread_cjk(text)))

    add("title", "", " ".join(filter(None, [row["title"], row["title_en"], row["author"]])))
    add("transcript", "", row["transcript_zh"])
    add("summary_de", "de", _strip_tags(row["summary_de"]))
    add("summary_zh", "zh", _strip_tags(row["summary_zh"]))

    # transcript_de is a JSON array of {"zh","de"} pairs (#553/#772). The zh
    # side is already covered by transcript_zh, so only the translated side is
    # added — indexing both would double the largest document in the table.
    try:
        pairs = json.loads(row["transcript_de"]) if row["transcript_de"] else []
    except (ValueError, TypeError):
        pairs = []
    if isinstance(pairs, list):
        translated = " ".join(p.get("de", "") for p in pairs if isinstance(p, dict))
        add("transcript_de", "de", translated)

    for r in conn.execute(
            "SELECT lang, summary FROM knowledge_renditions WHERE episode_id = ?",
            (episode_id,)).fetchall():
        add("rendition", r["lang"], _strip_tags(r["summary"]))

    return docs


def reindex_episode(episode_id: int) -> int:
    """Rebuild one episode's search index. Returns the number of documents
    written. Call after anything that changes its text: a finished summary, a
    generated rendition, a hand edit (#937)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM knowledge_fts WHERE episode_id = ?", (episode_id,))
        docs = _episode_documents(conn, episode_id)
        conn.executemany(
            "INSERT INTO knowledge_fts (episode_id, field, lang, body) VALUES (?, ?, ?, ?)",
            [(episode_id, f, lang, body) for f, lang, body in docs])
        conn.commit()
        return len(docs)
    finally:
        conn.close()


def delete_episode_index(episode_id: int) -> None:
    """FTS5 virtual tables have no foreign keys, so a deleted episode's rows
    would otherwise linger and keep turning up as results for material that no
    longer exists."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM knowledge_fts WHERE episode_id = ?", (episode_id,))
        conn.commit()
    finally:
        conn.close()


def reindex_all() -> int:
    """Rebuild the whole index. Returns the number of episodes indexed."""
    conn = get_db()
    ids = [r["id"] for r in conn.execute("SELECT id FROM podcast_episodes").fetchall()]
    conn.close()
    for episode_id in ids:
        reindex_episode(episode_id)
    return len(ids)


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

_FIELD_LABEL = {
    "title": "Title",
    "transcript": "Transcript",
    "transcript_de": "Transcript (de)",
    "summary_de": "Summary (de)",
    "summary_zh": "Summary (zh)",
    "rendition": "Reading version",
}


def search_knowledge(query: str, limit: int = 50) -> list[dict]:
    """Search everything. Returns one entry per EPISODE (not per matching
    field), best match first, each carrying the fields it matched in and one
    snippet.

    Merging per episode is deliberate: a word that appears in the transcript
    usually also appears in the summary and in two renditions, and four rows
    for the same article would push everything else off the screen.

    The snippet comes from FTS5's own snippet(), with match markers as
    \\x02/\\x03 sentinels — characters that can't occur in the text, so the
    frontend can find them and wrap them in <mark> without ever parsing HTML
    out of material the AI wrote.
    """
    match = build_fts_query(query)
    if not match:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT f.episode_id, f.field, f.lang,
                      snippet(knowledge_fts, 3, char(2), char(3), '…', 12) AS snippet,
                      bm25(knowledge_fts) AS rank
               FROM knowledge_fts f
               WHERE knowledge_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (match, limit * 6),
        ).fetchall()
    except Exception:
        # A MATCH that SQLite rejects means the query builder let something
        # through. No results is the honest answer; an exception here would
        # turn a typo into a 500.
        conn.close()
        return []

    merged: dict[int, dict] = {}
    for r in rows:
        entry = merged.get(r["episode_id"])
        if entry is None:
            if len(merged) >= limit:
                continue
            entry = {
                "episode_id": r["episode_id"],
                "fields": [],
                # The best-ranked field's snippet wins — that's the one whose
                # text actually explains the hit.
                "snippet": _collapse_cjk(r["snippet"] or "").strip(),
                "field": r["field"],
                "lang": r["lang"] or "",
                "rank": r["rank"],
            }
            merged[r["episode_id"]] = entry
        label = _FIELD_LABEL.get(r["field"], r["field"])
        if r["field"] == "rendition" and r["lang"]:
            label = f"{label} ({r['lang']})"
        if label not in entry["fields"]:
            entry["fields"].append(label)

    ids = list(merged)
    if not ids:
        conn.close()
        return []
    placeholders = ",".join("?" * len(ids))
    meta = conn.execute(
        f"""SELECT id, title, title_en, author, kind, platform, status,
                   processed_at, published_at, created_at
            FROM podcast_episodes WHERE id IN ({placeholders})""", ids).fetchall()
    conn.close()
    by_id = {r["id"]: dict(r) for r in meta}

    out = []
    for episode_id, entry in merged.items():
        row = by_id.get(episode_id)
        if row is None:
            continue        # deleted between the two queries
        out.append({**entry, **{k: v for k, v in row.items() if k != "id"}})
    out.sort(key=lambda e: e["rank"])
    return out

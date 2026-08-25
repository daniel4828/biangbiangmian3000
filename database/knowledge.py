"""Tags and user-defined lists over knowledge-base material (#935, umbrella
#934).

Everything here hangs off podcast_episodes rows by id. It lives in its own
module rather than in database/podcast.py because that file is already 440
lines of episode/feed/config storage — and because tags and lists are about
organizing material, not about crawling it.

Two rules the rest of the app depends on:

  * Tag names are unique case-insensitively (idx_knowledge_tags_name uses
    COLLATE NOCASE). With a shared filter bar, 'Politik' and 'politik' as two
    separate tags is strictly worse than one.
  * knowledge_item_tags.source separates 'user' from 'ai'. The AI tagger
    (#938) may only ever replace its own rows; a tag Daniel typed must
    survive every re-tag. set_item_tags() enforces this — do not write to
    knowledge_item_tags from anywhere else.
"""
import json

from .core import get_db


# Where a piece of material came from (#935). Not the same axis as `kind`
# (podcast/video/article/newsletter): kind is what it IS, platform is where it
# arrived from, and Daniel filters on both. Inferred once at ingest time and
# hand-editable afterwards (#937), so this list is a UI/validation whitelist,
# not a database constraint — an unknown value must never lose a row.
KNOWLEDGE_PLATFORMS = (
    "youtube", "instagram", "podcast", "web", "upload", "paste", "email", "signal",
)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def list_tags() -> list[dict]:
    """All tags with a usage count, most-used first. Feeds the filter bar and
    the tag-management UI, so the count matters: an unused tag is a candidate
    for deletion, a huge one for splitting."""
    conn = get_db()
    rows = conn.execute(
        """SELECT t.id, t.name, t.created_at, COUNT(it.episode_id) AS count
           FROM knowledge_tags t
           LEFT JOIN knowledge_item_tags it ON it.tag_id = t.id
           GROUP BY t.id
           ORDER BY count DESC, t.name COLLATE NOCASE"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_or_create_tag(name: str) -> int:
    """Tag id for `name`, creating the tag on first use. Matching is
    case-insensitive, so an existing 'Politik' is reused when the AI proposes
    'politik' — the stored display form stays whichever came first."""
    name = (name or "").strip()
    if not name:
        raise ValueError("tag name is required")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM knowledge_tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO knowledge_tags (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def rename_tag(tag_id: int, name: str) -> bool:
    """Rename a tag, merging it into an existing one if the new name is
    already taken. Merging is the point, not a side effect: the AI tagger
    will produce near-duplicates ('KI' / 'AI'), and 'rename onto the good one'
    is how Daniel cleans that up. Returns False if `tag_id` doesn't exist."""
    name = (name or "").strip()
    if not name:
        raise ValueError("tag name is required")
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM knowledge_tags WHERE id = ?", (tag_id,)).fetchone():
            return False
        target = conn.execute(
            "SELECT id FROM knowledge_tags WHERE name = ? COLLATE NOCASE AND id != ?",
            (name, tag_id),
        ).fetchone()
        if target:
            # Merge. OR IGNORE skips items that already carry the target tag
            # (the composite primary key would otherwise abort the whole move).
            conn.execute(
                "UPDATE OR IGNORE knowledge_item_tags SET tag_id = ? WHERE tag_id = ?",
                (target["id"], tag_id),
            )
            conn.execute("DELETE FROM knowledge_tags WHERE id = ?", (tag_id,))
        else:
            conn.execute("UPDATE knowledge_tags SET name = ? WHERE id = ?", (name, tag_id))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_tag(tag_id: int) -> bool:
    """Delete a tag and every item's use of it. False if it didn't exist —
    callers turn that into a 404 rather than pretending it worked."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM knowledge_tags WHERE id = ?", (tag_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def tags_for_items(episode_ids) -> dict[int, list[dict]]:
    """{episode_id: [{id, name, source}, ...]} for many episodes in ONE query.

    The material list pulls up to 1000 rows at a time; a per-row lookup here
    would be 1000 round trips for a list that has to feel instant."""
    ids = list(episode_ids)
    if not ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT it.episode_id, t.id, t.name, it.source
            FROM knowledge_item_tags it
            JOIN knowledge_tags t ON t.id = it.tag_id
            WHERE it.episode_id IN ({placeholders})
            ORDER BY t.name COLLATE NOCASE""",
        ids,
    ).fetchall()
    conn.close()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["episode_id"], []).append(
            {"id": r["id"], "name": r["name"], "source": r["source"]})
    return out


def item_tags(episode_id: int) -> list[dict]:
    """Tags on one episode — the single-row form of tags_for_items()."""
    return tags_for_items([episode_id]).get(episode_id, [])


def add_item_tag(episode_id: int, name: str, source: str = "user") -> dict:
    """Attach one tag to one episode, creating the tag if needed. Re-tagging
    with an existing name is a no-op except that it upgrades an 'ai' tag to
    'user' — Daniel typing a tag the AI had already guessed means he owns it
    now, and #938 must stop touching it."""
    _check_source(source)
    tag_id = get_or_create_tag(name)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_item_tags (episode_id, tag_id, source) "
            "VALUES (?, ?, ?)", (episode_id, tag_id, source))
        if source == "user":
            conn.execute(
                "UPDATE knowledge_item_tags SET source = 'user' "
                "WHERE episode_id = ? AND tag_id = ?", (episode_id, tag_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": tag_id, "name": (name or "").strip(), "source": source}


def remove_item_tag(episode_id: int, tag_id: int) -> bool:
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM knowledge_item_tags WHERE episode_id = ? AND tag_id = ?",
            (episode_id, tag_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_item_tags(episode_id: int, names, source: str = "user") -> list[dict]:
    """Replace an episode's tags *of this source* with `names`.

    The source scoping is the whole point. Called with source='ai' (#938) this
    replaces the machine's previous guesses and leaves every hand-typed tag
    alone; called with source='user' (#937, the edit form) it replaces what
    Daniel had set and leaves the AI's suggestions alone. Neither side can
    ever silently delete the other's work.

    Returns the episode's full tag list afterwards."""
    _check_source(source)
    wanted: list[str] = []
    seen = set()
    for n in names or []:
        n = (n or "").strip()
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            wanted.append(n)

    tag_ids = [get_or_create_tag(n) for n in wanted]
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM knowledge_item_tags WHERE episode_id = ? AND source = ?",
            (episode_id, source))
        for tag_id in tag_ids:
            # OR IGNORE: the same tag may already sit on this episode under the
            # *other* source. That row wins — it was not ours to replace.
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_item_tags (episode_id, tag_id, source) "
                "VALUES (?, ?, ?)", (episode_id, tag_id, source))
        conn.commit()
    finally:
        conn.close()
    return item_tags(episode_id)


def _check_source(source: str) -> None:
    if source not in ("user", "ai"):
        raise ValueError(f"invalid tag source: {source!r}")


# ---------------------------------------------------------------------------
# Lists (Read Later & friends, #940)
# ---------------------------------------------------------------------------

def list_lists() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT l.id, l.name, l.icon, l.is_builtin, l.position, l.created_at,
                  COUNT(li.episode_id) AS count
           FROM knowledge_lists l
           LEFT JOIN knowledge_list_items li ON li.list_id = l.id
           GROUP BY l.id
           ORDER BY l.is_builtin DESC, l.position, l.id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_list(list_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM knowledge_lists WHERE id = ?", (list_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_builtin_list() -> dict | None:
    """The Read Later list. Looked up by is_builtin rather than by name so a
    rename doesn't break the swipe gesture that targets it."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM knowledge_lists WHERE is_builtin = 1 ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def create_list(name: str, icon: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("list name is required")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO knowledge_lists (name, icon, position) "
            "VALUES (?, ?, COALESCE((SELECT MAX(position) + 1 FROM knowledge_lists), 1))",
            (name, icon))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_list(list_id: int, **fields) -> bool:
    """Rename / re-icon / reorder a list. The built-in list may be renamed
    (it's just a label) — only deletion is blocked."""
    allowed = {k: v for k, v in fields.items() if k in ("name", "icon", "position")}
    if "name" in allowed:
        allowed["name"] = (allowed["name"] or "").strip()
        if not allowed["name"]:
            raise ValueError("list name is required")
    if not allowed:
        return get_list(list_id) is not None
    conn = get_db()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in allowed)
        cur = conn.execute(
            f"UPDATE knowledge_lists SET {set_clause} WHERE id = ?",
            (*allowed.values(), list_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_list(list_id: int) -> bool:
    """Delete a list and its membership rows. Refuses the built-in Read Later
    list — the swipe gesture (#940) has nowhere to put things without it."""
    lst = get_list(list_id)
    if lst is None:
        return False
    if lst["is_builtin"]:
        raise ValueError("the built-in list cannot be deleted")
    conn = get_db()
    try:
        conn.execute("DELETE FROM knowledge_lists WHERE id = ?", (list_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def add_to_list(list_id: int, episode_id: int) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_list_items (list_id, episode_id) VALUES (?, ?)",
            (list_id, episode_id))
        conn.commit()
    finally:
        conn.close()


def remove_from_list(list_id: int, episode_id: int) -> bool:
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM knowledge_list_items WHERE list_id = ? AND episode_id = ?",
            (list_id, episode_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_membership(episode_ids) -> dict[int, list[int]]:
    """{episode_id: [list_id, ...]} for many episodes in one query — same
    reason as tags_for_items()."""
    ids = list(episode_ids)
    if not ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT episode_id, list_id FROM knowledge_list_items "
        f"WHERE episode_id IN ({placeholders})", ids).fetchall()
    conn.close()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["episode_id"], []).append(r["list_id"])
    return out


def list_episode_ids(list_id: int) -> list[int]:
    conn = get_db()
    rows = conn.execute(
        "SELECT episode_id FROM knowledge_list_items WHERE list_id = ? ORDER BY added_at DESC",
        (list_id,)).fetchall()
    conn.close()
    return [r["episode_id"] for r in rows]


# ---------------------------------------------------------------------------
# Filter facets (#936)
# ---------------------------------------------------------------------------

def knowledge_facets() -> dict:
    """Everything the material list's filter bar needs to render its dropdowns,
    in one request: which kinds/platforms/authors/statuses actually occur, plus
    the tag and list catalogs.

    Derived from the data rather than hard-coded, so a platform or author that
    exists only in Daniel's library still shows up — and one that doesn't
    exist never offers an option that can only return an empty list.

    One call, not one per dropdown: the filter bar renders as a unit.
    """
    conn = get_db()

    def _distinct(column: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT {column} AS value, COUNT(*) AS count FROM podcast_episodes "
            f"WHERE {column} IS NOT NULL AND {column} != '' "
            f"GROUP BY {column} ORDER BY count DESC, value COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    facets = {
        "kinds": _distinct("kind"),
        "platforms": _distinct("platform"),
        "authors": _distinct("author"),
        "statuses": _distinct("status"),
    }
    feeds = conn.execute(
        """SELECT f.id, f.url, f.title, COUNT(e.id) AS count
           FROM podcast_feeds f
           LEFT JOIN podcast_episodes e ON e.channel_id = f.url
           GROUP BY f.id ORDER BY f.title COLLATE NOCASE, f.id"""
    ).fetchall()
    facets["feeds"] = [dict(r) for r in feeds]
    archived = conn.execute(
        "SELECT COUNT(*) AS c FROM podcast_episodes WHERE archived_at IS NOT NULL").fetchone()
    facets["archived_count"] = archived["c"]
    conn.close()

    facets["tags"] = list_tags()
    facets["lists"] = list_lists()
    return facets


# ---------------------------------------------------------------------------
# Hand-edited metadata (#937)
# ---------------------------------------------------------------------------

# Columns Daniel may edit by hand. A whitelist because these names go into an
# UPDATE statement, and because "editable" is a deliberate, small set: the
# transcript, the summary and the status are results, not metadata.
EDITABLE_EPISODE_FIELDS = ("title", "title_en", "author", "platform",
                           "published_at", "youtube_url")


def manual_fields(episode_id: int) -> set[str]:
    """Which columns of this episode Daniel edited by hand (#937).

    Every AI path — the title suggestion in podcast.summarize(), the metadata
    extraction in knowledge/ingest.py, the auto tagger (#938) — must consult
    this before writing: he was looking at the source, the model is guessing.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT manual_fields FROM podcast_episodes WHERE id = ?", (episode_id,)).fetchone()
    conn.close()
    if not row or not row["manual_fields"]:
        return set()
    try:
        return set(json.loads(row["manual_fields"]) or [])
    except (ValueError, TypeError):
        # A corrupt marker must not make the episode uneditable; the worst
        # case of treating it as empty is that an AI path overwrites one field.
        return set()


def is_manual(episode_id: int, field: str) -> bool:
    return field in manual_fields(episode_id)


def update_episode_metadata(episode_id: int, fields: dict, *, source: str = "user") -> dict | None:
    """Update an episode's editable metadata. Returns the updated row, or None
    if the episode doesn't exist (callers turn that into a 404).

    `fields` may contain any of EDITABLE_EPISODE_FIELDS; anything else is
    ignored rather than raising — an unknown key is a frontend bug, not a
    reason to lose the edit that came with it. An empty string clears the
    column; a key that isn't present is left untouched.

    With source='user' (the default) every column written is recorded in
    manual_fields, which is what keeps later AI passes off it. source='ai'
    writes the same columns without claiming them — and skips any column
    Daniel has already claimed.
    """
    if source not in ("user", "ai"):
        raise ValueError(f"invalid source: {source!r}")

    claimed = manual_fields(episode_id)
    updates = {}
    for key in EDITABLE_EPISODE_FIELDS:
        if key not in fields:
            continue
        if source == "ai" and key in claimed:
            continue
        value = fields[key]
        value = value.strip() if isinstance(value, str) else value
        updates[key] = value or None

    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM podcast_episodes WHERE id = ?", (episode_id,)).fetchone():
            return None
        if updates:
            if source == "user":
                claimed = claimed | set(updates)
                updates["manual_fields"] = json.dumps(sorted(claimed))
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE podcast_episodes SET {set_clause} WHERE id = ?",
                         (*updates.values(), episode_id))
            conn.commit()
    finally:
        conn.close()
    # #939: title/author are indexed, so a hand edit has to update the search
    # index too — otherwise searching for the title Daniel just typed fails.
    from .search import reindex_episode
    reindex_episode(episode_id)
    from .podcast import get_episode
    return get_episode(episode_id)


def set_archived(episode_id: int, archived: bool = True) -> None:
    """Archive / un-archive one item (#940). A column on podcast_episodes
    rather than a fifth table: "am I done with this one" is a property of the
    item, and a join would sit in the way of every single list query."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE podcast_episodes SET archived_at = %s WHERE id = ?"
            % ("datetime('now')" if archived else "NULL"),
            (episode_id,))
        conn.commit()
    finally:
        conn.close()

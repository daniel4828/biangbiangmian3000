import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from .core import get_db, anki_today, get_day_cutoff_hour
from .presets import get_preset_for_deck
from .decks import get_deck, get_locked_deck_ids
from .entries import surface_forms

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def insert_card(word_id: int, category: str, deck_id: int,
                state: str = "new", due: str | None = None) -> int:
    conn = get_db()
    if due is not None:
        conn.execute(
            """INSERT OR IGNORE INTO cards (word_id, deck_id, category, state, due)
               VALUES (?, ?, ?, ?, ?)""",
            (word_id, deck_id, category, state, due),
        )
    else:
        conn.execute(
            """INSERT OR IGNORE INTO cards (word_id, deck_id, category, state)
               VALUES (?, ?, ?, ?)""",
            (word_id, deck_id, category, state),
        )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM cards WHERE word_id = ? AND category = ?",
        (word_id, category),
    ).fetchone()
    conn.close()
    return row["id"]


def reset_word_to_new(word_id: int, target_deck_ids: dict, due: str,
                      from_deck_id: int | None = None) -> int:
    """Move a word's cards into the per-category leaf decks as fresh 'new' cards
    due on `due`, clearing all scheduling state.

    target_deck_ids: {"listening": id, "reading": id, "creating": id} — each card is
    routed to the leaf deck matching its category, so due counts and review queues
    (keyed by deck_id, category) pick it up.

    from_deck_id restricts the move to cards currently in that one deck; None
    takes the word's cards wherever they are. Note what "clearing" costs when
    the card was actually being studied: stability/difficulty/interval/lapses
    are the word's entire FSRS memory model and there is no undo. Callers must
    only pass from_deck_id=None when the user asked for exactly that (#675).

    Returns the number of cards reset.
    """
    conn = get_db()
    count = 0
    for category, target_deck_id in target_deck_ids.items():
        sql = """UPDATE cards
                 SET deck_id=?, state='new', due=?, step_index=0, interval=0,
                     ease=2.5, repetitions=0, lapses=0, stability=NULL, difficulty=NULL,
                     last_review=NULL, learning_again_count=0, is_leech=0, leeched_at=NULL,
                     probation=0, buried_until=NULL, pre_suspend_state=NULL
                 WHERE word_id=? AND category=? AND deleted_at IS NULL"""
        params = [target_deck_id, due, word_id, category]
        if from_deck_id is not None:
            sql += " AND deck_id=?"
            params.append(from_deck_id)
        count += conn.execute(sql, params).rowcount
    conn.commit()
    conn.close()
    return count


def stage_word_in_saved(word_id: int, saved_deck_id: int) -> int:
    """Park all of a word's cards in the Saved deck as suspended — the "add it
    to my list, I'm not studying it yet" case (#677).

    The inverse of promote_saved_word. Scheduling fields are deliberately left
    alone: state='suspended' is what keeps a card out of every queue (`due` is
    NOT NULL and stays whatever it was), and promote_saved_word resets them
    anyway if the word is ever activated — so parking a word costs nothing that
    promoting wouldn't cost later.

    Returns the number of cards staged.
    """
    conn = get_db()
    cur = conn.execute(
        """UPDATE cards SET deck_id=?, state='suspended', buried_until=NULL
           WHERE word_id=? AND deleted_at IS NULL""",
        (saved_deck_id, word_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def promote_saved_word(word_id: int, target_deck_ids: dict,
                       saved_deck_id: int, due: str) -> int:
    """Promote a word staged in the Saved deck — reset_word_to_new limited to
    cards sitting in Saved (which are suspended and carry no progress)."""
    return reset_word_to_new(word_id, target_deck_ids, due, from_deck_id=saved_deck_id)


def set_card_note(card_id: int, note: str | None) -> None:
    """Store (or clear) the free-text 'note for next time' on a card.

    Empty/blank strings are stored as NULL so the card shows no note next time.
    """
    note = (note or "").strip() or None
    conn = get_db()
    conn.execute("UPDATE cards SET next_note=? WHERE id=?", (note, card_id))
    conn.commit()
    conn.close()


def move_card_to_deck(card_id: int, deck_id: int) -> bool:
    conn = get_db()
    cur = conn.execute(
        "UPDATE cards SET deck_id=? WHERE id=? AND deleted_at IS NULL",
        (deck_id, card_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def reset_card_progress(word_id: int) -> int:
    """Reset all cards for a word to 'new' state, clearing all SRS progress.

    Returns the number of cards reset.
    """
    today = anki_today()
    conn = get_db()
    cur = conn.execute(
        """UPDATE cards
           SET state='new', due=?, interval=0, ease=2.5,
               step_index=0, lapses=0, buried_until=NULL, probation=0,
               stability=NULL, difficulty=NULL, last_review=NULL
           WHERE word_id=? AND deleted_at IS NULL AND state != 'suspended'""",
        (today, word_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def move_cards_to_deck(word_id: int, target_deck_ids: dict,
                       categories: list[str] | None = None) -> int:
    """Move cards for a word to new deck IDs.

    target_deck_ids: {"reading": deck_id, "listening": deck_id, "creating": deck_id}
    categories: if given, only move cards in those categories; else move all.

    Returns the number of cards moved.
    """
    conn = get_db()
    moved = 0
    cats = categories if categories else list(target_deck_ids.keys())
    for cat in cats:
        deck_id = target_deck_ids.get(cat)
        if deck_id is None:
            continue
        cur = conn.execute(
            """UPDATE cards SET deck_id=?,
               state=CASE WHEN state='suspended' THEN COALESCE(pre_suspend_state,'new') ELSE state END,
               pre_suspend_state=CASE WHEN state='suspended' THEN NULL ELSE pre_suspend_state END
               WHERE word_id=? AND category=? AND deleted_at IS NULL""",
            (deck_id, word_id, cat),
        )
        moved += cur.rowcount
    conn.commit()
    conn.close()
    return moved


def get_card(card_id: int) -> dict | None:
    """Joined with word, deck, and preset — everything srs.py needs."""
    conn = get_db()
    row = conn.execute(
        """WITH RECURSIVE ancestors(id, name, parent_id, depth) AS (
               SELECT id, name, parent_id, 0 FROM decks WHERE id = (
                   SELECT deck_id FROM cards WHERE id = ?
               )
               UNION ALL
               SELECT d.id, d.name, d.parent_id, a.depth + 1
               FROM decks d JOIN ancestors a ON d.id = a.parent_id
           )
           SELECT c.*,
                  w.word_zh, w.pinyin, w.definition, w.pos, w.hsk_level,
                  w.traditional, w.definition_zh, w.note_type, w.notes, w.definition_de, w.definition_fr, w.register,
                  w.lang AS word_lang,
                  d.name AS deck_name,
                  CASE WHEN d.category IS NOT NULL THEN
                    (SELECT group_concat(name, ' › ')
                     FROM (SELECT name FROM ancestors WHERE depth > 0 ORDER BY depth DESC))
                    || ' · ' ||
                    CASE c.category
                      WHEN 'listening' THEN 'Listening'
                      WHEN 'reading'   THEN 'Reading'
                      WHEN 'creating'  THEN 'Creating'
                      ELSE c.category
                    END
                  ELSE
                    (SELECT group_concat(name, ' › ')
                     FROM (SELECT name FROM ancestors ORDER BY depth DESC))
                  END AS deck_path,
                  p.id AS preset_id,
                  p.learning_steps, p.graduating_interval, p.easy_interval,
                  p.relearning_steps, p.minimum_interval, p.learned_interval,
                  p.leech_threshold, p.learning_leech_threshold, p.enable_learning_leech, p.leech_action,
                  p.desired_retention, p.maximum_interval, p.fsrs_weights, p.enable_fsrs,
                  p.learning_hard_1d, p.learning_hard_days,
                  p.new_per_day, p.reviews_per_day
           FROM cards c
           JOIN entries w ON w.id = c.word_id
           JOIN decks d ON d.id = c.deck_id
           JOIN deck_presets p ON p.id = d.preset_id
           WHERE c.id = ?""",
        (card_id, card_id),
    ).fetchone()
    if not row:
        conn.close()
        return None
    card = dict(row)
    # Apply category-level scheduling overrides on top of the preset defaults
    category = card.get("category")
    preset_id = card.get("preset_id")
    if category and preset_id:
        override = conn.execute(
            "SELECT * FROM preset_category_overrides WHERE preset_id = ? AND category = ?",
            (preset_id, category),
        ).fetchone()
        if override:
            for key, val in dict(override).items():
                if key not in ("id", "preset_id", "category") and val is not None:
                    card[key] = val
    conn.close()
    _attach_word_forms(card)
    return card


def _attach_word_forms(card: dict) -> None:
    """Attach card["word_forms"] — every conjugated/inflected surface form of
    this card's word (issue #903), for the review UI's cloze-blank matcher.

    Chinese words never change form, and the frontend's existing plain
    indexOf() matching is already exact for them, so this deliberately skips
    zh cards rather than adding a field every caller has to ignore.
    """
    if card.get("word_lang", "zh") == "zh":
        return
    card["word_forms"] = surface_forms(card.get("word_id"), card.get("word_zh") or "", card["word_lang"])


def _count_new_introduced_today_multi(conn, deck_ids: list[int], category: str, today: str) -> int:
    """Batched: one grouped query instead of one query per deck (issue #513)."""
    if not deck_ids:
        return 0
    ph = ",".join("?" * len(deck_ids))
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT c.id) FROM cards c
            WHERE c.deck_id IN ({ph}) AND c.category = ?
              AND EXISTS (
                SELECT 1 FROM review_log rl
                WHERE rl.card_id = c.id AND date(rl.reviewed_at) = ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM review_log rl
                WHERE rl.card_id = c.id AND date(rl.reviewed_at) < ?
              )""",
        deck_ids + [category, today, today],
    ).fetchone()
    return row[0]


def _count_new_introduced_today(conn, deck_id: int, category: str, today: str) -> int:
    """Cards whose very first review log entry is today (introduced as new today)."""
    return conn.execute(
        """SELECT COUNT(DISTINCT c.id) FROM cards c
           WHERE c.deck_id = ? AND c.category = ?
             AND EXISTS (
               SELECT 1 FROM review_log rl
               WHERE rl.card_id = c.id AND date(rl.reviewed_at) = ?
             )
             AND NOT EXISTS (
               SELECT 1 FROM review_log rl
               WHERE rl.card_id = c.id AND date(rl.reviewed_at) < ?
             )""",
        (deck_id, category, today, today),
    ).fetchone()[0]


def _get_virtually_buried_word_ids(
    word_ids: set[int], category: str, conn, today: str, now: str
) -> set[int]:
    """Return the subset of word_ids suppressed by a higher-priority sibling card due today.

    Each word is "owned" by the due card with the best combined rank:
      state rank  : learning/relearn=0, review=1, new=2
      category rank: listening=0, reading=1, creating=2
    A card is suppressed if a sibling with a strictly lower combined rank exists and is due.
    """
    if not word_ids:
        return set()
    placeholders = ",".join("?" * len(word_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT c_mine.word_id
            FROM cards c_mine
            JOIN cards c_sib ON c_sib.word_id = c_mine.word_id AND c_sib.id != c_mine.id
            WHERE c_mine.word_id IN ({placeholders})
              AND c_mine.category = ?
              AND c_mine.deleted_at IS NULL
              AND c_sib.category != ?
              AND c_sib.deleted_at IS NULL
              AND c_sib.state != 'suspended'
              AND (c_sib.buried_until IS NULL OR c_sib.buried_until < ?)
              AND (
                (c_sib.state IN ('learning', 'relearn') AND {_learning_due_now_sql('c_sib.')})
                OR (c_sib.state IN ('review', 'new') AND c_sib.due <= ?)
              )
              AND (
                CASE c_sib.state
                  WHEN 'learning' THEN 0 WHEN 'relearn' THEN 0 WHEN 'review' THEN 1 ELSE 2
                END <
                CASE c_mine.state
                  WHEN 'learning' THEN 0 WHEN 'relearn' THEN 0 WHEN 'review' THEN 1 ELSE 2
                END
                OR (
                  CASE c_sib.state
                    WHEN 'learning' THEN 0 WHEN 'relearn' THEN 0 WHEN 'review' THEN 1 ELSE 2
                  END =
                  CASE c_mine.state
                    WHEN 'learning' THEN 0 WHEN 'relearn' THEN 0 WHEN 'review' THEN 1 ELSE 2
                  END
                  AND
                  CASE c_sib.category WHEN 'listening' THEN 0 WHEN 'reading' THEN 1 ELSE 2 END <
                  CASE c_mine.category WHEN 'listening' THEN 0 WHEN 'reading' THEN 1 ELSE 2 END
                )
              )""",
        (*word_ids, category, category, today, now, today, today),
    ).fetchall()
    return {r["word_id"] for r in rows}


def resolve_bury_flags(preset: dict) -> tuple[bool, bool, bool]:
    """Return (bury_new, bury_review, bury_learning) based on bury_quick_mode."""
    mode = preset.get("bury_quick_mode", "all")
    if mode == "all":
        return True, True, True
    if mode == "none":
        return False, False, False
    # custom: use the individual fields
    return (
        bool(preset.get("bury_new_siblings", 0)),
        bool(preset.get("bury_review_siblings", 0)),
        bool(preset.get("bury_interday_siblings", 0)),
    )


def _still_learning(card: dict, learned_interval: int) -> bool:
    """True if a card is not yet "learned" — either in learning/relearn, or a
    review card whose interval hasn't reached the learned_interval threshold.
    Such cards queue with the learning group and are shown before learned cards.
    """
    if card["state"] in ("learning", "relearn"):
        return True
    return card["state"] == "review" and (card.get("interval") or 0) < learned_interval


def story_sort_key(card: dict, story_pos: dict) -> tuple[int, int]:
    """Where a due card sits relative to today's story (issue #732).

    Every caller that orders a review queue by story position used to write
    `story_pos.get(word_id, <some max>)`, which drops cards missing from the
    story behind *everything* — including new cards.  A story only covers the
    words that were due when it was generated, so re-generating it after a
    partial review session leaves the morning's leftovers (especially the ones
    rated Again) outside it, and on a day with many new cards they were never
    reached.  Deck badges still counted them, so they looked lost.

    Three groups instead:
      0 — due cards outside the story that are NOT new: real debt with no
          sentence to read, and the learning-first order that applies when no
          story exists must keep applying when one does.
      1 — cards in the story, in narrative order.
      2 — new cards outside the story (added or promoted after generation).
    """
    pos = story_pos.get(card.get("word_id"))
    if pos is not None:
        return (1, pos)
    return (2, 0) if card.get("state") == "new" else (0, 0)


def _interleave_cards(base: list, inserts: list) -> list:
    """Distribute inserts evenly across the full combined length.

    Uses Bresenham-style spacing so inserts are spread uniformly from
    start to end, regardless of whether base or inserts is larger.
    """
    if not inserts:
        return base
    if not base:
        return inserts
    total = len(base) + len(inserts)
    result = []
    bi = ii = 0
    # For each of the `total` slots, decide whether to place a base or insert
    # card by tracking accumulated "debt" for insert cards.
    # insert cards get slot k if: round(k * len(inserts) / total) > ii
    for k in range(total):
        next_insert_count = round((k + 1) * len(inserts) / total)
        if next_insert_count > ii and ii < len(inserts):
            result.append(inserts[ii])
            ii += 1
        else:
            result.append(base[bi])
            bi += 1
    return result


def get_due_cards(deck_id: int, category: str, *, sibling_suppression: bool = False,
                   _preset: dict | None = None, _new_done_today: int | None = None) -> list[dict]:
    """All due cards for a category, ordered per preset display-order settings.

    _preset / _new_done_today let callers that already batch-fetched these values
    for many decks at once (get_due_cards_multi, issue #513) skip the per-deck
    preset/new-count queries this function would otherwise run on every call.
    """
    import random
    from itertools import groupby

    # Future-dated daily decks are locked until their date — no card is reviewable.
    if deck_id in get_locked_deck_ids():
        return []

    today = anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()

    preset = _preset if _preset is not None else get_preset_for_deck(deck_id, category)
    new_limit = preset["new_per_day"]
    new_done_today = (_new_done_today if _new_done_today is not None
                       else _count_new_introduced_today(conn, deck_id, category, today))
    new_remaining = max(0, new_limit - new_done_today)

    rows = conn.execute(
        f"""SELECT c.*, w.word_zh, w.pinyin, w.definition, w.pos,
                  w.hsk_level, w.traditional, w.definition_zh,
                  w.note_type, w.source_sentence, w.notes, w.definition_de, w.definition_fr, w.register
           FROM cards c
           JOIN entries w ON w.id = c.word_id
           WHERE c.deck_id = ?
             AND c.category = ?
             AND c.state != 'suspended'
             AND c.deleted_at IS NULL
             AND (c.buried_until IS NULL OR c.buried_until < ?)
             AND (
               (c.state IN ('learning', 'relearn') AND {_learning_due_today_sql()})
               OR (c.state = 'review' AND c.due <= ?)
               OR (c.state = 'new' AND c.due <= ?)
             )""",
        (deck_id, category, today, _tomorrow_cutoff(), today, today, today),
    ).fetchall()

    all_cards = [dict(r) for r in rows]
    # A 'review' card whose interval hasn't reached learned_interval is still a
    # learning card: it queues with the learning group (before learned cards).
    threshold = preset.get("learned_interval", 4)
    learning_cards = [c for c in all_cards if _still_learning(c, threshold)]
    review_cards   = [c for c in all_cards if c["state"] == "review" and not _still_learning(c, threshold)]
    new_cards_raw  = [c for c in all_cards if c["state"] == "new"]

    # ── 1. Gather & sort new cards ────────────────────────────────────────────
    gather = preset.get("new_gather_order", "ascending_position")
    if gather == "ascending_position":
        new_cards_raw.sort(key=lambda c: c["id"])
    elif gather == "descending_position":
        new_cards_raw.sort(key=lambda c: c["id"], reverse=True)
    elif gather == "deck":
        new_cards_raw.sort(key=lambda c: (c["deck_id"], c["id"]))
    elif gather == "deck_random_notes":
        by_deck: dict = {}
        for c in new_cards_raw:
            by_deck.setdefault(c["deck_id"], []).append(c)
        gathered = []
        for dk in sorted(by_deck):
            grp = by_deck[dk]
            random.shuffle(grp)
            gathered.extend(grp)
        new_cards_raw = gathered
    elif gather in ("random_notes", "random_cards"):
        random.shuffle(new_cards_raw)

    sort_o = preset.get("new_sort_order", "card_type_gathered")
    if sort_o in ("random", "card_type_random", "random_note_card_type"):
        random.shuffle(new_cards_raw)
    # else: card_type_gathered / gathered → keep gather order

    new_cards = new_cards_raw[:new_remaining]

    # ── 2. Sort review cards ──────────────────────────────────────────────────
    rev_o = preset.get("review_sort_order", "due_random")
    if rev_o == "due_random":
        review_cards.sort(key=lambda c: c["due"])
        shuffled: list = []
        for _, grp in groupby(review_cards, key=lambda c: c["due"]):
            g = list(grp)
            random.shuffle(g)
            shuffled.extend(g)
        review_cards = shuffled
    elif rev_o == "due_deck":
        review_cards.sort(key=lambda c: (c["due"], c["deck_id"]))
    elif rev_o == "deck_due":
        review_cards.sort(key=lambda c: (c["deck_id"], c["due"]))
    elif rev_o == "ascending_intervals":
        review_cards.sort(key=lambda c: c["interval"])
    elif rev_o == "descending_intervals":
        review_cards.sort(key=lambda c: c["interval"], reverse=True)
    elif rev_o == "ascending_ease":
        review_cards.sort(key=lambda c: c["ease"])
    elif rev_o == "descending_ease":
        review_cards.sort(key=lambda c: c["ease"], reverse=True)
    elif rev_o == "relative_overdueness":
        today_d = date.fromisoformat(today)
        def _overdueness(c: dict) -> float:
            if c["interval"] <= 0:
                return 0.0
            try:
                overdue = (today_d - date.fromisoformat(c["due"][:10])).days
                return overdue / c["interval"]
            except Exception:
                return 0.0
        review_cards.sort(key=_overdueness, reverse=True)

    # ── 3. Learning cards always sorted by due time ───────────────────────────
    learning_cards.sort(key=lambda c: c["due"])

    # ── 4. Assemble queue ─────────────────────────────────────────────────────
    il_o = preset.get("interday_learning_review_order", "mixed")
    if il_o == "learning_first":
        lr = learning_cards + review_cards
    elif il_o == "reviews_first":
        lr = review_cards + learning_cards
    else:  # mixed: merge by due time
        lr = sorted(learning_cards + review_cards, key=lambda c: c["due"])

    nr_o = preset.get("new_review_order_override") or preset.get("new_review_order", "mixed")
    if nr_o == "new_first":
        cards = new_cards + lr
    elif nr_o == "reviews_first":
        cards = lr + new_cards
    else:  # mixed: learning first, then interleave new cards evenly among reviews
        cards = learning_cards + _interleave_cards(review_cards, new_cards)

    # ── 5. Sibling suppression (for story word-list building) ─────────────────
    if sibling_suppression and any(resolve_bury_flags(preset)):
        word_ids = {c["word_id"] for c in cards}
        suppressed = _get_virtually_buried_word_ids(word_ids, category, conn, today, now)
        if suppressed:
            cards = [c for c in cards if c["word_id"] not in suppressed]

    conn.close()
    return cards


def get_next_card(deck_id: int, category: str) -> dict | None:
    """Top-priority card for the review session, ordered by today's story position."""
    cards = get_due_cards(deck_id, category)
    if not cards:
        return None

    # Reorder by story sentence position if a story exists for today
    today = anki_today().isoformat()
    # Import here to avoid circular import at module level
    from .stories import get_active_story, get_story_sentences
    story = get_active_story(today, category, deck_id)
    if story:
        sentences = get_story_sentences(story["id"])
        # word_id → story position
        story_pos = {s["word_id"]: s["position"] for s in sentences}
        cards.sort(key=lambda c: story_sort_key(c, story_pos))

    return cards[0]


def _get_presets_bulk(deck_ids: list[int], category: str | None = None) -> dict[int, dict]:
    """Batched equivalent of calling get_preset_for_deck(did, category) for each
    deck_id — one JOIN query + one override query instead of 2 queries per deck
    (issue #513: aggregate decks with 100+ leaves were paying this N+1 on every
    /api/today request)."""
    if not deck_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(deck_ids))
    rows = conn.execute(
        f"""SELECT d.id AS deck_id, p.*,
                   d.new_review_order_override AS deck_nro_override,
                   d.bury_quick_mode           AS deck_bury_quick_mode
            FROM decks d JOIN deck_presets p ON p.id = d.preset_id
            WHERE d.id IN ({placeholders})""",
        deck_ids,
    ).fetchall()

    result: dict[int, dict] = {}
    for row in rows:
        d = dict(row)
        did = d.pop("deck_id")
        deck_nro = d.pop("deck_nro_override", None)
        if deck_nro is not None:
            d["new_review_order_override"] = deck_nro
        deck_bury = d.pop("deck_bury_quick_mode", None)
        if deck_bury is not None:
            d["bury_quick_mode"] = deck_bury
        result[did] = d

    if category:
        preset_ids = list({p["id"] for p in result.values()})
        if preset_ids:
            ph2 = ",".join("?" * len(preset_ids))
            override_rows = conn.execute(
                f"SELECT * FROM preset_category_overrides WHERE category = ? AND preset_id IN ({ph2})",
                [category] + preset_ids,
            ).fetchall()
            overrides_by_preset = {r["preset_id"]: dict(r) for r in override_rows}
            for preset in result.values():
                override = overrides_by_preset.get(preset["id"])
                if override:
                    for key, val in override.items():
                        if key not in ("id", "preset_id", "category") and val is not None:
                            preset[key] = val
    conn.close()
    return result


def _count_due_bulk(deck_ids: list[int], category: str) -> dict[int, dict]:
    """Batched equivalent of calling count_due(did, category) for each deck_id.

    Turns the O(N) per-deck queries (due rows / new-introduced-today / learning-
    future / preset, each on its own connection) into a fixed handful of grouped
    queries — this was the dominant cost of /api/today for aggregate decks with
    many leaves (issue #513: ~1300 queries for a 111-leaf deck)."""
    zero = {"new": 0, "learning": 0, "review": 0, "learning_future": 0, "learning_soon": 0}
    result = {did: dict(zero) for did in deck_ids}
    if not deck_ids:
        return result

    locked = get_locked_deck_ids()
    active_ids = [did for did in deck_ids if did not in locked]
    if not active_ids:
        return result

    today = anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    presets = _get_presets_bulk(active_ids, category)

    conn = get_db()
    ph = ",".join("?" * len(active_ids))

    new_today_rows = conn.execute(
        f"""SELECT c.deck_id, COUNT(DISTINCT c.id) AS cnt FROM cards c
            WHERE c.deck_id IN ({ph}) AND c.category = ?
              AND EXISTS (
                SELECT 1 FROM review_log rl
                WHERE rl.card_id = c.id AND date(rl.reviewed_at) = ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM review_log rl
                WHERE rl.card_id = c.id AND date(rl.reviewed_at) < ?
              )
            GROUP BY c.deck_id""",
        active_ids + [category, today, today],
    ).fetchall()
    new_today = {r["deck_id"]: r["cnt"] for r in new_today_rows}

    due_rows = conn.execute(
        f"""SELECT c.deck_id, c.state, c.due, c.interval FROM cards c
            WHERE c.deck_id IN ({ph}) AND c.category = ?
              AND c.state != 'suspended'
              AND c.deleted_at IS NULL
              AND (c.buried_until IS NULL OR c.buried_until < ?)
              AND (
                (c.state IN ('learning', 'relearn') AND {_learning_due_now_sql()})
                OR (c.state = 'review' AND c.due <= ?)
                OR (c.state = 'new' AND c.due <= ?)
              )""",
        active_ids + [category, today, now, today, today, today],
    ).fetchall()
    by_deck_rows: dict[int, list] = {}
    for r in due_rows:
        by_deck_rows.setdefault(r["deck_id"], []).append(r)

    learning_future_rows = conn.execute(
        f"""SELECT deck_id, COUNT(*) AS cnt FROM cards
            WHERE deck_id IN ({ph}) AND category = ?
              AND state IN ('learning', 'relearn')
              AND NOT {_learning_due_now_sql('')}
              AND deleted_at IS NULL
              AND (buried_until IS NULL OR buried_until < ?)
            GROUP BY deck_id""",
        active_ids + [category, now, today, today],
    ).fetchall()
    learning_future = {r["deck_id"]: r["cnt"] for r in learning_future_rows}

    learning_soon_rows = conn.execute(
        f"""SELECT deck_id, COUNT(*) AS cnt FROM cards
            WHERE deck_id IN ({ph}) AND category = ?
              AND state IN ('learning', 'relearn')
              AND {_learning_due_soon_sql('')}
              AND deleted_at IS NULL
              AND (buried_until IS NULL OR buried_until < ?)
            GROUP BY deck_id""",
        active_ids + [category, now, _tomorrow_cutoff(), today],
    ).fetchall()
    learning_soon = {r["deck_id"]: r["cnt"] for r in learning_soon_rows}
    conn.close()

    for did in active_ids:
        preset = presets.get(did) or {}
        new_limit = preset.get("new_per_day", 20)
        threshold = preset.get("learned_interval", 4)
        new_done_today = new_today.get(did, 0)
        new_remaining = max(0, new_limit - new_done_today)
        rows = by_deck_rows.get(did, [])

        def _is_learning(r) -> bool:
            return (r["state"] in ("learning", "relearn")
                    or (r["state"] == "review" and (r["interval"] or 0) < threshold))

        learning = sum(1 for r in rows if _is_learning(r))
        review = sum(1 for r in rows if r["state"] == "review" and not _is_learning(r))
        new_avail = sum(1 for r in rows if r["state"] == "new")

        result[did] = {
            "new": min(new_avail, new_remaining),
            "learning": learning,
            "review": review,
            "learning_future": learning_future.get(did, 0),
            "learning_soon": learning_soon.get(did, 0),
        }
    return result


def count_due(deck_id: int, category: str) -> dict:
    """Returns {new, learning, review} counts for deck badge display."""
    if deck_id in get_locked_deck_ids():
        return {"new": 0, "learning": 0, "review": 0, "learning_future": 0, "learning_soon": 0}
    today = anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    preset = get_preset_for_deck(deck_id, category)
    new_limit = preset["new_per_day"]

    conn = get_db()
    new_done_today = _count_new_introduced_today(conn, deck_id, category, today)
    new_remaining = max(0, new_limit - new_done_today)

    rows = conn.execute(
        f"""SELECT c.word_id, c.state, c.due, c.interval FROM cards c
           WHERE c.deck_id = ? AND c.category = ?
             AND c.state != 'suspended'
             AND c.deleted_at IS NULL
             AND (c.buried_until IS NULL OR c.buried_until < ?)
             AND (
               (c.state IN ('learning', 'relearn') AND {_learning_due_now_sql()})
               OR (c.state = 'review' AND c.due <= ?)
               OR (c.state = 'new' AND c.due <= ?)
             )""",
        (deck_id, category, today, now, today, today, today),
    ).fetchall()

    # A 'review' card whose interval hasn't reached learned_interval is still
    # "learning" for badge purposes (not yet learned/mature).
    threshold = preset.get("learned_interval", 4)
    def _is_learning(r) -> bool:
        return (r["state"] in ("learning", "relearn")
                or (r["state"] == "review" and (r["interval"] or 0) < threshold))

    learning = sum(1 for r in rows if _is_learning(r))
    review   = sum(1 for r in rows if r["state"] == "review" and not _is_learning(r))
    new_avail = sum(1 for r in rows if r["state"] == "new")

    learning_future = conn.execute(
        f"""SELECT COUNT(*) FROM cards
           WHERE deck_id = ? AND category = ?
             AND state IN ('learning', 'relearn')
             AND NOT {_learning_due_now_sql('')}
             AND deleted_at IS NULL
             AND (buried_until IS NULL OR buried_until < ?)""",
        (deck_id, category, now, today, today),
    ).fetchone()[0]

    learning_soon = conn.execute(
        f"""SELECT COUNT(*) FROM cards
           WHERE deck_id = ? AND category = ?
             AND state IN ('learning', 'relearn')
             AND {_learning_due_soon_sql('')}
             AND deleted_at IS NULL
             AND (buried_until IS NULL OR buried_until < ?)""",
        (deck_id, category, now, _tomorrow_cutoff(), today),
    ).fetchone()[0]

    conn.close()
    return {
        "new": min(new_avail, new_remaining),
        "learning": learning,
        "review": review,
        "learning_future": learning_future,
        "learning_soon": learning_soon,
    }


def update_word(word_id: int, fields: dict) -> None:
    allowed = {"word_zh", "pinyin", "definition", "pos", "traditional", "definition_zh", "notes", "hsk_level", "definition_de", "definition_fr"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = get_db()
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE entries SET {sets} WHERE id=?", (*updates.values(), word_id))
    conn.commit()
    conn.close()


_UNSET = object()


def update_card(card_id: int, *, state: str, due: str,
                step_index: int, interval: int,
                ease: float, repetitions: int, lapses: int,
                learning_again_count: int | None = None,
                probation: int | None = None,
                stability=_UNSET, difficulty=_UNSET, last_review=_UNSET) -> None:
    """Update a card's scheduling state.

    stability/difficulty/last_review are optional: omit them to leave the column
    untouched, or pass an explicit value (including None) to set it. This keeps
    legacy callers working while letting FSRS persist its memory state.
    """
    sets = ["state=?", "due=?", "step_index=?", "interval=?",
            "ease=?", "repetitions=?", "lapses=?"]
    vals = [state, due, step_index, interval, ease, repetitions, lapses]
    if learning_again_count is not None:
        sets.append("learning_again_count=?")
        vals.append(learning_again_count)
    if probation is not None:
        sets.append("probation=?")
        vals.append(probation)
    if stability is not _UNSET:
        sets.append("stability=?")
        vals.append(stability)
    if difficulty is not _UNSET:
        sets.append("difficulty=?")
        vals.append(difficulty)
    if last_review is not _UNSET:
        sets.append("last_review=?")
        vals.append(last_review)
    vals.append(card_id)
    conn = get_db()
    conn.execute(f"UPDATE cards SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def bury_card(card_id: int) -> None:
    """Bury a card until tomorrow (hidden for the rest of today)."""
    today = anki_today().isoformat()
    conn = get_db()
    conn.execute("UPDATE cards SET buried_until = ? WHERE id = ?", (today, card_id))
    conn.commit()
    conn.close()


def unbury_card(card_id: int) -> None:
    """Remove burial — card becomes available immediately."""
    conn = get_db()
    conn.execute("UPDATE cards SET buried_until = NULL WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()


def set_card_buried_until(card_id: int, buried_until: str | None) -> None:
    """Restore buried_until to an exact value (used by undo)."""
    conn = get_db()
    conn.execute("UPDATE cards SET buried_until = ? WHERE id = ?", (buried_until, card_id))
    conn.commit()
    conn.close()


def get_descendant_leaf_deck_ids(deck_id: int, category: str | None = None, lang: str | None = None) -> list[int]:
    """Return all category-leaf deck IDs under deck_id (depth-first). Optionally filter by category and/or lang."""
    conn = get_db()
    rows = conn.execute("SELECT id, parent_id, category, lang FROM decks WHERE deleted_at IS NULL").fetchall()
    conn.close()

    children_map: dict = {}
    deck_cat: dict = {}
    deck_lang: dict = {}
    for row in rows:
        deck_cat[row["id"]] = row["category"]
        deck_lang[row["id"]] = row["lang"]
        pid = row["parent_id"]
        children_map.setdefault(pid, []).append(row["id"])

    result = []
    stack = [deck_id]
    while stack:
        current = stack.pop()
        cat = deck_cat.get(current)
        kids = children_map.get(current, [])
        if cat is not None:  # category leaf
            if (category is None or cat == category) and (lang is None or deck_lang.get(current) == lang):
                result.append(current)
        for kid in kids:
            stack.append(kid)
    return result


def get_parent_deck_id(deck_id: int) -> int | None:
    """Return the parent deck ID for a given deck, or None if it's a root deck."""
    conn = get_db()
    row = conn.execute("SELECT parent_id FROM decks WHERE id = ?", (deck_id,)).fetchone()
    conn.close()
    return row["parent_id"] if row else None


_CATEGORY_ENABLED_COLUMN = {
    "reading": "reading_enabled",
    "listening": "listening_enabled",
    "creating": "creating_enabled",
}


def reading_disabled_deck_ids() -> set[int]:
    """IDs of decks whose preset has reading_enabled = 0.

    Reading cards of these decks are excluded from mixed/any-cat queues,
    due counts and the unfinished virtual deck. The cards themselves are
    kept untouched so re-enabling the preset flag brings them back.
    """
    return category_disabled_deck_ids("reading")


def category_disabled_deck_ids(category: str) -> set[int]:
    """IDs of decks whose preset has <category>_enabled = 0.

    Cards of that category in these decks are excluded from mixed/any-cat
    queues, due counts and the unfinished virtual deck. The cards themselves
    are kept untouched so re-enabling the preset flag brings them back.
    """
    col = _CATEGORY_ENABLED_COLUMN[category]
    conn = get_db()
    rows = conn.execute(
        f"""SELECT d.id FROM decks d
           JOIN deck_presets p ON p.id = d.preset_id
           WHERE p.{col} = 0"""
    ).fetchall()
    conn.close()
    return {r["id"] for r in rows}


# SQL fragment matching cards that belong to a category disabled for their
# deck's preset (reading/listening/creating each independently switchable).
_CATEGORY_DISABLED_SQL = (
    "EXISTS (SELECT 1 FROM decks d JOIN deck_presets p ON p.id = d.preset_id "
    "WHERE d.id = deck_id AND ("
    "  (category = 'reading' AND p.reading_enabled = 0)"
    "  OR (category = 'listening' AND p.listening_enabled = 0)"
    "  OR (category = 'creating' AND p.creating_enabled = 0)"
    "))"
)


def _learning_due_now_sql(prefix: str = "c.") -> str:
    """SQL fragment matching learning/relearn cards due RIGHT NOW.

    Takes two params: now (ISO datetime), today (anki_today() ISO date).

    cards.due holds two shapes for learning cards: the minute steps (1m/10m)
    store an ISO datetime, the cross-day steps (1d/3d) store a bare date. A
    bare date means "due when that Anki day starts", so it must be compared
    against anki_today() — never against now. Between midnight and the day
    cutoff (4am) the Anki day is still yesterday, and a plain string comparison
    reads tomorrow's date as already due ('2026-08-16' <= '2026-08-16T03:13'),
    so the deck badges showed dozens of cards the queue refused to hand out —
    Daniel saw "73 due" and an immediate "all done" (#762).
    """
    d = f"{prefix}due"
    return (f"((instr({d}, 'T') > 0 AND {d} <= ?) "
            f" OR (instr({d}, 'T') = 0 AND {d} <= ?))")


def _learning_due_today_sql(prefix: str = "c.") -> str:
    """SQL fragment matching learning/relearn cards due anywhere in the current
    Anki day — the queue's cut, wider than _learning_due_now_sql() because
    queue_manager re-checks each timestamp against `now` when handing cards out.

    Takes two params: _tomorrow_cutoff(), today (anki_today() ISO date).

    Same two due shapes as _learning_due_now_sql(). The old cut was a single
    `due < tomorrow` against a bare date, which between midnight and the cutoff
    dropped every minute-step card of the current Anki day
    ('2026-08-16T03:00' < '2026-08-16' is false) — the other half of #762.
    """
    d = f"{prefix}due"
    return (f"((instr({d}, 'T') > 0 AND {d} < ?) "
            f" OR (instr({d}, 'T') = 0 AND {d} <= ?))")


def _learning_due_soon_sql(prefix: str = "c.") -> str:
    """SQL fragment matching learning/relearn cards that come back LATER TODAY —
    after `now` but before tomorrow's day cutoff.

    Takes two params: now (ISO datetime), _tomorrow_cutoff().

    Only the minute steps (1m/10m) qualify, so this deliberately matches the
    datetime shape of `due` alone. A bare date means "due when that Anki day
    starts": if it is <= today the card is already due now, and if it is later
    it belongs to another day — the 1d/3d steps must never be counted as
    "coming back today" (#844).
    """
    d = f"{prefix}due"
    return f"(instr({d}, 'T') > 0 AND {d} > ? AND {d} < ?)"


def _tomorrow_cutoff() -> str:
    """The moment the current Anki day ends, as a full ISO datetime."""
    return datetime.combine(
        anki_today() + timedelta(days=1), time(hour=get_day_cutoff_hour())
    ).isoformat(timespec="seconds")


def _leaf_decks_with_category(root_deck_id: int, lang: str | None = None) -> list[tuple[int, str]]:
    """Return [(deck_id, category)] for all category leaves under root_deck_id.

    Leaves whose preset disables their own category are omitted. Optionally
    filter descendant leaves by lang (direct category-leaf decks are never
    filtered — same rule as leaf_ids()).
    """
    disabled = {cat: category_disabled_deck_ids(cat) for cat in _CATEGORY_ENABLED_COLUMN}

    def _keep(deck_id: int, category: str) -> bool:
        return deck_id not in disabled.get(category, ())

    all_leaf_ids = get_descendant_leaf_deck_ids(root_deck_id, lang=lang)
    if not all_leaf_ids:
        deck = get_deck(root_deck_id)
        if deck and deck["category"] and _keep(root_deck_id, deck["category"]):
            return [(root_deck_id, deck["category"])]
        return []
    conn = get_db()
    placeholders = ','.join('?' * len(all_leaf_ids))
    rows = conn.execute(
        f"SELECT id, category FROM decks WHERE id IN ({placeholders})", all_leaf_ids
    ).fetchall()
    conn.close()
    return [(r["id"], r["category"]) for r in rows if r["category"] and _keep(r["id"], r["category"])]


def get_due_cards_any_cat(root_deck_id: int, lang: str | None = None) -> list[dict]:
    """All due cards across every category under root_deck_id, priority-sorted.

    Cards are ordered so that the highest-priority card is first.  When a story
    exists the order follows its narrative position; otherwise cards are sorted
    by state (learning/relearn → review → new), category order, and due time.
    """
    leaf_pairs = _leaf_decks_with_category(root_deck_id, lang=lang)
    all_cards = []
    for deck_id, cat in leaf_pairs:
        all_cards.extend(get_due_cards(deck_id, cat))
    if not all_cards:
        return []

    # Apply root deck's per-category new cap across all leaf decks combined
    today = anki_today().isoformat()
    conn = get_db()
    cats_in_tree = {cat for _, cat in leaf_pairs}
    for cat in cats_in_tree:
        cat_deck_ids = [did for did, c in leaf_pairs if c == cat]
        if len(cat_deck_ids) <= 1:
            continue
        root_preset = get_preset_for_deck(root_deck_id, cat)
        if not root_preset:
            continue
        root_new_done = _count_new_introduced_today_multi(conn, cat_deck_ids, cat, today)
        root_new_remaining = max(0, root_preset["new_per_day"] - root_new_done)
        cat_new = [c for c in all_cards if c["state"] == "new" and c["category"] == cat]
        if len(cat_new) > root_new_remaining:
            remove_ids = {c["id"] for c in cat_new[root_new_remaining:]}
            all_cards = [c for c in all_cards if c["id"] not in remove_ids]
    conn.close()

    # Build category order index from root deck's preset
    preset = get_preset_for_deck(root_deck_id)
    order_str = preset.get("category_order", "listening,reading,creating")
    cat_order = {c.strip(): i for i, c in enumerate(order_str.split(","))}

    # Reorder by story sentence position (same as get_next_card for single-cat)
    today = anki_today().isoformat()
    from .stories import get_active_story, get_story_sentences
    story_pos: dict = {}
    for deck_id, cat in leaf_pairs:
        story = get_active_story(today, cat, deck_id)
        if story:
            for s in get_story_sentences(story["id"]):
                # One sentence can map to several words (token-segmentation):
                # get_story_sentences() exposes word_ids (list), not word_id.
                for wid in s["word_ids"]:
                    story_pos[wid] = s["position"]
    # Unified story is stored as category="unified" on the root deck —
    # the per-category loop above never finds it, so check explicitly.
    if not story_pos:
        unified_story = get_active_story(today, "unified", root_deck_id)
        if unified_story:
            for s in get_story_sentences(unified_story["id"]):
                for wid in s["word_ids"]:
                    story_pos[wid] = s["position"]

    if story_pos:
        # Story exists: follow narrative order so the session reads top-to-bottom.
        # Cards the story doesn't cover are grouped by story_sort_key (#732) —
        # learning/review leftovers ahead of it, new cards behind it — instead of
        # all landing behind every new card.
        # Use category_order as tiebreaker so same-word cards respect deck settings.
        all_cards.sort(key=lambda c: (
            story_sort_key(c, story_pos),
            cat_order.get(c["category"], 99),
        ))
    else:
        # No story: learning-first. A review card below its deck's
        # learned_interval is still "learning" (rank 0), before learned cards.
        thresholds = {
            did: (get_preset_for_deck(did) or {}).get("learned_interval", 4)
            for did, _ in leaf_pairs
        }
        def _rank(c: dict) -> int:
            if _still_learning(c, thresholds.get(c["deck_id"], 4)):
                return 0
            return 1 if c["state"] == "review" else 2
        all_cards.sort(key=lambda c: (
            _rank(c),
            cat_order.get(c["category"], 99),
            c["due"],
        ))
    return all_cards


def get_next_card_any_cat(root_deck_id: int) -> dict | None:
    """Highest-priority card across all categories under root_deck_id."""
    cards = get_due_cards_any_cat(root_deck_id)
    return cards[0] if cards else None


def count_due_any_cat(root_deck_id: int, lang: str | None = None) -> dict:
    """Deduplicated due counts across all categories under root_deck_id.

    Each word is counted once (in its highest-priority category), matching
    the deduplication logic used for parent-deck badges on the main page.
    """
    leaf_pairs = _leaf_decks_with_category(root_deck_id, lang=lang)
    return count_due_deduped(leaf_pairs)


def count_due_by_category(root_deck_id: int, lang: str | None = None) -> dict:
    """Per-category {new, learning, review} counts for mixed review display."""
    leaf_pairs = _leaf_decks_with_category(root_deck_id, lang=lang)
    ids_by_category: dict[str, list[int]] = {}
    for deck_id, category in leaf_pairs:
        ids_by_category.setdefault(category, []).append(deck_id)

    result: dict[str, dict[str, int]] = {}
    for category, cat_deck_ids in ids_by_category.items():
        per_deck = _count_due_bulk(cat_deck_ids, category)
        agg = {"new": 0, "learning": 0, "review": 0, "learning_soon": 0}
        for c in per_deck.values():
            for k in agg:
                agg[k] += c.get(k, 0)
        result[category] = agg

    # Apply root deck's per-category new cap (Anki parent-deck behaviour)
    today = anki_today().isoformat()
    conn = get_db()
    for category in result:
        cat_deck_ids = [did for did, cat in leaf_pairs if cat == category]
        if len(cat_deck_ids) <= 1:
            continue
        root_preset = get_preset_for_deck(root_deck_id, category)
        if not root_preset:
            continue
        root_new_done = _count_new_introduced_today_multi(conn, cat_deck_ids, category, today)
        root_new_remaining = max(0, root_preset["new_per_day"] - root_new_done)
        result[category]["new"] = min(result[category]["new"], root_new_remaining)
    conn.close()

    return result


def get_due_cards_multi(deck_ids: list[int], category: str, *, root_deck_id: int | None = None, sibling_suppression: bool = False) -> list[dict]:
    """Due cards across multiple decks, merged and priority-sorted.

    Learning cards always come first (sorted by due), then review and new cards
    are combined according to new_review_order: "mixed" interleaves new cards
    evenly among review cards; "reviews_first" appends new cards after reviews;
    "new_first" prepends new cards before reviews.

    root_deck_id: if provided, its new_per_day limit acts as a combined cap
    across all leaf decks (Anki parent-deck behaviour).
    """
    # Batch-fetch what get_due_cards() would otherwise look up per deck (issue
    # #513: this was 2 preset queries + 1 new-count query PER deck — for a
    # 111-leaf aggregate deck that's 300+ queries just for these inputs).
    presets_by_cat = _get_presets_bulk(deck_ids, category)
    presets_base = _get_presets_bulk(deck_ids, None)
    new_today_map: dict[int, int] = {}
    if deck_ids:
        today = anki_today().isoformat()
        conn = get_db()
        ph = ",".join("?" * len(deck_ids))
        rows = conn.execute(
            f"""SELECT c.deck_id, COUNT(DISTINCT c.id) AS cnt FROM cards c
                WHERE c.deck_id IN ({ph}) AND c.category = ?
                  AND EXISTS (
                    SELECT 1 FROM review_log rl
                    WHERE rl.card_id = c.id AND date(rl.reviewed_at) = ?
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM review_log rl
                    WHERE rl.card_id = c.id AND date(rl.reviewed_at) < ?
                  )
                GROUP BY c.deck_id""",
            deck_ids + [category, today, today],
        ).fetchall()
        new_today_map = {r["deck_id"]: r["cnt"] for r in rows}
        conn.close()

    all_cards = []
    for deck_id in deck_ids:
        all_cards.extend(get_due_cards(
            deck_id, category, sibling_suppression=sibling_suppression,
            _preset=presets_by_cat.get(deck_id),
            _new_done_today=new_today_map.get(deck_id, 0),
        ))

    # Each deck may have its own learned_interval; a review card below its deck's
    # threshold is still "learning" and queues with the learning group.
    thresholds = {
        did: (presets_base.get(did) or {}).get("learned_interval", 4)
        for did in deck_ids
    }
    def _lrn(c: dict) -> bool:
        return _still_learning(c, thresholds.get(c["deck_id"], 4))
    learning_cards = [c for c in all_cards if _lrn(c)]
    review_cards   = [c for c in all_cards if c["state"] == "review" and not _lrn(c)]
    new_cards      = [c for c in all_cards if c["state"] == "new"]

    # Apply parent deck's combined new-card cap (Anki-style)
    if root_deck_id is not None and len(deck_ids) > 1:
        root_preset = get_preset_for_deck(root_deck_id, category)
        if root_preset:
            root_new_limit = root_preset["new_per_day"]
            today = anki_today().isoformat()
            conn = get_db()
            root_new_done = _count_new_introduced_today_multi(conn, deck_ids, category, today)
            conn.close()
            root_new_remaining = max(0, root_new_limit - root_new_done)
            new_cards = new_cards[:root_new_remaining]

    learning_cards.sort(key=lambda c: c["due"])
    review_cards.sort(key=lambda c: c["due"])
    # new_cards keep the per-deck gather/sort order from get_due_cards

    preset = presets_base.get(deck_ids[0], {}) if deck_ids else {}
    nr_o = preset.get("new_review_order_override") or preset.get("new_review_order", "mixed")

    if nr_o == "new_first":
        review_new = new_cards + review_cards
    elif nr_o == "reviews_first":
        review_new = review_cards + new_cards
    else:  # mixed: interleave new cards evenly among review cards
        review_new = _interleave_cards(review_cards, new_cards)

    return learning_cards + review_new


def get_next_card_multi(deck_ids: list[int], category: str) -> dict | None:
    """Highest-priority card across multiple decks."""
    cards = get_due_cards_multi(deck_ids, category)
    return cards[0] if cards else None


def count_due_multi(deck_ids: list[int], category: str, *, root_deck_id: int | None = None) -> dict:
    """Aggregate due counts across multiple decks."""
    total = {"new": 0, "learning": 0, "review": 0, "learning_soon": 0}
    per_deck = _count_due_bulk(deck_ids, category)
    for deck_id in deck_ids:
        c = per_deck.get(deck_id, {})
        for k in total:
            total[k] += c.get(k, 0)

    if root_deck_id is not None and len(deck_ids) > 1:
        root_preset = get_preset_for_deck(root_deck_id, category)
        if root_preset:
            root_new_limit = root_preset["new_per_day"]
            today = anki_today().isoformat()
            conn = get_db()
            root_new_done = _count_new_introduced_today_multi(conn, deck_ids, category, today)
            conn.close()
            total["new"] = min(total["new"], max(0, root_new_limit - root_new_done))

    return total


def count_due_deduped(leaf_pairs: list[tuple[int, str]]) -> dict:
    """Count unique due words across multiple category leaf decks for parent badge display.

    Each word is counted once, in the category of its highest-priority due card:
      state rank  : learning/relearn=0, review=1, new=2  (lower = better)
      category rank: listening=0, reading=1, creating=2  (lower = better)

    Respects the bury_siblings setting. Falls back to a simple sum if disabled.
    """
    # Drop locked (future-dated daily) leaves so they don't inflate parent badges.
    locked = get_locked_deck_ids()
    leaf_pairs = [(d, c) for d, c in leaf_pairs if d not in locked]
    if not leaf_pairs:
        return {"new": 0, "learning": 0, "review": 0, "learning_soon": 0}

    today = anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()

    preset = get_preset_for_deck(leaf_pairs[0][0])
    if not preset.get("bury_siblings", 1):
        conn.close()
        total = {"new": 0, "learning": 0, "review": 0, "learning_soon": 0}
        for deck_id, cat in leaf_pairs:
            c = count_due(deck_id, cat)
            for k in total:
                total[k] += c.get(k, 0)
        return total

    cat_rank_map = {"listening": 0, "reading": 1, "creating": 2}
    # word_id -> (state_rank, cat_rank, state, deck_id, category)
    best: dict[int, tuple] = {}
    new_remaining_map: dict[tuple, int] = {}

    for deck_id, category in leaf_pairs:
        pr = get_preset_for_deck(deck_id)
        threshold = pr.get("learned_interval", 4)
        new_done = _count_new_introduced_today(conn, deck_id, category, today)
        new_remaining_map[(deck_id, category)] = max(0, pr["new_per_day"] - new_done)

        rows = conn.execute(
            f"""SELECT c.word_id, c.state, c.interval FROM cards c
               WHERE c.deck_id = ? AND c.category = ?
                 AND c.state != 'suspended'
                 AND c.deleted_at IS NULL
                 AND (c.buried_until IS NULL OR c.buried_until < ?)
                 AND (
                   (c.state IN ('learning', 'relearn') AND {_learning_due_now_sql()})
                   OR (c.state = 'review' AND c.due <= ?)
                   OR (c.state = 'new' AND c.due <= ?)
                 )""",
            (deck_id, category, today, now, today, today, today),
        ).fetchall()

        for r in rows:
            sr = 0 if r["state"] in ("learning", "relearn") else 1 if r["state"] == "review" else 2
            cr = cat_rank_map[category]
            # A young 'review' card (interval below learned_interval) is tallied
            # as learning; dedup priority still uses the raw state rank so which
            # category a word is attributed to is unchanged.
            is_lrn = (r["state"] in ("learning", "relearn")
                      or (r["state"] == "review" and (r["interval"] or 0) < threshold))
            if r["word_id"] not in best or (sr, cr) < best[r["word_id"]][:2]:
                best[r["word_id"]] = (sr, cr, r["state"], deck_id, category, is_lrn)

    # Cards coming back later today (#844). Deduped by word for the same reason
    # the counts above are: one word must not inflate a parent badge three times.
    soon_count = 0
    if leaf_pairs:
        pair_clause = " OR ".join("(deck_id = ? AND category = ?)" for _ in leaf_pairs)
        pair_params = [v for pair in leaf_pairs for v in pair]
        soon_count = conn.execute(
            f"""SELECT COUNT(DISTINCT word_id) FROM cards
               WHERE ({pair_clause})
                 AND state IN ('learning', 'relearn')
                 AND {_learning_due_soon_sql('')}
                 AND deleted_at IS NULL
                 AND (buried_until IS NULL OR buried_until < ?)""",
            (*pair_params, now, _tomorrow_cutoff(), today),
        ).fetchone()[0]

    conn.close()

    learning_count = 0
    review_count = 0
    new_by_deck: dict[tuple, int] = {}

    for sr, cr, state, deck_id, category, is_lrn in best.values():
        if state == "new":
            key = (deck_id, category)
            new_by_deck[key] = new_by_deck.get(key, 0) + 1
        elif is_lrn:
            learning_count += 1
        else:
            review_count += 1

    new_count = sum(
        min(count, new_remaining_map.get(key, 0))
        for key, count in new_by_deck.items()
    )
    return {
        "new": new_count,
        "learning": learning_count,
        "review": review_count,
        "learning_soon": soon_count,
    }


def _locked_exclusion() -> tuple[str, list]:
    """SQL fragment excluding locked (future-dated daily) decks, plus its params."""
    locked = list(get_locked_deck_ids())
    if not locked:
        return "", []
    placeholders = ",".join("?" * len(locked))
    return f" AND deck_id NOT IN ({placeholders})", locked


def _unfinished_where(scope: str) -> tuple[str, list]:
    """Build the WHERE clause + params for the unfinished virtual deck.

    scope='unfinished' (default): only learning/relearn cards due right now.
    scope='all': every card still due today (new + review + learning/relearn).
    """
    now = datetime.now().isoformat(timespec="seconds")
    lock_clause, lock_params = _locked_exclusion()
    if scope == "all":
        today = anki_today().isoformat()
        clause = (
            "state != 'suspended' AND deleted_at IS NULL "
            "AND (buried_until IS NULL OR buried_until < ?) "
            "AND ("
            f"  (state IN ('learning', 'relearn') AND {_learning_due_today_sql('')})"
            "  OR (state = 'review' AND due <= ?)"
            "  OR (state = 'new' AND due <= ?)"
            ")"
            + lock_clause
            + f" AND NOT {_CATEGORY_DISABLED_SQL}"
        )
        return clause, [today, _tomorrow_cutoff(), today, today, today, *lock_params]
    today = anki_today().isoformat()
    clause = (
        f"state IN ('learning', 'relearn') AND {_learning_due_now_sql('')} "
        "AND deleted_at IS NULL "
        "AND (buried_until IS NULL OR buried_until < date('now'))"
        + lock_clause
        + f" AND NOT {_CATEGORY_DISABLED_SQL}"
    )
    return clause, [now, today, *lock_params]


def _lang_subquery_clause(lang: str | None, params: list) -> tuple[str, list]:
    """Append a 'deck_id IN (...)' lang filter to a cards WHERE clause via subquery
    (avoids column-name ambiguity from a JOIN against decks, which also has
    columns like deleted_at)."""
    if lang is None:
        return "", params
    return " AND deck_id IN (SELECT id FROM decks WHERE lang = ?)", [*params, lang]


def count_unfinished(scope: str = "unfinished", lang: str | None = None) -> dict:
    """Count cards on the unfinished virtual deck, grouped by state. Optionally filter by deck lang."""
    clause, params = _unfinished_where(scope)
    lang_clause, params = _lang_subquery_clause(lang, params)
    conn = get_db()
    rows = conn.execute(
        f"SELECT deck_id, state, interval FROM cards WHERE {clause}{lang_clause}",
        params,
    ).fetchall()
    conn.close()
    # A 'review' card whose interval hasn't reached its deck's learned_interval
    # is still "learning" — same classification as count_due().
    thresholds: dict[int, int] = {}
    counts = {"new": 0, "learning": 0, "review": 0, "learning_soon": 0}
    for r in rows:
        if r["state"] == "new":
            counts["new"] += 1
            continue
        if r["state"] in ("learning", "relearn"):
            counts["learning"] += 1
            continue
        if r["state"] != "review":
            continue
        did = r["deck_id"]
        if did not in thresholds:
            thresholds[did] = (get_preset_for_deck(did) or {}).get("learned_interval", 4)
        if (r["interval"] or 0) < thresholds[did]:
            counts["learning"] += 1
        else:
            counts["review"] += 1
    # scope='all' already counts the whole Anki day, so its `learning` includes
    # the cards coming back later today — reporting them again would double up.
    if scope != "all":
        counts["learning_soon"] = _count_unfinished_learning_soon(lang)
    return counts


def _count_unfinished_learning_soon(lang: str | None = None) -> int:
    """Learning/relearn cards on the unfinished virtual deck that come back
    later today (#844) — same locked-deck / disabled-category filters as
    _unfinished_where(), which only ever matches cards due right now."""
    lock_clause, lock_params = _locked_exclusion()
    clause = (
        f"state IN ('learning', 'relearn') AND {_learning_due_soon_sql('')} "
        "AND deleted_at IS NULL "
        "AND (buried_until IS NULL OR buried_until < date('now'))"
        + lock_clause
        + f" AND NOT {_CATEGORY_DISABLED_SQL}"
    )
    params = [datetime.now().isoformat(timespec="seconds"), _tomorrow_cutoff(), *lock_params]
    lang_clause, params = _lang_subquery_clause(lang, params)
    conn = get_db()
    n = conn.execute(f"SELECT COUNT(*) FROM cards WHERE {clause}{lang_clause}", params).fetchone()[0]
    conn.close()
    return n


def get_unfinished_deck_categories(scope: str = "unfinished", lang: str | None = None) -> list[dict]:
    """Return distinct (deck_id, category) pairs that have unfinished cards due now. Optionally filter by deck lang."""
    clause, params = _unfinished_where(scope)
    lang_clause, params = _lang_subquery_clause(lang, params)
    conn = get_db()
    rows = conn.execute(
        f"SELECT DISTINCT deck_id, category FROM cards WHERE {clause}{lang_clause}",
        params,
    ).fetchall()
    conn.close()
    return [{"deck_id": r["deck_id"], "category": r["category"]} for r in rows]


def get_next_unfinished_card(scope: str = "unfinished", lang: str | None = None) -> dict | None:
    """Highest-priority card on the unfinished virtual deck.

    Learning/relearn first (time-sensitive), then review, then new; each by due ASC.
    Optionally filter by deck lang.
    """
    clause, params = _unfinished_where(scope)
    lang_clause, params = _lang_subquery_clause(lang, params)
    conn = get_db()
    row = conn.execute(
        f"""SELECT id FROM cards WHERE {clause}{lang_clause}
           ORDER BY CASE state
                      WHEN 'learning' THEN 0 WHEN 'relearn' THEN 0
                      WHEN 'review' THEN 1 ELSE 2 END,
                    due ASC
           LIMIT 1""",
        params,
    ).fetchone()
    conn.close()
    return get_card(row["id"]) if row else None


def bury_siblings(word_id: int, reviewed_category: str, *,
                  bury_new: bool = False, bury_review: bool = False,
                  bury_learning: bool = False) -> None:
    """Bury other-category cards for this word based on which states should be buried."""
    states = []
    if bury_new:
        states.append("'new'")
    if bury_review:
        states.append("'review'")
    if bury_learning:
        states.extend(["'learning'", "'relearn'"])
    if not states:
        return
    today = anki_today().isoformat()
    conn = get_db()
    conn.execute(
        f"UPDATE cards SET buried_until = ? WHERE word_id = ? AND category != ?"
        f" AND state IN ({','.join(states)})",
        (today, word_id, reviewed_category),
    )
    conn.commit()
    conn.close()


def get_sibling_cards(card_id: int) -> list[dict]:
    """The other 2 cards for the same word."""
    conn = get_db()
    card = conn.execute("SELECT word_id FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT * FROM cards WHERE word_id = ? AND id != ?",
        (card["word_id"], card_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def apply_sibling_repulsion(card_id: int, new_interval: int,
                            sibling_separation: int, sibling_factor: float = 0.2) -> None:
    """Push sibling due dates that are too close after a review.

    effective_separation = max(sibling_separation, floor(new_interval * sibling_factor))

    Only applied when new_interval exceeds sibling_separation so the initial
    staggered-introduction phase (small intervals) is unaffected.
    Only review/new-state siblings are adjusted (learning/relearn cards use
    datetime-based due values that must not be touched here).
    """
    if new_interval <= sibling_separation:
        logger.debug(
            "[SIBLING REPULSION]  card=#%d  interval=%dd  ≤  threshold=%dd"
            "  →  SKIP (still in early intro phase, repulsion not active yet)",
            card_id, new_interval, sibling_separation,
        )
        return

    effective_sep = max(sibling_separation, int(new_interval * sibling_factor))
    today = anki_today()
    min_due = (today + timedelta(days=effective_sep)).isoformat()

    conn = get_db()
    try:
        card_row = conn.execute(
            """SELECT c.category, e.word_zh
               FROM cards c JOIN entries e ON e.id = c.word_id
               WHERE c.id = ?""",
            (card_id,),
        ).fetchone()
        if not card_row:
            return

        cat_label  = card_row["category"]
        word_label = card_row["word_zh"] or "?"
        factor_val = int(new_interval * sibling_factor)

        logger.debug(
            "[SIBLING REPULSION]  #%d %s 「%s」  interval=%dd"
            "  →  effective_sep = max(%dd, ⌊%d×%.2f⌋=%dd) = %dd  →  min_due=%s",
            card_id, cat_label, word_label, new_interval,
            sibling_separation, new_interval, sibling_factor, factor_val,
            effective_sep, min_due,
        )

        siblings = conn.execute(
            """SELECT id, category, state, due FROM cards
               WHERE word_id = (SELECT word_id FROM cards WHERE id = ?)
                 AND id != ? AND deleted_at IS NULL
                 AND state NOT IN ('suspended', 'learning', 'relearn')""",
            (card_id, card_id),
        ).fetchall()

        pushed = []
        for s in siblings:
            due_date = s["due"][:10]
            if due_date < min_due:
                conn.execute("UPDATE cards SET due = ? WHERE id = ?", (min_due, s["id"]))
                days_pushed = (date.fromisoformat(min_due) - date.fromisoformat(due_date)).days
                pushed.append((s["id"], s["category"], s["due"], min_due, days_pushed))

        if pushed:
            for sid, cat, old_due, new_due, days_pushed in pushed:
                logger.debug(
                    "  →  pushed  #%d %-10s  %s  →  %s  (+%dd)",
                    sid, cat, old_due, new_due, days_pushed,
                )
        else:
            logger.debug("  →  all siblings already beyond min_due=%s, no push needed", min_due)

        conn.commit()
    finally:
        conn.close()


def get_creating_all_suspended(deck_id: int) -> bool:
    """Return True if all non-sentence creating cards in the deck are suspended (and at least one exists)."""
    conn = get_db()
    row = conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN c.state = 'suspended' THEN 1 ELSE 0 END) as suspended_count
           FROM cards c
           JOIN entries w ON w.id = c.word_id
           WHERE c.deck_id = ? AND c.category = 'creating'
             AND c.deleted_at IS NULL
             AND w.note_type != 'sentence'""",
        (deck_id,),
    ).fetchone()
    conn.close()
    total = row["total"] or 0
    suspended = row["suspended_count"] or 0
    return total > 0 and total == suspended


def toggle_deck_creating_suspension(deck_id: int) -> dict:
    """Toggle all creating cards in a deck between suspended and new.

    Sentence notes (words with note_type='sentence') are excluded.
    Logic: if any cards are state='new', suspend all new ones;
           otherwise unsuspend all suspended ones.
    Returns {"all_suspended": bool, "count": int}.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.state FROM cards c
           JOIN entries w ON w.id = c.word_id
           WHERE c.deck_id = ? AND c.category = 'creating'
             AND c.deleted_at IS NULL
             AND w.note_type != 'sentence'""",
        (deck_id,),
    ).fetchall()

    has_active = any(r["state"] != "suspended" for r in rows)
    if has_active:
        conn.execute(
            """UPDATE cards SET pre_suspend_state=state, state='suspended'
               WHERE deck_id = ? AND category = 'creating'
                 AND deleted_at IS NULL AND state != 'suspended'
                 AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            (deck_id,),
        )
        all_suspended = True
    else:
        conn.execute(
            """UPDATE cards SET state=COALESCE(pre_suspend_state, 'new'), pre_suspend_state=NULL
               WHERE deck_id = ? AND category = 'creating'
                 AND deleted_at IS NULL AND state = 'suspended'
                 AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            (deck_id,),
        )
        all_suspended = False

    conn.commit()
    conn.close()
    return {"all_suspended": all_suspended, "count": len(rows)}


def get_category_all_suspended(deck_id: int, category: str) -> bool:
    """Return True if all non-sentence cards of given category in deck and all descendants are suspended."""
    conn = get_db()
    row = conn.execute(
        """WITH RECURSIVE descendants AS (
             SELECT id FROM decks WHERE id = ?
             UNION ALL
             SELECT d.id FROM decks d JOIN descendants p ON d.parent_id = p.id
           )
           SELECT COUNT(*) as total,
                  SUM(CASE WHEN c.state = 'suspended' THEN 1 ELSE 0 END) as suspended_count
           FROM cards c
           JOIN entries w ON w.id = c.word_id
           JOIN descendants des ON c.deck_id = des.id
           WHERE c.category = ?
             AND c.deleted_at IS NULL
             AND w.note_type != 'sentence'""",
        (deck_id, category),
    ).fetchone()
    conn.close()
    total = row["total"] or 0
    suspended = row["suspended_count"] or 0
    # total==0 means no suspendable cards (only sentence-type cards) → treat as suspended
    return total == suspended


def toggle_category_suspension(deck_id: int, category: str) -> dict:
    """Toggle all non-sentence cards of given category in a deck and all descendants."""
    conn = get_db()
    deck_rows = conn.execute(
        """WITH RECURSIVE descendants AS (
             SELECT id FROM decks WHERE id = ?
             UNION ALL
             SELECT d.id FROM decks d JOIN descendants p ON d.parent_id = p.id
           )
           SELECT id FROM descendants""",
        (deck_id,),
    ).fetchall()
    deck_ids = [r["id"] for r in deck_rows]
    placeholders = ",".join("?" * len(deck_ids))
    active_row = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM cards c
            JOIN entries w ON w.id = c.word_id
            WHERE c.deck_id IN ({placeholders})
              AND c.category = ?
              AND c.state != 'suspended'
              AND c.deleted_at IS NULL
              AND w.note_type != 'sentence'""",
        deck_ids + [category],
    ).fetchone()
    has_active = active_row["cnt"] > 0
    if has_active:
        conn.execute(
            f"""UPDATE cards SET pre_suspend_state=state, state='suspended'
                WHERE deck_id IN ({placeholders})
                  AND category = ?
                  AND state != 'suspended'
                  AND deleted_at IS NULL
                  AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            deck_ids + [category],
        )
    else:
        conn.execute(
            f"""UPDATE cards SET state=COALESCE(pre_suspend_state, 'new'), pre_suspend_state=NULL
                WHERE deck_id IN ({placeholders})
                  AND category = ?
                  AND state = 'suspended'
                  AND deleted_at IS NULL
                  AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            deck_ids + [category],
        )
    conn.commit()
    conn.close()
    return {"all_suspended": not has_active}


def count_due_all_decks() -> dict:
    """Bulk count due cards for ALL decks in 4 queries instead of ~4 per deck.

    Returns {(deck_id, category): {new, learning, review, learning_future}}.
    Also returns suspension flags as a second dict:
    {(deck_id, category): all_suspended_bool}.
    """
    today = anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    today_end = (anki_today() + timedelta(days=1)).isoformat()

    conn = get_db()

    # 1. Due cards grouped by (deck_id, category, state)
    due_rows = conn.execute(
        f"""SELECT c.deck_id, c.category, c.state, COUNT(*) AS cnt
           FROM cards c
           WHERE c.state != 'suspended'
             AND c.deleted_at IS NULL
             AND (c.buried_until IS NULL OR c.buried_until < ?)
             AND (
               (c.state IN ('learning', 'relearn') AND {_learning_due_now_sql()})
               OR (c.state = 'review' AND c.due <= ?)
               OR (c.state = 'new' AND c.due <= ?)
             )
           GROUP BY c.deck_id, c.category, c.state""",
        (today, now, today, today, today),
    ).fetchall()

    # 2. Future learning cards grouped by (deck_id, category)
    #    Mirror of _learning_due_now_sql() — a card that is not due now must
    #    land here, or the 1d/3d steps would vanish from both counts overnight.
    future_rows = conn.execute(
        f"""SELECT deck_id, category, COUNT(*) AS cnt
           FROM cards
           WHERE state IN ('learning', 'relearn')
             AND NOT {_learning_due_now_sql('')}
             AND deleted_at IS NULL
             AND (buried_until IS NULL OR buried_until < ?)
           GROUP BY deck_id, category""",
        (now, today, today),
    ).fetchall()

    # 2b. Of those, the ones coming back before tomorrow's cutoff (#844) — the
    #     Again cards the badge must show, unlike the 1d/3d steps.
    soon_rows = conn.execute(
        f"""SELECT deck_id, category, COUNT(*) AS cnt
           FROM cards
           WHERE state IN ('learning', 'relearn')
             AND {_learning_due_soon_sql('')}
             AND deleted_at IS NULL
             AND (buried_until IS NULL OR buried_until < ?)
           GROUP BY deck_id, category""",
        (now, _tomorrow_cutoff(), today),
    ).fetchall()

    # 3. New cards introduced today grouped by (deck_id, category)
    new_today_rows = conn.execute(
        """SELECT c.deck_id, c.category, COUNT(DISTINCT c.id) AS cnt
           FROM cards c
           WHERE EXISTS (
               SELECT 1 FROM review_log rl
               WHERE rl.card_id = c.id
                 AND rl.reviewed_at >= ?
                 AND rl.reviewed_at < ?
           )
           AND NOT EXISTS (
               SELECT 1 FROM review_log rl
               WHERE rl.card_id = c.id
                 AND rl.reviewed_at < ?
           )
           GROUP BY c.deck_id, c.category""",
        (today, today_end, today),
    ).fetchall()

    # 4. new_per_day limit per (deck_id, category) with category overrides
    limit_rows = conn.execute(
        """SELECT d.id AS deck_id, d.category,
                  COALESCE(pco.new_per_day, p.new_per_day, 20) AS new_per_day
           FROM decks d
           JOIN deck_presets p ON p.id = d.preset_id
           LEFT JOIN preset_category_overrides pco
             ON pco.preset_id = d.preset_id AND pco.category = d.category
           WHERE d.deleted_at IS NULL""",
    ).fetchall()

    # 5. Suspension flags: all cards suspended per (deck_id, category)?
    susp_rows = conn.execute(
        """SELECT c.deck_id, c.category,
                  COUNT(*) AS total,
                  SUM(CASE WHEN c.state = 'suspended' THEN 1 ELSE 0 END) AS suspended_count
           FROM cards c
           JOIN entries e ON e.id = c.word_id
           WHERE c.deleted_at IS NULL
             AND e.note_type != 'sentence'
           GROUP BY c.deck_id, c.category""",
    ).fetchall()

    conn.close()

    # Build counts dict
    counts: dict[tuple, dict] = {}
    for row in due_rows:
        key = (row["deck_id"], row["category"])
        if key not in counts:
            counts[key] = {"new_raw": 0, "learning": 0, "review": 0,
                           "learning_future": 0, "learning_soon": 0}
        s = row["state"]
        if s in ("learning", "relearn"):
            counts[key]["learning"] += row["cnt"]
        elif s == "review":
            counts[key]["review"] += row["cnt"]
        elif s == "new":
            counts[key]["new_raw"] += row["cnt"]

    for row in future_rows:
        key = (row["deck_id"], row["category"])
        if key not in counts:
            counts[key] = {"new_raw": 0, "learning": 0, "review": 0,
                           "learning_future": 0, "learning_soon": 0}
        counts[key]["learning_future"] = row["cnt"]

    for row in soon_rows:
        key = (row["deck_id"], row["category"])
        if key not in counts:
            counts[key] = {"new_raw": 0, "learning": 0, "review": 0,
                           "learning_future": 0, "learning_soon": 0}
        counts[key]["learning_soon"] = row["cnt"]

    new_today: dict[tuple, int] = {
        (r["deck_id"], r["category"]): r["cnt"] for r in new_today_rows
    }
    new_limits: dict[tuple, int] = {
        (r["deck_id"], r["category"]): r["new_per_day"] for r in limit_rows
    }

    # Future-dated daily decks are locked: force their counts to zero so they
    # neither display due cards nor contribute to parent aggregation.
    locked = get_locked_deck_ids()

    # Disabled categories count as zero everywhere (deck badges, parent
    # aggregation) without touching the cards themselves.
    cat_disabled = {cat: category_disabled_deck_ids(cat) for cat in _CATEGORY_ENABLED_COLUMN}

    for key, c in counts.items():
        if key[0] in locked or key[0] in cat_disabled.get(key[1], ()):
            c["new_raw"] = 0
            c["learning"] = 0
            c["review"] = 0
            c["learning_future"] = 0
            c["learning_soon"] = 0
        limit = new_limits.get(key, 20)
        done = new_today.get(key, 0)
        c["new"] = min(c.pop("new_raw", 0), max(0, limit - done))

    # Build suspension flags dict
    susp_flags: dict[tuple, bool] = {}
    for row in susp_rows:
        key = (row["deck_id"], row["category"])
        total = row["total"] or 0
        suspended = row["suspended_count"] or 0
        susp_flags[key] = total > 0 and total == suspended

    return counts, susp_flags


def due_notification_status() -> dict:
    """Decide whether today's leftover reviews can be finished in one sitting
    right now — the trigger for the reminder email (issue #701).

    After Daniel clears the queue, the cards he rated Again are still sitting in
    the learning steps (1m 10m 1d 3d) and come back in batches. Notifying on the
    first one to return would just send him back for two cards and another wait,
    so the reminder waits until nothing is pending anymore:

      due_now      learning/relearn cards due right now — the leftovers.
      later_today  learning/relearn cards coming back before tomorrow's day
                   cutoff. Any of these pending means "not yet". Cards on the
                   1d/3d steps are due after the cutoff and are deliberately
                   excluded — they belong to a later day, and waiting for them
                   would mean the reminder never fires.
      other_due    new + review cards still due today. If any are left the queue
                   never actually hit zero, so there is no session to finish.

    Counts reuse count_due_all_decks(), which already applies the new-card daily
    limit and zeroes out locked daily decks and disabled categories — a
    hand-rolled COUNT(*) here would disagree with the badges in the UI.
    """
    counts, _ = count_due_all_decks()
    due_now = sum(c["learning"] for c in counts.values())
    other_due = sum(c["new"] + c["review"] for c in counts.values())

    now = datetime.now().isoformat(timespec="seconds")
    # Tomorrow's cutoff as a full ISO datetime: cards.due holds datetimes for
    # learning cards, so comparing against a bare date string would place
    # everything due between midnight and the cutoff on the wrong day.
    tomorrow_cutoff = _tomorrow_cutoff()

    lock_clause, lock_params = _locked_exclusion()
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM cards WHERE "
        # _learning_due_soon_sql(), not a bare `due > ? AND due < ?`: the plain
        # string comparison also matched the 1d/3d steps, whose due is a bare
        # date ('2026-08-17' < '2026-08-17T04:00:00' is true). Those belong to
        # tomorrow — counting them here meant later_today was almost never 0 and
        # the reminder practically never fired (#844).
        f"state IN ('learning', 'relearn') AND {_learning_due_soon_sql('')} "
        "AND deleted_at IS NULL "
        "AND (buried_until IS NULL OR buried_until < date('now'))"
        + lock_clause
        + f" AND NOT {_CATEGORY_DISABLED_SQL}",
        [now, tomorrow_cutoff, *lock_params],
    ).fetchone()
    conn.close()
    later_today = row["cnt"] or 0

    return {
        "due_now": due_now,
        "later_today": later_today,
        "other_due": other_due,
        "ready": due_now > 0 and later_today == 0 and other_due == 0,
    }


def get_deck_all_suspended(deck_id: int) -> bool:
    """Return True if ALL non-sentence cards in deck and all descendant decks are suspended."""
    conn = get_db()
    row = conn.execute(
        """WITH RECURSIVE descendants AS (
             SELECT id FROM decks WHERE id = ?
             UNION ALL
             SELECT d.id FROM decks d JOIN descendants p ON d.parent_id = p.id
           )
           SELECT COUNT(*) as total,
                  SUM(CASE WHEN c.state = 'suspended' THEN 1 ELSE 0 END) as suspended_count
           FROM cards c
           JOIN entries w ON w.id = c.word_id
           JOIN descendants des ON c.deck_id = des.id
           WHERE c.deleted_at IS NULL
             AND w.note_type != 'sentence'""",
        (deck_id,),
    ).fetchone()
    conn.close()
    total = row["total"] or 0
    suspended = row["suspended_count"] or 0
    return total > 0 and total == suspended


def toggle_deck_all_suspension(deck_id: int) -> dict:
    """Toggle ALL non-sentence cards in deck and all descendant decks."""
    conn = get_db()
    deck_rows = conn.execute(
        """WITH RECURSIVE descendants AS (
             SELECT id FROM decks WHERE id = ?
             UNION ALL
             SELECT d.id FROM decks d JOIN descendants p ON d.parent_id = p.id
           )
           SELECT id FROM descendants""",
        (deck_id,),
    ).fetchall()
    deck_ids = [r["id"] for r in deck_rows]
    placeholders = ",".join("?" * len(deck_ids))
    active_row = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM cards c
            JOIN entries w ON w.id = c.word_id
            WHERE c.deck_id IN ({placeholders})
              AND c.state != 'suspended'
              AND c.deleted_at IS NULL
              AND w.note_type != 'sentence'""",
        deck_ids,
    ).fetchone()
    has_active = active_row["cnt"] > 0
    if has_active:
        conn.execute(
            f"""UPDATE cards SET pre_suspend_state=state, state='suspended'
                WHERE deck_id IN ({placeholders})
                  AND state != 'suspended'
                  AND deleted_at IS NULL
                  AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            deck_ids,
        )
    else:
        conn.execute(
            f"""UPDATE cards SET state=COALESCE(pre_suspend_state, 'new'), pre_suspend_state=NULL
                WHERE deck_id IN ({placeholders})
                  AND state = 'suspended'
                  AND deleted_at IS NULL
                  AND word_id IN (SELECT id FROM entries WHERE note_type != 'sentence')""",
            deck_ids,
        )
    conn.commit()
    conn.close()
    return {"all_suspended": not has_active}


def get_card_calendar_data(card_id: int) -> dict:
    """Return past review history and future due dates for all category cards of the same word."""
    conn = get_db()

    word_row = conn.execute("SELECT word_id FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not word_row:
        conn.close()
        return {"history": [], "future": []}
    word_id = word_row["word_id"]

    history = conn.execute(
        """SELECT DATE(rl.reviewed_at) AS date, rl.rating, c.category
           FROM review_log rl
           JOIN cards c ON c.id = rl.card_id
           WHERE c.word_id = ?
           ORDER BY rl.reviewed_at""",
        (word_id,),
    ).fetchall()

    future = conn.execute(
        """SELECT MAX(DATE(c.due), DATE('now')) AS due, c.category, c.state
           FROM cards c
           WHERE c.word_id = ?
             AND c.state NOT IN ('suspended')
             AND c.deleted_at IS NULL""",
        (word_id,),
    ).fetchall()

    conn.close()
    return {
        "history": [{"date": r["date"], "rating": r["rating"], "category": r["category"]} for r in history],
        "future":  [{"due": r["due"][:10], "category": r["category"], "state": r["state"]} for r in future],
    }


def _parse_step_minutes(steps_str: str) -> list[float]:
    """Parse a learning/relearning step string to minutes.

    Mirrors srs._parse_steps: plain numbers and "m" are minutes, "d" is days
    (×1440). Malformed tokens are skipped; empty input falls back to [1].
    """
    out = []
    for s in str(steps_str).strip().split():
        try:
            if s.endswith("d"):
                out.append(float(s[:-1]) * 1440)
            elif s.endswith("m"):
                out.append(float(s[:-1]))
            else:
                out.append(float(s))
        except ValueError:
            continue
    return out or [1.0]


def _replay_schedule_step(state, step, interval, ease, rating, P):
    """Pure SM-2 replay of one review (no fuzz, no DB).

    Returns (new_state, new_step, new_interval, new_ease, gap_days) where
    gap_days is the scheduled delay until the card's next due — i.e. the
    interval the scheduler assigned. Learning/relearn steps are sub-day
    (minutes/1440); review intervals are whole days.
    """
    ls, rs = P["learning_steps"], P["relearning_steps"]

    if state in ("new", "learning"):
        if rating == 4:                         # Easy → graduate
            return ("review", 0, P["easy_interval"], ease, float(P["easy_interval"]))
        if rating == 1:                         # Again → back to step 0
            return ("learning", 0, interval, ease, ls[0] / 1440)
        if rating == 2:                         # Hard → repeat step
            if step == 0 and len(ls) > 1:
                delay = (ls[0] + ls[1]) / 2
            elif len(ls) == 1:
                delay = ls[0] * 1.5
            else:
                delay = ls[step]
            return ("learning", step, interval, ease, delay / 1440)
        if step >= len(ls) - 1:                 # Good on last step → graduate
            return ("review", 0, P["graduating_interval"], ease, float(P["graduating_interval"]))
        return ("learning", step + 1, interval, ease, ls[step + 1] / 1440)

    if state == "review":
        if rating == 1:                         # Again → lapse + relearn
            ne = max(1.3, ease - 0.20)
            iv = max(P["minimum_interval"], int(interval * 0.5))
            return ("relearn", 0, iv, ne, rs[0] / 1440)
        if rating == 2:                         # Hard
            ne = max(1.3, ease - 0.15)
            iv = max(P["minimum_interval"], int(interval * 1.2))
            return ("review", 0, iv, ne, float(iv))
        if rating == 3:                         # Good
            iv = max(P["minimum_interval"], int(interval * ease))
            return ("review", 0, iv, ease, float(iv))
        ne = ease + 0.15                        # Easy
        iv = max(P["minimum_interval"], int(interval * ne * 1.3))
        return ("review", 0, iv, ne, float(iv))

    if state == "relearn":
        if rating == 1:
            return ("relearn", 0, interval, ease, rs[0] / 1440)
        if rating == 2:
            if step == 0 and len(rs) > 1:
                delay = (rs[0] + rs[1]) / 2
            elif len(rs) == 1:
                delay = rs[0] * 1.5
            else:
                delay = rs[step]
            return ("relearn", step, interval, ease, delay / 1440)
        if rating == 4:                         # Easy → review
            iv = max(P["minimum_interval"], int(interval * ease))
            return ("review", 0, iv, ease, float(iv))
        if step >= len(rs) - 1:                 # Good on last step → review
            iv = max(P["minimum_interval"], interval)
            return ("review", 0, iv, ease, float(iv))
        return ("relearn", step + 1, interval, ease, rs[step + 1] / 1440)

    return (state, step, interval, ease, float(interval))


def get_card_timeline_data(card_id: int) -> dict:
    """Per-card interval timeline for all category cards of the same word.

    The y-value of each point is the *scheduled* SRS interval the card was
    given after that review (whole days for review cards, sub-day for
    learning/relearn steps), reconstructed by replaying the full SM-2
    scheduler over the rating history (review_log stores ratings, not
    intervals). This is what Anki's card-info graph shows and — unlike the
    raw elapsed time between reviews — diverges per card by ease/rating even
    when several cards are studied together on the same days.

    Recorded review_log.state values recalibrate the replayed state, and the
    card's current state pins the end. Each card gets a final dashed point at
    its real due date with the current interval.
    """
    from .stats import _EVOLUTION_STATES

    conn = get_db()
    word_row = conn.execute("SELECT word_id FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not word_row:
        conn.close()
        return {"cards": []}

    cards = conn.execute(
        """SELECT c.id, c.deck_id, c.category, c.state, c.pre_suspend_state,
                  c.interval, c.due
           FROM cards c
           WHERE c.word_id = ? AND c.deleted_at IS NULL
           ORDER BY c.category""",
        (word_row["word_id"],),
    ).fetchall()

    result = []
    for c in cards:
        rows = conn.execute(
            """SELECT datetime(reviewed_at, 'localtime') AS at, rating, state
               FROM review_log WHERE card_id = ? ORDER BY reviewed_at""",
            (c["id"],),
        ).fetchall()

        preset = get_preset_for_deck(c["deck_id"], c["category"]) or {}
        P = {
            "learning_steps":      _parse_step_minutes(preset.get("learning_steps", "1 10")),
            "relearning_steps":    _parse_step_minutes(preset.get("relearning_steps", "10")),
            "graduating_interval": preset.get("graduating_interval", 1),
            "easy_interval":       preset.get("easy_interval", 4),
            "minimum_interval":    preset.get("minimum_interval", 1),
        }

        final = c["state"]
        if final == "suspended":
            final = c["pre_suspend_state"]

        points = []
        state, step, interval, ease = "new", 0, 0, 2.5
        for r in rows:
            if r["state"] in _EVOLUTION_STATES and r["state"] != state:
                state, step = r["state"], 0  # recalibrate from recorded truth
            state, step, interval, ease, gap = _replay_schedule_step(
                state, step, interval, ease, r["rating"], P)
            points.append({
                "at": r["at"],
                "gap": round(gap, 3),
                "rating": r["rating"],
                "state": state,
            })
        if points and final in _EVOLUTION_STATES:
            points[-1]["state"] = final  # pin the end to the card's real state

        # Final dashed point: the card's real current interval, plotted at its due date
        scheduled = None
        if points and c["state"] in ("learning", "relearn", "review"):
            due_raw = c["due"]
            due_dt = datetime.fromisoformat(due_raw) if len(due_raw) > 10 \
                else datetime.fromisoformat(due_raw + "T04:00:00")
            if c["state"] == "review":
                gap = float(c["interval"])
            else:
                last_dt = datetime.fromisoformat(rows[-1]["at"])
                gap = max(0.0, (due_dt - last_dt).total_seconds() / 86400)
            scheduled = {
                "at": due_dt.isoformat(sep=" "),
                "gap": round(gap, 3),
                "state": c["state"],
            }

        result.append({
            "card_id": c["id"],
            "category": c["category"],
            "state": c["state"],
            "interval": c["interval"],
            "due": c["due"],
            "points": points,
            "scheduled": scheduled,
        })

    conn.close()
    return {"cards": result}


def get_session_timelines(card_ids: list[int]) -> dict:
    """Interval timelines for an explicit list of cards (one review session).

    Like get_card_timeline_data but keyed by card id (not word) and tagged with
    each card's word label, so the session summary graph can show which card a
    line is and link to it. Cards with no reviews yet are skipped.
    """
    from .stats import _EVOLUTION_STATES

    if not card_ids:
        return {"cards": []}

    conn = get_db()
    ph = ",".join("?" * len(card_ids))
    cards = conn.execute(
        f"""SELECT c.id, c.word_id, c.deck_id, c.category, c.state,
                   c.pre_suspend_state, c.interval, c.due,
                   w.word_zh, w.pinyin
            FROM cards c JOIN entries w ON w.id = c.word_id
            WHERE c.id IN ({ph}) AND c.deleted_at IS NULL""",
        card_ids,
    ).fetchall()

    result = []
    for c in cards:
        rows = conn.execute(
            """SELECT datetime(reviewed_at, 'localtime') AS at, rating, state
               FROM review_log WHERE card_id = ? ORDER BY reviewed_at""",
            (c["id"],),
        ).fetchall()
        if not rows:
            continue

        preset = get_preset_for_deck(c["deck_id"], c["category"]) or {}
        P = {
            "learning_steps":      _parse_step_minutes(preset.get("learning_steps", "1 10")),
            "relearning_steps":    _parse_step_minutes(preset.get("relearning_steps", "10")),
            "graduating_interval": preset.get("graduating_interval", 1),
            "easy_interval":       preset.get("easy_interval", 4),
            "minimum_interval":    preset.get("minimum_interval", 1),
        }

        final = c["state"]
        if final == "suspended":
            final = c["pre_suspend_state"]

        points = []
        state, step, interval, ease = "new", 0, 0, 2.5
        for r in rows:
            if r["state"] in _EVOLUTION_STATES and r["state"] != state:
                state, step = r["state"], 0
            state, step, interval, ease, gap = _replay_schedule_step(
                state, step, interval, ease, r["rating"], P)
            points.append({"at": r["at"], "gap": round(gap, 3),
                           "rating": r["rating"], "state": state})
        if points and final in _EVOLUTION_STATES:
            points[-1]["state"] = final

        scheduled = None
        if points and c["state"] in ("learning", "relearn", "review"):
            due_raw = c["due"]
            due_dt = datetime.fromisoformat(due_raw) if len(due_raw) > 10 \
                else datetime.fromisoformat(due_raw + "T04:00:00")
            if c["state"] == "review":
                gap = float(c["interval"])
            else:
                last_dt = datetime.fromisoformat(rows[-1]["at"])
                gap = max(0.0, (due_dt - last_dt).total_seconds() / 86400)
            scheduled = {"at": due_dt.isoformat(sep=" "), "gap": round(gap, 3),
                         "state": c["state"]}

        result.append({
            "card_id":  c["id"],
            "word_id":  c["word_id"],
            "word_zh":  c["word_zh"],
            "pinyin":   c["pinyin"],
            "category": c["category"],
            "state":    c["state"],
            "interval": c["interval"],
            "due":      c["due"],
            "points":   points,
            "scheduled": scheduled,
        })

    conn.close()
    return {"cards": result}



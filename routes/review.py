import logging
import threading
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException

import ai
import database
import review_notify
import srs
import tts
from . import tasks
from .utils import leaf_ids, queue_mgr as _queue_mgr, ai_disabled

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory undo stack: list of {card_before, log_id, queue_key, ...}
_undo_stack: list[dict] = []


# ---------------------------------------------------------------------------
# "Again" → background single-sentence regeneration
# ---------------------------------------------------------------------------

def _attach_again_sentence(card: dict | None) -> dict | None:
    """If a fresh sentence was regenerated for this word today (from a previous
    Again, or the "new sentence" requeue button), attach it as
    card["again_sentence"] so the frontend shows it instead of the old story one.

    Not gated on card state: the requeue button leaves scheduling untouched, so
    the card can be in any state when it reappears with its new sentence."""
    if (card and card.get("note_type") != "sentence" and card.get("word_id")):
        today = database.anki_today().isoformat()
        again = database.get_again_sentence_for_word(card["word_id"], today)
        if again:
            card["again_sentence"] = again
            logger.debug("again-regen  HIT word=%s — showing regenerated sentence",
                         card.get("word_zh"))
    return card


def again_regen_enabled() -> bool:
    """User switch for the Again → regenerate behaviour (issue #714). Default on,
    so an untouched install keeps the behaviour it always had.

    Only the *automatic* trigger on a rating is gated. The "New sentence" button
    (`/api/review/requeue`) asks for a regeneration explicitly and must keep
    working regardless — a button silently swallowed by a global switch is a
    broken button."""
    return database.get_app_setting("again_regen_enabled", "1") == "1"


def _spawn_again_regen(card: dict) -> None:
    """Fire-and-forget: regenerate one fresh sentence for this word in the
    background so the card shows something new when it reappears (~1-10 min)."""
    if ai_disabled() or card.get("note_type") == "sentence" or not card.get("word_id"):
        return
    # All three vocab categories use story sentences (listening audio, reading text,
    # creating cloze/word-bank), so regenerate a fresh sentence for any of them.
    if card.get("category") not in ("listening", "reading", "creating"):
        return

    logger.info("again-regen  TRIGGER word=%s cat=%s — scheduling background regen",
                card.get("word_zh"), card.get("category"))

    # Visible in the header task indicator (#821) — this is the one background
    # job in the app that publishes no progress state of its own.
    task_id = f"sentence:{card['word_id']}:{card['category']}"
    tasks.register(task_id, "sentence",
                   f"New sentence · {card.get('word_zh') or card['word_id']}")

    def _run() -> None:
        try:
            from .story import generate_sentence_for_word
            today = database.anki_today().isoformat()
            # Reuse the deck story's generation settings (mode/topic/grammar/model;
            # a random chapter for kahneman) so the new sentence matches its style
            # instead of always being a plain story sentence.
            gen_params = database.get_story_gen_params_for_word(card["word_id"], today)
            if (gen_params or {}).get("mode") == "briefing":
                # briefing sentences are part of a connected news summary — a
                # standalone regenerated sentence would break that context, and
                # the queue-ordering feature (issue #454) means the card still
                # reappears at its original position in the summary anyway.
                logger.info("again-regen  word=%s mode=briefing — skipping regen "
                            "(briefing cards keep their original summary sentence)",
                            card.get("word_zh"))
                return
            sentence = generate_sentence_for_word(card, gen_params)
            if not sentence:
                return
            database.store_again_sentence(card["deck_id"], card["word_id"], sentence, today)
            logger.info("again-regen  word=%s mode=%s → new sentence stored",
                        card.get("word_zh"), (gen_params or {}).get("mode", "story"))
            try:
                tts.preload(sentence.get("sentence_zh", ""), lang=database.get_deck_lang(card["deck_id"]))
            except Exception:
                pass
        except Exception as e:
            logger.warning("again-regen failed for word=%s: %s", card.get("word_zh"), e)
        finally:
            tasks.finish(task_id)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Queue key / build-function helpers
# ---------------------------------------------------------------------------

def _order_by_story(cards: list[dict], story_deck_id: int | None,
                    story_category: str | None, lang: str | None) -> list[dict]:
    """Reorder due cards to match today's News-flow-style story (briefing/news/
    paste — issue #454): word_id → story_sentences.position, via
    database.get_story_position_map(). `sorted` is stable, so cards whose word
    isn't in the story keep their existing interleaved order within their group;
    database.story_sort_key() decides the groups — learning/review leftovers the
    story doesn't cover come first, new cards it doesn't cover come last (#732).
    Without that split, a story regenerated mid-session (it only covers the words
    due at generation time) pushed every leftover behind all new cards.

    Single-category sessions that don't find a per-category story also try the
    'unified' story — a unified story (from a mixed/"All" review session)
    drives ordering for per-category sessions too, since it covers every word
    due that day regardless of category.
    """
    if not story_deck_id or not story_category or not cards:
        return cards
    today = database.anki_today().isoformat()
    pos = database.get_story_position_map(story_deck_id, story_category, today, lang)
    if not pos and story_category != "unified":
        pos = database.get_story_position_map(story_deck_id, "unified", today, lang)
    if not pos:
        return cards
    ordered = sorted(cards, key=lambda c: database.story_sort_key(c, pos))
    # Mark the result so QueueManager._build() knows story order is in effect:
    # already-due intraday learning cards must NOT jump ahead of it (issue #462).
    for c in ordered:
        c["_story_ordered"] = True
    return ordered


def _key_and_build(
    *,
    ids: list[int] | None = None,
    category: str | None = None,
    root_deck_id: int | None = None,
    deck_id: int | None = None,
    parent_for_multi: int | None = None,
    lang: str | None = None,
):
    """Return (queue_key, build_fn) for the given review context.

    `lang` is appended to every key tuple so 'All zh' and 'All fr' (or any other
    lang split of the same deck/id set) never share a cached in-memory queue.

    build_fn's output is reordered to match the active News-flow story's
    sentence order (issue #454, see _order_by_story) — this only runs once per
    Anki day / queue invalidation, so the extra position-map lookup is cheap.
    """
    if root_deck_id:
        key = ("any_cat", root_deck_id, lang)

        def build_fn():
            cards = database.get_due_cards_any_cat(root_deck_id, lang=lang)
            return _order_by_story(cards, root_deck_id, "unified", lang)
    elif ids and len(ids) > 1:
        key = ("multi", tuple(sorted(ids)), category, lang)
        _root = parent_for_multi

        def build_fn():
            cards = database.get_due_cards_multi(ids, category, root_deck_id=_root)
            return _order_by_story(cards, _root, category, lang)
    else:
        actual = (ids[0] if ids else deck_id)
        key = ("single", actual, category, lang)

        def build_fn():
            cards = database.get_due_cards(actual, category)
            return _order_by_story(cards, actual, category, lang)
    return key, build_fn


def _next_card_from_queue(key, build_fn) -> dict | None:
    """Ask the queue manager for the next card ID, then fetch the full card.

    Queues are built once per Anki day, so a card can be buried/suspended/
    deleted in the DB after the queue was built (e.g. bury_siblings from a
    review in another category, issue #573).  Such stale entries are dropped
    here instead of being served.
    """
    today = database.anki_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    while True:
        card_id = _queue_mgr.get_next(key, build_fn, today, now)
        if card_id is None:
            return None
        card = database.get_card(card_id)
        if (card is None or card["state"] == "suspended"
                or (card.get("buried_until") or "") >= today):
            logger.debug(
                "[review] skipping stale queue entry #%s (%s)",
                card_id,
                "deleted" if card is None else
                f"state={card['state']} buried_until={card.get('buried_until')}",
            )
            _queue_mgr.discard_everywhere([card_id])
            continue
        card["intervals"] = srs.preview_intervals(card)
        card["fsrs"] = srs.explain_card(card)
        _attach_again_sentence(card)
        return card


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/today/{deck_id}/{category}")
def get_today(deck_id: int, category: str, lang: str | None = None):
    ids = leaf_ids(deck_id, category, lang=lang)
    key, build_fn = _key_and_build(ids=ids, category=category, parent_for_multi=deck_id, lang=lang)

    card = _next_card_from_queue(key, build_fn)

    if len(ids) == 1:
        counts = database.count_due(ids[0], category)
    else:
        counts = database.count_due_multi(ids, category, root_deck_id=deck_id)

    parent_id = database.get_parent_deck_id(deck_id)
    counts["by_cat"] = database.count_due_by_category(parent_id or deck_id, lang=lang)
    return {"card": card, "counts": counts}


@router.get("/api/today-unfinished")
def get_today_unfinished(scope: str = "unfinished", lang: str | None = None):
    card = database.get_next_unfinished_card(scope, lang=lang)
    if card:
        card["intervals"] = srs.preview_intervals(card)
        card["fsrs"] = srs.explain_card(card)
        _attach_again_sentence(card)
    return {"card": card, "counts": database.count_unfinished(scope, lang=lang)}


@router.get("/api/today-unfinished-decks")
def get_today_unfinished_decks(scope: str = "unfinished", lang: str | None = None):
    return database.get_unfinished_deck_categories(scope, lang=lang)


@router.get("/api/today-mixed/{deck_id}")
def get_today_mixed(deck_id: int, lang: str | None = None):
    key, build_fn = _key_and_build(root_deck_id=deck_id, lang=lang)
    card = _next_card_from_queue(key, build_fn)
    counts = database.count_due_any_cat(deck_id, lang=lang)
    counts["by_cat"] = database.count_due_by_category(deck_id, lang=lang)
    return {"card": card, "counts": counts}


@router.post("/api/review")
def submit_review(card_id: int, rating: int, user_response: str | None = None,
                  root_deck_id: int | None = None, unfinished_mode: bool = False,
                  parent_deck_id: int | None = None, duration_ms: int | None = None,
                  next_note: str | None = None, unfinished_scope: str = "unfinished",
                  lang: str | None = None):
    _t0 = time.perf_counter()
    card_before = database.get_card(card_id)
    # Persist the free-text "note for next time" the user left on this card
    # (None means "leave the existing note untouched"; "" clears it).
    if next_note is not None:
        database.set_card_note(card_id, next_note)
    updated, log_id = srs.apply_review(card_id, rating, user_response=user_response,
                                       duration_ms=duration_ms)
    deck_id = updated["deck_id"]
    cat     = updated["category"]

    # Rated Again → regenerate a fresh sentence for this word in the background,
    # so it shows something new when the card reappears in a few minutes.
    # Switchable (issue #714): off means the card keeps its original sentence,
    # which is what you want when the sentence was fine and only the recall failed.
    if rating == 1:
        if again_regen_enabled():
            _spawn_again_regen(card_before)
        else:
            logger.debug("again-regen  word=%s — skipped, switch off",
                         (card_before or {}).get("word_zh"))

    # Apply sibling repulsion: push sibling due dates that are too close to today.
    # Only kicks in when the reviewed card has a long enough interval (> sibling_separation),
    # leaving the initial staggered-introduction phase unaffected.
    preset = database.get_preset_for_deck(deck_id)
    sibling_sep    = preset.get("sibling_separation", 3)
    sibling_factor = preset.get("sibling_factor", 0.2)
    new_interval   = updated.get("interval", 0)
    logger.debug(
        "[sibling_repulsion] triggering for card=#%d  new_interval=%d  sep=%d  factor=%.2f",
        card_id, new_interval, sibling_sep, sibling_factor,
    )
    database.apply_sibling_repulsion(card_id, new_interval, sibling_sep, sibling_factor)

    # Snapshot sibling buried_until values BEFORE burying so undo can restore them
    siblings_before = database.get_sibling_cards(card_id)
    siblings_snapshot = [
        {"id": s["id"], "buried_until": s["buried_until"]}
        for s in siblings_before
    ]

    bury_new, bury_review, bury_learning = database.resolve_bury_flags(preset)
    logger.debug(
        "[preset] deck=%d  bury_quick_mode=%s → new=%s review=%s learning=%s\n"
        "  new_per_day=%d  reviews_per_day=%d  learning_steps=%r\n"
        "  graduating_interval=%d  easy_interval=%d  relearning_steps=%r\n"
        "  new_review_order=%s  new_gather_order=%s  new_sort_order=%s\n"
        "  interday_learning_review_order=%s  review_sort_order=%s\n"
        "  leech_threshold=%d  leech_action=%s  category_order=%s",
        deck_id,
        preset.get("bury_quick_mode"), bury_new, bury_review, bury_learning,
        preset.get("new_per_day", 0), preset.get("reviews_per_day", 0), preset.get("learning_steps", ""),
        preset.get("graduating_interval", 0), preset.get("easy_interval", 0), preset.get("relearning_steps", ""),
        preset.get("new_review_order"), preset.get("new_gather_order"), preset.get("new_sort_order"),
        preset.get("interday_learning_review_order"), preset.get("review_sort_order"),
        preset.get("leech_threshold", 0), preset.get("leech_action"), preset.get("category_order"),
    )
    database.bury_siblings(
        updated["word_id"], cat,
        bury_new=bury_new,
        bury_review=bury_review,
        bury_learning=bury_learning,
    )

    # IDs that bury_siblings() just newly buried — needed to purge them from
    # the in-memory queue so the queue stays consistent with the DB.
    today_str = database.anki_today().isoformat()
    was_buried = {s["id"] for s in siblings_before
                  if s.get("buried_until") is not None and s.get("buried_until") >= today_str}
    siblings_after = database.get_sibling_cards(card_id)
    newly_buried = [
        s["id"] for s in siblings_after
        if s.get("buried_until") == today_str and s["id"] not in was_buried
    ]
    logger.debug(
        "[review] submit card=#%d word=%s cat=%s state=%s→%s\n"
        "  bury_flags: new=%s review=%s learning=%s\n"
        "  siblings_before: %s\n"
        "  siblings_after:  %s\n"
        "  newly_buried:    %s",
        card_id, card_before.get("word_zh"), cat,
        card_before.get("state"), updated.get("state"),
        bury_new, bury_review, bury_learning,
        [(s["id"], s.get("category"), s.get("buried_until")) for s in siblings_before],
        [(s["id"], s.get("category"), s.get("buried_until")) for s in siblings_after],
        newly_buried,
    )

    # Determine queue key for this review context
    if unfinished_mode:
        queue_key = None
    elif root_deck_id:
        queue_key, build_fn = _key_and_build(root_deck_id=root_deck_id, lang=lang)
    elif parent_deck_id:
        ids = leaf_ids(parent_deck_id, cat, lang=lang)
        queue_key, build_fn = _key_and_build(ids=ids, category=cat, parent_for_multi=parent_deck_id, lang=lang)
    else:
        queue_key, build_fn = _key_and_build(deck_id=deck_id, category=cat, lang=lang)

    _undo_stack.append({
        "card_before":        card_before,
        "log_id":             log_id,
        "queue_key":          queue_key,
        "root_deck_id":       root_deck_id,
        "parent_deck_id":     parent_deck_id,
        "unfinished_mode":    unfinished_mode,
        "unfinished_scope":   unfinished_scope,
        "deck_id":            deck_id,
        "category":           cat,
        "siblings_snapshot":  siblings_snapshot,
    })

    if unfinished_mode:
        next_card = database.get_next_unfinished_card(unfinished_scope, lang=lang)
        if next_card:
            next_card["intervals"] = srs.preview_intervals(next_card)
            next_card["fsrs"] = srs.explain_card(next_card)
            _attach_again_sentence(next_card)
        counts = database.count_unfinished(unfinished_scope, lang=lang)
    elif root_deck_id:
        _queue_mgr.after_review(queue_key, card_id, updated, newly_buried)
        next_card = _next_card_from_queue(queue_key, build_fn)
        counts = database.count_due_any_cat(root_deck_id, lang=lang)
        counts["by_cat"] = database.count_due_by_category(root_deck_id, lang=lang)
    elif parent_deck_id:
        _queue_mgr.after_review(queue_key, card_id, updated, newly_buried)
        next_card = _next_card_from_queue(queue_key, build_fn)
        counts = database.count_due_multi(ids, cat, root_deck_id=parent_deck_id)
        counts["by_cat"] = database.count_due_by_category(parent_deck_id, lang=lang)
    else:
        _queue_mgr.after_review(queue_key, card_id, updated, newly_buried)
        next_card = _next_card_from_queue(queue_key, build_fn)
        counts = database.count_due(deck_id, cat)
        parent_id = database.get_parent_deck_id(deck_id)
        counts["by_cat"] = database.count_due_by_category(parent_id or deck_id, lang=lang)

    rating_label = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}.get(rating, str(rating))
    ivl = updated["interval"]
    ivl_str = f"{ivl}d" if ivl >= 1 else f"{round(ivl * 1440)}m"
    pinyin = card_before.get("pinyin", "")
    pinyin_part = f" ({pinyin})" if pinyin else ""
    logger.info(
        "Card #%d %s%s  %s → %s  %s  due=%s  ivl=%s  ease=%.2f  lapses=%d",
        card_before["id"], card_before["word_zh"], pinyin_part,
        card_before["state"], updated["state"],
        rating_label, updated["due"], ivl_str,
        updated["ease"], updated["lapses"],
    )
    # The per-category totals below cost three extra count_due queries per review
    # purely for this log line — only compute them when DEBUG logging is on so a
    # normal review submit stays fast (issue #452).
    if logger.isEnabledFor(logging.DEBUG):
        cat_totals = {
            c: sum(database.count_due(deck_id, c).values())
            for c in ("listening", "reading", "creating")
        }
        logger.info(
            "Queue: %d lrn  %d rev  %d new  │ 听=%d  读=%d  创=%d",
            counts["learning"], counts["review"], counts["new"],
            cat_totals["listening"], cat_totals["reading"], cat_totals["creating"],
        )
    else:
        logger.info(
            "Queue: %d lrn  %d rev  %d new",
            counts["learning"], counts["review"], counts["new"],
        )
    if updated.get("state") == "suspended":
        logger.warning(
            "Card #%d %s SUSPENDED (lapses=%d)",
            card_before["id"], card_before["word_zh"], updated["lapses"],
        )
    state_from = card_before["state"]
    state_to   = updated["state"]
    # A card only counts as "learned" once its interval reaches learned_interval;
    # graduating to 'review' with a shorter interval is still learning.
    learned_threshold = updated.get("learned_interval", 4)
    is_learned = state_to == "review" and (updated.get("interval") or 0) >= learned_threshold
    transition = {
        "from":    state_from,
        "to":      state_to,
        "changed": state_from != state_to,
        "leech":   bool(updated.get("is_leech")) and state_to == "suspended",
        "learned": is_learned,
    }
    _elapsed_ms = (time.perf_counter() - _t0) * 1000
    if _elapsed_ms > 300:
        logger.warning("submit_review SLOW: %.0f ms (card=#%d)", _elapsed_ms, card_id)
    else:
        logger.info("submit_review: %.0f ms", _elapsed_ms)
    return {"next_card": next_card, "counts": counts, "transition": transition}


@router.get("/api/again-regen-enabled")
def get_again_regen_enabled():
    """Whether rating Again auto-regenerates the card's sentence (issue #714)."""
    return {"enabled": again_regen_enabled()}


@router.put("/api/again-regen-enabled")
def put_again_regen_enabled(body: dict):
    """Set the Again → regenerate switch (issue #714). body: {"enabled": bool}.
    Stored server-side because the regeneration itself runs there — a browser-only
    preference would leave the other devices (and the phone) doing something else."""
    enabled = bool(body.get("enabled"))
    database.set_app_setting("again_regen_enabled", "1" if enabled else "0")
    logger.info("again-regen-enabled  set to %s", enabled)
    return {"ok": True, "enabled": enabled}


@router.post("/api/review/requeue")
def requeue_card(card_id: int, root_deck_id: int | None = None,
                 parent_deck_id: int | None = None, unfinished_mode: bool = False,
                 unfinished_scope: str = "unfinished", delay_seconds: int = 60,
                 lang: str | None = None):
    """"New sentence" button: re-show this card ~delay_seconds later WITHOUT any
    scheduling change, and regenerate its sentence in the background.

    Unlike a rating this touches no SRS state (ease/interval/state/lapses/today's
    review count are all untouched). It mirrors /api/review's queue context so the
    card lands back in the same session queue. Returns {next_card, counts} so the
    frontend advances exactly as it does after a rating."""
    card = database.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    deck_id = card["deck_id"]
    cat = card["category"]

    # Background: regenerate one fresh sentence for this word (reuses Again infra).
    _spawn_again_regen(card)

    due = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")

    if unfinished_mode:
        # The unfinished virtual deck isn't backed by an in-memory queue, so there
        # is nothing to soft-requeue into; just advance (regen still happens).
        next_card = database.get_next_unfinished_card(unfinished_scope, lang=lang)
        if next_card:
            next_card["intervals"] = srs.preview_intervals(next_card)
            next_card["fsrs"] = srs.explain_card(next_card)
            _attach_again_sentence(next_card)
        counts = database.count_unfinished(unfinished_scope, lang=lang)
    elif root_deck_id:
        key, build_fn = _key_and_build(root_deck_id=root_deck_id, lang=lang)
        _queue_mgr.soft_requeue(key, card_id, due)
        next_card = _next_card_from_queue(key, build_fn)
        counts = database.count_due_any_cat(root_deck_id, lang=lang)
        counts["by_cat"] = database.count_due_by_category(root_deck_id, lang=lang)
    elif parent_deck_id:
        ids = leaf_ids(parent_deck_id, cat, lang=lang)
        key, build_fn = _key_and_build(ids=ids, category=cat, parent_for_multi=parent_deck_id, lang=lang)
        _queue_mgr.soft_requeue(key, card_id, due)
        next_card = _next_card_from_queue(key, build_fn)
        counts = database.count_due_multi(ids, cat, root_deck_id=parent_deck_id)
        counts["by_cat"] = database.count_due_by_category(parent_deck_id, lang=lang)
    else:
        key, build_fn = _key_and_build(deck_id=deck_id, category=cat, lang=lang)
        _queue_mgr.soft_requeue(key, card_id, due)
        next_card = _next_card_from_queue(key, build_fn)
        counts = database.count_due(deck_id, cat)
        parent_id = database.get_parent_deck_id(deck_id)
        counts["by_cat"] = database.count_due_by_category(parent_id or deck_id, lang=lang)

    logger.info("requeue  card=#%d word=%s cat=%s → re-show at %s (no scheduling change)",
                card_id, card.get("word_zh"), cat, due)
    return {"next_card": next_card, "counts": counts}


@router.post("/api/review/undo")
def undo_review():
    if not _undo_stack:
        raise HTTPException(status_code=404, detail="Nothing to undo")

    entry = _undo_stack.pop()

    cb = entry["card_before"]

    # Restore the card to its pre-review state
    database.update_card(
        cb["id"],
        state=cb["state"],
        due=cb["due"],
        step_index=cb["step_index"],
        interval=cb["interval"],
        ease=cb["ease"],
        repetitions=cb["repetitions"],
        lapses=cb["lapses"],
        probation=cb.get("probation", 0),
        stability=cb.get("stability"),
        difficulty=cb.get("difficulty"),
        last_review=cb.get("last_review"),
    )
    database.delete_review_log(entry["log_id"])

    # Restore siblings' buried_until to their pre-review values
    for sib in entry.get("siblings_snapshot", []):
        database.set_card_buried_until(sib["id"], sib["buried_until"])

    # Invalidate ALL queues: the restored card belongs to this queue, but the
    # un-buried siblings may sit in queues of other categories (issue #573).
    _queue_mgr.invalidate()

    # Return the restored card so the frontend can show it
    restored = database.get_card(cb["id"])
    restored["intervals"] = srs.preview_intervals(restored)
    restored["fsrs"] = srs.explain_card(restored)
    _attach_again_sentence(restored)

    deck_id         = entry["deck_id"]
    cat             = entry["category"]
    unfinished_mode = entry["unfinished_mode"]
    root_deck_id    = entry["root_deck_id"]
    parent_deck_id  = entry.get("parent_deck_id")

    if unfinished_mode:
        counts = database.count_unfinished(entry.get("unfinished_scope", "unfinished"))
    elif root_deck_id:
        counts = database.count_due_any_cat(root_deck_id)
    elif parent_deck_id:
        ids    = leaf_ids(parent_deck_id, cat)
        counts = database.count_due_multi(ids, cat)
    else:
        counts = database.count_due(deck_id, cat)

    logger.info("undo review for %s, restored state=%s (stack_size=%d)",
                restored["word_zh"], restored["state"], len(_undo_stack))
    return {"card": restored, "counts": counts, "stack_size": len(_undo_stack)}



@router.post("/api/cards/{card_id}/bury")
def bury_card(card_id: int):
    database.bury_card(card_id)
    return {"ok": True}


@router.post("/api/cards/{card_id}/unbury")
def unbury_card(card_id: int):
    database.unbury_card(card_id)
    return {"ok": True}


@router.get("/api/cards/{card_id}/calendar")
def get_card_calendar(card_id: int):
    return database.get_card_calendar_data(card_id)


@router.get("/api/cards/{card_id}/timeline")
def get_card_timeline(card_id: int):
    return database.get_card_timeline_data(card_id)


@router.post("/api/review/due-notify-check")
def due_notify_check(force: bool = False):
    """Run one review-reminder check (issue #701). Called by cron every few
    minutes; sends the mail only when today's leftovers are all due at once and
    nothing was sent yet today. Returns the verdict either way — a check that
    decides not to send is a normal outcome, not an error."""
    return review_notify.check_and_notify(force=force)


@router.post("/api/session-timelines")
def session_timelines(body: dict):
    """Interval timelines for the cards reviewed in one session (summary graph)."""
    ids = [int(i) for i in body.get("ids", [])]
    return database.get_session_timelines(ids)


@router.post("/api/sentence-question")
def sentence_question(body: dict):
    """Ask AI about the sentence currently showing on a card's back (#853).

    Single-turn, no follow-up — same shape as /api/dict/lookup. An empty
    question defaults to "is anything wrong with this sentence?" inside
    ai.ask_about_sentence(), which is also told to judge the sentence's own
    quality before answering — story generation occasionally produces
    awkward or outright wrong sentences, and this button exists to catch that.
    """
    sentence_zh = (body.get("sentence_zh") or "").strip()
    if not sentence_zh:
        raise HTTPException(400, "sentence_zh required")
    if ai_disabled():
        raise HTTPException(400, "AI is disabled")

    question = (body.get("question") or "").strip()
    word_zh = body.get("word_zh")
    lang = body.get("lang") or "zh"

    try:
        answer = ai.ask_about_sentence(sentence_zh, question=question, word_zh=word_zh, lang=lang)
    except Exception as e:
        logger.error("sentence_question failed for %r: %s", sentence_zh, e)
        raise HTTPException(500, str(e))

    return {"answer": answer}

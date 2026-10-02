import math
import random
from datetime import datetime, timedelta, time as dtime

import database
import fsrs


# When a card has a single learning step, "Hard" can't advance to a next step,
# so it repeats with a slower delay. We use ~1 day (instead of step×1.5) so a
# half-remembered new card comes back tomorrow rather than in minutes, while
# "Good" still graduates straight into FSRS.
LEARNING_HARD_SINGLE_STEP_MINUTES = 1440  # 1 day


# ---------------------------------------------------------------------------
# FSRS configuration helpers
# ---------------------------------------------------------------------------

def _fsrs_cfg(card: dict) -> tuple[list[float], float, int]:
    """Extract (weights, desired_retention, maximum_interval) from a card dict."""
    w = fsrs.parse_weights(card.get("fsrs_weights"))
    dr = card.get("desired_retention") or 0.9
    mx = card.get("maximum_interval") or 36500
    return w, dr, mx


def _fsrs_enabled(card: dict) -> bool:
    val = card.get("enable_fsrs", 1)
    return bool(val) if val is not None else True


def _hard_1d_enabled(card: dict) -> bool:
    val = card.get("learning_hard_1d", 1)
    return bool(val) if val is not None else True


def _hard_minutes(card: dict) -> int:
    """Minutes that "Hard" delays a learning/relearn card when learning_hard_1d
    is on — learning_hard_days (default 1 day), fractional days allowed."""
    days = card.get("learning_hard_days", 1)
    if days is None:
        days = 1
    try:
        days = float(days)
    except (TypeError, ValueError):
        days = 1.0
    return max(1, round(days * 1440))


def _learning_hard_delay(steps: list[int], idx: int, preset: dict) -> float:
    """Hard delay for a learning or relearning step, in minutes."""
    if _hard_1d_enabled(preset):
        return _hard_minutes(preset)
    if idx == 0 and len(steps) > 1:
        return (steps[0] + steps[1]) / 2
    if len(steps) == 1:
        return steps[0] * 1.5
    return steps[idx]


def _learning_good_compromise(card: dict, preset: dict, steps: list[int],
                              idx: int, relearn: bool) -> int | None:
    """Day interval for Good when its normal 1d step collides with Hard.

    The card graduates early so Good can sit between a 1d Hard and Easy's
    longer graduation interval. Other step schedules keep their usual flow.
    """
    if _fmt_min(_learning_hard_delay(steps, idx, preset)) != "1d":
        return None
    cfg = {**card, **preset}
    normal_good = (steps[idx + 1] if idx < len(steps) - 1 else
                   (_relearn_graduate_interval(cfg, 3) if relearn else
                    _graduate_interval(cfg, 3)) * 1440)
    if _fmt_min(normal_good) != "1d":
        return None
    easy_days = (_relearn_graduate_interval(cfg, 4) if relearn else
                 _graduate_interval(cfg, 4))
    if easy_days < 3:
        return None  # No whole-day interval strictly between Hard and Easy.
    return max(2, (1 + easy_days) // 2)


def _elapsed_days(card: dict) -> int:
    """Days since the previous review, for computing retrievability.

    Falls back to the scheduled interval (assume reviewed on time) when no
    last_review timestamp is stored yet — true for freshly seeded cards.
    """
    lr = card.get("last_review")
    if lr:
        try:
            d = datetime.fromisoformat(lr[:10]).date()
            return max(0, (database.anki_today() - d).days)
        except (ValueError, TypeError):
            pass
    return max(0, card.get("interval") or 0)


def _graduate_interval(card: dict, rating: int) -> int:
    """Day-interval when a learning card graduates with the given rating.

    Mirrors _handle_learning's _graduate: an already-seeded stability (from
    step answers) drives the interval directly; otherwise it is seeded from
    the graduating rating. FSRS off falls back to the preset values."""
    if not _fsrs_enabled(card):
        base = card.get("easy_interval", 4) if rating == 4 else card.get("graduating_interval", 1)
        return max(1, base)
    w, dr, mx = _fsrs_cfg(card)
    s = card.get("stability")
    if s:
        if rating == 4:
            s = fsrs.next_short_term_stability(w, s, 4)
    else:
        s = fsrs.init_stability(w, rating)
    return fsrs.next_interval(s, dr, mx)


def _relearn_graduate_interval(card: dict, rating: int) -> int:
    """Day-interval when a relearn card returns to review (Good/Easy)."""
    if not _fsrs_enabled(card) or not card.get("stability"):
        base = max(card.get("minimum_interval", 1), card.get("interval", 1))
        return base if rating != 4 else max(base, math.floor(base * card.get("ease", 2.5)))
    w, dr, mx = _fsrs_cfg(card)
    s = card["stability"]
    if rating == 4:
        s = fsrs.next_short_term_stability(w, s, 4)
    return max(card.get("minimum_interval", 1), fsrs.next_interval(s, dr, mx))


def _touch_learning_memory(c: dict, preset: dict, rating: int) -> None:
    """Short-term (same-day) FSRS memory update for a non-graduating step answer.

    The first answer on a fresh card seeds S/D from that rating (FSRS-5
    behaviour); later step answers nudge stability via the short-term formula
    and update difficulty, so the graduation interval adapts to how the card
    is actually going. No-op when FSRS is off."""
    if not _fsrs_enabled(preset):
        return
    w, _, _ = _fsrs_cfg(preset)
    if c.get("stability"):
        c["stability"] = fsrs.next_short_term_stability(w, c["stability"], rating)
        if c.get("difficulty") is not None:
            c["difficulty"] = fsrs.next_difficulty(w, c["difficulty"], rating)
    else:
        c["stability"] = fsrs.init_stability(w, rating)
        c["difficulty"] = fsrs.init_difficulty(w, rating)


# ---------------------------------------------------------------------------
# Interval preview — compute human-readable next-interval labels per rating
# Used by the frontend to label the Again/Hard/Good/Easy buttons
# ---------------------------------------------------------------------------

def preview_intervals(card: dict) -> dict:
    """
    Returns {1: "1m", 2: "5m", 3: "10m", 4: "4d"} for the four ratings.
    Requires a full card dict (with preset fields) from database.get_card().

    Deterministic: no random fuzz is applied here, so the labels exactly match
    what the user will see (fuzz is only applied when a rating is committed).
    """
    state      = card["state"]
    step_index = card.get("step_index", 0)
    interval   = card.get("interval", 0)
    ease       = card.get("ease", 2.5)
    l_steps    = _parse_steps(card.get("learning_steps",  "1 10"))
    r_steps    = _parse_steps(card.get("relearning_steps", "10"))
    min_int    = card.get("minimum_interval", 1)

    # Probation: steps are done, the card is out on a day-interval but not yet
    # "learned". Again restarts the steps; Hard/Good/Easy behave like a review
    # (day intervals), possibly graduating the card to real 'review' state.
    if state in ("learning", "relearn") and card.get("probation"):
        steps = l_steps if state == "learning" else r_steps
        again = _fmt_min(steps[0])
        if _fsrs_enabled(card) and card.get("stability"):
            w, dr, mx = _fsrs_cfg(card)
            ivs = fsrs.review_intervals(
                w, card["difficulty"], card["stability"], _elapsed_days(card), dr, mx
            )
            hard = _fmt_day(ivs[2])
            good = _fmt_day(ivs[3])
            easy = _fmt_day(ivs[4])
        else:
            hard = _fmt_day(max(min_int, math.floor(interval * 1.2)))
            good = _fmt_day(max(min_int, math.floor(interval * ease)))
            easy = _fmt_day(max(min_int, math.floor(interval * ease * 1.3)))
        return {1: again, 2: hard, 3: good, 4: easy}

    if state in ("new", "learning"):
        # Clamp: a card's stored step_index may exceed the current step count if
        # the steps were shortened after the card entered learning.
        si = min(step_index, len(l_steps) - 1)
        again = _fmt_min(l_steps[0])
        hard = _fmt_min(_learning_hard_delay(l_steps, si, card))
        compromise = _learning_good_compromise(card, card, l_steps, si, False)
        if compromise is not None:
            good = _fmt_day(compromise)
        elif si >= len(l_steps) - 1:
            good = _fmt_day(_graduate_interval(card, 3))
        else:
            good = _fmt_min(l_steps[si + 1])
        easy = _fmt_day(_graduate_interval(card, 4))

    elif state == "review":
        if _fsrs_enabled(card) and card.get("stability"):
            w, dr, mx = _fsrs_cfg(card)
            ivs = fsrs.review_intervals(
                w, card["difficulty"], card["stability"], _elapsed_days(card), dr, mx
            )
            again = _fmt_min(r_steps[0])
            hard  = _fmt_day(ivs[2])
            good  = _fmt_day(ivs[3])
            easy  = _fmt_day(ivs[4])
        else:
            again = _fmt_min(r_steps[0])
            hard  = _fmt_day(max(min_int, math.floor(interval * 1.2)))
            good  = _fmt_day(max(min_int, math.floor(interval * ease)))
            easy  = _fmt_day(max(min_int, math.floor(interval * ease * 1.3)))

    elif state == "relearn":
        si = min(step_index, len(r_steps) - 1)
        again = _fmt_min(r_steps[0])
        hard = _fmt_min(_learning_hard_delay(r_steps, si, card))
        compromise = _learning_good_compromise(card, card, r_steps, si, True)
        if compromise is not None:
            good = _fmt_day(compromise)
        elif si >= len(r_steps) - 1:
            good = _fmt_day(_relearn_graduate_interval(card, 3))
        else:
            good = _fmt_min(r_steps[si + 1])
        easy = _fmt_day(_relearn_graduate_interval(card, 4))

    else:
        again = hard = good = easy = "—"

    return {1: again, 2: hard, 3: good, 4: easy}


def _fmt_min(minutes: float) -> str:
    m = round(minutes)
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    return _fmt_day(h // 24)


def _fmt_day(days: int) -> str:
    if days < 1:
        return "1d"
    if days < 31:
        return f"{days}d"
    if days < 365:
        months = days // 30
        remaining = days % 30
        if remaining > 0:
            return f"{months}mo {remaining}d"
        return f"{months}mo"
    return f"{round(days / 365)}y"


# ---------------------------------------------------------------------------
# Pure math helpers — no DB calls
# ---------------------------------------------------------------------------

def _fuzz_delta(interval: int) -> int:
    """Return the fuzz delta (in days) for a given interval."""
    if interval < 2:
        return 0
    if interval < 7:
        return max(1, round(interval * 0.25))
    if interval < 30:
        return max(2, round(interval * 0.15))
    return max(4, round(interval * 0.05))


def _fuzz_interval(interval: int) -> int:
    """Apply Anki-style random fuzz to prevent card bunching."""
    fuzz = _fuzz_delta(interval)
    return random.randint(interval - fuzz, interval + fuzz) if fuzz else interval


def _fmt_day_fuzzed(days: int) -> str:
    """Format interval with fuzz range shown when fuzz > 1 day (e.g. '7-11d')."""
    fuzz = _fuzz_delta(days)
    if fuzz <= 1:
        return _fmt_day(days)
    return f"{_fmt_day(max(1, days - fuzz))}-{_fmt_day(days + fuzz)}"

def _parse_steps(steps_str: str) -> list[int]:
    """Parse step strings to minutes. Supports plain ints ("1 10"), "m" suffix ("1m 10m"), and "d" suffix ("1d" = 1440 min)."""
    result = []
    for s in steps_str.strip().split():
        if s.endswith('d'):
            result.append(int(s[:-1]) * 1440)
        elif s.endswith('m'):
            result.append(int(s[:-1]))
        else:
            result.append(int(s))
    return result


def _smart_due(due_dt: datetime) -> str:
    """Return a due string for a learning/relearn card.

    If the due datetime falls within today's Anki day (before tomorrow 4 AM),
    return a full ISO datetime so the card appears at the right minute.
    If it falls on a future Anki day, return just the ISO date — no specific
    time is stored or needed, and the date-only string sorts before any
    same-day datetime in SQL comparisons, so the card is correctly included
    in story generation and review queries from the start of that day.
    """
    cutoff_hour = database.get_day_cutoff_hour()
    next_cutoff = datetime.combine(
        database.anki_today() + timedelta(days=1),
        dtime(cutoff_hour, 0, 0),
    )
    if due_dt >= next_cutoff:
        # Determine the Anki day the card will be shown on
        due_date = (due_dt.date() if due_dt.hour >= cutoff_hour
                    else (due_dt - timedelta(days=1)).date())
        return due_date.isoformat()
    return due_dt.isoformat(timespec="seconds")


def next_learning_due(steps: list[int], step_index: int) -> str:
    """Returns due string for learning/relearn step.

    Same-day due → full ISO datetime. Future Anki day → ISO date only.
    """
    due = datetime.now() + timedelta(minutes=steps[step_index])
    return _smart_due(due)


def next_review_due(interval: int) -> str:
    """Returns ISO date = today + interval days."""
    return (database.anki_today() + timedelta(days=interval)).isoformat()


def calc_review(interval: int, ease: float,
                lapses: int, rating: int) -> tuple[int, float, int]:
    """SM-2 variant for review cards (legacy fallback when enable_fsrs = 0).

    Returns (new_interval, new_ease, new_lapses).
    Caller handles state transition to 'relearn' for rating=1.
    """
    if rating == 1:  # Again
        new_ease = max(1.3, ease - 0.20)
        new_interval = max(1, math.floor(interval * 0.5))
        return new_interval, new_ease, lapses + 1

    if rating == 2:  # Hard
        new_ease = max(1.3, ease - 0.15)
        new_interval = math.floor(interval * 1.2)
        return max(1, new_interval), new_ease, lapses

    if rating == 3:  # Good
        new_interval = math.floor(interval * ease)
        return max(1, new_interval), ease, lapses

    # rating == 4: Easy
    new_ease = ease + 0.15
    new_interval = math.floor(interval * new_ease * 1.3)
    return max(1, new_interval), new_ease, lapses


# ---------------------------------------------------------------------------
# Scheduler inspector — structured breakdown for the in-review FSRS panel
# ---------------------------------------------------------------------------

def explain_card(card: dict) -> dict:
    """Return the card's current FSRS parameters and a per-rating breakdown of
    what each button would do, for the Shift+S inspector popup.

    For review-state cards this includes retrievability and the resulting
    stability/difficulty/interval per rating. Other states return the current
    state with no review math (the buttons there follow learning steps)."""
    enabled = _fsrs_enabled(card)
    w, dr, mx = _fsrs_cfg(card)
    el = _elapsed_days(card)
    S = card.get("stability")
    D = card.get("difficulty")
    state = card["state"]

    info = {
        "enabled": enabled,
        "state": state,
        "desired_retention": dr,
        "maximum_interval": mx,
        "stability": S,
        "difficulty": D,
        "elapsed_days": el,
        "last_review": card.get("last_review"),
        "retrievability": None,
        "ratings": {},
    }

    if enabled and S and D is not None and state == "review":
        R = fsrs.retrievability(el, S)
        info["retrievability"] = R
        ivs = fsrs.review_intervals(w, D, S, el, dr, mx)
        for rating in (1, 2, 3, 4):
            new_s, new_d = fsrs.review_state(w, D, S, el, rating)
            entry = {
                "stability": round(new_s, 2),
                "difficulty": round(new_d, 2),
            }
            if rating == 1:
                entry["interval"] = fsrs.next_interval(new_s, dr, mx)
                entry["note"] = "lapse"  # → relearn steps first
            else:
                entry["interval"] = ivs[rating]
            info["ratings"][str(rating)] = entry

    return info


# ---------------------------------------------------------------------------
# Orchestration — calls database.py
# ---------------------------------------------------------------------------

def apply_review(card_id: int, rating: int,
                 user_response: str | None = None,
                 duration_ms: int | None = None) -> dict:
    """Main entry point. Returns updated card dict."""
    card = database.get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")
    state_before = card["state"]
    interval_before = card.get("interval") or 0

    preset = {
        "learning_steps":      card["learning_steps"],
        "graduating_interval": card["graduating_interval"],
        "easy_interval":       card["easy_interval"],
        "relearning_steps":    card["relearning_steps"],
        "minimum_interval":    card["minimum_interval"],
        "learned_interval":    card.get("learned_interval", 4),
        "enable_probation":    card.get("enable_probation", 1),
        "leech_threshold":     card["leech_threshold"],
        "learning_leech_threshold": card["learning_leech_threshold"],
        "enable_learning_leech":    card.get("enable_learning_leech", 1),
        "leech_action":        card["leech_action"],
        "desired_retention":   card.get("desired_retention", 0.9),
        "maximum_interval":    card.get("maximum_interval", 36500),
        "fsrs_weights":        card.get("fsrs_weights"),
        "enable_fsrs":         card.get("enable_fsrs", 1),
        "learning_hard_1d":    card.get("learning_hard_1d", 1),
        "learning_hard_days":  card.get("learning_hard_days", 1),
    }

    if card["state"] in ("learning", "relearn") and card.get("probation"):
        updated = _handle_probation(card, preset, rating)
    elif card["state"] in ("new", "learning"):
        updated = _handle_learning(card, preset, rating)
    elif card["state"] == "review":
        updated = _handle_review(card, preset, rating)
    elif card["state"] == "relearn":
        updated = _handle_relearn(card, preset, rating)
    else:
        # suspended — shouldn't be reviewed, but handle gracefully
        updated = card

    # Learning-phase leech: count Again presses while the card is still in a
    # pre-review state (new/learning/relearn, probation included). Review-state
    # lapses are handled separately by _check_leech via cards.lapses.
    again_count = card.get("learning_again_count") or 0
    if rating == 1 and state_before in ("new", "learning", "relearn"):
        again_count += 1
        if (preset["enable_learning_leech"]
                and again_count >= preset["learning_leech_threshold"]
                and preset["leech_action"] == "suspend"
                and updated["state"] != "suspended"):
            database.mark_leech_suspend(card_id)
            updated["state"] = "suspended"
    updated["learning_again_count"] = again_count

    database.update_card(
        card_id,
        state=updated["state"],
        due=updated["due"],
        step_index=updated["step_index"],
        interval=updated["interval"],
        ease=updated["ease"],
        repetitions=updated["repetitions"],
        lapses=updated["lapses"],
        probation=updated.get("probation", 0),
        stability=updated.get("stability"),
        difficulty=updated.get("difficulty"),
        last_review=updated.get("last_review"),
        learning_again_count=updated["learning_again_count"],
    )
    log_id = database.insert_review(
        card_id, rating, user_response=user_response,
        duration_ms=duration_ms, state=state_before,
        last_interval=interval_before,
    )
    return database.get_card(card_id), log_id


def _handle_learning(card: dict, preset: dict, rating: int) -> dict:
    """Advance through learning steps, or graduate.

    On graduation (Good at last step, or Easy) the card enters the FSRS review
    phase: initial stability/difficulty are seeded from the graduating rating."""
    steps = _parse_steps(preset["learning_steps"])
    c = dict(card)
    # Clamp a stale step_index (steps may have been shortened after this card
    # entered learning) so step lookups below can never go out of range.
    c["step_index"] = min(c.get("step_index", 0), len(steps) - 1)
    w, dr, mx = _fsrs_cfg(preset)
    use_fsrs = _fsrs_enabled(preset)

    probation_on = bool(preset.get("enable_probation", 1))

    def _graduate(grad_rating: int, interval_override: int | None = None) -> None:
        # With probation on the card is NOT a review card yet: it enters
        # probation and must survive an interval >= learned_interval first
        # (failing restarts the steps without counting a lapse). With probation
        # off this is the classic Anki behaviour — graduate straight to review.
        c["state"] = "learning" if probation_on else "review"
        c["probation"] = 1 if probation_on else 0
        c["step_index"] = 0
        c["repetitions"] += 1
        c["last_review"] = database.anki_today().isoformat()
        if use_fsrs:
            if c.get("stability"):
                # Re-graduating after a probation fail — keep the FSRS memory
                # state instead of re-seeding it from scratch.
                s = c["stability"]
                if grad_rating == 4:
                    s = fsrs.next_short_term_stability(w, s, 4)
                    c["stability"] = s
            else:
                s = fsrs.init_stability(w, grad_rating)
                c["stability"] = s
                c["difficulty"] = fsrs.init_difficulty(w, grad_rating)
            base = fsrs.next_interval(s, dr, mx)
        else:
            base = preset["easy_interval"] if grad_rating == 4 else preset["graduating_interval"]
        c["interval"] = _fuzz_interval(max(1, interval_override or base))
        if interval_override is not None:
            easy_days = _graduate_interval({**card, **preset}, 4)
            c["interval"] = max(2, min(c["interval"], easy_days - 1))
        c["due"] = next_review_due(c["interval"])

    if rating == 4:  # Easy — graduate immediately
        _graduate(4)
        return c

    if rating == 1:  # Again — reset to step 0
        c["state"] = "learning"
        c["step_index"] = 0
        c["due"] = next_learning_due(steps, 0)
        _touch_learning_memory(c, preset, rating)
        return c

    if rating == 2:  # Hard — stay on current step, slow delay
        c["state"] = "learning"
        idx = c["step_index"]
        delay = _learning_hard_delay(steps, idx, preset)
        c["due"] = _smart_due(datetime.now() + timedelta(minutes=delay))
        _touch_learning_memory(c, preset, rating)
        return c

    # rating == 3: Good — advance step
    idx = c["step_index"]
    last = len(steps) - 1

    compromise = _learning_good_compromise(card, preset, steps, idx, False)
    if compromise is not None:
        _graduate(3, compromise)
    elif idx >= last:  # Graduate
        _graduate(3)
    else:
        c["step_index"] = idx + 1
        c["state"] = "learning"
        c["due"] = next_learning_due(steps, c["step_index"])
        _touch_learning_memory(c, preset, rating)

    return c


def _handle_review(card: dict, preset: dict, rating: int) -> dict:
    """Apply the review-phase scheduler (FSRS, or SM-2 fallback)."""
    if not _fsrs_enabled(preset) or not card.get("stability"):
        return _handle_review_sm2(card, preset, rating)

    c = dict(card)
    w, dr, mx = _fsrs_cfg(preset)
    el = _elapsed_days(card)
    new_s, new_d = fsrs.review_state(w, card["difficulty"], card["stability"], el, rating)

    c["difficulty"] = new_d
    c["stability"] = new_s
    c["last_review"] = database.anki_today().isoformat()

    if rating == 1:  # Lapse → relearn
        c["lapses"] = c["lapses"] + 1
        c["interval"] = max(preset["minimum_interval"], fsrs.next_interval(new_s, dr, mx))
        c["state"] = "relearn"
        c["step_index"] = 0
        relearn_steps = _parse_steps(preset["relearning_steps"])
        c["due"] = next_learning_due(relearn_steps, 0)
        _check_leech(c, preset)
    else:
        # Use the monotonic, ordering-enforced interval so the committed value
        # matches the previewed button (fuzz aside).
        ivs = fsrs.review_intervals(w, card["difficulty"], card["stability"], el, dr, mx)
        c["interval"] = max(preset["minimum_interval"], _fuzz_interval(ivs[rating]))
        c["state"] = "review"
        c["due"] = next_review_due(c["interval"])
        c["repetitions"] += 1

    return c


def _handle_review_sm2(card: dict, preset: dict, rating: int) -> dict:
    """Legacy SM-2 review handler (used only when enable_fsrs = 0)."""
    c = dict(card)
    new_interval, new_ease, new_lapses = calc_review(
        c["interval"], c["ease"], c["lapses"], rating
    )

    c["ease"] = new_ease
    c["lapses"] = new_lapses

    if rating == 1:  # Lapse → relearn
        relearn_steps = _parse_steps(preset["relearning_steps"])
        c["interval"] = max(preset["minimum_interval"], new_interval)
        c["state"] = "relearn"
        c["step_index"] = 0
        c["due"] = next_learning_due(relearn_steps, 0)
        _check_leech(c, preset)
    else:
        c["interval"] = max(preset["minimum_interval"], _fuzz_interval(new_interval))
        c["state"] = "review"
        c["due"] = next_review_due(c["interval"])
        c["repetitions"] += 1

    return c


def _handle_relearn(card: dict, preset: dict, rating: int) -> dict:
    """Advance through relearning steps. Completion → back to review.

    Stability/difficulty were set when the card lapsed; the relearn steps are
    sub-day spacing only. On graduation the stored stability sets the interval."""
    steps = _parse_steps(preset["relearning_steps"])
    c = dict(card)
    # Clamp a stale step_index (relearn steps may have been shortened).
    c["step_index"] = min(c.get("step_index", 0), len(steps) - 1)

    if rating == 1:  # Again — reset relearn
        c["state"] = "relearn"
        c["step_index"] = 0
        c["due"] = next_learning_due(steps, 0)
        _touch_learning_memory(c, preset, rating)
        return c

    if rating == 2:  # Hard — repeat current step
        c["state"] = "relearn"
        idx = c["step_index"]
        delay = _learning_hard_delay(steps, idx, preset)
        c["due"] = _smart_due(datetime.now() + timedelta(minutes=delay))
        _touch_learning_memory(c, preset, rating)
        return c

    idx = c["step_index"]
    last = len(steps) - 1

    probation_on = bool(preset.get("enable_probation", 1))

    def _graduate(grad_rating: int, interval_override: int | None = None) -> None:
        # With probation on the relearn card must survive an interval of
        # >= learned_interval days before returning to 'review'; with probation
        # off it returns to 'review' immediately (classic Anki behaviour).
        c["state"] = "relearn" if probation_on else "review"
        c["probation"] = 1 if probation_on else 0
        base = interval_override or _relearn_graduate_interval(card, grad_rating)
        c["interval"] = _fuzz_interval(base)
        if interval_override is not None:
            easy_days = _relearn_graduate_interval({**card, **preset}, 4)
            c["interval"] = max(2, min(c["interval"], easy_days - 1))
        c["due"] = next_review_due(c["interval"])
        c["step_index"] = 0
        c["repetitions"] += 1
        c["last_review"] = database.anki_today().isoformat()
        if _fsrs_enabled(preset) and card.get("stability") and grad_rating == 4:
            w, _, _ = _fsrs_cfg(preset)
            c["stability"] = fsrs.next_short_term_stability(w, card["stability"], 4)

    if rating == 4:  # Easy — skip steps, graduate with bonus
        _graduate(4)
    else:
        compromise = _learning_good_compromise(card, preset, steps, idx, True)
        if compromise is not None:
            _graduate(3, compromise)
        elif idx >= last:  # Good at last step — back to review
            _graduate(3)
        else:
            c["step_index"] = idx + 1
            c["state"] = "relearn"
            c["due"] = next_learning_due(steps, c["step_index"])
            _touch_learning_memory(c, preset, rating)

    return c


def _handle_probation(card: dict, preset: dict, rating: int) -> dict:
    """A learning/relearn card that finished its steps and is out on a
    day-interval, but has not yet proven itself.

    - Again  → back to step 0 of its steps. NOT a lapse and no leech
               check — the card never became a review card.
    - Hard/Good/Easy → it survived the interval. If that interval was
               >= learned_interval days, the card truly graduates to 'review';
               otherwise it stays in probation with a normally-grown interval.
    """
    c = dict(card)
    survived  = c.get("interval") or 0
    threshold = preset.get("learned_interval", 4)
    steps_str = (preset["learning_steps"] if c["state"] == "learning"
                 else preset["relearning_steps"])
    steps = _parse_steps(steps_str)
    use_fsrs = _fsrs_enabled(preset) and card.get("stability")
    el = _elapsed_days(card)
    c["last_review"] = database.anki_today().isoformat()

    if rating == 1:  # Failed the probation interval — restart steps
        if use_fsrs:
            w, dr, mx = _fsrs_cfg(preset)
            new_s, new_d = fsrs.review_state(w, card["difficulty"], card["stability"], el, 1)
            c["stability"] = new_s
            c["difficulty"] = new_d
            c["interval"] = max(preset["minimum_interval"], fsrs.next_interval(new_s, dr, mx))
        else:
            c["interval"] = max(1, math.floor(survived * 0.5)) if survived else 1
        c["probation"] = 0
        c["step_index"] = 0
        c["due"] = next_learning_due(steps, 0)
        return c

    # Hard/Good/Easy — survived the probation interval
    if use_fsrs:
        w, dr, mx = _fsrs_cfg(preset)
        new_s, new_d = fsrs.review_state(w, card["difficulty"], card["stability"], el, rating)
        c["stability"] = new_s
        c["difficulty"] = new_d
        ivs = fsrs.review_intervals(w, card["difficulty"], card["stability"], el, dr, mx)
        nxt = ivs[rating]
    else:
        nxt, new_ease, _ = calc_review(max(1, survived), c.get("ease", 2.5), 0, rating)
        c["ease"] = new_ease
    c["interval"] = max(preset["minimum_interval"], _fuzz_interval(nxt))
    c["repetitions"] += 1
    if survived >= threshold:
        c["state"] = "review"
        c["probation"] = 0
        c["step_index"] = 0
    # else: interval grows but the card stays in probation until it survives
    # an interval of >= learned_interval days
    c["due"] = next_review_due(c["interval"])
    return c


def _check_leech(card: dict, preset: dict) -> None:
    """Flag + suspend a review-phase leech once lapses >= leech_threshold."""
    if card["lapses"] >= preset["leech_threshold"]:
        if preset["leech_action"] == "suspend":
            # mark_leech_suspend sets is_leech=1 and preserves pre_suspend_state,
            # so the review notification and the Leeched browse filter both work.
            database.mark_leech_suspend(card["id"])
            card["state"] = "suspended"
            card["is_leech"] = 1

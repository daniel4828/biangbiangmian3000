import re
import sqlite3
import uuid
import datetime as _dt
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from .core import get_db, anki_today


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats(deck_id: int | None = None, lang: str | None = None) -> dict:
    today = anki_today().isoformat()
    conn = get_db()

    deck_filter = "AND c.deck_id = ?" if deck_id else ""
    params_deck = [deck_id] if deck_id else []
    if lang is not None:
        deck_filter += " AND c.deck_id IN (SELECT id FROM decks WHERE lang = ?)"
        params_deck = params_deck + [lang]

    # Total words: count distinct words that have at least one card in this deck
    if deck_id:
        total_words = conn.execute(
            "SELECT COUNT(DISTINCT c.word_id) FROM cards c WHERE c.deck_id = ?",
            [deck_id],
        ).fetchone()[0]
    elif lang is not None:
        total_words = conn.execute("SELECT COUNT(*) FROM entries WHERE lang = ?", [lang]).fetchone()[0]
    else:
        total_words = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    reviews_today = conn.execute(
        f"""SELECT COUNT(*) FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            WHERE date(rl.reviewed_at) = ? {deck_filter}""",
        [today] + params_deck,
    ).fetchone()[0]

    new_today = conn.execute(
        f"""SELECT COUNT(DISTINCT rl.card_id) FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            WHERE date(rl.reviewed_at) = ? AND c.state IN ('new','learning') {deck_filter}""",
        [today] + params_deck,
    ).fetchone()[0]

    streak = _calc_streak(conn, deck_id)

    # Reviews per day — last 14 days (oldest first)
    day_rows = conn.execute(
        f"""SELECT date(rl.reviewed_at) as d, COUNT(*) as cnt
            FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            WHERE 1=1 {deck_filter}
            GROUP BY d ORDER BY d DESC LIMIT 14""",
        params_deck,
    ).fetchall()
    reviews_by_day = [{"date": r["d"], "count": r["cnt"]} for r in reversed(day_rows)]

    # Card state totals
    state_rows = conn.execute(
        f"""SELECT c.state, COUNT(*) as cnt
            FROM cards c
            WHERE 1=1 {deck_filter}
            GROUP BY c.state""",
        params_deck,
    ).fetchall()
    state_counts = {r["state"]: r["cnt"] for r in state_rows}

    conn.close()
    return {
        "total_words": total_words,
        "reviews_today": reviews_today,
        "new_today": new_today,
        "streak_days": streak,
        "reviews_by_day": reviews_by_day,
        "state_counts": state_counts,
    }


# ---------------------------------------------------------------------------
# API cost tracking
# ---------------------------------------------------------------------------

# Prices per million tokens (USD). Providers don't expose a pricing API, so
# this table is hand-maintained — update it (and the static price table in
# static/index.html's story-setup modal) whenever a provider changes pricing
# or a new model is adopted. See CLAUDE.md "规范与约束".
_PRICING_AS_OF = "2026-08-15"
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI (news/briefing/podcast summaries)
    "gpt-5.6-sol":   {"input": 5.00, "cached": 0.50, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.00, "cached": 0.20, "output": 12.00},
    "gpt-5.6-luna":  {"input": 0.20, "cached": 0.02, "output": 1.20},
    "gpt-5.1":      {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5":        {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5-mini":   {"input": 0.25, "cached": 0.025, "output": 2.00},
    # OpenAI audio transcription (podcast Whisper path) — billed per minute;
    # input_tokens stores audio *seconds* for these rows (see podcast.py).
    "gpt-4o-mini-transcribe": {"per_minute": 0.003},
    # whisper-1 (#750): the Instagram Reel transcription fallback
    # (podcast._transcribe_instagram) — the only OpenAI transcription model
    # that supports response_format="verbose_json" (needed for the
    # hallucination filter's per-segment no_speech_prob/avg_logprob).
    "whisper-1": {"per_minute": 0.006},
    # Groq whisper-large-v3-turbo (#750): Instagram Reel transcription,
    # primary path — $0.04/hour of audio = $0.04/60 per minute.
    "whisper-large-v3-turbo": {"per_minute": 0.04 / 60},
    # DeepSeek (default story/enrichment models)
    "deepseek-v4-flash": {"input": 0.14,  "cached": 0.0028,   "output": 0.28},
    "deepseek-v4-pro":   {"input": 0.435, "cached": 0.003625, "output": 0.87},
    # Legacy models kept so old api_call_log rows still price correctly
    "deepseek-chat":     {"input": 0.28, "output": 0.42},
    "deepseek-reasoner": {"input": 0.50, "output": 2.18},
    "glm-4-flash":       {"input": 0.00, "output": 0.00},
    "glm-4-air":         {"input": 0.06, "output": 0.06},
    # GLM-4.7/5 (podcast mode rework, issue #561)
    "glm-5":           {"input": 1.00, "output": 3.20},
    "glm-4.7":         {"input": 0.60, "output": 2.20},
    "glm-4.7-flashx":  {"input": 0.07, "output": 0.40},
    "glm-4.7-flash":   {"input": 0.00, "output": 0.00},
    "qwen-turbo":        {"input": 0.065, "output": 0.26},
    "qwen-plus":         {"input": 0.40, "output": 1.20},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":   {"input": 15.00, "output": 75.00},
}

_SNAPSHOT_SUFFIX_RE = re.compile(r"(-\d{4}-\d{2}-\d{2}|-\d{8})$")


def _lookup_pricing(model: str) -> dict | None:
    """Resolve a model id to its pricing entry.

    Tries, in order: exact match; the id with a trailing date-snapshot suffix
    (-YYYY-MM-DD or -YYYYMMDD) stripped; the longest pricing-table key that is
    a prefix of the model id (so "gpt-5.1-2026-04-14" matches "gpt-5.1" before
    the shorter "gpt-5"). Returns None if nothing matches.
    """
    if model in _MODEL_PRICING:
        return _MODEL_PRICING[model]

    stripped = _SNAPSHOT_SUFFIX_RE.sub("", model)
    if stripped in _MODEL_PRICING:
        return _MODEL_PRICING[stripped]

    matches = [k for k in _MODEL_PRICING if stripped.startswith(k) or model.startswith(k)]
    if matches:
        best = max(matches, key=len)
        return _MODEL_PRICING[best]

    return None


def _row_cost(row: dict) -> float | None:
    """Compute the USD cost of one api_call_log row, or None if the model's
    pricing is unknown."""
    pricing = _lookup_pricing(row["model"])
    if pricing is None:
        return None

    if "per_minute" in pricing:
        # Audio transcription rows store duration in *seconds* in input_tokens.
        return row["input_tokens"] / 60 * pricing["per_minute"]

    cached = min(row.get("cached_input_tokens") or 0, row["input_tokens"])
    miss = row["input_tokens"] - cached
    cached_price = pricing.get("cached", pricing["input"])
    return (miss * pricing["input"] + cached * cached_price +
            row["output_tokens"] * pricing["output"]) / 1_000_000


# Current "action" (a coherent unit of work that may make several API calls,
# e.g. "generate a story" or "transcribe & summarize an episode") — issue #525.
# Module-level ContextVar so log_api_call can tag calls without every caller
# threading an action id/label through its call chain. contextvars are
# per-thread/per-task by default and do NOT propagate into a thread started
# with threading.Thread — callers running work in a background thread must
# enter action_context() from *inside* that thread, not the thread that
# spawned it.
_ACTION_CTX: ContextVar[tuple[str, str] | None] = ContextVar("_action_ctx", default=None)


@contextmanager
def action_context(label: str):
    """Tag every database.log_api_call() made inside this block with a shared
    action_id/action_label, so the cost modal can group them as one operation.

    Must be entered in the thread that will actually run the API calls.
    """
    token = _ACTION_CTX.set((uuid.uuid4().hex, label))
    try:
        yield
    finally:
        _ACTION_CTX.reset(token)


# Prompts/responses are stored for the cost-modal Prompt/Response buttons but
# must not bloat api_call_log indefinitely — truncate here, in the single place
# every log_api_call() caller funnels through, rather than trusting each call
# site to have already truncated. 30000 chars fits every current prompt whole
# (issue #579: 10000 cut off the format guides of long briefing prompts).
_PROMPT_MAX_CHARS = 30000

# Rows predating the action_context() grouping (#525) have a NULL action_id.
# Rather than list each on its own line, get_api_costs clusters NULL-action rows
# that fired within this many seconds of each other into one synthetic "legacy"
# action (#537) — a single story/briefing run made several calls seconds apart,
# so time-adjacency reconstructs the action well enough for old history. Purely a
# display-time heuristic (no DB write); such actions are flagged approx=True.
_LEGACY_CLUSTER_GAP_SECONDS = 90

# Auxiliary steps that ride along with a primary generation (comma cleanup, news
# fact-check/repair) — excluded when labeling a legacy cluster so the row shows
# the main action ("story"), not a 1:1 helper pass ("fix_commas").
_AUX_PURPOSES = {"fix_commas", "briefing_fact_check", "briefing_repair",
                 "briefing_naturalness"}

# Single-word Again regenerations get a per-mode "… Again Sentences" action label
# (issue #578). Each click is its own action (one call), so adjacent actions with
# the same Again label within this gap are merged into one expandable row —
# a review session's Again clicks read as one line instead of dozens.
_AGAIN_MERGE_GAP_SECONDS = 1800
_AGAIN_LABEL_SUFFIX = "Again Sentences"


def log_api_call(model: str, input_tokens: int, output_tokens: int,
                 purpose: str = "story", cached_input_tokens: int = 0,
                 prompt: str | None = None, response: str | None = None) -> None:
    action = _ACTION_CTX.get()
    action_id, action_label = action if action else (None, None)
    if prompt is not None and len(prompt) > _PROMPT_MAX_CHARS:
        prompt = prompt[:_PROMPT_MAX_CHARS]
    if response is not None and len(response) > _PROMPT_MAX_CHARS:
        response = response[:_PROMPT_MAX_CHARS]

    conn = get_db()
    conn.execute(
        """INSERT INTO api_call_log
           (model, input_tokens, output_tokens, purpose, cached_input_tokens,
            action_id, action_label, prompt, response)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (model, input_tokens, output_tokens, purpose, cached_input_tokens,
         action_id, action_label, prompt, response),
    )
    conn.commit()
    conn.close()


def get_api_call_details(call_id: int) -> dict | None:
    """Prompt + response of one logged call (fetched on demand, issue #579) —
    or None if the call id doesn't exist."""
    conn = get_db()
    row = conn.execute(
        "SELECT prompt, response FROM api_call_log WHERE id = ?", (call_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"prompt": row["prompt"], "response": row["response"]}


def _parse_log_time(called_at: str) -> _dt.datetime | None:
    """Parse api_call_log.called_at ("YYYY-MM-DD HH:MM:SS", from SQLite's
    datetime('now')) into a datetime, or None if it doesn't match — the caller
    then treats the row as un-clusterable rather than crashing."""
    try:
        return _dt.datetime.strptime(called_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def get_api_costs(limit: int = 100) -> dict:
    conn = get_db()
    # Never SELECT prompt here — actions/calls feed the /api/costs payload and
    # prompts/responses are fetched on demand via get_api_call_details() instead.
    rows = [dict(r) for r in conn.execute(
        """SELECT id, called_at, model, input_tokens, output_tokens, purpose,
                  cached_input_tokens, action_id, action_label,
                  (prompt IS NOT NULL) AS has_prompt,
                  (response IS NOT NULL) AS has_response
           FROM api_call_log ORDER BY called_at DESC"""
    ).fetchall()]
    conn.close()

    # called_at is stored via SQLite's datetime('now') — "YYYY-MM-DD HH:MM:SS"
    # (UTC, space-separated, no microseconds). Match that format exactly so
    # the string comparison below is valid.
    thirty_days_ago = (datetime.utcnow() - _dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    total_cost = 0.0
    total_cost_30d = 0.0
    unknown_calls = 0
    unknown_models: list[str] = []

    # Group calls into "actions". A shared action_id (database.action_context,
    # #525) groups its calls into one operation. Rows with a NULL action_id
    # predate #525; rather than list each on its own line, cluster the ones that
    # fired close together in time into a synthetic "legacy" action (#537) — see
    # _LEGACY_CLUSTER_GAP_SECONDS. Such actions are flagged approx=True and get
    # labeled by their dominant purpose after the loop.
    actions: dict[str, dict] = {}
    order: list[str] = []  # preserves first-seen order per synthetic key
    legacy_key: str | None = None            # open legacy cluster's key, or None
    legacy_last: _dt.datetime | None = None  # timestamp of its most recent call

    for r in rows:
        cost = _row_cost(r)
        is_recent = r["called_at"] >= thirty_days_ago

        if cost is None:
            unknown_calls += 1
            if r["model"] not in unknown_models:
                unknown_models.append(r["model"])
        else:
            total_cost += cost
            if is_recent:
                total_cost_30d += cost

        if r["action_id"] is not None:
            key = r["action_id"]
            label = r["action_label"] or r["purpose"]
            approx = False
            legacy_key = legacy_last = None  # a real action breaks any open cluster
        else:
            ts = _parse_log_time(r["called_at"])
            # rows are DESC, so legacy_last (the previous NULL row) is at or after
            # ts — join the open cluster when the gap is small, else start a new one.
            if (legacy_key is not None and legacy_last is not None and ts is not None
                    and (legacy_last - ts).total_seconds() <= _LEGACY_CLUSTER_GAP_SECONDS):
                key = legacy_key
            else:
                key = legacy_key = f"legacy:{r['id']}"
            legacy_last = ts
            label = None  # filled from dominant purpose after the loop
            approx = True

        a = actions.get(key)
        if a is None:
            a = actions[key] = {
                "action_id": r["action_id"], "label": label,
                "started_at": r["called_at"], "calls": [],
                "call_count": 0, "total_cost": None, "approx": approx,
            }
            order.append(key)
        # rows are in called_at DESC order, so the *last* row seen for this
        # action has the earliest timestamp — keep that as started_at.
        a["started_at"] = r["called_at"]
        a["calls"].append({
            "id": r["id"], "called_at": r["called_at"], "model": r["model"],
            "purpose": r["purpose"], "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cached_input_tokens": r["cached_input_tokens"] or 0,
            "cost": round(cost, 6) if cost is not None else None,
            "has_prompt": bool(r["has_prompt"]),
            "has_response": bool(r["has_response"]),
        })
        a["call_count"] += 1
        if cost is not None:
            a["total_cost"] = (a["total_cost"] or 0.0) + cost

    for a in actions.values():
        if a["total_cost"] is not None:
            a["total_cost"] = round(a["total_cost"], 6)
        # Legacy clusters have no stored label — name them by dominant purpose so
        # the row reads e.g. "briefing · legacy" instead of a bare synthetic key.
        # Ignore auxiliary steps (comma-fix, fact-check, repair) when picking the
        # label so it reflects the primary action, not a cleanup pass tied 1:1 with it.
        if a["label"] is None:
            purposes = [c["purpose"] for c in a["calls"]]
            primary = [p for p in purposes if p not in _AUX_PURPOSES] or purposes
            dominant = max(set(primary), key=primary.count)
            a["label"] = f"{dominant} · legacy"

    # rows (and thus order) are already called_at DESC, and an action's
    # started_at is its earliest call — sort actions by that so the most
    # recently-started operation shows first, matching the flat log's order.
    actions_sorted = sorted(
        (actions[k] for k in order),
        key=lambda a: a["started_at"], reverse=True,
    )

    # Merge adjacent same-label "… Again Sentences" actions (issue #578): each
    # Again click is its own one-call action, so without this a review session
    # fills the list with dozens of single-call rows. Newest-first order means
    # the previous merged row's earliest call vs this action's latest call is
    # the true gap between them.
    merged: list[dict] = []
    for a in actions_sorted:
        prev = merged[-1] if merged else None
        if (prev is not None and a["label"] == prev["label"]
                and (a["label"] or "").endswith(_AGAIN_LABEL_SUFFIX)):
            prev_earliest = _parse_log_time(prev["started_at"])
            a_latest = _parse_log_time(a["calls"][0]["called_at"]) if a["calls"] else None
            if (prev_earliest is not None and a_latest is not None
                    and (prev_earliest - a_latest).total_seconds() <= _AGAIN_MERGE_GAP_SECONDS):
                prev["calls"].extend(a["calls"])
                prev["call_count"] += a["call_count"]
                if a["total_cost"] is not None:
                    prev["total_cost"] = round((prev["total_cost"] or 0.0) + a["total_cost"], 6)
                prev["started_at"] = a["started_at"]
                prev["action_id"] = None  # spans several action_ids now
                continue
        merged.append(a)
    actions_list = merged[:limit]

    return {
        "pricing_as_of": _PRICING_AS_OF,
        "total_cost": round(total_cost, 6),
        "total_cost_30d": round(total_cost_30d, 6),
        "unknown_calls": unknown_calls,
        "unknown_models": unknown_models,
        "actions": actions_list,
    }


def get_retention_bulk(days: int = 30, lang: str | None = None) -> dict:
    """Return retention rate data for all decks, grouped by leaf deck_id.

    Returns:
        {
          "by_deck": {deck_id: {"correct": int, "total": int, "category": str|None}},
          "all":     {"correct": int, "total": int},
          "days":    int
        }
    Rating > 1 (Hard/Good/Easy) counts as correct; rating == 1 (Again) counts as wrong.

    Only counts reviews of *learned* cards — those answered in the 'review' phase
    (state='review') whose interval had reached the deck's learned_interval
    threshold (default 4 days). Learning/relearning/new-card steps and young
    review cards (interval below the threshold) are excluded, matching an
    Anki-style "mature retention". Legacy rows with no recorded state are excluded;
    legacy 'review' rows with no recorded interval keep counting (not retroactive).
    """
    conn = get_db()
    since = (anki_today() - _dt.timedelta(days=days)).isoformat()

    lang_clause = " AND d.lang = ?" if lang is not None else ""
    params = [since] + ([lang] if lang is not None else [])
    rows = conn.execute(
        f"""SELECT c.deck_id, d.category,
                  COUNT(*) AS total,
                  SUM(CASE WHEN rl.rating > 1 THEN 1 ELSE 0 END) AS correct
           FROM review_log rl
           JOIN cards c ON c.id = rl.card_id
           JOIN decks d ON d.id = c.deck_id
           JOIN deck_presets p ON p.id = d.preset_id
           WHERE date(datetime(rl.reviewed_at, 'localtime')) >= ?
             AND rl.state = 'review'
             AND (rl.last_interval IS NULL OR rl.last_interval >= p.learned_interval)
             {lang_clause}
           GROUP BY c.deck_id""",
        params,
    ).fetchall()
    conn.close()

    by_deck: dict = {}
    total_all = 0
    correct_all = 0
    for r in rows:
        correct = r["correct"] or 0
        total   = r["total"]   or 0
        by_deck[r["deck_id"]] = {
            "correct":  correct,
            "total":    total,
            "category": r["category"],
        }
        total_all   += total
        correct_all += correct

    return {
        "by_deck": by_deck,
        "all":     {"correct": correct_all, "total": total_all},
        "days":    days,
    }


def get_calendar_stats(days: int = 365, deck_id: int | None = None) -> dict:
    """Per-day review statistics for the home-page calendar heatmap.

    Days are bucketed by the Anki day boundary (local time, 4am cutoff) so they
    line up with the rest of the app. Returns, for each day in the window:
      - reviews / cards (distinct) studied, overall + per category
      - retention (correct = rating > 1) overall, per category, and split by
        phase (learning vs review) — both overall and per category
      - total study time (duration_ms) and the count of timed reviews (for avg)
    Plus `future`: cards scheduled for review from today onward (from cards.due).

    Legacy review rows have NULL duration_ms / state: they still count toward
    review/retention totals but are excluded from time and phase splits.
    """
    conn = get_db()
    today = anki_today()
    since = (today - _dt.timedelta(days=days)).isoformat()

    deck_filter = "AND c.deck_id = ?" if deck_id else ""
    params = [since] + ([deck_id] if deck_id else [])

    # Anki-day bucket: UTC timestamp → local → shift back past the 4am cutoff.
    day_expr = "date(datetime(rl.reviewed_at, 'localtime', '-4 hours'))"
    rows = conn.execute(
        f"""SELECT {day_expr} AS d, c.category AS cat, rl.rating AS rating,
                   rl.state AS state, rl.duration_ms AS dur, rl.card_id AS card_id,
                   rl.last_interval AS last_ivl, p.learned_interval AS learned_int
            FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            JOIN decks d ON d.id = c.deck_id
            JOIN deck_presets p ON p.id = d.preset_id
            WHERE {day_expr} >= ? {deck_filter}""",
        params,
    ).fetchall()

    def _new_cat() -> dict:
        return {
            "reviews": 0, "correct": 0, "total": 0,
            "duration_ms": 0, "timed_count": 0, "_cards": set(),
            "learning": {"correct": 0, "total": 0},
            "review":   {"correct": 0, "total": 0},
        }

    by_date: dict[str, dict] = {}
    for r in rows:
        d = r["d"]
        day = by_date.get(d)
        if day is None:
            day = by_date[d] = {
                "reviews": 0, "correct": 0, "total": 0,
                "duration_ms": 0, "timed_count": 0, "_cards": set(),
                "learning": {"correct": 0, "total": 0},
                "review":   {"correct": 0, "total": 0},
                "by_cat": {},
            }
        cat = r["cat"]
        c = day["by_cat"].get(cat)
        if c is None:
            c = day["by_cat"][cat] = _new_cat()

        correct = 1 if r["rating"] > 1 else 0
        for bucket in (day, c):
            bucket["reviews"] += 1
            bucket["total"]   += 1
            bucket["correct"] += correct
            bucket["_cards"].add(r["card_id"])
            if r["dur"] is not None:
                bucket["duration_ms"] += r["dur"]
                bucket["timed_count"] += 1

        # Phase split (learning/relearn/new = "learning"; review = "review").
        # A 'review'-state card whose interval hadn't yet reached the deck's
        # learned_interval threshold counts as "learning" (not yet learned).
        # Legacy 'review' rows with no recorded interval keep counting as review.
        phase = None
        if r["state"] in ("new", "learning", "relearn"):
            phase = "learning"
        elif r["state"] == "review":
            li = r["last_ivl"]
            thr = r["learned_int"]
            phase = "learning" if (li is not None and li < thr) else "review"
        if phase:
            for bucket in (day, c):
                bucket[phase]["total"]   += 1
                bucket[phase]["correct"] += correct

    # Finalize: replace card sets with counts
    for day in by_date.values():
        day["cards"] = len(day.pop("_cards"))
        for c in day["by_cat"].values():
            c["cards"] = len(c.pop("_cards"))

    # Future scheduled reviews (today onward), grouped by due date + category
    future_filter = "AND deck_id = ?" if deck_id else ""
    future_params = [today.isoformat()] + ([deck_id] if deck_id else [])
    future_rows = conn.execute(
        f"""SELECT date(due) AS d, category AS cat, COUNT(*) AS cnt
            FROM cards
            WHERE state IN ('review', 'learning', 'relearn')
              AND deleted_at IS NULL
              AND date(due) >= ? {future_filter}
            GROUP BY date(due), category""",
        future_params,
    ).fetchall()
    conn.close()

    future: dict[str, dict] = {}
    for r in future_rows:
        d = r["d"]
        f = future.get(d)
        if f is None:
            f = future[d] = {"total": 0, "by_cat": {}}
        f["total"] += r["cnt"]
        f["by_cat"][r["cat"]] = r["cnt"]

    return {
        "days": days,
        "today": today.isoformat(),
        "by_date": by_date,
        "future": future,
    }


_EVOLUTION_STATES = ("new", "learning", "review", "relearn")


def _next_state(state: str, step: int, rating: int,
                n_learn: int, n_relearn: int) -> tuple[str, int]:
    """SM-2 state transition for one review (states only, no intervals)."""
    if state in ("new", "learning"):
        if rating == 1:
            return ("learning", 0)
        if rating == 2:
            return ("learning", step)
        if rating == 3:
            if step + 1 >= n_learn:
                return ("review", 0)
            return ("learning", step + 1)
        return ("review", 0)  # Easy graduates immediately
    if state == "review":
        return ("relearn", 0) if rating == 1 else ("review", 0)
    if state == "relearn":
        if rating == 1:
            return ("relearn", 0)
        if rating == 2:
            return ("relearn", step)
        if rating == 3:
            if step + 1 >= n_relearn:
                return ("review", 0)
            return ("relearn", step + 1)
        return ("review", 0)
    return (state, step)


def get_card_evolution(days: int = 365, deck_id: int | None = None, lang: str | None = None) -> dict:
    """Per-day card-state counts over time, reconstructed from review_log.

    Most legacy review rows have NULL state, so the history is rebuilt by
    replaying the SM-2 state machine over each card's rating sequence (using
    the deck preset's learning/relearning step counts). Rows that *do* record
    a state recalibrate the replay, and the card's current state pins the end
    of the timeline. Before the first review a card is 'new' (from the
    entry's date_added). Suspended cards count under their pre_suspend_state
    (or stay in their last replayed state when that is unset).

    Days use the Anki boundary (local time, 4am cutoff) like get_calendar_stats.

    Returns:
        {
          "days": int, "today": "YYYY-MM-DD",
          "dates": ["YYYY-MM-DD", ...],            # oldest → today, len == days
          "series": {category: {state: [int, ...]}}  # aligned with dates
        }
    """
    conn = get_db()
    today = anki_today()
    dates = [(today - _dt.timedelta(days=days - 1 - i)).isoformat()
             for i in range(days)]

    deck_filter = "AND c.deck_id = ?" if deck_id else ""
    params = [deck_id] if deck_id else []
    if lang is not None:
        deck_filter += " AND c.deck_id IN (SELECT id FROM decks WHERE lang = ?)"
        params.append(lang)
    day_of = "date(datetime({col}, 'localtime', '-4 hours'))"

    cards = conn.execute(
        f"""SELECT c.id, c.deck_id, c.category, c.state, c.pre_suspend_state,
                   {day_of.format(col='e.date_added')} AS created
            FROM cards c JOIN entries e ON e.id = c.word_id
            WHERE c.deleted_at IS NULL {deck_filter}""",
        params,
    ).fetchall()

    reviews = conn.execute(
        f"""SELECT rl.card_id, {day_of.format(col='rl.reviewed_at')} AS d,
                   rl.state, rl.rating
            FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            WHERE c.deleted_at IS NULL {deck_filter}
            ORDER BY rl.card_id, rl.reviewed_at""",
        params,
    ).fetchall()

    # Learning/relearning step counts per deck (category override wins)
    preset_rows = conn.execute(
        """SELECT d.id AS deck_id,
                  p.learning_steps, p.relearning_steps,
                  o.learning_steps AS o_ls, o.relearning_steps AS o_rs
           FROM decks d
           JOIN deck_presets p ON p.id = d.preset_id
           LEFT JOIN preset_category_overrides o
                  ON o.preset_id = d.preset_id AND o.category = d.category"""
    ).fetchall()
    conn.close()

    steps_by_deck = {
        r["deck_id"]: (
            max(1, len((r["o_ls"] or r["learning_steps"]).split())),
            max(1, len((r["o_rs"] or r["relearning_steps"]).split())),
        )
        for r in preset_rows
    }

    revs_by_card: dict[int, list] = {}
    for r in reviews:
        revs_by_card.setdefault(r["card_id"], []).append(
            (r["d"], r["state"], r["rating"]))

    # deltas[category][day][state] — state-count changes taking effect that day
    deltas: dict[str, dict[str, dict[str, int]]] = {}
    for c in cards:
        final = c["state"]
        if final == "suspended":
            final = c["pre_suspend_state"]
        n_learn, n_relearn = steps_by_deck.get(c["deck_id"], (2, 1))
        rl = revs_by_card.get(c["id"], [])

        # Checkpoints: (day, state the card holds from the end of that day on)
        seq = [(c["created"], "new")]
        state, step = "new", 0
        for day, recorded, rating in rl:
            if recorded in _EVOLUTION_STATES and recorded != state:
                state, step = recorded, 0  # recalibrate from recorded truth
            state, step = _next_state(state, step, rating, n_learn, n_relearn)
            seq.append((day, state))
        if rl and final in _EVOLUTION_STATES:
            seq[-1] = (seq[-1][0], final)  # pin the end to the card's real state

        # Several checkpoints on one day: the last one wins
        last_for_day: dict[str, str] = {}
        for day, st in seq:
            last_for_day[day] = st

        cat_deltas = deltas.setdefault(c["category"], {})
        prev = None
        for day in sorted(last_for_day):
            st = last_for_day[day]
            if st == prev:
                continue
            d = cat_deltas.setdefault(day, {})
            if prev is not None:
                d[prev] = d.get(prev, 0) - 1
            d[st] = d.get(st, 0) + 1
            prev = st

    # Accumulate deltas into daily series (deltas before the window roll into day 0)
    series: dict[str, dict[str, list[int]]] = {}
    for cat, cat_deltas in deltas.items():
        running = dict.fromkeys(_EVOLUTION_STATES, 0)
        out = {s: [] for s in _EVOLUTION_STATES}
        delta_days = sorted(cat_deltas)
        idx = 0
        for date_str in dates:
            while idx < len(delta_days) and delta_days[idx] <= date_str:
                for s, n in cat_deltas[delta_days[idx]].items():
                    running[s] += n
                idx += 1
            for s in _EVOLUTION_STATES:
                out[s].append(running[s])
        series[cat] = out

    return {
        "days": days,
        "today": today.isoformat(),
        "dates": dates,
        "series": series,
    }


def _calc_streak(conn: sqlite3.Connection, deck_id: int | None) -> int:
    deck_filter = "AND c.deck_id = ?" if deck_id else ""
    params = [deck_id] if deck_id else []
    rows = conn.execute(
        f"""SELECT DISTINCT date(rl.reviewed_at) as d
            FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            WHERE 1=1 {deck_filter}
            ORDER BY d DESC""",
        params,
    ).fetchall()
    if not rows:
        return 0
    streak = 0
    today = anki_today()
    for i, row in enumerate(rows):
        expected = (today - __import__("datetime").timedelta(days=i)).isoformat()
        if row["d"] == expected:
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Study sessions (#1023)
# ---------------------------------------------------------------------------

# A "session" is a run of reviews with no long pause in it. Anki has no such
# entity, so it is derived here from review_log alone: consecutive rows more
# than this many minutes apart start a new session. 30 min is long enough that
# a slow card or a short interruption stays inside one session, and short
# enough that morning and evening never merge into one.
SESSION_GAP_MINUTES = 30


def get_study_sessions(lang: str | None = None, limit: int = 60,
                       max_rows: int = 20000) -> list[dict]:
    """Recent study sessions, newest first.

    Grouping happens in Python, not SQL: SQLite has no window functions we can
    rely on across the versions this runs on, and the row count here is small.
    `max_rows` bounds how far back we look — a session older than that is not
    something anyone scrolls to.
    """
    conn = get_db()
    where = ""
    params: list = []
    if lang is not None:
        where = "WHERE c.deck_id IN (SELECT id FROM decks WHERE lang = ?)"
        params.append(lang)
    rows = conn.execute(
        f"""SELECT rl.reviewed_at, rl.rating, rl.duration_ms, rl.state, rl.card_id,
                   c.category, c.word_id, e.word_zh
            FROM review_log rl
            JOIN cards c ON c.id = rl.card_id
            LEFT JOIN entries e ON e.id = c.word_id
            {where}
            ORDER BY rl.reviewed_at DESC
            LIMIT ?""",
        params + [max_rows],
    ).fetchall()
    conn.close()

    gap = _dt.timedelta(minutes=SESSION_GAP_MINUTES)
    sessions: list[dict] = []
    cur: dict | None = None
    prev_ts: _dt.datetime | None = None

    for r in rows:  # newest → oldest
        ts = _parse_log_time(r["reviewed_at"])
        if ts is None:
            continue
        if cur is None or prev_ts is None or (prev_ts - ts) > gap:
            if cur is not None:
                sessions.append(cur)
                if len(sessions) >= limit:
                    return [_finish_session(s) for s in sessions]
            cur = {
                "ended_at": r["reviewed_at"],
                "started_at": r["reviewed_at"],
                "count": 0,
                "ratings": {1: 0, 2: 0, 3: 0, 4: 0},
                "duration_ms": 0,
                "cards": set(),
                "categories": {},
                "review_total": 0,
                "review_again": 0,
                "words": [],
            }
        cur["started_at"] = r["reviewed_at"]
        cur["count"] += 1
        cur["ratings"][r["rating"]] = cur["ratings"].get(r["rating"], 0) + 1
        cur["duration_ms"] += r["duration_ms"] or 0
        cur["cards"].add(r["card_id"])
        if r["category"]:
            cur["categories"][r["category"]] = cur["categories"].get(r["category"], 0) + 1
        # Retention is only meaningful on cards that were actually due for
        # review — a failed learning step is not a lapse.
        if r["state"] == "review":
            cur["review_total"] += 1
            if r["rating"] == 1:
                cur["review_again"] += 1
        if r["word_zh"] and len(cur["words"]) < 400:
            cur["words"].append({"word_id": r["word_id"], "word_zh": r["word_zh"],
                                 "rating": r["rating"]})
        prev_ts = ts

    if cur is not None:
        sessions.append(cur)
    return [_finish_session(s) for s in sessions[:limit]]


def _finish_session(s: dict) -> dict:
    s["unique_cards"] = len(s.pop("cards"))
    s["words"] = list(reversed(s["words"]))  # oldest → newest, i.e. review order
    s["retention"] = (
        round(100 * (s["review_total"] - s["review_again"]) / s["review_total"])
        if s["review_total"] else None
    )
    start = _parse_log_time(s["started_at"])
    end = _parse_log_time(s["ended_at"])
    s["elapsed_ms"] = int((end - start).total_seconds() * 1000) if start and end else 0
    s["ratings"] = {str(k): v for k, v in s["ratings"].items()}
    return s

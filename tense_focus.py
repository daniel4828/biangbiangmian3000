"""Zeit des Tages: one Spanish tense per day (#1434).

Seven tenses at once and persons weighted by frequency felt chaotic. With the
focus on, each day (or every `days` days) has exactly ONE tense; verb_slots /
conj_patterns then walk the persons in a fixed order (yo, tú, él, nosotros,
vosotros, ellos) inside that tense. This module only answers "which tense is
today's": the setting, the day math and the validation.

Setting: app_settings key "es_tense_focus", JSON
    {"enabled": bool, "tenses": [codes], "days": int,
     "anchor_date": "YYYY-MM-DD", "anchor_index": int,
     "weighted_persons": bool, "verb_tenses": {code: weight} | null}
`verb_tenses` (default null): when set, verbs that are NOT form cards ignore
today's tense and get a tense by these weights (e.g. 80 Indefinido / 20
Imperfecto); form cards keep following today's tense.
`weighted_persons` (#1479, default on): persons rotate by real usage frequency
(verb_slots.weights()["persons"]) instead of evenly.
The tense of a day = tenses[(anchor_index + (day - anchor_date) // days) % n],
so the rotation wraps around after the last tense. The user only toggles which
tenses take part; their ORDER is always the canonical one of languages.py.

current() is total (None on any failure); validate()/step() raise ValueError
with a readable reason.
"""
import json
import logging
from datetime import date, timedelta

import database
import lexicon
import verb_slots

logger = logging.getLogger(__name__)

LANG = "es"
KEY = "es_tense_focus"
RELEASED_KEY = "es_form_intro_released"  # ordered list, see database.sync_form_card_pause
MIN_DAYS, MAX_DAYS = 1, 30
_KNOWN_KEYS = {"enabled", "tenses", "days", "weighted_persons", "verb_tenses"}
MAX_WEIGHT = 1000


def canonical_tenses() -> list[str]:
    """All tenses that may take part, in canonical order (the verb-slot config)."""
    cfg = verb_slots.config(LANG)
    return list(cfg["tenses"]) if cfg else []


def tense_label(code: str) -> str:
    return lexicon._TENSE_LABELS.get(code) or code


def available() -> list[dict]:
    return [{"code": t, "label": tense_label(t)} for t in canonical_tenses()]


def _store(data: dict) -> None:
    database.set_app_setting(KEY, json.dumps(data))


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def settings() -> dict:
    """The full setting with defaults merged in. Broken stored data logs a
    warning and falls back to the defaults. A missing anchor is persisted as
    (today, index 0) so day 1 is the first tense and later days count from it."""
    canon = canonical_tenses()
    out = {"enabled": True, "tenses": list(canon), "days": 1,
           "anchor_date": None, "anchor_index": 0, "weighted_persons": True,
           "verb_tenses": None}
    stored: dict = {}
    raw = database.get_app_setting(KEY)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = parsed
            else:
                logger.warning("tense focus: stored value is not an object, using defaults")
        except ValueError:
            logger.warning("tense focus: stored value is not JSON, using defaults")
    if isinstance(stored.get("enabled"), bool):
        out["enabled"] = stored["enabled"]
    if isinstance(stored.get("weighted_persons"), bool):
        out["weighted_persons"] = stored["weighted_persons"]
    if stored.get("verb_tenses") is not None:
        try:
            out["verb_tenses"] = _clean_verb_tenses(stored["verb_tenses"])
        except ValueError as e:
            logger.warning("tense focus: stored verb_tenses ignored — %s", e)
    tenses = stored.get("tenses")
    if isinstance(tenses, list):
        picked = [t for t in canon if t in tenses]  # canonical order, known codes only
        if picked:
            out["tenses"] = picked
        else:
            logger.warning("tense focus: stored tenses are empty/unknown, using defaults")
    days = stored.get("days")
    if isinstance(days, int) and not isinstance(days, bool) and MIN_DAYS <= days <= MAX_DAYS:
        out["days"] = days
    anchor = _as_date(stored.get("anchor_date"))
    index = stored.get("anchor_index")
    if anchor is not None and isinstance(index, int) and not isinstance(index, bool):
        out["anchor_date"] = anchor.isoformat()
        out["anchor_index"] = index
    else:
        out["anchor_date"] = database.anki_today().isoformat()
        out["anchor_index"] = 0
        if canon:
            _store(out)
    return out


def _index_on(s: dict, day: date) -> int:
    n = len(s["tenses"])
    anchor = date.fromisoformat(s["anchor_date"])
    return (s["anchor_index"] + (day - anchor).days // s["days"]) % n


def current(today=None) -> dict | None:
    """{"enabled", "tense", "index", "count", "label", "next_change"} for the
    day, or None when the focus is off / has no tenses / anything fails."""
    try:
        s = settings()
        if not s["enabled"] or not s["tenses"]:
            return None
        day = _as_date(today) or database.anki_today()
        idx = _index_on(s, day)
        anchor = date.fromisoformat(s["anchor_date"])
        periods = (day - anchor).days // s["days"]
        next_change = anchor + timedelta(days=(periods + 1) * s["days"])
        tense = s["tenses"][idx]
        return {"enabled": True, "tense": tense, "index": idx,
                "count": len(s["tenses"]), "label": tense_label(tense),
                "next_change": next_change.isoformat()}
    except Exception as e:
        logger.warning("tense focus unavailable — %s", e)
        return None


def sync_form_cards() -> int:
    """Pause irregular form cards of other tenses than today's, restore the
    rest (#1438). Returns the number of cards changed. A failing current()
    while the focus is enabled must not mass-restore, so it changes nothing."""
    cur = current()
    s = settings()
    if cur is None and s["enabled"]:
        logger.warning("tense focus: current tense unavailable, form card pause not synced")
        return 0
    if cur is not None:
        released = []
        raw = database.get_app_setting(RELEASED_KEY)
        if raw:
            try:
                parsed = json.loads(raw)
                released = [str(t) for t in parsed] if isinstance(parsed, list) else []
            except ValueError:
                logger.warning("tense focus: released tenses setting is broken")
        if (released and all(t in released for t in s["tenses"])
                and cur["tense"] != released[-1]):
            # The introduction round is over: back to the normal pattern planner.
            logger.info("tense focus: all %d tenses introduced, switching the focus off",
                        len(s["tenses"]))
            save({"enabled": False})
            return database.sync_form_card_pause(None)
    return database.sync_form_card_pause(cur["tense"] if cur else None)


def _clean_verb_tenses(v) -> dict | None:
    """None/{} = off. Otherwise {known tense code: weight 0..MAX_WEIGHT}, zeros
    dropped, canonical order, at least one positive weight. Raises ValueError."""
    if v is None or v == {}:
        return None
    if not isinstance(v, dict):
        raise ValueError("'verb_tenses' must be an object or null")
    canon = canonical_tenses()
    for code, w in v.items():
        if code not in canon:
            raise ValueError(f"unknown tense {code!r}")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 <= w <= MAX_WEIGHT:
            raise ValueError(f"weight of {code} must be a number between 0 and {MAX_WEIGHT}")
    out = {t: v[t] for t in canon if t in v and v[t] > 0}
    if not out:
        raise ValueError("'verb_tenses' needs at least one weight above 0")
    return out


def verb_tense_weights() -> dict | None:
    """Tense weights for non-form-card verbs while the focus is on, or None
    (they follow today's tense). Never raises."""
    try:
        s = settings()
        return s["verb_tenses"] if s["enabled"] else None
    except Exception as e:
        logger.warning("verb tense weights unavailable — %s", e)
        return None


def validate(body) -> dict:
    """Validate a partial payload; returns only the given keys, normalized
    (tenses deduped and in canonical order). Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    unknown = sorted(set(body) - _KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(map(str, unknown))}")
    out: dict = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise ValueError("'enabled' must be true or false")
        out["enabled"] = body["enabled"]
    if "weighted_persons" in body:
        if not isinstance(body["weighted_persons"], bool):
            raise ValueError("'weighted_persons' must be true or false")
        out["weighted_persons"] = body["weighted_persons"]
    if "verb_tenses" in body:
        out["verb_tenses"] = _clean_verb_tenses(body["verb_tenses"])
    if "tenses" in body:
        v = body["tenses"]
        canon = canonical_tenses()
        if not isinstance(v, list) or not v:
            raise ValueError("'tenses' must be a non-empty list")
        for t in v:
            if not isinstance(t, str) or t not in canon:
                raise ValueError(f"unknown tense {t!r}")
        out["tenses"] = [t for t in canon if t in v]
    if "days" in body:
        v = body["days"]
        if isinstance(v, bool) or not isinstance(v, int) or not MIN_DAYS <= v <= MAX_DAYS:
            raise ValueError(f"'days' must be an integer between {MIN_DAYS} and {MAX_DAYS}")
        out["days"] = v
    return out


def save(clean: dict) -> None:
    """Store the keys of a validate() result. Changing `days` or `tenses`
    re-anchors at today with the CURRENT tense (its index in the new list, 0 if
    it dropped out) so today's tense does not jump."""
    old = settings()
    today = database.anki_today()
    new = {**old, **clean}
    if new["tenses"] != old["tenses"] or new["days"] != old["days"]:
        current_tense = old["tenses"][_index_on(old, today)]
        new["anchor_date"] = today.isoformat()
        new["anchor_index"] = (new["tenses"].index(current_tense)
                               if current_tense in new["tenses"] else 0)
    _store(new)


def step(delta: int) -> dict | None:
    """Move to the previous (-1) / next (+1) tense, starting today's period now.
    Returns current()."""
    if delta not in (-1, 1):
        raise ValueError("delta must be 1 or -1")
    s = settings()
    if not s["tenses"]:
        raise ValueError("no tenses available")
    today = database.anki_today()
    s["anchor_index"] = (_index_on(s, today) + delta) % len(s["tenses"])
    s["anchor_date"] = today.isoformat()
    _store(s)
    return current()

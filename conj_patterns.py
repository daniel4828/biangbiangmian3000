"""Conjugation PATTERNS and their scheduler (#1395).

A Spanish verb card's story sentence carries a (tense, person) slot
(verb_slots.py). Besides the vocabulary rating, the user rates the conjugation
(right/wrong). That rating does not belong to the verb but to the PATTERN the
concrete form follows: "tengo" (yo-go), "tuve" (strong preterite with -u-),
"hablo" (regular -ar present). A miss brings the same pattern back in a few
days with a different verb.

Pattern keys:
    reg:{tense}:{cls}      regular endings (cls: ar|er|ir|er_ir|all)
    irr:{tense}:{group}    a group of irregular verbs (yo_go, strong_u, ...)
    verb:{base}:{tense}    a single verb (ser, ir, estar, haber, dar, ver, or the
                           "Sonderform" fallback for anything no detector caught)

Each pattern has two independent FSRS states (table conj_patterns): mode
"recog" (reading + listening cards) and "prod" (creating cards).

Classification is derived from the lexicon forms, never hand-listed. Everything
on the classification path is total: pattern_for() returns None instead of
raising.
"""
import json
import logging
import math
import unicodedata
from datetime import date, timedelta

import database
import fsrs
import lexicon
import verb_slots

logger = logging.getLogger(__name__)

LANG = "es"
MODES = ("recog", "prod")
TENSES = ("ind_pres", "ind_pret", "ind_impf", "ind_fut", "ind_cond",
          "perf", "subj_pres", "subj_impf")
PERSONS = verb_slots.PERSONS
SPECIAL = ("ser", "ir", "estar", "haber", "dar", "ver")
_SER_IR_TENSES = ("ind_pret", "subj_impf")

# Settings (app_settings, Spanish only)
KEY_ENABLED = "es_conj_enabled"
KEY_NEW_PER_DAY = "es_conj_new_per_day"
KEY_RETENTION = "es_conj_retention"
DEFAULT_NEW_PER_DAY = 3
MAX_NEW_PER_DAY = 50
DEFAULT_RETENTION = 0.90
MIN_RETENTION, MAX_RETENTION = 0.80, 0.97
KEY_PULL_PER_DAY = "es_conj_pull_per_day"
KEY_NO_BURY = "es_conj_irregular_no_bury"
KEY_PULL_BATCH = "es_conj_pull_batch"
DEFAULT_PULL_PER_DAY = 10
MAX_PULL_PER_DAY = 50


def mode_for_category(category) -> str:
    """Creating cards train production, reading + listening recognition."""
    return "prod" if category == "creating" else "recog"


# ---------------------------------------------------------------------------
# Regular forms
# ---------------------------------------------------------------------------

_COND = ("ía", "ías", "ía", "íamos", "íais", "ían")
_SUBJ_PRES = {"ar": ("e", "es", "e", "emos", "éis", "en"),
              "er": ("a", "as", "a", "amos", "áis", "an")}
_SUBJ_IMPF = {"ar": ("ara", "aras", "ara", "áramos", "arais", "aran"),
              "er": ("iera", "ieras", "iera", "iéramos", "ierais", "ieran")}


def _unaccent(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(ch))


def _regular(base: str, tense: str, person: str) -> str | None:
    """Rule-based regular form (perf: the regular participle); None when the
    verb is no plain -ar/-er/-ir infinitive."""
    if len(base) < 2 or base[-2:] not in ("ar", "er", "ir") or person not in PERSONS:
        return None
    idx = PERSONS.index(person)
    stem = base[:-2]
    kind = "ar" if base.endswith("ar") else "er"
    if tense in ("ind_pres", "ind_pret", "ind_impf", "ind_fut"):
        return verb_slots.regular_form(base, tense, person)
    if tense == "ind_cond":
        return base + _COND[idx]
    if tense == "subj_pres":
        return stem + _SUBJ_PRES[kind][idx]
    if tense == "subj_impf":
        return stem + _SUBJ_IMPF[kind][idx]
    if tense == "perf":
        return stem + ("ado" if kind == "ar" else "ido")
    return None


def _lex_form(base: str, tense: str, person: str) -> str | None:
    if tense == "perf":
        return lexicon.form_for(base, "part", "", LANG)
    return lexicon.form_for(base, tense, person, LANG)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_REG_CLASSES = {
    "ind_pres": ("ar", "er", "ir"),
    "ind_pret": ("ar", "er_ir"),
    "ind_impf": ("ar", "er_ir"),
    "ind_fut": ("all",),
    "ind_cond": ("all",),
    "perf": ("ar", "er_ir"),
    "subj_pres": ("ar", "er_ir"),
    "subj_impf": ("ar", "er_ir"),
}
_CLASS_LABEL = {"ar": "-ar", "er": "-er", "ir": "-ir", "er_ir": "-er/-ir", "all": None}

_GROUPS: dict[tuple[str, str], str] = {
    ("ind_pres", "yo_go"): "Presente · yo auf -go (tengo, hago, pongo)",
    ("ind_pres", "yo_zco"): "Presente · yo auf -zco (conozco, parezco)",
    ("ind_pres", "e_ie"): "Presente · Stammwechsel e→ie (tienes, piensa)",
    ("ind_pres", "o_ue"): "Presente · Stammwechsel o→ue (puedo, duerme)",
    ("ind_pres", "e_i"): "Presente · Stammwechsel e→i (pido, dice)",
    ("ind_pres", "u_ue"): "Presente · Stammwechsel u→ue (juego)",
    ("ind_pres", "uir_y"): "Presente · -uir mit y (construyo, huyes)",
    ("ind_pres", "ortho"): "Presente · Rechtschreibwechsel (escojo, venzo, distingo)",
    ("ind_pret", "strong_u"): "Indefinido · starker Stamm mit -u- (tuve, pude, puse, supe)",
    ("ind_pret", "strong_i"): "Indefinido · starker Stamm mit -i- (hice, quise, vine)",
    ("ind_pret", "strong_j"): "Indefinido · starker Stamm auf -j (dije, traje, conduje)",
    ("ind_pret", "stem_3rd"): "Indefinido · Stammwechsel in der 3. Person (pidió, durmió)",
    ("ind_pret", "y_3rd"): "Indefinido · i→y / Akzent (leyó, oyeron, leíste)",
    ("ind_pret", "ortho"): "Indefinido · Rechtschreibwechsel in der 1. Sg. (busqué, llegué, empecé)",
    ("ind_fut", "stem"): "Futuro · unregelmäßiger Stamm (tendr-, har-, dir-)",
    ("ind_cond", "stem"): "Condicional · unregelmäßiger Stamm (tendría, haría, diría)",
    ("perf", "participle"): "Perfecto · unregelmäßiges Partizip (hecho, dicho, visto, puesto)",
    ("subj_pres", "yo_stem"): "Subjuntivo presente · vom yo-Stamm (tenga, haga, conozca, pida)",
    ("subj_pres", "ortho"): "Subjuntivo presente · Rechtschreibwechsel (busque, llegue, empiece, escoja)",
    ("subj_impf", "strong_u"): "Subjuntivo imperfecto · starker Stamm mit -u- (tuviera, pudiera)",
    ("subj_impf", "strong_i"): "Subjuntivo imperfecto · starker Stamm mit -i- (hiciera, quisiera)",
    ("subj_impf", "strong_j"): "Subjuntivo imperfecto · starker Stamm auf -j (dijera, trajera)",
    ("subj_impf", "stem_3rd"): "Subjuntivo imperfecto · Stammwechsel (pidiera, durmiera)",
    ("subj_impf", "y_3rd"): "Subjuntivo imperfecto · i→y (leyera, oyera, construyera)",
}


def _tense_name(tense: str) -> str:
    return lexicon._TENSE_LABELS.get(tense, tense)


def _reg_label(tense: str, cls: str) -> str:
    suffix = _CLASS_LABEL.get(cls)
    name = _tense_name(tense)
    return f"{name} · {suffix} (regelmäßig)" if suffix else f"{name} (regelmäßig)"


def _reg_class(tense: str, base: str) -> str:
    if tense in ("ind_fut", "ind_cond"):
        return "all"
    if tense == "ind_pres":
        return base[-2:]
    return "ar" if base.endswith("ar") else "er_ir"


def _verb_pattern(base: str, tense: str, sonder: bool) -> dict:
    if base in ("ser", "ir") and tense in _SER_IR_TENSES:
        name, disp = "ser_ir", "ser/ir"
    else:
        name = disp = base
    label = f"{disp} · {_tense_name(tense)}" + (" (Sonderform)" if sonder else "")
    return {"key": f"verb:{name}:{tense}", "label": label, "kind": "verb"}


def label_for_key(key: str) -> str:
    """German label of a pattern key (the key itself when it can't be parsed)."""
    try:
        parts = key.split(":")
        if parts[0] == "reg" and len(parts) == 3:
            return _reg_label(parts[1], parts[2])
        if parts[0] == "irr" and len(parts) == 3:
            return _GROUPS.get((parts[1], parts[2])) or key
        if parts[0] == "verb" and len(parts) == 3:
            name, tense = parts[1], parts[2]
            sonder = name not in SPECIAL and name != "ser_ir"
            disp = "ser/ir" if name == "ser_ir" else name
            return f"{disp} · {_tense_name(tense)}" + (" (Sonderform)" if sonder else "")
    except Exception:
        pass
    return key


# ---------------------------------------------------------------------------
# Detectors for irregular forms
# ---------------------------------------------------------------------------

def _vowel_change(stem: str, ending: str, form: str, pairs) -> str | None:
    """Label of the first (old, new) stem-vowel replacement that turns the
    regular form into `form` (last matching vowel first)."""
    for old, new, label in pairs:
        for i in range(len(stem) - 1, -1, -1):
            if stem[i] == old and form == stem[:i] + new + stem[i + 1:] + ending:
                return label
    return None


def _spell_before_e(base: str) -> str | None:
    """Stem spelling in front of e (-car/-gar/-zar/-guar)."""
    stem = base[:-2]
    if base.endswith("car"):
        return stem[:-1] + "qu"
    if base.endswith("gar"):
        return stem + "u"
    if base.endswith("zar"):
        return stem[:-1] + "c"
    if base.endswith("guar"):
        return stem[:-1] + "ü"
    return None


def _spell_before_ao(base: str) -> str | None:
    """Stem spelling in front of a/o (-ger/-gir/-cer/-cir after a consonant,
    -guir, -quir)."""
    stem = base[:-2]
    if base.endswith(("ger", "gir")):
        return stem[:-1] + "j"
    if base.endswith(("cer", "cir")) and len(stem) >= 2 and stem[-2] not in "aeiouáéíóú":
        return stem[:-1] + "z"
    if base.endswith("guir"):
        return stem[:-1]
    if base.endswith("quir"):
        return stem[:-2] + "c"
    return None


def _ortho(base: str, tense: str, person: str, form: str, reg: str) -> bool:
    stem = base[:-2]
    end = reg[len(stem):]
    ar = base.endswith("ar")
    if tense == "ind_pret":
        alt = _spell_before_e(base) if (person == "1s" and ar) else None
    elif tense == "ind_pres":
        alt = _spell_before_ao(base) if person == "1s" else None
    elif tense == "subj_pres":
        alt = _spell_before_e(base) if ar else _spell_before_ao(base)
    else:
        alt = None
    return alt is not None and form == alt + end


def _strong_pret_group(base: str) -> str | None:
    """strong_u / strong_i / strong_j when the verb's 1s preterite is a strong
    one (ends in unaccented -e and differs from the regular form)."""
    first = lexicon.form_for(base, "ind_pret", "1s", LANG)
    reg1 = _regular(base, "ind_pret", "1s")
    if not first or not reg1:
        return None
    first = first.lower()
    if not first.endswith("e") or first == reg1:
        return None
    stem = first[:-1]
    if stem.endswith("j"):
        return "strong_j"
    for ch in reversed(_unaccent(stem)):
        if ch in "aeiou":
            return {"u": "strong_u", "i": "strong_i"}.get(ch)
    return None


def _group_pres(base, person, form, reg):
    stem = base[:-2]
    end = reg[len(stem):]
    if person == "1s":
        if form.endswith("go") and not reg.endswith("go"):
            return "yo_go"
        if form.endswith("zco") and not reg.endswith("zco"):
            return "yo_zco"
    if base.endswith("uir") and not base.endswith(("guir", "quir")) \
            and form == stem + "y" + end:
        return "uir_y"
    change = _vowel_change(stem, end, form, (
        ("e", "ie", "e_ie"), ("o", "ue", "o_ue"), ("e", "i", "e_i"), ("u", "ue", "u_ue")))
    if change:
        return change
    if _ortho(base, "ind_pres", person, form, reg):
        return "ortho"
    return None


def _group_pret(base, person, form, reg):
    strong = _strong_pret_group(base)
    if strong:
        return strong
    stem = base[:-2]
    end = reg[len(stem):]
    if person in ("3s", "3p"):
        if _vowel_change(stem, end, form, (("e", "i", "e_i"), ("o", "u", "o_u"))):
            return "stem_3rd"
        if end.startswith("i") and form == stem + "y" + end[1:]:
            return "y_3rd"
    if _unaccent(form) == _unaccent(reg):
        return "y_3rd"
    if person == "1s" and _ortho(base, "ind_pret", person, form, reg):
        return "ortho"
    return None


def _group_subj_pres(base, person, form, reg):
    if _ortho(base, "subj_pres", person, form, reg):
        return "ortho"
    yo = lexicon.form_for(base, "ind_pres", "1s", LANG)
    if yo and yo.lower().endswith("o") and form.startswith(yo.lower()[:-1]):
        return "yo_stem"
    return None


def _group_subj_impf(base):
    f3 = lexicon.form_for(base, "ind_pret", "3p", LANG)
    if not f3:
        return None
    pat = _classify(base, "ind_pret", "3p", f3.lower())
    if pat and pat["kind"] == "group":
        return pat["key"].split(":")[-1]
    return None


def _classify(base: str, tense: str, person: str, form: str) -> dict:
    reg = _regular(base, tense, person)
    if reg is not None and form == reg.lower():
        cls = _reg_class(tense, base)
        return {"key": f"reg:{tense}:{cls}", "label": _reg_label(tense, cls),
                "kind": "regular"}
    if base in SPECIAL:
        return _verb_pattern(base, tense, sonder=False)
    group = None
    if reg is not None:
        reg = reg.lower()
        if tense == "ind_pres":
            group = _group_pres(base, person, form, reg)
        elif tense == "ind_pret":
            group = _group_pret(base, person, form, reg)
        elif tense in ("ind_fut", "ind_cond"):
            group = "stem"
        elif tense == "subj_pres":
            group = _group_subj_pres(base, person, form, reg)
        elif tense == "subj_impf":
            group = _group_subj_impf(base)
        elif tense == "perf":
            group = "participle"
    if group and (tense, group) in _GROUPS:
        return {"key": f"irr:{tense}:{group}", "label": _GROUPS[(tense, group)],
                "kind": "group"}
    return _verb_pattern(base, tense, sonder=True)


def pattern_for(lemma: str, tense: str, person: str, lang: str = LANG) -> dict | None:
    """{"key", "label", "kind"} of the pattern the lexicon form of `lemma` in
    (tense, person) follows; None for non-Spanish, unknown verbs/tenses or a
    missing form. Reflexive verbs are judged by the base verb. Never raises."""
    try:
        if lang != LANG or tense not in TENSES:
            return None
        base = verb_slots._base_verb(lemma)
        if len(base) < 2 or base[-2:] not in ("ar", "er", "ir"):
            return None
        form = _lex_form(base, tense, person)
        if not form:
            return None
        return _classify(base, tense, person, form.lower())
    except Exception as e:
        logger.debug("pattern_for(%r, %r, %r) failed — %s", lemma, tense, person, e)
        return None


def slot_pattern(word_zh: str, slot: str, lang: str = LANG) -> dict | None:
    """Pattern of a story sentence's slot ("ind_pret:3s") for the card's word
    (plain infinitive or a form card like "tener · Indefinido"); None for any
    other word. Never raises."""
    try:
        lemma = verb_slots._slot_lemma(word_zh, lang)
        if not lemma or not slot:
            return None
        tense, _, person = slot.partition(":")
        return pattern_for(lemma, tense, person, lang)
    except Exception:
        return None


def _special_irregular(base: str, tense: str) -> bool:
    for person in (PERSONS if tense != "perf" else ("1s",)):
        form, reg = _lex_form(base, tense, person), _regular(base, tense, person)
        if form and reg and form.lower() != reg.lower():
            return True
    return False


def catalog(lang: str = LANG) -> list[dict]:
    """Every pattern that can occur in the enabled tenses: regular keys, all
    irregular groups, and single-verb keys of the special verbs where they are
    irregular. [{key, label, kind, tense}], tense order of the config, then
    regular, group, verb."""
    if lang != LANG:
        return []
    try:
        cfg = verb_slots.config(lang)
        tenses = [t for t in (cfg["tenses"] if cfg else TENSES) if t in TENSES]
        out = []
        for tense in tenses:
            for cls in _REG_CLASSES[tense]:
                out.append({"key": f"reg:{tense}:{cls}", "label": _reg_label(tense, cls),
                            "kind": "regular", "tense": tense})
            for (t, group), label in _GROUPS.items():
                if t == tense:
                    out.append({"key": f"irr:{tense}:{group}", "label": label,
                                "kind": "group", "tense": tense})
            seen = set()
            for base in SPECIAL:
                if _special_irregular(base, tense):
                    p = _verb_pattern(base, tense, sonder=False)
                    if p["key"] not in seen:
                        seen.add(p["key"])
                        out.append({**p, "tense": tense})
        return out
    except Exception as e:
        logger.warning("conj catalog failed — %s", e)
        return []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings() -> dict:
    """{"enabled", "new_per_day", "desired_retention"}; defaults when unset,
    unparsable stored values fall back to the default with a warning."""
    enabled, new_per_day, retention = True, DEFAULT_NEW_PER_DAY, DEFAULT_RETENTION
    raw = database.get_app_setting(KEY_ENABLED)
    if raw in ("0", "1"):
        enabled = raw == "1"
    raw = database.get_app_setting(KEY_NEW_PER_DAY)
    if raw not in (None, ""):
        try:
            new_per_day = max(0, min(MAX_NEW_PER_DAY, int(raw)))
        except ValueError:
            logger.warning("conj settings: bad new_per_day %r, using default", raw)
    raw = database.get_app_setting(KEY_RETENTION)
    if raw not in (None, ""):
        try:
            v = float(raw)
            if math.isfinite(v):
                retention = max(MIN_RETENTION, min(MAX_RETENTION, v))
        except ValueError:
            logger.warning("conj settings: bad retention %r, using default", raw)
    pull = DEFAULT_PULL_PER_DAY
    raw = database.get_app_setting(KEY_PULL_PER_DAY)
    if raw not in (None, ""):
        try:
            pull = max(0, min(MAX_PULL_PER_DAY, int(raw)))
        except ValueError:
            logger.warning("conj settings: bad pull_per_day %r, using default", raw)
    no_bury = database.get_app_setting(KEY_NO_BURY) != "0"
    return {"enabled": enabled, "new_per_day": new_per_day,
            "desired_retention": retention, "pull_per_day": pull,
            "irregular_creating_unburied": no_bury}


def validate_settings(body) -> dict:
    """Validate a (partial) payload; returns only the given keys, normalized.
    Raises ValueError with a readable reason."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    known = {"enabled", "new_per_day", "desired_retention", "pull_per_day",
             "irregular_creating_unburied"}
    unknown = sorted(set(body) - known)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(map(str, unknown))}")
    out = {}
    if "enabled" in body:
        v = body["enabled"]
        if isinstance(v, bool):
            out["enabled"] = v
        elif isinstance(v, int) and v in (0, 1):
            out["enabled"] = bool(v)
        else:
            raise ValueError("'enabled' must be true or false")
    if "new_per_day" in body:
        v = body["new_per_day"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_NEW_PER_DAY:
            raise ValueError(f"'new_per_day' must be an integer between 0 and {MAX_NEW_PER_DAY}")
        out["new_per_day"] = v
    if "desired_retention" in body:
        v = body["desired_retention"]
        if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                or not MIN_RETENTION <= v <= MAX_RETENTION):
            raise ValueError(
                f"'desired_retention' must be a number between {MIN_RETENTION} and {MAX_RETENTION}")
        out["desired_retention"] = float(v)
    return out


def save_settings(clean: dict) -> None:
    """Store the keys of a validate_settings() result."""
    if "enabled" in clean:
        database.set_app_setting(KEY_ENABLED, "1" if clean["enabled"] else "0")
    if "new_per_day" in clean:
        database.set_app_setting(KEY_NEW_PER_DAY, str(int(clean["new_per_day"])))
    if "desired_retention" in clean:
        database.set_app_setting(KEY_RETENTION, repr(float(clean["desired_retention"])))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

_SNAPSHOT_FIELDS = ("state", "stability", "difficulty", "due", "last_review",
                    "reps", "lapses")


def _snapshot(row: dict | None) -> str | None:
    if row is None:
        return None
    return json.dumps({k: row.get(k) for k in _SNAPSHOT_FIELDS})


def _rate(base: dict | None, ok: bool, today: str, retention: float) -> dict:
    """Apply one rating (Good for ok, Again otherwise) to the state fields in
    `base` (None / state 'new' = never rated). Returns the new state fields."""
    w = fsrs.DEFAULT_WEIGHTS
    rating = 3 if ok else 1
    today_d = date.fromisoformat(today)
    reps = int((base or {}).get("reps") or 0) + 1
    lapses = int((base or {}).get("lapses") or 0)
    fresh = (base is None or base.get("state") in (None, "new")
             or base.get("stability") is None or base.get("difficulty") is None)
    if fresh:
        s = fsrs.init_stability(w, rating)
        d = fsrs.init_difficulty(w, rating)
    else:
        last = base.get("last_review")
        elapsed = max(0, (today_d - date.fromisoformat(last)).days) if last else 0
        s, d = fsrs.review_state(w, base["difficulty"], base["stability"], elapsed, rating)
        if not ok:
            lapses += 1
    if ok:
        state = "review"
        due = today_d + timedelta(days=fsrs.next_interval(s, retention))
    else:
        state = "learning"
        due = today_d + timedelta(days=1)
    return {"state": state, "stability": s, "difficulty": d, "due": due.isoformat(),
            "last_review": today, "reps": reps, "lapses": lapses}


def record(lang: str, pattern: str, mode: str, ok: bool, *, card_id=None,
           sentence_id=None, slot=None, lemma=None, form=None) -> dict:
    """Rate a pattern (ok = Good, otherwise Again), update its FSRS state and
    log it. Returns {"pattern", "label", "mode", "ok", "due", "state", "undo"}.

    Same-day rule: only the first rating of a day moves the state, except that
    a first success can still be corrected to a failure once (recomputed from
    the snapshot taken before that day's first rating)."""
    if mode not in MODES:
        raise ValueError(f"unknown conjugation mode {mode!r}")
    if not pattern:
        raise ValueError("pattern is required")
    ok = bool(ok)
    retention = settings()["desired_retention"]
    today = database.anki_today().isoformat()
    row_before = database.get_conj_state(lang, pattern, mode)

    if row_before is not None and row_before.get("day") == today:
        if ok or row_before.get("day_failed"):
            row_after = dict(row_before)  # log only
        else:
            snap = json.loads(row_before["day_before"]) if row_before.get("day_before") else None
            row_after = {**row_before, **_rate(snap, False, today, retention),
                         "day_failed": 1}
    else:
        row_after = {
            "lang": lang, "pattern": pattern, "mode": mode,
            **_rate(row_before, ok, today, retention),
            "day": today, "day_before": _snapshot(row_before),
            "day_failed": 0 if ok else 1,
        }
    row_after.update({"lang": lang, "pattern": pattern, "mode": mode})
    database.save_conj_state(row_after)
    log_id = database.add_conj_log(lang, pattern, mode, ok, today, card_id=card_id,
                                   sentence_id=sentence_id, slot=slot, lemma=lemma,
                                   form=form)
    return {
        "pattern": pattern, "label": label_for_key(pattern), "mode": mode, "ok": ok,
        "due": row_after["due"], "state": row_after["state"],
        "undo": {"lang": lang, "pattern": pattern, "mode": mode,
                 "row_before": row_before, "log_id": log_id},
    }


def undo_record(undo: dict) -> None:
    """Revert one record(): restore the previous state row (delete it if the
    pattern was new) and drop the log row."""
    if undo.get("row_before") is None:
        database.delete_conj_state(undo["lang"], undo["pattern"], undo["mode"])
    else:
        database.save_conj_state(undo["row_before"])
    database.delete_conj_log(undo["log_id"])


# ---------------------------------------------------------------------------
# Status for the UI
# ---------------------------------------------------------------------------

def pattern_status(lang: str, key: str, mode: str, states: dict, stats: dict,
                   today: str) -> dict:
    """{"state", "due", "due_in_days", "interval_days", "reps", "lapses",
    "total", "correct"} of one (pattern, mode); never-rated = state "new"."""
    row = states.get((key, mode))
    st = stats.get((key, mode)) or {}
    state = (row or {}).get("state") or "new"
    due = row.get("due") if row and state != "new" else None
    due_in = interval = None
    try:
        if due:
            due_in = (date.fromisoformat(due) - date.fromisoformat(today)).days
            if row.get("last_review"):
                interval = (date.fromisoformat(due) - date.fromisoformat(row["last_review"])).days
    except ValueError:
        logger.warning("conj status: bad date in %s/%s", key, mode)
    return {"state": state, "due": due, "due_in_days": due_in, "interval_days": interval,
            "reps": int((row or {}).get("reps") or 0),
            "lapses": int((row or {}).get("lapses") or 0),
            "total": int(st.get("total") or 0), "correct": int(st.get("correct") or 0)}


# ---------------------------------------------------------------------------
# Story slot planning (used by verb_slots.assign_slots)
# ---------------------------------------------------------------------------

class SlotPlanner:
    """Priority of conjugation patterns when slots are assigned for one batch:
    1. DUE   - a pattern whose review is due (each (pattern, mode) once per call)
    2. NEW   - a not yet introduced pattern while the daily budget lasts
    3. rest  - the caller's weighted rotation, restricted to introduced patterns
    """

    def __init__(self, states: dict, budget: dict, today: str):
        self.states = states
        self.budget = dict(budget)
        self.today = today
        self.used_due: set = set()
        self.planned_new: set = set()

    @classmethod
    def create(cls, lang, conj_states=None, new_budget=None, today=None):
        """None when conjugation scheduling is disabled."""
        s = settings()
        if not s["enabled"]:
            return None
        if today is None:
            today = database.anki_today().isoformat()
        elif hasattr(today, "isoformat"):
            today = today.isoformat()
        states = conj_states if conj_states is not None else database.get_conj_states(lang)
        if new_budget is None:
            budget = {m: max(0, s["new_per_day"] - database.conj_introduced_today(lang, m, today))
                      for m in MODES}
        elif isinstance(new_budget, int):
            budget = {m: new_budget for m in MODES}
        else:
            budget = {m: int(new_budget.get(m, 0)) for m in MODES}
        return cls(states, budget, today)

    def introduced(self, mode: str, key) -> bool:
        row = self.states.get((key, mode)) if key else None
        return bool(row) and row.get("state") != "new"

    def choose(self, mode: str, cands: list, rng):
        """cands = [((slot, form, weight), pattern_key | None)]. Returns the
        chosen (option, pattern_key) from stage DUE or NEW, or None."""
        due = []
        for opt, key in cands:
            if not key or (key, mode) in self.used_due:
                continue
            row = self.states.get((key, mode))
            if (row and row.get("state") != "new" and row.get("due")
                    and row["due"] <= self.today):
                due.append((opt, key, row["due"]))
        if due:
            oldest = min(d[2] for d in due)
            pool = [d for d in due if d[2] == oldest]
            top = max(d[0][2] for d in pool)
            opt, key, _ = rng.choice([d for d in pool if d[0][2] == top])
            self.used_due.add((key, mode))
            return opt, key
        if self.budget.get(mode, 0) > 0:
            fresh = [(opt, key) for opt, key in cands
                     if key and not self.introduced(mode, key)
                     and (key, mode) not in self.planned_new]
            if fresh:
                top = max(o[2] for o, _ in fresh)
                opt, key = rng.choice([c for c in fresh if c[0][2] == top])
                self.budget[mode] -= 1
                self.planned_new.add((key, mode))
                return opt, key
        return None

"""Verb slot rotation for story sentences (#1295).

For Chinese, knowing a word = being able to use it. For Spanish, knowing "ir"
is not knowing "fui": a story that always shows verbs as infinitives never
practises conjugation. So each time a story sentence is generated for a verb
card we pick a (tense, person) SLOT and require the sentence to use exactly
that form. Slots are rotated by weighted fair rotation (#1306): the slot with
the smallest (times used + 1) / weight across ALL verbs wins (history =
story_sentences.verb_slot, no extra table), so the long-run share of a slot is
about its share of the total weight (tense weight x person weight, defaults
from corpus frequency, editable in Settings); weight 0 = never. Verbs keep
their three cards; nothing new is scheduled.

Slot codes are "tense:person" ("ind_pret:3s"). Tense codes are the lexicon's
(lexicon.py, #1293) plus the composed "perf" (haber + participle). Which
tenses/persons are enabled lives in languages.py ("verb_slots").

Reflexive infinitives (quejarse, #1301) are supported: the forms come from the
base verb (quejar) and the reflexive pronoun is put in front by person
("me quejé", "te quejas", "me he quejado").

Everything on the generation path is total: failures log a warning and degrade
to "no slots" (the story is generated as before).
"""
import json
import logging
import math
import random

import database
import languages
import lexicon

logger = logging.getLogger(__name__)

# Fallback for the auxiliary of the perfect tenses if the lexicon lacks haber.
_HABER_PRES = {"1s": "he", "2s": "has", "3s": "ha", "1p": "hemos", "2p": "habéis", "3p": "han"}

_TENSE_EN = {
    "ind_pres": "Presente (present)",
    "ind_pret": "Indefinido (preterite)",
    "ind_impf": "Imperfecto (imperfect)",
    "ind_fut": "Futuro (future)",
    "ind_cond": "Condicional (conditional)",
    "perf": "Pretérito perfecto (present perfect)",
    "subj_pres": "Presente de subjuntivo (present subjunctive — the sentence needs a "
                 "trigger such as 'que', 'ojalá', 'para que')",
    "subj_impf": "Imperfecto de subjuntivo (imperfect subjunctive, -ra form — needs a "
                 "trigger such as 'si', 'quería que', 'como si')",
}
# Reflexive pronoun in front of the conjugated form (#1301).
_REFLEXIVE_PRONOUN = {"1s": "me", "2s": "te", "3s": "se", "1p": "nos", "2p": "os", "3p": "se"}
_PERSON_EN = {"1s": "1st person singular", "2s": "2nd person singular",
              "3s": "3rd person singular", "1p": "1st person plural",
              "2p": "2nd person plural", "3p": "3rd person plural"}


def config(lang: str) -> dict | None:
    """The language's "verb_slots" config, or None when the feature is off."""
    try:
        cfg = languages.get_lang_config(lang).get("verb_slots")
        return cfg if cfg and cfg.get("tenses") and cfg.get("persons") else None
    except Exception:
        return None


def is_slottable_verb(word: str, lang: str) -> bool:
    """True for a plain verb infinitive the lexicon can conjugate."""
    try:
        if not config(lang) or not lexicon.available(lang):
            return False
        w = (word or "").strip().lower()
        if not w or len(w.split()) != 1:
            return False
        if w.endswith("se") and len(w) > 4:  # reflexive: judge the base verb (#1301)
            w = w[:-2]
        info = lexicon.lemma_info(w, lang)
        if not info or info.get("pos") != "v":
            return False
        return lexicon.form_for(w, "ind_pres", "1s", lang) is not None
    except Exception:
        return False


def target_form(lemma: str, tense: str, person: str, lang: str) -> str | None:
    """The exact string a sentence must contain for this slot, or None.

    Reflexive infinitives (quejarse) use the base verb's form with the
    pronoun in front: "me quejé", "me he quejado" (#1301)."""
    try:
        lemma = (lemma or "").strip().lower()
        pronoun = ""
        if lemma.endswith("se") and len(lemma) > 4:
            pronoun = _REFLEXIVE_PRONOUN.get(person, "")
            if not pronoun:
                return None
            lemma = lemma[:-2]
        if tense == "perf":
            aux = lexicon.form_for("haber", "ind_pres", person, lang) or _HABER_PRES.get(person)
            part = lexicon.form_for(lemma, "part", "", lang)
            form = f"{aux} {part}" if aux and part else None
        else:
            form = lexicon.form_for(lemma, tense, person, lang)
        if not form:
            return None
        return f"{pronoun} {form}" if pronoun else form
    except Exception:
        return None


def describe(slot: str) -> str:
    """English description for the prompt: "Indefinido (preterite), 3rd person singular"."""
    tense, _, person = (slot or "").partition(":")
    return f"{_TENSE_EN.get(tense, tense)}, {_PERSON_EN.get(person, person)}"


# ---------------------------------------------------------------------------
# Irregular-verb form cards (#1296)
#
# One CARD per (irregular verb, irregular tense) — "tener · Indefinido" — not
# one per person. The card is a plain `entries` row (note_type 'grammar'); the
# PERSON rotates inside it via assign_slots(): every story sentence for the card
# uses another person of that tense, least-used first. Irregularity is DERIVED
# (lexicon forms vs. rule-generated regular forms), never hand-listed.
# ---------------------------------------------------------------------------

_FORM_LANG = "es"
FORM_CARD_TENSES = ("ind_pret", "ind_pres", "ind_fut", "ind_impf")
PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")
_SEP = " · "

_ENDINGS = {
    ("ind_pres", "ar"): ("o", "as", "a", "amos", "áis", "an"),
    ("ind_pres", "er"): ("o", "es", "e", "emos", "éis", "en"),
    ("ind_pres", "ir"): ("o", "es", "e", "imos", "ís", "en"),
    ("ind_pret", "ar"): ("é", "aste", "ó", "amos", "asteis", "aron"),
    ("ind_pret", "er"): ("í", "iste", "ió", "imos", "isteis", "ieron"),
    ("ind_pret", "ir"): ("í", "iste", "ió", "imos", "isteis", "ieron"),
    ("ind_impf", "ar"): ("aba", "abas", "aba", "ábamos", "abais", "aban"),
    ("ind_impf", "er"): ("ía", "ías", "ía", "íamos", "íais", "ían"),
    ("ind_impf", "ir"): ("ía", "ías", "ía", "íamos", "íais", "ían"),
}
_FUT_ENDINGS = ("é", "ás", "á", "emos", "éis", "án")
_PERSON_DE = {"1s": "1. Sg.", "2s": "2. Sg.", "3s": "3. Sg.",
              "1p": "1. Pl.", "2p": "2. Pl.", "3p": "3. Pl."}


def regular_form(lemma: str, tense: str, person: str) -> str | None:
    """Rule-based regular form of an -ar/-er/-ir infinitive; None for anything
    else (other endings, reflexive -se, unknown tense/person). Never raises."""
    try:
        lemma = (lemma or "").strip().lower()
        # len 2 is allowed on purpose: "ir" = empty stem + the -ir endings.
        if person not in PERSONS or len(lemma) < 2 or lemma.endswith("se"):
            return None
        idx = PERSONS.index(person)
        if tense == "ind_fut":
            if lemma[-2:] not in ("ar", "er", "ir"):
                return None
            return lemma + _FUT_ENDINGS[idx]
        endings = _ENDINGS.get((tense, lemma[-2:]))
        if not endings:
            return None
        return lemma[:-2] + endings[idx]
    except Exception:
        return None


def _lexicon_row(lemma: str, tense: str) -> list[str | None]:
    return [lexicon.form_for(lemma, tense, p, _FORM_LANG) for p in PERSONS]


def irregular_tenses(lemma: str, lang: str) -> list[str]:
    """Tenses (FORM_CARD_TENSES order) in which any lexicon form differs from
    the regular one (case-insensitive; persons the lexicon lacks are skipped).

    Orthographic changes (busqué, leyó, empecé) count as irregular too — they
    are exactly the forms worth drilling, so this is deliberate. Never raises."""
    out = []
    try:
        lemma = (lemma or "").strip().lower()
        for tense in FORM_CARD_TENSES:
            for person, form in zip(PERSONS, _lexicon_row(lemma, tense)):
                if not form:
                    continue
                reg = regular_form(lemma, tense, person)
                if reg is None:
                    break
                if form.lower() != reg.lower():
                    out.append(tense)
                    break
    except Exception as e:
        logger.debug("irregular_tenses(%r) failed — %s", lemma, e)
    return out


def verb_regularity(lemma: str, lang: str) -> str | None:
    """"regular" / "irregular" for a verb headword, None when it is no verb
    the lexicon knows (#1315: Etikett "reg."/"irreg." auf der Karte).

    Same definition as irregular_tenses() — orthographic changes count as
    irregular. Never raises."""
    try:
        if lang != _FORM_LANG:
            return None
        lemma = (lemma or "").strip().lower()
        # Reflexiv (rendirse, quejarse): Regularität des Grundverbs gilt (#1352)
        if len(lemma) > 4 and lemma.endswith(("arse", "erse", "irse")):
            lemma = lemma[:-2]
        if not any(f for t in FORM_CARD_TENSES for f in _lexicon_row(lemma, t)):
            return None
        return "irregular" if irregular_tenses(lemma, lang) else "regular"
    except Exception as e:
        logger.debug("verb_regularity(%r) failed — %s", lemma, e)
        return None


def form_card_word(lemma: str, tense: str) -> str:
    """Headword of a form card: ("tener", "ind_pret") -> "tener · Indefinido"."""
    return f"{lemma}{_SEP}{lexicon._TENSE_LABELS.get(tense, tense)}"


def parse_form_card(word: str) -> tuple[str, str] | None:
    """Inverse of form_card_word(): "tener · Indefinido" -> ("tener", "ind_pret").

    None unless the pattern matches, the label maps back to a FORM_CARD_TENSES
    tense, and the lexicon knows that verb in that tense. Never raises."""
    try:
        if not word or _SEP not in word:
            return None
        lemma, _, label = word.partition(_SEP)
        lemma, label = lemma.strip().lower(), label.strip()
        if not lemma or len(lemma.split()) != 1:
            return None
        for tense in FORM_CARD_TENSES:
            if lexicon._TENSE_LABELS.get(tense) == label:
                if lexicon.form_for(lemma, tense, "1s", _FORM_LANG) is None:
                    return None
                return lemma, tense
        return None
    except Exception:
        return None


def _differing_persons(lemma: str, tense: str, row: list[str]) -> list[str]:
    out = []
    for person, form in zip(PERSONS, row):
        reg = regular_form(lemma, tense, person)
        if reg is not None and form.lower() != reg.lower():
            out.append(_PERSON_DE[person])
    return out


def _unregular_note(lemma: str, tense: str, row: list[str]) -> str:
    """Generic fallback note. Person labels already end in "." ("3. Sg."), so
    the sentence period is only added when the last label doesn't supply one."""
    text = "Unregelmäßig in: " + (", ".join(_differing_persons(lemma, tense, row))
                                  or "einzelnen Formen")
    return text if text.endswith(".") else text + "."


def _stem_change(lemma: str, row: list[str]) -> str | None:
    """Detect e→ie / o→ue / e→i in the present (3rd person singular)."""
    reg = regular_form(lemma, "ind_pres", "3s")
    if not reg or len(row) < 3:
        return None
    stem, ending = lemma[:-2], reg[len(lemma) - 2:]
    got = row[2].lower()
    for old, new, label in (("e", "ie", "e→ie"), ("o", "ue", "o→ue"), ("e", "i", "e→i")):
        i = stem.rfind(old)
        if i >= 0 and got == stem[:i] + new + stem[i + 1:] + ending:
            return label
    return None


def _pattern(lemma: str, tense: str, row: list[str]) -> tuple[str, str]:
    """(category key, German note) for one form card. The key groups cards that
    share a pattern, for the "Wie: …" list. An EMPTY key means "generic note,
    no real pattern category" — such cards get no "Wie:" (it would group
    unrelated verbs: ser, estar, ver, saber)."""
    if tense == "ind_pret":
        reg1 = (regular_form(lemma, tense, "1s") or "").lower()
        first = row[0].lower()
        if lemma in ("ser", "ir"):
            return "", ("ser und ir haben dasselbe Indefinido: "
                                  "fui, fuiste, fue, fuimos, fuisteis, fueron.")
        if len(first) < 3:
            # vi/di: monosyllables lose the accent; not a pretérito fuerte.
            note = f"Einsilbige Formen ohne Akzent ({row[0]}, {row[2]})."
            if lemma == "dar":
                note += " dar konjugiert wie ein -er/-ir-Verb (di, diste, dio …)."
            return "", note
        if first.endswith("e") and first != reg1:
            note = ("Starkes Indefinido (pretérito fuerte): unbetonte Endungen -e, -iste, -o, "
                    "-imos, -isteis, -ieron.")
            key = "pret_strong"
            if row[5].lower().endswith("eron") and not row[5].lower().endswith("ieron"):
                note += " 3. Pl. auf -eron."
                key = "pret_strong_eron"
            return key, note
        return "", _unregular_note(lemma, tense, row)
    if tense == "ind_pres":
        parts, keys = [], []
        reg1 = regular_form(lemma, tense, "1s") or ""
        if row[0].lower().endswith("go") and not reg1.endswith("go"):
            parts.append("yo-Form auf -go.")
            keys.append("go")
        elif row[0].lower().endswith("zco") and not reg1.endswith("zco"):
            parts.append("yo-Form auf -zco.")
            keys.append("zco")
        change = _stem_change(lemma, row)
        if change:
            parts.append(f"Stammwechsel {change} (nicht in nosotros/vosotros).")
            keys.append(change)
        if parts:
            return "pres_" + "_".join(keys), " ".join(parts)
        return "", _unregular_note(lemma, tense, row)
    if tense == "ind_fut":
        stem = row[0][:-1] if row[0].endswith("é") else row[0]
        return "fut_stem", f"Unregelmäßiger Futurstamm: {stem}- (Endungen regelmäßig)."
    if tense == "ind_impf":
        return "impf", "Nur ser, ir und ver haben ein unregelmäßiges Imperfecto."
    return "", ""


def pattern_note(lemma: str, tense: str, row: list[str], lang: str) -> str:
    """Short German explanation of what is irregular (heuristic). Never raises."""
    try:
        return _pattern(lemma, tense, row)[1]
    except Exception:
        return ""


def form_card_candidates(lang: str, max_verbs: int = 25, max_tenses: int = 3,
                         limit: int = 60) -> list[dict]:
    """The form cards worth creating: the `max_verbs` most frequent irregular
    verbs × their first `max_tenses` irregular tenses, capped at `limit`.

    Each item: {lemma, tense, word_zh, row (6 forms), note_de}. Deterministic.

    Never raises — [] without a lexicon."""
    items: list[dict] = []
    keys: list[str] = []
    try:
        verbs = 0
        for lemma in lexicon.verb_lemmas(lang):
            if verbs >= max_verbs or len(items) >= limit:
                break
            if lemma.endswith("se"):
                continue
            taken = 0
            for tense in irregular_tenses(lemma, lang):
                if taken >= max_tenses or len(items) >= limit:
                    break
                row = _lexicon_row(lemma, tense)
                if not all(row):
                    continue
                key, note = _pattern(lemma, tense, row)
                items.append({"lemma": lemma, "tense": tense,
                              "word_zh": form_card_word(lemma, tense),
                              "row": row, "note_de": note})
                keys.append(f"{tense}:{key}" if key else "")
                taken += 1
            if taken:
                verbs += 1
        for item, key in zip(items, keys):
            if not key:
                continue
            like =[o["lemma"] for o, k in zip(items, keys)
                    if k == key and o["lemma"] != item["lemma"]][:4]
            if like:
                item["note_de"] += " Wie: " + ", ".join(like) + "."
    except Exception as e:
        logger.warning("form_card_candidates failed — %s", e)
        return []
    return items


# ---------------------------------------------------------------------------
# Slot weights (#1306)
#
# Not every slot is equally worth drilling: "seréis" is far rarer than "habla".
# Config carries default weights (languages.py); Settings can override them
# (app_settings JSON). A slot's weight = tense weight x person weight; form
# cards (fixed tense) use the person weight only.
# ---------------------------------------------------------------------------

SETTING_KEY = "es_verb_slot_weights"
MAX_WEIGHT = 1000
_FALLBACK_TENSE_WEIGHTS = {"ind_cond": 3.0}


def default_weights(lang: str) -> dict:
    """{"tenses": {code: w}, "persons": {code: w}} from the config, in config
    order. Codes without a configured weight get 1.0. {} when the feature is off."""
    cfg = config(lang)
    if not cfg:
        return {}
    tw, pw = cfg.get("tense_weights") or {}, cfg.get("person_weights") or {}
    return {
        "tenses": {t: float(tw.get(t, _FALLBACK_TENSE_WEIGHTS.get(t, 1.0))) for t in cfg["tenses"]},
        "persons": {p: float(pw.get(p, 1.0)) for p in cfg["persons"]},
    }


def _valid_weight(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and 0 <= v <= MAX_WEIGHT)


def weights(lang: str) -> dict:
    """Effective weights: defaults overlaid with the stored override (Spanish
    only). Broken stored data is ignored with a warning. Never raises."""
    try:
        out = default_weights(lang)
        if not out or lang != _FORM_LANG:
            return out
        raw = database.get_app_setting(SETTING_KEY)
        if not raw:
            return out
        try:
            stored = json.loads(raw)
        except ValueError:
            logger.warning("verb slot weights: stored value is not JSON, using defaults")
            return out
        if not isinstance(stored, dict):
            logger.warning("verb slot weights: stored value is not an object, using defaults")
            return out
        for part in ("tenses", "persons"):
            sub = stored.get(part)
            if not isinstance(sub, dict):
                logger.warning("verb slot weights: %r missing or broken, using defaults", part)
                continue
            for code, v in sub.items():
                if code in out[part] and _valid_weight(v):
                    out[part][code] = float(v)
        return out
    except Exception as e:
        logger.warning("verb slot weights failed, using defaults — %s", e)
        try:
            return default_weights(lang)
        except Exception:
            return {}


def validate_weights(lang: str, data) -> dict:
    """Validate an API payload; return the full normalized set of floats.
    Raises ValueError with a readable reason."""
    defaults = default_weights(lang)
    if not defaults:
        raise ValueError(f"verb slot weights are not available for {lang!r}")
    if not isinstance(data, dict):
        raise ValueError("body must be an object with 'tenses' and 'persons'")
    out = {}
    for part in ("tenses", "persons"):
        sub = data.get(part)
        if not isinstance(sub, dict):
            raise ValueError(f"'{part}' must be an object")
        merged = dict(defaults[part])
        for code, v in sub.items():
            if code not in merged:
                raise ValueError(f"unknown {part[:-1]} code {code!r}")
            if not _valid_weight(v):
                raise ValueError(f"weight for {code!r} must be a number between 0 and {MAX_WEIGHT}")
            merged[code] = float(v)
        if not any(w > 0 for w in merged.values()):
            raise ValueError(f"at least one of the {part} needs a weight above 0")
        out[part] = merged
    return out


def assign_slots(cards: list[dict], lang: str, usage: dict | None = None,
                 rng: random.Random | None = None,
                 weights: dict | None = None) -> dict[int, dict]:
    """Pick a slot for every slottable verb card: {word_id: {slot, form, label}}.

    Weighted fair rotation (#1306): the slot with the smallest
    (times used + 1) / weight wins, so the long-run share of a slot is about
    its share of the total weight; weight 0 = never. Ties -> larger weight,
    then least recently used (never used = oldest), then random. A slot's
    weight is tense weight x person weight; form cards (fixed tense) use the
    person weight only. `weights` = {"tenses": {...}, "persons": {...}}, default
    = weights(lang). The local count is bumped after each pick so one story
    spreads over several slots. Never raises.
    """
    try:
        cfg = config(lang)
        if not cfg:
            return {}
        if usage is None:
            usage = database.verb_slot_usage(lang)
        if weights is None:
            weights = globals()["weights"](lang)  # the parameter shadows the function
        w_tense = (weights or {}).get("tenses") or {}
        w_person = (weights or {}).get("persons") or {}
        rng = rng or random.Random()
        counts = {k: v[0] for k, v in usage.items()}
        last = {k: (v[1] or "") for k, v in usage.items()}
        slots = [f"{t}:{p}" for t in cfg["tenses"] for p in cfg["persons"]]

        result: dict[int, dict] = {}
        for card in cards:
            word = card.get("word_zh") or ""
            options = []
            parsed = parse_form_card(word) if lang == _FORM_LANG else None
            if parsed:
                # Form card (#1296): the tense is fixed by the card, only the
                # person rotates; the enabled-tenses config does not apply.
                lemma, tense = parsed
                for person in PERSONS:
                    form = lexicon.form_for(lemma, tense, person, lang)
                    if form:
                        options.append((f"{tense}:{person}", form, w_person.get(person, 1.0)))
            elif is_slottable_verb(word, lang):
                lemma = word.strip().lower()
                for slot in slots:
                    tense, _, person = slot.partition(":")
                    form = target_form(lemma, tense, person, lang)
                    if form:
                        options.append((slot, form,
                                        w_tense.get(tense, 1.0) * w_person.get(person, 1.0)))
            else:
                continue
            options = [o for o in options if o[2] > 0]  # weight 0 = never
            if not options:
                continue

            def key(o):
                # rounded so float noise can't break ties
                return (round((counts.get(o[0], 0) + 1) / o[2], 9), -round(o[2], 9),
                        last.get(o[0], ""))
            best = min(key(o) for o in options)
            tied = [o for o in options if key(o) == best]
            slot, form, _w = rng.choice(tied)
            counts[slot] = counts.get(slot, 0) + 1
            tense, _, person = slot.partition(":")
            result[card["word_id"]] = {"slot": slot, "form": form,
                                       "label": lexicon.slot_label(tense, person)}
        return result
    except Exception as e:
        logger.warning("verb slot assignment failed — %s", e)
        return {}

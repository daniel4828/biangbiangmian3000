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
import re
import unicodedata

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


# Tense context (#1481): which context makes the tense the ONLY natural choice
# (generation) and how a German translation keeps it recognisable (translation).
# German has no Indefinido/Imperfecto split and uses Perfekt and Präteritum
# almost interchangeably, so the tense must be carried by signal words.
_TENSE_CONTEXT_ES = {
    "ind_pres": "a habitual or current action — e.g. normalmente, ahora, cada día",
    "ind_pret": "one completed action at a specific, finished point in the past — "
                "include a marker such as ayer, anoche, el lunes pasado, en 2019, "
                "hace dos años, de repente, una vez",
    "ind_impf": "a past habit, ongoing background or state (not a completed event) — "
                "include a marker such as siempre, cada verano, de niño, antes, "
                "todos los días, mientras, cuando era joven",
    "perf": "a past action within a time frame that still includes now, or life "
            "experience — include a marker such as hoy, esta mañana, esta semana, "
            "este año, ya, todavía no, nunca, alguna vez",
    "ind_fut": "a future event — include a marker such as mañana, el año que viene, pronto",
    "ind_cond": "a hypothetical or polite wish — e.g. with si…, me gustaría, en tu lugar",
    "subj_pres": "a subjunctive trigger such as quiero que, ojalá, para que, es importante que",
    "subj_impf": "a past or unreal subjunctive trigger such as quería que, si yo…, "
                 "como si, ojalá (unreal)",
}
_TENSE_CUE_DE = {
    "ind_pres": "Präsens; keep markers like normalerweise, jetzt, jeden Tag",
    "ind_pret": "a single, completed event at a definite past time: the German must "
                "contain a concrete time marker (gestern, letzte Woche, 2019, vor zwei "
                "Jahren, plötzlich, einmal …) so it clearly reads as one finished event, "
                "never as a habit or background",
    "ind_impf": "a habit, repeated action, ongoing background or past state: the German "
                "must make that explicit (früher, damals, immer, jeden Sommer, als Kind, "
                "gerade, während …) so it never reads as one completed event",
    "perf": "a past action inside a period that still includes now, or life experience: "
            "use German Perfekt and keep/add a present-related marker (heute, heute "
            "Morgen, diese Woche, dieses Jahr, schon, noch nie, noch nicht, schon "
            "einmal …) so it never reads as a finished past point like 'gestern'",
    "ind_fut": "Futur with werden plus a future marker (morgen, nächstes Jahr, bald …)",
    "ind_cond": "Konjunktiv II with würde / hätte / wäre",
    "subj_pres": "keep the trigger clearly visible (dass, damit, hoffentlich, es ist wichtig, dass …)",
    "subj_impf": "keep the trigger clearly visible (wollte, dass …; wenn … wäre/hätte; als ob …)",
}


def tense_context(slot: str) -> str:
    """English hint for the story prompt: which context makes this slot's tense
    the only natural one. '' for unknown tenses."""
    return _TENSE_CONTEXT_ES.get((slot or "").partition(":")[0], "")


def tense_cue_de(slot: str) -> str:
    """English instruction for the German translation of a sentence in this
    slot's tense. '' for unknown tenses."""
    return _TENSE_CUE_DE.get((slot or "").partition(":")[0], "")


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
# Tenses a form card may carry (#1438): the seven of the "Zeit des Tages"
# rotation. FORM_CARD_TENSES above stays the set used to judge irregularity.
FORM_CARD_ALL_TENSES = ("ind_pres", "ind_pret", "ind_impf", "perf", "ind_fut",
                        "subj_pres", "subj_impf")
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

    None unless the pattern matches, the label maps back to a FORM_CARD_ALL_TENSES
    tense, and the verb has a form in that tense (#1438: all seven). Never raises."""
    try:
        if not word or _SEP not in word:
            return None
        lemma, _, label = word.partition(_SEP)
        lemma, label = lemma.strip().lower(), label.strip()
        if not lemma or len(lemma.split()) != 1:
            return None
        for tense in FORM_CARD_ALL_TENSES:
            if lexicon._TENSE_LABELS.get(tense) == label:
                if target_form(lemma, tense, "1s", _FORM_LANG) is None:
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
    if tense == "perf":
        part = row[0].split()[-1] if row and row[0] else ""
        if part and not part.lower().endswith(("ado", "ido")):
            return "perf_part", f"Unregelmäßiges Partizip: {part} (haber + Partizip)."
        return "", "Regelmäßig in dieser Zeit (haber + Partizip)."
    if tense == "subj_pres":
        return "", "Subjuntivo: Stamm von der 1. Sg. (yo) des Indikativs abgeleitet."
    if tense == "subj_impf":
        return "", "Subjuntivo Imperfecto: Stamm von der 3. Pl. Indefinido (-ron → -ra/-ras/…)."
    if tense in ("ind_pres", "ind_pret", "ind_impf", "ind_fut"):
        regs = [regular_form(lemma, tense, p) for p in PERSONS]
        if all(regs) and all(f.lower() == r.lower() for f, r in zip(row, regs)):
            return "", "Regelmäßig in dieser Zeit."
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


def form_card_candidates(lang: str, max_verbs: int = 25, limit: int = 175) -> list[dict]:
    """The form cards worth creating: the `max_verbs` most frequent irregular
    verbs × ALL configured tenses (also those in which the verb is regular,
    e.g. "tener · Imperfecto"), capped at `limit` (25 × 7 = 175). A tense is
    skipped when any of its six forms is unavailable.

    Each item: {lemma, tense, word_zh, row (6 forms), note_de}. Deterministic.

    Never raises — [] without a lexicon."""
    items: list[dict] = []
    keys: list[str] = []
    try:
        cfg = config(lang)
        tenses = list(cfg["tenses"]) if cfg else []
        verbs = 0
        for lemma in lexicon.verb_lemmas(lang):
            if verbs >= max_verbs or len(items) >= limit:
                break
            if lemma.endswith("se"):
                continue
            if not irregular_tenses(lemma, lang):
                continue
            taken = 0
            for tense in tenses:
                if len(items) >= limit:
                    break
                row = [target_form(lemma, tense, p, lang) for p in PERSONS]
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


def _focus_tense() -> str | None:
    """Today's focus tense (Zeit des Tages, #1434) or None (off / failure)."""
    try:
        import tense_focus
        cur = tense_focus.current()
        return cur["tense"] if cur else None
    except Exception as e:
        logger.warning("tense focus unavailable, using the normal rotation — %s", e)
        return None


def _assign_focus(cards: list[dict], focus_tense: str, lang: str) -> dict[int, dict]:
    """Slots in focus mode (#1434). Every card rotates over the persons on its
    OWN (#1475): the person this word has drilled least in the tense (its story
    history, main sentences + Folgesätze), then the person given least often to
    the other cards of this story (so cards with the same history — e.g. form
    cards introduced together — differ), then the person least drilled over all
    words, then the oldest last use, then PERSONS order. Exception (#1440): a
    creating card of an irregular verb picks inside the persons of
    conj_patterns.focus_persons() (its irregularity type). Form cards keep their
    fixed tense. Cards with no form in the tense are left out (the caller uses
    the normal logic)."""
    out: dict[int, dict] = {}
    use_patterns = False
    try:
        import conj_patterns
        use_patterns = conj_patterns.settings()["enabled"]
    except Exception as e:
        logger.warning("conjugation pattern settings unavailable — %s", e)
    usage_by_word: dict = {}
    try:
        ids = [c["word_id"] for c in cards if c.get("word_id") is not None]
        usage_by_word = database.verb_slot_usage_by_word(ids)
    except Exception as e:
        logger.warning("per-word slot usage unavailable — %s", e)
        usage_by_word = {}
    hist: dict = {}
    try:
        hist = database.verb_slot_usage(lang) or {}
    except Exception as e:
        logger.warning("slot history unavailable — %s", e)
    # Slots already handed to other cards of this story.
    local: dict[str, int] = {}

    def pick(avail: list[str], tense: str, usage: dict | None) -> str:
        usage = usage or {}

        def key(person: str):
            slot = f"{tense}:{person}"
            wc, wlast = usage.get(slot) or (0, None)
            gc, _glast = hist.get(slot) or (0, None)
            return (wc or 0, local.get(slot, 0), gc or 0, wlast or "",
                    PERSONS.index(person))
        return min(avail, key=key)

    for card in cards:
        word = card.get("word_zh") or ""
        parsed = parse_form_card(word)
        if parsed:
            lemma, tense = parsed
        elif is_slottable_verb(word, lang):
            lemma, tense = word.strip().lower(), focus_tense
        else:
            continue

        def make_entry(person: str) -> dict | None:
            form = target_form(lemma, tense, person, lang)
            if not form:
                return None
            entry = {"slot": f"{tense}:{person}", "form": form,
                     "label": lexicon.slot_label(tense, person)}
            if use_patterns:
                try:
                    pat = conj_patterns.pattern_for(lemma, tense, person, lang)
                    if pat:
                        entry["pattern"] = pat["key"]
                except Exception as e:
                    logger.warning("pattern of %r failed — %s", lemma, e)
            return entry

        usage = usage_by_word.get(card.get("word_id"))
        entry = None
        # creating + irregular verb: the irregularity type picks the person (#1440)
        if use_patterns and card.get("category") == "creating":
            try:
                plan = [x for x in conj_patterns.focus_persons(lemma, tense, lang,
                                                               usage=usage, local=local)
                        if target_form(lemma, tense, x, lang)]
            except Exception as e:
                logger.warning("focus persons of %r failed — %s", lemma, e)
                plan = []
            entry = make_entry(plan[0]) if plan else None
        if entry is None:
            avail = [x for x in PERSONS if target_form(lemma, tense, x, lang)]
            entry = make_entry(pick(avail, tense, usage)) if avail else None
        if entry:
            out[card["word_id"]] = entry
            local[entry["slot"]] = local.get(entry["slot"], 0) + 1
    return out


def assign_slots(cards: list[dict], lang: str, usage: dict | None = None,
                 rng: random.Random | None = None,
                 weights: dict | None = None, conj_states: dict | None = None,
                 today=None) -> dict[int, dict]:
    """Pick a slot for every slottable verb card: {word_id: {slot, form, label}}.

    Weighted fair rotation (#1306): the slot with the smallest
    (times used + 1) / weight wins, so the long-run share of a slot is about
    its share of the total weight; weight 0 = never. Ties -> larger weight,
    then least recently used (never used = oldest), then random. A slot's
    weight is tense weight x person weight; form cards (fixed tense) use the
    person weight only. `weights` = {"tenses": {...}, "persons": {...}}, default
    = weights(lang). The local count is bumped after each pick so one story
    spreads over several slots. Never raises.

    Conjugation patterns (#1395, Spanish only, switchable in Settings): before
    the rotation, a card gets (1) a slot whose conjugation PATTERN is due for
    review (each (pattern, mode) once per call), else (2) a slot of a not yet
    introduced pattern (not limited per day since #1401), else (3) the
    rotation above restricted to already introduced patterns. Mode: creating
    cards = "prod", reading/listening = "recog". `conj_states` ({(pattern, mode):
    row}) and `today` can be injected (tests);
    the planner is only active when they are given or `usage` is loaded from the
    database here (an injected `usage` = isolated rotation test, old behaviour).
    Any failure in the pattern logic logs a warning and falls back to the plain
    rotation. Entries then carry an extra "pattern" key.
    """
    try:
        cfg = config(lang)
        if not cfg:
            return {}
        # Zeit des Tages (#1434): one tense, fixed person order. Cards it cannot
        # serve (no form in that tense) go through the normal logic below.
        focus_result: dict[int, dict] = {}
        if lang == _FORM_LANG:
            focus = _focus_tense()
            if focus:
                focus_result = _assign_focus(cards, focus, lang)
                cards = [c for c in cards if c["word_id"] not in focus_result]
                if not cards:
                    return focus_result
        planner = None
        if lang == _FORM_LANG and (usage is None or conj_states is not None):
            try:
                import conj_patterns
                planner = conj_patterns.SlotPlanner.create(
                    lang, conj_states, today)
            except Exception as e:
                logger.warning("conjugation pattern planner unavailable — %s", e)
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
                    form = target_form(lemma, tense, person, lang)
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

            pattern_of: dict[str, str | None] = {}
            chosen = None
            mode = None
            if planner is not None:
                try:
                    import conj_patterns
                    mode = conj_patterns.mode_for_category(card.get("category"))
                    for o in options:
                        tense, _, person = o[0].partition(":")
                        pat = conj_patterns.pattern_for(lemma, tense, person, lang)
                        pattern_of[o[0]] = pat["key"] if pat else None
                    chosen = planner.choose(
                        mode, [(o, pattern_of[o[0]]) for o in options], rng)
                except Exception as e:
                    logger.warning("conjugation pattern priority failed, "
                                   "using plain rotation — %s", e)
                    planner, chosen = None, None

            pool = options
            if chosen is None and planner is not None:
                # Stage 3: rotate only over patterns that are already introduced
                # (none introduced yet = all options, i.e. the old behaviour).
                introduced = [o for o in options
                              if planner.introduced(mode, pattern_of.get(o[0]))]
                pool = introduced or options

            def key(o):
                # rounded so float noise can't break ties
                return (round((counts.get(o[0], 0) + 1) / o[2], 9), -round(o[2], 9),
                        last.get(o[0], ""))
            if chosen is not None:
                (slot, form, _w), pattern_key = chosen
            else:
                best = min(key(o) for o in pool)
                tied = [o for o in pool if key(o) == best]
                slot, form, _w = rng.choice(tied)
                pattern_key = pattern_of.get(slot)
            counts[slot] = counts.get(slot, 0) + 1
            tense, _, person = slot.partition(":")
            entry = {"slot": slot, "form": form,
                     "label": lexicon.slot_label(tense, person)}
            if planner is not None and pattern_key:
                entry["pattern"] = pattern_key
            result[card["word_id"]] = entry
        result.update(focus_result)
        return result
    except Exception as e:
        logger.warning("verb slot assignment failed — %s", e)
        return {}


# ---------------------------------------------------------------------------
# Label helpers for the conjugation table (#1356). The practice counts that used
# to live here were replaced by conjugation PATTERNS (conj_patterns.py, #1395).
# ---------------------------------------------------------------------------

# Tenses whose regularity follows another tense's (the lexicon-based
# irregular_tenses() only covers FORM_CARD_TENSES).
_DERIVED_TENSE = {"ind_cond": "ind_fut", "subj_pres": "ind_pres", "subj_impf": "ind_pret"}
_PERSON_PRONOUNS = {
    "yo": "1s", "tu": "2s", "el": "3s", "ella": "3s", "usted": "3s",
    "nosotros": "1p", "nosotras": "1p", "vosotros": "2p", "vosotras": "2p",
    "ellos": "3p", "ellas": "3p", "ustedes": "3p",
}


def _norm_label(label) -> str:
    s = unicodedata.normalize("NFKD", str(label or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).strip()


def tense_code_for_label(label) -> str | None:
    """Map a free-text conjugation tense label ("Pretérito indefinido",
    "Imperfecto de subjuntivo", …) to a slot tense code; None for tenses that
    are never drilled (Participio, Gerundio, Imperativo, pluscuamperfecto,
    compound/perfect subjunctive, future perfect, …). Never raises."""
    try:
        s = _norm_label(label)
        if not s or "pluscuamperfecto" in s or "anterior" in s or "compuesto" in s:
            return None
        perfecto = re.search(r"\bperfecto\b", s) is not None  # not "imperfecto"
        if "subjuntivo" in s:
            if perfecto or "futuro" in s:
                return None
            if "imperfecto" in s or "preterito" in s:
                return "subj_impf"
            return "subj_pres"
        if "imperativo" in s:
            return None
        if perfecto:
            return None if ("futuro" in s or "condicional" in s) else "perf"
        if "indefinido" in s or ("preterito" in s and "imperfecto" not in s):
            return "ind_pret"
        if "imperfecto" in s:
            return "ind_impf"
        if "futuro" in s:
            return "ind_fut"
        if "condicional" in s:
            return "ind_cond"
        if "presente" in s:
            return "ind_pres"
        return None
    except Exception:
        return None


def person_code_for_label(label) -> str | None:
    """"yo" -> "1s", "él/ella/usted" -> "3s", "nosotros/as" -> "1p", … None if
    the label is not a person pronoun. Never raises."""
    try:
        tokens = re.split(r"[\s/]+", _norm_label(label))
        return _PERSON_PRONOUNS.get(tokens[0]) if tokens else None
    except Exception:
        return None


def _base_verb(lemma: str) -> str:
    """Lower-cased infinitive; reflexive -arse/-erse/-irse loses the "se"
    (same rule as verb_regularity)."""
    lemma = (lemma or "").strip().lower()
    if len(lemma) > 4 and lemma.endswith(("arse", "erse", "irse")):
        lemma = lemma[:-2]
    return lemma


def tense_is_irregular(lemma: str, tense: str, lang: str) -> bool:
    """Is `tense` irregular for this verb? Reflexive verbs are judged by the
    base verb. ind_pres/ind_pret/ind_impf/ind_fut come from irregular_tenses();
    the rest is derived: ind_cond follows ind_fut (same stem), subj_pres follows
    ind_pres (built on the yo-form), subj_impf follows ind_pret (built on the
    ellos-form); perf is irregular iff the lexicon participle differs from the
    regular one (-ar -> -ado, -er/-ir -> -ido; missing participle = regular).
    Never raises (False on error)."""
    try:
        lemma = _base_verb(lemma)
        if tense == "perf":
            part = lexicon.form_for(lemma, "part", "", lang)
            if not part:
                return False
            if lemma.endswith("ar"):
                regular = lemma[:-2] + "ado"
            elif lemma.endswith(("er", "ir")):
                regular = lemma[:-2] + "ido"
            else:
                return False
            return part.lower() != regular
        base = _DERIVED_TENSE.get(tense, tense)
        return base in irregular_tenses(lemma, lang)
    except Exception:
        return False


def _slot_lemma(word_zh: str, lang: str) -> str | None:
    """The verb a story-sentence word stands for: a form card's lemma, or a
    slottable verb's (base) infinitive; None for any other word."""
    try:
        parsed = parse_form_card(word_zh) if lang == _FORM_LANG else None
        if parsed:
            return _base_verb(parsed[0])
        if is_slottable_verb(word_zh, lang):
            return _base_verb(word_zh)
    except Exception:
        pass
    return None



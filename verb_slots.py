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
# Fallback for the auxiliary of the Presente progresivo (#1606).
_ESTAR_PRES = {"1s": "estoy", "2s": "estás", "3s": "está", "1p": "estamos", "2p": "estáis", "3p": "están"}

_TENSE_EN = {
    "ind_pres": "Presente (present)",
    "ind_pret": "Indefinido (preterite)",
    "ind_impf": "Imperfecto (imperfect)",
    "ind_fut": "Futuro (future)",
    "ind_cond": "Condicional (conditional)",
    "perf": "Pretérito perfecto (present perfect)",
    "prog": "Presente progresivo (estar + gerund, an action in progress right now)",
    "subj_pres": "Presente de subjuntivo (present subjunctive — the sentence needs a "
                 "trigger such as 'que', 'ojalá', 'para que')",
    "subj_impf": "Imperfecto de subjuntivo (imperfect subjunctive, -ra form — needs a "
                 "trigger such as 'si', 'quería que', 'como si')",
    "imp_aff": "Imperativo (affirmative command addressed to someone — use the form "
               "exactly as given, without attaching object pronouns)",
    "imp_neg": "Imperativo negativo (negative command: 'no' + the form exactly as "
               "given, e.g. ¡No hables!, without attaching object pronouns)",
    "part": "Participio (past participle)",
    "ger": "Gerundio (gerund)",
}
# Reflexive pronoun in front of the conjugated form (#1301).
_REFLEXIVE_PRONOUN = {"1s": "me", "2s": "te", "3s": "se", "1p": "nos", "2p": "os", "3p": "se"}
_PERSON_EN = {"1s": "1st person singular", "2s": "2nd person singular",
              "3s": "3rd person singular", "1p": "1st person plural",
              "2p": "2nd person plural", "3p": "3rd person plural"}
# Imperative persons are addressees (#1540): 3s/3p are the formal usted/ustedes.
_IMP_PERSON_EN = {"2s": "tú", "3s": "usted (formal)", "1p": "nosotros (let's …)",
                  "2p": "vosotros", "3p": "ustedes"}


def config(lang: str) -> dict | None:
    """The language's "verb_slots" config, or None when the feature is off."""
    try:
        cfg = languages.get_lang_config(lang).get("verb_slots")
        return cfg if cfg and cfg.get("tenses") and cfg.get("persons") else None
    except Exception:
        return None


def expression_parts(word: str, lang: str) -> dict | None:
    """Split a multi-word Spanish verb expression (#1546) into its conjugated
    head and its fixed rest: "echar de menos" -> head "echar", rest "de menos";
    "darse cuenta de algo" -> head "darse", rest "cuenta"; "irle bien a alguien"
    -> head "ir", clitic "le", rest "bien", persons ["3s","3p"].

    Returns {"head", "clitic", "rest", "persons"} (persons None = all) or None
    when `word` is not such an expression (single token, form card, other
    language, first token no verb the lexicon conjugates, placeholder not at
    the end, nothing left after the head). Agreeing adjectives in the rest
    ("estar orgulloso", "pasarla lindo") restrict the persons to singular.
    Never raises."""
    try:
        if lang != _FORM_LANG or not word or _SEP in word:
            return None
        import expressions
        tokens = word.strip().lower().split()
        if len(tokens) < 2:
            return None
        m = expressions._ES_HEAD.match(tokens[0])
        if not m:
            return None
        inf, clitic_part = m.group(1), m.group(2)
        clitics = expressions._ES_CLITIC.findall(clitic_part)
        if len(clitics) > 1:
            return None
        clitic = clitics[0] if clitics else ""
        candidates = [inf]
        if inf.endswith("ír"):
            stripped = inf[:-2].translate(expressions._ACCENTS)
            candidates += [stripped + "ír", stripped + "ir"]
        else:
            candidates.append(inf.translate(expressions._ACCENTS))
        base = None
        for cand in dict.fromkeys(candidates):
            info = lexicon.lemma_info(cand, lang)
            if info and info.get("pos") == "v" \
                    and lexicon.form_for(cand, "ind_pres", "1s", lang) is not None:
                base = cand
                break
        if base is None:
            return None
        placeholders = expressions._PLACEHOLDERS["es"]
        preps = expressions._PREPOSITIONS["es"]
        rest = tokens[1:]
        had_placeholder = False
        while rest and rest[-1] in placeholders:
            rest.pop()
            had_placeholder = True
        if any(t in placeholders for t in rest):
            return None  # placeholder in the middle: no fixed surface form
        if had_placeholder and rest and rest[-1] in preps:
            rest.pop()
        if len(rest) >= 2 and rest[-1] in preps:
            rest.pop()
        if not rest:
            return None
        persons = None
        if clitic in ("le", "les"):
            persons = ["3s", "3p"]
        # only a predicative adjective right after the verb agrees with the
        # subject; one inside a noun phrase ("el mismo significado") does not
        if (lexicon.lemma_info(rest[0], lang) or {}).get("pos") == "adj":
            sing = ["1s", "2s", "3s"]
            persons = [p for p in persons if p in sing] if persons else sing
        return {"head": base + "se" if clitic == "se" else base,
                "clitic": "" if clitic == "se" else clitic,
                "rest": " ".join(rest), "persons": persons}
    except Exception:
        return None


def is_verb_expression(word: str, lang: str = "es") -> bool:
    """True for a multi-word verb expression ("tener éxito", "estar orgulloso",
    "darse cuenta de"). Such expressions always count as regular verbs: no form
    cards, label "reg.", no red dots, plain person rotation — whatever their
    head verb is. Never raises."""
    try:
        w = (word or "").strip().lower()
        return len(w.split()) > 1 and expression_parts(w, lang) is not None
    except Exception:
        return False


def head_verb(lemma: str, lang: str = "es") -> str:
    """The conjugated verb of a multi-word expression ("tener éxito" -> "tener",
    "darse cuenta de" -> "darse"); any other lemma unchanged (#1546)."""
    parts = expression_parts(lemma, lang)
    return parts["head"] if parts else lemma


def is_slottable_verb(word: str, lang: str) -> bool:
    """True for a plain verb infinitive the lexicon can conjugate, or a
    multi-word verb expression whose head verb it can (#1546)."""
    try:
        if not config(lang) or not lexicon.available(lang):
            return False
        w = (word or "").strip().lower()
        if not w:
            return False
        if len(w.split()) != 1:
            return expression_parts(w, lang) is not None
        if w.endswith("se") and len(w) > 4:  # reflexive: judge the base verb (#1301)
            w = w[:-2]
        info = lexicon.lemma_info(w, lang)
        if not info or info.get("pos") != "v":
            return False
        return lexicon.form_for(w, "ind_pres", "1s", lang) is not None
    except Exception:
        return False


def _reflexive_nonfinite(lemma: str, tense: str, person: str, lang: str) -> str | None:
    """Reflexive imperative/gerund attach the pronoun (ríndete, rindiéndose);
    the participle has none (rendido). Lexicon form first, else built."""
    base = lemma[:-2]
    if tense == "part":
        return lexicon.form_for(base, "part", "", lang)
    form = lexicon.form_for(lemma, tense, person, lang)
    if form:
        return form
    pron = "se" if tense == "ger" else _REFLEXIVE_PRONOUN.get(person, "")
    plain = lexicon.form_for(base, tense, person, lang)
    if not plain or not pron:
        return None
    import conj_table  # lazy: conj_table imports this module
    return conj_table._attach(plain, pron)


def target_form(lemma: str, tense: str, person: str, lang: str) -> str | None:
    """The exact string a sentence must contain for this slot, or None.

    Reflexive infinitives (quejarse) use the base verb's form with the
    pronoun in front: "me quejé", "me he quejado" (#1301)."""
    try:
        lemma = (lemma or "").strip().lower()
        parts = expression_parts(lemma, lang)
        if parts:  # multi-word expression: conjugate the head, keep the rest (#1546)
            if not person or (parts["persons"] and person not in parts["persons"]):
                return None
            if tense in ("imp_aff", "imp_neg") and parts["clitic"]:
                return None
            base_form = target_form(parts["head"], tense, person, lang)
            if not base_form:
                return None
            front = parts["clitic"] + " " if parts["clitic"] else ""
            return f"{front}{base_form} {parts['rest']}"
        if lemma.endswith("se") and len(lemma) > 4 and tense in ("imp_aff", "ger", "part"):
            return _reflexive_nonfinite(lemma, tense, person, lang)
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
        elif tense == "prog":
            aux = lexicon.form_for("estar", "ind_pres", person, lang) or _ESTAR_PRES.get(person)
            ger = lexicon.form_for(lemma, "ger", "", lang)
            form = f"{aux} {ger}" if aux and ger else None
        else:
            form = lexicon.form_for(lemma, tense, person, lang)
        if not form:
            return None
        form = f"{pronoun} {form}" if pronoun else form
        # Negative imperative (#1580): "no hables", "no te quejes"
        return f"no {form}" if tense == "imp_neg" else form
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
    "prog": "an action in progress at this very moment — include a marker such as "
            "ahora mismo, en este momento, ahora, ¿qué estás haciendo?",
    "ind_fut": "a future event — include a marker such as mañana, el año que viene, pronto",
    "ind_cond": "a hypothetical or polite wish — e.g. with si…, me gustaría, en tu lugar",
    "subj_pres": "a subjunctive trigger such as quiero que, ojalá, para que, es importante que",
    "subj_impf": "a past or unreal subjunctive trigger such as quería que, si yo…, "
                 "como si, ojalá (unreal)",
    "imp_aff": "a direct request or instruction in dialogue, e.g. ¡…, por favor!; "
               "usted/ustedes = polite address, nosotros = let's …",
    "imp_neg": "a direct prohibition or warning in dialogue, e.g. ¡No …!, no …, por favor; "
               "usted/ustedes = polite address, nosotros = let's not …",
    "part": "the past participle, e.g. in a perfect tense (he/ha/hemos …) or as an adjective",
    "ger": "an ongoing action: estar/seguir + gerund, or a simultaneous action",
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
    "prog": "an action in progress right now: the German must contain gerade / im Moment / "
            "gerade dabei … so it never reads as a habit",
    "ind_fut": "Futur with werden plus a future marker (morgen, nächstes Jahr, bald …)",
    "ind_cond": "Konjunktiv II with würde / hätte / wäre",
    "subj_pres": "keep the trigger clearly visible (dass, damit, hoffentlich, es ist wichtig, dass …)",
    "subj_impf": "keep the trigger clearly visible (wollte, dass …; wenn … wäre/hätte; als ob …)",
    "imp_aff": "German imperative matching the addressee: du-form (Komm!), Sie-form for "
               "usted/ustedes (Kommen Sie!), 'Lass uns …' for nosotros, ihr-form for vosotros",
    "imp_neg": "negated German imperative matching the addressee: Komm nicht! / Kommen "
               "Sie nicht! / Lass uns nicht … / Kommt nicht!",
    "part": "Perfekt oder Partizip als Adjektiv",
    "ger": "Verlaufsform: gerade dabei …, oder gleichzeitige Handlung",
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
    if not person:  # participle / gerund: no person
        return _TENSE_EN.get(tense, tense)
    persons = _IMP_PERSON_EN if tense in ("imp_aff", "imp_neg") else _PERSON_EN
    return f"{_TENSE_EN.get(tense, tense)}, {persons.get(person, person)}"


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
# Tenses a form card may carry (#1438): the eight of the "Zeit des Tages"
# rotation. FORM_CARD_TENSES above is the set irregular_tenses() looks at.
FORM_CARD_ALL_TENSES = ("ind_pres", "ind_pret", "ind_impf", "perf", "ind_fut",
                        "subj_pres", "subj_impf", "ind_cond")
# Tenses that get an irregular-verb form card (#1539): the four indicative
# tenses, the affirmative imperative and the two non-finite forms. No
# subjunctive/conditional (they follow the indicative), no "perf" (the
# participle card stands for the perfect).
IRREGULAR_CARD_TENSES = ("ind_pres", "ind_pret", "ind_impf", "ind_fut",
                         "imp_aff", "part", "ger")
PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")
# Every tense a form-card headword may name (old cards stay parseable).
_PARSE_TENSES = FORM_CARD_ALL_TENSES + tuple(
    t for t in IRREGULAR_CARD_TENSES if t not in FORM_CARD_ALL_TENSES)
_IMP_PERSONS = ("2s", "3s", "1p", "2p", "3p")
_NO_PERSON_TENSES = ("part", "ger")
_IMP_LABEL_DE = {"2s": "tú", "3s": "usted", "1p": "nosotros", "2p": "vosotros",
                 "3p": "ustedes"}
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
_COND_ENDINGS = ("ía", "ías", "ía", "íamos", "íais", "ían")
_SUBJ_PRES_ENDINGS = {
    "ar": ("e", "es", "e", "emos", "éis", "en"),
    "er": ("a", "as", "a", "amos", "áis", "an"),
    "ir": ("a", "as", "a", "amos", "áis", "an"),
}
_SUBJ_IMPF_ENDINGS = {
    "ar": (("ara", "aras", "ara", "áramos", "arais", "aran"),
           ("ase", "ases", "ase", "ásemos", "aseis", "asen")),
    "er": (("iera", "ieras", "iera", "iéramos", "ierais", "ieran"),
           ("iese", "ieses", "iese", "iésemos", "ieseis", "iesen")),
}
_SUBJ_IMPF_ENDINGS["ir"] = _SUBJ_IMPF_ENDINGS["er"]
_IMP_ENDINGS = {
    "ar": {"2s": "a", "3s": "e", "1p": "emos", "2p": "ad", "3p": "en"},
    "er": {"2s": "e", "3s": "a", "1p": "amos", "2p": "ed", "3p": "an"},
    "ir": {"2s": "e", "3s": "a", "1p": "amos", "2p": "id", "3p": "an"},
}
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


# --- Irregularity by PRONUNCIATION (#1539) -----------------------------------
# A form is irregular when it sounds different from the regular formation.
# Pure spelling changes are regular: busqué, pagué, cojo, venzo, leyó, trayendo.
# Both sides are reduced to a rough "sound spelling" and compared.

def _strip_accents(s: str) -> str:
    """Remove accents but keep ü (güe/güi sounds the u)."""
    s = s.replace("ü", "\0")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", s).replace("\0", "ü")


def _cons(s: str) -> str:
    """Spelling of consonant sounds -> one symbol per sound (g/j, c/z/qu, gu)."""
    s = s.replace("gü", "Gw")
    s = re.sub(r"gu(?=[ei])", "G", s)
    s = re.sub(r"gu(?=[ao])", "Gw", s)
    s = re.sub(r"g(?=[ei])", "X", s)
    s = s.replace("g", "G").replace("j", "X")
    s = re.sub(r"qu(?=[ei])", "K", s)
    s = re.sub(r"c(?=[ei])", "S", s)
    return s.replace("c", "K").replace("z", "S")


def _vow(s: str) -> str:
    """y between vowels = i; ñ/ll swallow a following i."""
    s = re.sub(r"(?<=[aeiou])y(?=[aeiou])", "i", s)
    s = re.sub(r"(?<=ñ)i(?=[aeo])", "", s)
    return re.sub(r"(?<=ll)i(?=[aeo])", "", s)


def _phon(s: str) -> str:
    return _vow(_cons(_strip_accents(s.lower())))


def _sounds_regular(form: str, lemma: str, ending: str) -> bool:
    """Does `form` sound like lemma[:-2] + `ending` (the regular formation)?"""
    stem = _cons(_strip_accents(lemma))[:-2]  # the stem as pronounced in the infinitive
    return _phon(form) == _vow(stem + _strip_accents(ending))


def _regular_endings(end: str, tense: str, person: str) -> tuple[str, ...]:
    """Regular endings of one table cell, relative to lemma[:-2] (futuro and
    condicional: relative to the infinitive's ar/er/ir). () = no verdict."""
    if end not in ("ar", "er", "ir"):
        return ()
    if tense == "part":
        return ("ado" if end == "ar" else "ido",)
    if tense == "ger":
        return ("ando" if end == "ar" else "iendo",)
    if tense == "imp_aff":
        e = _IMP_ENDINGS[end].get(person)
        return (e,) if e else ()
    if person not in PERSONS:
        return ()
    idx = PERSONS.index(person)
    if tense in ("ind_pres", "ind_pret", "ind_impf"):
        return (_ENDINGS[(tense, end)][idx],)
    if tense == "ind_fut":
        return (end + _FUT_ENDINGS[idx],)
    if tense == "ind_cond":
        return (end + _COND_ENDINGS[idx],)
    if tense == "subj_pres":
        return (_SUBJ_PRES_ENDINGS[end][idx],)
    if tense == "subj_impf":
        return tuple(e[idx] for e in _SUBJ_IMPF_ENDINGS[end])
    return ()


def _cell_regular(form: str, lemma: str, tense: str, person: str) -> bool | None:
    """True/False: does this cell sound regular? None = no verdict (not an
    -ar/-er/-ir verb, unknown tense/person)."""
    endings = _regular_endings(_strip_accents(lemma[-2:]), tense, person)
    if not endings or not form:
        return None
    return any(_sounds_regular(form, lemma, e) for e in endings)


def _row_irregular_persons(lemma: str, tense: str, forms: dict) -> list[str]:
    """Persons (given order) of {person: form} that sound irregular."""
    return [p for p, f in forms.items() if _cell_regular(f, lemma, tense, p) is False]


def irregular_persons(lemma: str, tense: str, lang: str) -> list[str]:
    """Persons (PERSONS order) whose lexicon form does not sound regular, for a
    tense of IRREGULAR_CARD_TENSES. imp_aff only has 2s/3s/1p/2p/3p; part/ger
    answer [""] when irregular, else []. Reflexive verbs are judged by the base
    verb. Persons the lexicon lacks are skipped. Never raises."""
    try:
        if lang != _FORM_LANG or tense not in IRREGULAR_CARD_TENSES:
            return []
        lemma = _base_verb(lemma)
        if _strip_accents(lemma[-2:]) not in ("ar", "er", "ir"):
            return []
        if tense in _NO_PERSON_TENSES:
            form = lexicon.form_for(lemma, tense, "", lang)
            return [""] if _cell_regular(form or "", lemma, tense, "") is False else []
        persons = _IMP_PERSONS if tense == "imp_aff" else PERSONS
        forms = {p: lexicon.form_for(lemma, tense, p, lang) for p in persons}
        return _row_irregular_persons(lemma, tense, {p: f for p, f in forms.items() if f})
    except Exception as e:
        logger.debug("irregular_persons(%r, %r) failed — %s", lemma, tense, e)
        return []


def irregular_form_tenses(lemma: str, lang: str) -> list[str]:
    """Tenses of IRREGULAR_CARD_TENSES in which the verb has irregular persons
    — the tenses that get a form card. Never raises."""
    return [t for t in IRREGULAR_CARD_TENSES if irregular_persons(lemma, t, lang)]


def irregular_tenses(lemma: str, lang: str) -> list[str]:
    """Tenses (FORM_CARD_TENSES order) in which any lexicon form sounds
    different from the regular formation (persons the lexicon lacks are
    skipped).

    Only pronunciation counts (#1539): spelling changes such as busqué, cojo,
    leyó are regular. Stem changes (resuelvo), yo-go (tengo) and strong
    preterites (tuve) are irregular. Never raises."""
    return [t for t in FORM_CARD_TENSES if irregular_persons(lemma, t, lang)]


def verb_regularity(lemma: str, lang: str) -> str | None:
    """"regular" / "irregular" for a verb headword, None when it is no verb
    the lexicon knows (#1315: Etikett "reg."/"irreg." auf der Karte).

    "irregular" iff the verb has an irregular tense that gets a form card
    (irregular_form_tenses(); pronunciation only, #1539). Never raises."""
    try:
        if lang != _FORM_LANG:
            return None
        if is_verb_expression(lemma, lang):  # Verbausdruck zählt als regelmäßig
            return "regular"
        lemma = _base_verb(lemma)  # Reflexiv (rendirse): Grundverb gilt (#1352)
        if not any(f for t in FORM_CARD_TENSES for f in _lexicon_row(lemma, t)):
            return None
        return "irregular" if irregular_form_tenses(lemma, lang) else "regular"
    except Exception as e:
        logger.debug("verb_regularity(%r) failed — %s", lemma, e)
        return None


def form_card_word(lemma: str, tense: str) -> str:
    """Headword of a form card: ("tener", "ind_pret") -> "tener · Indefinido"."""
    return f"{lemma}{_SEP}{lexicon._TENSE_LABELS.get(tense, tense)}"


def parse_form_card(word: str) -> tuple[str, str] | None:
    """Inverse of form_card_word(): "tener · Indefinido" -> ("tener", "ind_pret").

    None unless the pattern matches, the label maps back to a tense of
    FORM_CARD_ALL_TENSES or IRREGULAR_CARD_TENSES (old cards must keep parsing),
    and the verb has a form in that tense. Never raises."""
    try:
        split = split_form_card_word(word)
        if split is None:
            return None
        lemma, tense = split
        probe = "" if tense in _NO_PERSON_TENSES else ("2s" if tense == "imp_aff" else "1s")
        if target_form(lemma, tense, probe, _FORM_LANG) is None:
            return None
        return lemma, tense
    except Exception:
        return None


def split_form_card_word(word: str) -> tuple[str, str] | None:
    """Like parse_form_card() but without the "verb has this form" check: only
    the headword shape "lemma · Label" with a known tense label. Lets the
    migration (#1539) recognise cards whose tense the lexicon cannot conjugate.
    Never raises."""
    try:
        if not word or _SEP not in word:
            return None
        lemma, _, label = word.partition(_SEP)
        lemma, label = lemma.strip().lower(), label.strip()
        if not lemma or len(lemma.split()) != 1:
            return None
        for tense in _PARSE_TENSES:
            if lexicon._TENSE_LABELS.get(tense) == label:
                return lemma, tense
        return None
    except Exception:
        return None


def form_card_persons(lemma: str, tense: str, lang: str) -> list[str]:
    """The persons a form card rotates over: the irregular ones; when the tense
    has none (old cards in a regular tense) every person with a form. part/ger:
    [""] (one form). Never raises."""
    try:
        if tense in _NO_PERSON_TENSES:
            return [""] if target_form(lemma, tense, "", lang) else []
        irr = irregular_persons(lemma, tense, lang)
        if irr:
            return irr
        persons = _IMP_PERSONS if tense == "imp_aff" else PERSONS
        return [p for p in persons if target_form(lemma, tense, p, lang)]
    except Exception:
        return []


def _unregular_note(lemma: str, tense: str, row: list[str]) -> str:
    """Generic fallback note listing the irregular persons. Person labels
    already end in "." ("3. Sg."), so the sentence period is only added when
    the last label doesn't supply one."""
    persons = _row_irregular_persons(
        lemma, tense, {p: f for p, f in zip(PERSONS, row) if f})
    text = "Unregelmäßig in: " + (", ".join(_PERSON_DE[p] for p in persons)
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


def _pattern_note(lemma: str, tense: str, row: list[str]) -> str:
    """German note for one form card. `row` = the six forms in PERSONS order
    (None/"" for a missing one); imp_aff/part/ger read the lexicon instead."""
    if tense == "imp_aff":
        parts = []
        for p in irregular_persons(lemma, tense, _FORM_LANG):
            form = lexicon.form_for(_base_verb(lemma), tense, p, _FORM_LANG)
            parts.append(f"{form} ({_IMP_LABEL_DE[p]})")
        return ("Unregelmäßiger Imperativ: " + ", ".join(parts) + "." if parts
                else "Regelmäßiger Imperativ.")
    if tense in _NO_PERSON_TENSES:
        form = lexicon.form_for(_base_verb(lemma), tense, "", _FORM_LANG) or ""
        irregular = bool(irregular_persons(lemma, tense, _FORM_LANG))
        if tense == "part":
            if not irregular:
                return "Regelmäßiges Partizip."
            return f"Unregelmäßiges Partizip: {form} (he {form} …)."
        return (f"Unregelmäßiges Gerundio: {form}." if irregular
                else "Regelmäßiges Gerundio.")
    if tense == "perf":
        part = row[0].split()[-1] if row and row[0] else ""
        if part and _cell_regular(part, lemma, "part", "") is False:
            return f"Unregelmäßiges Partizip: {part} (haber + Partizip)."
        return "Regelmäßig in dieser Zeit (haber + Partizip)."
    if tense == "subj_pres":
        return "Subjuntivo: Stamm von der 1. Sg. (yo) des Indikativs abgeleitet."
    if tense == "subj_impf":
        return "Subjuntivo Imperfecto: Stamm von der 3. Pl. Indefinido (-ron → -ra/-ras/…)."
    if tense in ("ind_pres", "ind_pret", "ind_impf", "ind_fut"):
        forms = {p: f for p, f in zip(PERSONS, row) if f}
        if not _row_irregular_persons(lemma, tense, forms):
            return "Regelmäßig in dieser Zeit."
        if len(forms) < len(PERSONS):  # incomplete row: only the generic note is safe
            return _unregular_note(lemma, tense, row)
    if tense == "ind_pret":
        reg1 = (regular_form(lemma, tense, "1s") or "").lower()
        first = row[0].lower()
        if lemma in ("ser", "ir"):
            return ("ser und ir haben dasselbe Indefinido: "
                    "fui, fuiste, fue, fuimos, fuisteis, fueron.")
        if len(first) < 3:
            # vi/di: monosyllables lose the accent; not a pretérito fuerte.
            note = f"Einsilbige Formen ohne Akzent ({row[0]}, {row[2]})."
            if lemma == "dar":
                note += " dar konjugiert wie ein -er/-ir-Verb (di, diste, dio …)."
            return note
        if first.endswith("e") and first != reg1:
            note = ("Starkes Indefinido (pretérito fuerte): unbetonte Endungen -e, -iste, -o, "
                    "-imos, -isteis, -ieron.")
            if row[5].lower().endswith("eron") and not row[5].lower().endswith("ieron"):
                note += " 3. Pl. auf -eron."
            return note
        return _unregular_note(lemma, tense, row)
    if tense == "ind_pres":
        parts = []
        reg1 = regular_form(lemma, tense, "1s") or ""
        if row[0].lower().endswith("go") and not reg1.endswith("go"):
            parts.append("yo-Form auf -go.")
        elif row[0].lower().endswith("zco") and not reg1.endswith("zco"):
            parts.append("yo-Form auf -zco.")
        change = _stem_change(lemma, row)
        if change:
            parts.append(f"Stammwechsel {change} (nicht in nosotros/vosotros).")
        if parts:
            return " ".join(parts)
        return _unregular_note(lemma, tense, row)
    if tense == "ind_fut":
        stem = row[0][:-1] if row[0].endswith("é") else row[0]
        return f"Unregelmäßiger Futurstamm: {stem}- (Endungen regelmäßig)."
    if tense == "ind_impf":
        return "Nur ser, ir und ver haben ein unregelmäßiges Imperfecto."
    if tense == "ind_cond":
        first = row[0].lower() if row and row[0] else ""
        if not first.endswith("ía"):
            return _unregular_note(lemma, tense, row)
        if first == lemma.lower() + "ía":
            return "Regelmäßig in dieser Zeit (Infinitiv + -ía, -ías …)."
        return (f"Unregelmäßiger Condicionalstamm: {first[:-2]}- "
                "(wie im Futuro, Endungen -ía, -ías, -ía, -íamos, -íais, -ían).")
    return ""


def pattern_note(lemma: str, tense: str, row: list[str], lang: str) -> str:
    """Short German explanation of what is irregular (heuristic). Never raises."""
    try:
        return _pattern_note(lemma, tense, row)
    except Exception:
        return ""


def irregular_card_plan(lemma: str, lang: str) -> list[dict]:
    """The form cards a verb should have (#1539): one per tense of
    irregular_form_tenses(), as [{lemma, tense, word_zh, persons, note_de}].
    [] for non-verbs, verbs without
    irregular forms and anything but Spanish. Never raises."""
    out: list[dict] = []
    try:
        lemma = (lemma or "").strip().lower()
        if lang != _FORM_LANG or not lemma or len(lemma.split()) > 1:
            return []
        # Reflexive verbs (rendirse) are judged by the base verb (#1301).
        info = lexicon.lemma_info(_base_verb(lemma), lang)
        if not info or info.get("pos") != "v":
            return []
        for tense in irregular_form_tenses(lemma, lang):
            row = [target_form(lemma, tense, p, lang) for p in PERSONS]
            out.append({"lemma": lemma, "tense": tense,
                        "word_zh": form_card_word(lemma, tense),
                        "persons": irregular_persons(lemma, tense, lang),
                        "note_de": pattern_note(lemma, tense, row, lang)})
    except Exception as e:
        logger.warning("irregular_card_plan(%r) failed — %s", lemma, e)
    return out


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


IMPERATIVE_TENSES = ("imp_aff", "imp_neg")


def person_weight(person_weights: dict, tense: str, person: str, lang: str = "es") -> float:
    """Weight of `person` inside `tense` (#1605). Normally person_weights[person]
    (default 1.0). In the imperative there is no yo and 3s/3p mean usted/ustedes,
    so the configured `imperative_person_weights` apply instead, rescaled so
    their sum over the imperative persons equals the sum of the general weights
    over the same persons: the imperative's overall share (tense weight x person
    weight) stays unchanged. Never raises (falls back to the general weight)."""
    pw = person_weights or {}
    plain = float(pw.get(person, 1.0))
    if tense not in IMPERATIVE_TENSES:
        return plain
    try:
        imp = (config(lang) or {}).get("imperative_person_weights")
        if not imp:
            return plain
        imp_sum = sum(float(imp.get(p, 1.0)) for p in _IMP_PERSONS)
        if imp_sum <= 0 or person not in _IMP_PERSONS:
            return plain
        general_sum = sum(float(pw.get(p, 1.0)) for p in _IMP_PERSONS)
        return float(imp.get(person, 1.0)) / imp_sum * general_sum
    except Exception as e:
        logger.warning("imperative person weight failed, using general weight — %s", e)
        return plain


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


def focus_person_weights(lang: str) -> dict | None:
    """Person weights for Zeit-des-Tages mode (#1479): weights(lang)["persons"]
    (the editable "Verb slot weights", default = real usage frequency) when the
    setting "weighted_persons" is on, else None (= even rotation). Never raises."""
    try:
        import tense_focus
        if not tense_focus.settings().get("weighted_persons", True):
            return None
        return dict((weights(lang) or {}).get("persons") or {}) or None
    except Exception as e:
        logger.warning("person weights unavailable, rotating evenly — %s", e)
        return None


def _assign_focus(cards: list[dict], focus_tense: str, lang: str) -> dict[int, dict]:
    """Slots in focus mode (#1434). Every card rotates over the persons on its
    OWN (#1475): the person this word has drilled least in the tense (its story
    history, main sentences + legacy Folgesätze rows), then the person given least often to
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

    pw = focus_person_weights(lang)
    verb_mix = None
    try:
        import tense_focus
        verb_mix = tense_focus.verb_tense_weights()
    except Exception as e:
        logger.warning("verb tense weights unavailable — %s", e)
    local_tense: dict[str, int] = {}

    def pick_tense(lemma: str) -> str:
        """Non-form verbs with a tense mix: weighted fair rotation over the
        tenses (this story's picks first, then the global history), so the
        long-run share of a tense ~ its share of the weight."""
        opts = [t for t in verb_mix
                if any(target_form(lemma, t, x, lang) for x in PERSONS)]
        if not opts:
            return focus_tense

        def tkey(t: str):
            w = float(verb_mix[t])
            g = sum((c or 0) for slot, (c, _l) in hist.items()
                    if slot.partition(":")[0] == t)
            return (round((local_tense.get(t, 0) + 1) / w, 9),
                    round((g + 1) / w, 9), -w)
        return min(opts, key=tkey)

    def pick(avail: list[str], tense: str, usage: dict | None) -> str:
        usage = usage or {}
        if pw:
            def w(person: str) -> float:
                try:
                    return max(person_weight(pw, tense, person, lang), 0.0)
                except (TypeError, ValueError):
                    return 1.0
            pool = [x for x in avail if w(x) > 0] or list(avail)

            def wkey(person: str):
                # (this word's uses + uses given to other cards of this story + 1)
                # / weight: long-run share of a person ~ its share of the weight
                slot = f"{tense}:{person}"
                wc, wlast = usage.get(slot) or (0, None)
                gc, _glast = hist.get(slot) or (0, None)
                weight = w(person) or 1e-9
                return (round(((wc or 0) + local.get(slot, 0) + 1) / weight, 9),
                        round(((gc or 0) + 1) / weight, 9), wlast or "",
                        PERSONS.index(person))
            return min(pool, key=wkey)

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
            lemma = word.strip().lower()
            tense = pick_tense(lemma) if verb_mix else focus_tense
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
        # A form card only rotates over its own (irregular) persons (#1539).
        if parsed:
            allowed = form_card_persons(lemma, tense, lang)
        else:
            allowed = list(PERSONS)
        # creating + irregular verb: the irregularity type picks the person (#1440)
        if (use_patterns and card.get("category") == "creating"
                and tense in conj_patterns.TENSES):
            try:
                plan = [x for x in conj_patterns.focus_persons(lemma, tense, lang,
                                                               usage=usage, local=local,
                                                               weights=pw)
                        if x in allowed and target_form(lemma, tense, x, lang)]
            except Exception as e:
                logger.warning("focus persons of %r failed — %s", lemma, e)
                plan = []
            entry = make_entry(plan[0]) if plan else None
        if entry is None:
            avail = [x for x in allowed if target_form(lemma, tense, x, lang)]
            if len(avail) == 1:  # also the person-less participle/gerund ("")
                entry = make_entry(avail[0])
            elif avail:
                entry = make_entry(pick(avail, tense, usage))
        if entry:
            out[card["word_id"]] = entry
            local[entry["slot"]] = local.get(entry["slot"], 0) + 1
            if not parsed:
                local_tense[tense] = local_tense.get(tense, 0) + 1
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
                # Only its irregular persons (#1539).
                lemma, tense = parsed
                for person in form_card_persons(lemma, tense, lang):
                    form = target_form(lemma, tense, person, lang)
                    if form:
                        options.append((f"{tense}:{person}", form,
                                        person_weight(w_person, tense, person, lang)))
            elif is_slottable_verb(word, lang):
                lemma = word.strip().lower()
                for slot in slots:
                    tense, _, person = slot.partition(":")
                    form = target_form(lemma, tense, person, lang)
                    if form:
                        options.append((slot, form,
                                        w_tense.get(tense, 1.0)
                                        * person_weight(w_person, tense, person, lang)))
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
        if "progresivo" in s or "continuo" in s:
            return "prog"
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
    base verb. Tenses of IRREGULAR_CARD_TENSES: irregular iff irregular_persons()
    is non-empty (pronunciation, #1539). The rest is derived: ind_cond follows
    ind_fut (same stem), subj_pres follows ind_pres (built on the yo-form),
    subj_impf follows ind_pret (built on the ellos-form); perf is irregular iff
    the participle is (missing participle = regular).
    Never raises (False on error)."""
    try:
        lemma = _base_verb(lemma)
        if tense in IRREGULAR_CARD_TENSES:
            return bool(irregular_persons(lemma, tense, lang))
        if tense == "perf":
            return bool(irregular_persons(lemma, "part", lang))
        if tense == "prog":  # estar + gerund: only the gerund can be irregular
            return bool(irregular_persons(lemma, "ger", lang))
        base = _DERIVED_TENSE.get(tense, tense)
        return base in irregular_tenses(lemma, lang)
    except Exception:
        return False


_REFLEXIVE_PRONOUNS = ("me ", "te ", "se ", "nos ", "os ")


def _label_tense_code(label) -> str | None:
    s = _norm_label(label)
    if s.startswith("participio"):
        return "part"
    if s.startswith("gerundio"):
        return "ger"
    if s.startswith("imperativo"):
        return None if "negativ" in s else "imp_aff"
    return tense_code_for_label(label)


def form_is_irregular(lemma: str, tense_label, person_label, form, lang: str) -> bool | None:
    """Does this one conjugation-table cell sound different from the regular
    formation (#1537, #1539)? True/False, or None when there is no verdict
    (not Spanish, unknown tense such as the negative imperative, not an
    -ar/-er/-ir verb).

    Pronunciation only, like irregular_persons(): resuelvo, tengo, traiga are
    irregular; busqué, cojo, leyó are not. Perfecto = haber + participle:
    irregular iff the participle is. Reflexive verbs are judged by the base
    verb with the pronoun stripped. Cells listing variants ("resolviera /
    resolviese") are irregular if any variant is. Never raises."""
    try:
        if is_verb_expression(lemma, lang):  # Verbausdrücke: regelmäßig
            return False
        if lang != _FORM_LANG:
            return None
        reflexive = _base_verb(lemma) != (lemma or "").strip().lower()
        lemma = _base_verb(lemma)
        tense = _label_tense_code(tense_label)
        if tense is None:
            return None
        person = person_code_for_label(person_label) if person_label else ""
        if tense in _NO_PERSON_TENSES:
            person = ""
        text = (form or "").strip().lower()
        variants = [v.strip() for v in re.split(r"[/,]", text) if v.strip()]
        if tense == "imp_aff" and reflexive and person:
            # quéjate carries the pronoun at the end: judge the bare imperative
            bare = lexicon.form_for(lemma, tense, person, lang)
            variants = [bare] if bare else []
        if not variants:
            return None
        verdicts = []
        for v in variants:
            for pron in _REFLEXIVE_PRONOUNS:
                if v.startswith(pron):
                    v = v[len(pron):]
                    break
            if tense == "perf":  # haber + participle: only the participle can be irregular
                verdict = _cell_regular(v.split()[-1], lemma, "part", "")
            elif tense == "prog":  # estar + gerund: only the gerund can be irregular
                verdict = _cell_regular(v.split()[-1], lemma, "ger", "")
            else:
                verdict = _cell_regular(v, lemma, tense, person or "")
            if verdict is None:
                return None
            verdicts.append(verdict)
        return not all(verdicts)
    except Exception:
        return None


def _slot_lemma(word_zh: str, lang: str) -> str | None:
    """The verb a story-sentence word stands for: a form card's lemma, or a
    slottable verb's (base) infinitive; None for any other word."""
    try:
        parsed = parse_form_card(word_zh) if lang == _FORM_LANG else None
        if parsed:
            return _base_verb(parsed[0])
        if is_slottable_verb(word_zh, lang):
            return _base_verb(head_verb(word_zh, lang))
    except Exception:
        pass
    return None



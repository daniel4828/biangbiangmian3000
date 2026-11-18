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
import os
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
          "perf", "subj_pres", "subj_impf", "imp_aff", "imp_neg")
PERSONS = verb_slots.PERSONS
SPECIAL = ("ser", "ir", "estar", "haber", "dar", "ver")
_SER_IR_TENSES = ("ind_pret", "subj_impf")

# Settings (app_settings, Spanish only)
KEY_ENABLED = "es_conj_enabled"
KEY_RETENTION = "es_conj_retention"
DEFAULT_RETENTION = 0.90
MIN_RETENTION, MAX_RETENTION = 0.80, 0.97
KEY_PULL_PER_DAY = "es_conj_pull_per_day"
KEY_NO_BURY = "es_conj_irregular_no_bury"
KEY_VOCAB_VISIBLE = "es_irregular_vocab_creating_visible"  # #1552, default off
KEY_PULL_BATCH = "es_conj_pull_batch"
# Tense signal words (#1481): story sentences carry a context that fixes the
# tense, and their German translation (LLM instead of Google) keeps it visible.
KEY_TENSE_CUES = "es_tense_cues"
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
    if tense == "imp_aff":  # #1540: no yo; tú = él-Presente, vosotros = -d,
        if person == "1s":  # usted/nosotros/ustedes = Subjuntivo presente
            return None
        if person == "2s":
            return verb_slots.regular_form(base, "ind_pres", "3s")
        if person == "2p":
            return base[:-1] + "d"
        return stem + _SUBJ_PRES[kind][idx]
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
    "imp_aff": ("ar", "er_ir"),
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
    ("imp_aff", "short_tu"): "Imperativo · kurze tú-Form (ten, pon, haz, di, sal, ven)",
    ("imp_aff", "stem"): "Imperativo · Stammwechsel in der tú-Form (piensa, duerme, pide)",
    ("imp_aff", "subj"): "Imperativo · usted/nosotros/ustedes vom unregelmäßigen "
                         "Subjuntivo (tenga, hagamos, busquen)",
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


def _group_imp(base, person, form, reg):
    """#1540: tú = short form (ten, haz, di) or stem change (piensa);
    usted/nosotros/ustedes inherit any Subjuntivo irregularity (tenga, busque)."""
    if person == "2s":
        if len(form) <= 2 or not form.endswith(("a", "e")):
            return "short_tu"
        stem = base[:-2]
        if _vowel_change(stem, reg[len(stem):], form, (
                ("e", "ie", "e_ie"), ("o", "ue", "o_ue"), ("e", "i", "e_i"),
                ("u", "ue", "u_ue"))):
            return "stem"
        return None
    if person in ("3s", "1p", "3p"):
        return "subj"
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
    # #1468: in Perfecto only the participle can be irregular (haber changes with
    # the person, identically for every verb), so even ser/ir/ver/... follow the
    # shared participle group instead of becoming single-verb patterns.
    if tense == "perf" and reg is not None and ("perf", "participle") in _GROUPS:
        return {"key": "irr:perf:participle", "label": _GROUPS[("perf", "participle")],
                "kind": "group"}
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
        elif tense == "imp_aff":
            group = _group_imp(base, person, form, reg)
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
        if tense == "imp_neg":  # #1580: the form IS the Subjuntivo presente
            if person == "1s":
                return None
            tense = "subj_pres"
        lemma = verb_slots.head_verb(lemma, lang)  # expressions: head verb (#1546)
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


def _y_exception(base: str, tense: str, person: str) -> bool:
    """True when the lexicon form has a "y" that is not already in the verb's
    stem (leyó, creyeron) -- the real y_3rd exception; accent-only forms
    (creíste, creímos) are not."""
    form = _lex_form(base, tense, person)
    return bool(form) and "y" in form.lower() and "y" not in base[:-2]


def _stem_only(base: str, tense: str) -> bool:
    """True when every person = (some stem) + the REGULAR ending of that person
    (accents ignored): a pure stem change, no irregular endings (#1470).
    False when unsure (missing forms, no usable common prefix, perf)."""
    try:
        if tense == "perf":
            return False
        forms: dict[str, str] = {}
        regs: dict[str, str] = {}
        for person in PERSONS:
            form = _lex_form(base, tense, person)
            reg = _regular(base, tense, person)
            if not form or not reg:
                return False
            forms[person] = _unaccent(form.lower())
            regs[person] = _unaccent(reg.lower())
        # longest common prefix, capped so that no regular ending is empty
        prefix = os.path.commonprefix(list(regs.values()))
        prefix = prefix[:max(0, min(len(r) for r in regs.values()) - 1)]
        # an empty prefix (e.g. ir: stem "") is fine: the ending is then the
        # whole regular form
        for person in PERSONS:
            ending = regs[person][len(prefix):]
            if not ending:
                return False
            form = forms[person]
            if not form.endswith(ending) or not form[:-len(ending)]:
                return False
        return True
    except Exception:
        return False


def focus_groups(lemma: str, tense: str, lang: str = LANG) -> list[list[str]]:
    """Person groups a creating card practises in Zeit-des-Tages mode (#1440),
    one group per pattern; the caller drills one person of every group
    (pick_person). Irregular groups first, then the regular contrast group, each
    in PERSONS order:
    - no irregular person (regular verb) -> [];
    - stem change only (#1470): every person is a stem + the regular ending of
      that person -> ONE group with all persons (the main
      sentence's person rotates through pick_person);
    - a single-verb pattern (kind "verb": ser, ir, ...) -> every person alone;
    - y_3rd: only persons whose form has a new "y" (leyó, creyeron) are the
      exception; accent-only persons (creíste) join the regular contrast group
      (creer ind_pret: [["3s","3p"],["1s","2s","1p","2p"]]);
    Never raises ([] on any failure)."""
    try:
        if verb_slots.is_verb_expression(lemma, lang):
            return []  # Verbausdrücke zählen als regelmäßig: normale Rotation
        lemma = verb_slots.head_verb(lemma, lang)  # expressions: head verb (#1546)
        pats = []
        for person in PERSONS:
            pat = pattern_for(lemma, tense, person, lang)
            if pat:
                pats.append((person, pat))
        if not any(p["kind"] != "regular" for _, p in pats):
            return []
        base = verb_slots._base_verb(lemma)
        if _stem_only(base, tense):
            return [[person for person, _ in pats]]
        if any(p["kind"] == "verb" for _, p in pats):
            return [[person] for person, _ in pats]
        irregular: dict[str, list[str]] = {}
        regular: list[str] = []
        for person, pat in pats:
            if pat["kind"] == "regular":
                regular.append(person)
            elif pat["key"].endswith(":y_3rd") and not _y_exception(base, tense, person):
                regular.append(person)
            else:
                irregular.setdefault(pat["key"], []).append(person)
        return [g for g in list(irregular.values()) + [regular] if g]
    except Exception as e:
        logger.warning("focus_groups(%r, %r) failed — %s", lemma, tense, e)
        return []


def pick_person(group: list[str], tense: str, usage: dict | None = None,
                local: dict | None = None, weights: dict | None = None) -> str:
    """The person of `group` to drill next.

    Unweighted (weights None): fewest uses of the slot ("{tense}:{person}") in
    `usage` ({slot: (count, last_generated_at)}), then fewest picks of that slot
    in `local` ({slot: n}, the persons already given to other cards of the same
    story), then the oldest last use (never = oldest), then PERSONS order.
    Without usage/local that is the first person of the group.

    Weighted (#1479, `weights` = {person: weight}, e.g. verb_slots.weights()
    ["persons"]): smallest (uses + local picks + 1) / weight, so over time each
    person is drilled about as often as it occurs in real Spanish (él ~ half,
    vosotros rarely); weight 0 = never (unless the whole group is 0). Ties ->
    larger weight, oldest last use, PERSONS order.

    `local` matters because cards introduced together have the same history
    (#1438 introduces ~25 form cards of a tense on one day): without it every
    one of them got the same person in every story."""
    usage = usage or {}
    local = local or {}

    def idx(person: str) -> int:
        return PERSONS.index(person) if person in PERSONS else 99

    if weights:
        def w(person: str) -> float:
            try:
                return max(float(weights.get(person, 1.0)), 0.0)
            except (TypeError, ValueError):
                return 1.0
        pool = [p for p in group if w(p) > 0] or list(group)

        def wkey(person: str):
            slot = f"{tense}:{person}"
            count, last = usage.get(slot) or (0, None)
            weight = w(person) or 1e-9
            return (round(((count or 0) + local.get(slot, 0) + 1) / weight, 9),
                    -weight, last or "", idx(person))
        return min(pool, key=wkey)

    def key(person: str):
        slot = f"{tense}:{person}"
        count, last = usage.get(slot) or (0, None)
        return (count or 0, local.get(slot, 0), last or "", idx(person))

    return min(group, key=key)


def focus_persons(lemma: str, tense: str, lang: str = LANG,
                  usage: dict | None = None, local: dict | None = None,
                  weights: dict | None = None) -> list[str]:
    """One person per focus_groups() group, chosen by pick_person with the verb's
    own slot `usage` ({slot: (count, last)}; None = first person of each group)
    and the story-local picks `local`. [] for a regular verb (caller uses the
    person pointer)."""
    return [pick_person(g, tense, usage, local, weights)
            for g in focus_groups(lemma, tense, lang)]


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
        # imp_neg (#1580) shares the Subjuntivo presente patterns: none of its own
        tenses = [t for t in (cfg["tenses"] if cfg else TENSES)
                  if t in TENSES and t != "imp_neg"]
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
            if tense == "perf":
                continue  # #1468: no single-verb patterns in Perfecto (participle group only)
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
    """{"enabled", "desired_retention", "pull_per_day", "irregular_creating_unburied",
    "tense_cues"}; defaults when unset, unparsable stored
    values fall back to the default with a warning."""
    enabled, retention = True, DEFAULT_RETENTION
    raw = database.get_app_setting(KEY_ENABLED)
    if raw in ("0", "1"):
        enabled = raw == "1"
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
    tense_cues = database.get_app_setting(KEY_TENSE_CUES) != "0"
    return {"tense_cues": tense_cues,
            "enabled": enabled,
            "desired_retention": retention, "pull_per_day": pull,
            "irregular_creating_unburied": no_bury,
            "irregular_vocab_creating_visible":
                database.get_app_setting(KEY_VOCAB_VISIBLE) == "1"}


def validate_settings(body) -> dict:
    """Validate a (partial) payload; returns only the given keys, normalized.
    Raises ValueError with a readable reason."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    known = {"enabled", "desired_retention", "pull_per_day",
             "irregular_creating_unburied", "tense_cues",
             "irregular_vocab_creating_visible"}
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
    if "tense_cues" in body:
        v = body["tense_cues"]
        if isinstance(v, bool):
            out["tense_cues"] = v
        elif isinstance(v, int) and v in (0, 1):
            out["tense_cues"] = bool(v)
        else:
            raise ValueError("'tense_cues' must be true or false")
    if "desired_retention" in body:
        v = body["desired_retention"]
        if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                or not MIN_RETENTION <= v <= MAX_RETENTION):
            raise ValueError(
                f"'desired_retention' must be a number between {MIN_RETENTION} and {MAX_RETENTION}")
        out["desired_retention"] = float(v)
    if "pull_per_day" in body:
        v = body["pull_per_day"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_PULL_PER_DAY:
            raise ValueError(f"'pull_per_day' must be an integer between 0 and {MAX_PULL_PER_DAY}")
        out["pull_per_day"] = v
    if "irregular_creating_unburied" in body:
        v = body["irregular_creating_unburied"]
        if isinstance(v, bool):
            out["irregular_creating_unburied"] = v
        elif isinstance(v, int) and v in (0, 1):
            out["irregular_creating_unburied"] = bool(v)
        else:
            raise ValueError("'irregular_creating_unburied' must be true or false")
    if "irregular_vocab_creating_visible" in body:
        v = body["irregular_vocab_creating_visible"]
        if isinstance(v, bool):
            out["irregular_vocab_creating_visible"] = v
        elif isinstance(v, int) and v in (0, 1):
            out["irregular_vocab_creating_visible"] = bool(v)
        else:
            raise ValueError("'irregular_vocab_creating_visible' must be true or false")
    return out


def save_settings(clean: dict) -> None:
    """Store the keys of a validate_settings() result."""
    if "enabled" in clean:
        database.set_app_setting(KEY_ENABLED, "1" if clean["enabled"] else "0")
    if "desired_retention" in clean:
        database.set_app_setting(KEY_RETENTION, repr(float(clean["desired_retention"])))
    if "pull_per_day" in clean:
        database.set_app_setting(KEY_PULL_PER_DAY, str(int(clean["pull_per_day"])))
    if "tense_cues" in clean:
        database.set_app_setting(KEY_TENSE_CUES, "1" if clean["tense_cues"] else "0")
    if "irregular_creating_unburied" in clean:
        database.set_app_setting(KEY_NO_BURY, "1" if clean["irregular_creating_unburied"] else "0")
    if "irregular_vocab_creating_visible" in clean:
        database.set_app_setting(
            KEY_VOCAB_VISIBLE, "1" if clean["irregular_vocab_creating_visible"] else "0")


def vocab_creating_visible() -> bool:
    """Setting on -> the normal creating card of irregular verbs with form cards
    stays in the queue (#1552). Default off (hidden). Never raises."""
    try:
        return database.get_app_setting(KEY_VOCAB_VISIBLE) == "1"
    except Exception:
        return False


def tense_cues_active() -> bool:
    """Setting on (default) -> tense signal words in story sentences and an LLM
    German translation that keeps the tense (#1481). Never raises (False on
    error = Google-only translation, as before)."""
    try:
        return database.get_app_setting(KEY_TENSE_CUES) != "0"
    except Exception:
        return False


def is_irregular_card_word(word_zh: str, lang: str = LANG) -> bool:
    """True for an irregular Spanish verb card (plain verb or a parsed form
    card); used by the bury rules (#1395). Never raises."""
    try:
        if lang != LANG:
            return False
        if verb_slots.parse_form_card(word_zh):
            return True
        return verb_slots.verb_regularity(word_zh, lang) == "irregular"
    except Exception:
        return False


def irregular_no_bury_active() -> bool:
    """Setting on (default) -> reading/listening must not bury creating of an
    irregular verb. Never raises (False on error = old behaviour)."""
    try:
        return bool(settings()["irregular_creating_unburied"])
    except Exception:
        return False


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
# Pull-forward: a due pattern counts like a due card
# ---------------------------------------------------------------------------

def _word_pattern_keys(word_zh: str, cfg: dict, w_tense: dict, w_person: dict,
                       cache: dict, w_imp: dict | None = None) -> frozenset:
    """Pattern keys the verb card `word_zh` can produce in a story: the same
    option set as verb_slots.assign_slots (enabled slots with weight > 0; form
    cards only their fixed tense). Empty for any non-verb. Cached per word."""
    if word_zh in cache:
        return cache[word_zh]
    keys = set()
    try:
        parsed = verb_slots.parse_form_card(word_zh)
        if parsed:
            lemma, tense = parsed
            for person in verb_slots.form_card_persons(lemma, tense, LANG):
                wp = w_imp if (w_imp and tense in verb_slots.IMPERATIVE_TENSES) else w_person
                if wp.get(person, 1.0) > 0:
                    pat = pattern_for(lemma, tense, person)
                    if pat:
                        keys.add(pat["key"])
        elif verb_slots.is_slottable_verb(word_zh, LANG):
            lemma = word_zh.strip().lower()
            for tense in cfg["tenses"]:
                wp = w_imp if (w_imp and tense in verb_slots.IMPERATIVE_TENSES) else w_person
                for person in cfg["persons"]:
                    if w_tense.get(tense, 1.0) * wp.get(person, 1.0) <= 0:
                        continue
                    if verb_slots.target_form(lemma, tense, person, LANG) is None:
                        continue
                    pat = pattern_for(lemma, tense, person)
                    if pat:
                        keys.add(pat["key"])
    except Exception as e:
        logger.debug("pattern keys of %r failed — %s", word_zh, e)
    cache[word_zh] = frozenset(keys)
    return cache[word_zh]


def ensure_pullforward(lang: str = LANG) -> int:
    """Pull learned verb cards forward to today for due patterns nothing else
    covers (settings: es_conj_pull_per_day, 0 = off). Returns the number of
    cards pulled. Only `cards.due` changes. Idempotent per Anki day (batch in
    app_settings es_conj_pull_batch)."""
    if lang != LANG:
        return 0
    s = settings()
    limit = s["pull_per_day"]
    cfg = verb_slots.config(lang)
    if not s["enabled"] or limit <= 0 or not cfg:
        return 0
    ctx = database.load_pull_context(lang, KEY_PULL_BATCH)
    if ctx is None:
        return 0
    budget = limit - len(ctx["batch_ids"])
    if budget <= 0:
        return 0
    today = ctx["today"]
    states = database.get_conj_states(lang)
    due = [(0 if mode == "prod" else 1, row["due"], key, mode)
           for (key, mode), row in states.items()
           if row.get("state") not in (None, "new") and row.get("due")
           and row["due"][:10] <= today]
    if not due:
        return 0
    due.sort()
    due_list = [(key, mode) for _, _, key, mode in due]

    wts = verb_slots.weights(lang)
    w_tense, w_person = wts.get("tenses") or {}, wts.get("persons") or {}
    cache: dict = {}

    def keys_of(word_zh):
        return _word_pattern_keys(word_zh, cfg, w_tense, w_person, cache,
                                  wts.get("imp_persons"))

    cards = ctx["cards"]
    by_id = {c["id"]: c for c in cards}
    tomorrow = ctx["tomorrow_cutoff"]

    def due_today(c):
        d = c["due"] or ""
        if "T" in d:
            return c["state"] in ("learning", "relearn") and d < tomorrow
        return d <= today

    # Cards that already put a pattern in front of the user today
    covering = [c for c in cards if c["eligible"] and due_today(c)]
    covering += [by_id[r["id"]] for r in ctx["reviewed"] if r["id"] in by_id]
    used_words = {c["word_id"] for c in covering}
    used_words |= {by_id[i]["word_id"] for i in ctx["batch_ids"] if i in by_id}
    covered: set = set()
    # pulled cards first: they were chosen for a pattern, keep that stable
    for c in sorted({c["id"]: c for c in covering}.values(),
                    key=lambda c: (c["id"] not in ctx["batch_ids"], c["id"])):
        mode = mode_for_category(c["category"])
        keys = keys_of(c["word_zh"])
        if not keys:
            continue
        for key, m in due_list:
            if m == mode and key in keys and (key, m) not in covered:
                covered.add((key, m))
                break

    pool = {"prod": [], "recog": []}
    for c in cards:
        d = c["due"] or ""
        if (c["eligible"] and c["state"] == "review" and "T" not in d and d > today
                and c["word_id"] not in used_words):
            pool[mode_for_category(c["category"])].append(c)
    for lst in pool.values():
        lst.sort(key=lambda c: (c["due"], c["id"]))

    pulls: list[int] = []
    for key, mode in due_list:
        if budget <= 0:
            break
        if (key, mode) in covered:
            continue
        for c in pool[mode]:
            if c["word_id"] in used_words:
                continue
            if key in keys_of(c["word_zh"]):
                pulls.append(c["id"])
                used_words.add(c["word_id"])
                budget -= 1
                break
    return database.commit_pulls(lang, KEY_PULL_BATCH, pulls, limit)


# ---------------------------------------------------------------------------
# Story slot planning (used by verb_slots.assign_slots)
# ---------------------------------------------------------------------------

class SlotPlanner:
    """Priority of conjugation patterns when slots are assigned for one batch:
    1. DUE   - a pattern whose review is due (each (pattern, mode) once per call)
    2. NEW   - a not yet introduced pattern (no daily limit since #1401)
    3. rest  - the caller's weighted rotation, restricted to introduced patterns
    """

    def __init__(self, states: dict, today: str):
        self.states = states
        self.today = today
        self.used_due: set = set()
        self.planned_new: set = set()

    @classmethod
    def create(cls, lang, conj_states=None, today=None):
        """None when conjugation scheduling is disabled."""
        s = settings()
        if not s["enabled"]:
            return None
        if today is None:
            today = database.anki_today().isoformat()
        elif hasattr(today, "isoformat"):
            today = today.isoformat()
        states = conj_states if conj_states is not None else database.get_conj_states(lang)
        return cls(states, today)

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
        fresh = [(opt, key) for opt, key in cands
                 if key and not self.introduced(mode, key)
                 and (key, mode) not in self.planned_new]
        if fresh:
            top = max(o[2] for o, _ in fresh)
            opt, key = rng.choice([c for c in fresh if c[0][2] == top])
            self.planned_new.add((key, mode))
            return opt, key
        return None

"""Complete a Spanish conjugation table with the tenses the stored one lacks (#1547).

The table on the back of a verb card comes from entry_forms, written by the
AI when the word was added. Every stored Spanish verb has the same nine tenses
and lacks the Imperativo and the Imperfecto de subjuntivo. Instead of writing
to the production DB, the read path appends the missing tenses from the
lexicon (resources/es-lexicon.json.gz): every verb, today and in the future,
gets them, and a table that already has a tense is never changed.

Handled lemma shapes:
  - plain verb:           tener -> ten / tuviera
  - reflexive:            quejarse -> quéjate / me quejara (pronoun attached
                          to the imperative, accent recomputed: despiértate,
                          vámonos, idos, reíos)
  - one object clitic:    hacerlo -> hazlo, hágalo / lo hiciera
  - verb + fixed words:   tener éxito -> ten éxito / tuviera éxito
                          darse cuenta de -> date cuenta de / me diera cuenta de
Anything else (unknown verb, "y explícaselo") gets nothing. Never raises.
"""
import logging
import re

import lexicon
import verb_slots

logger = logging.getLogger(__name__)

LANG = "es"
SUBJ_IMPF_LABEL = "imperfecto de subjuntivo"
IMPERATIVE_LABEL = "imperativo"

_PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")
_PERSON_LABELS = {"1s": "yo", "2s": "tú", "3s": "él/ella",
                  "1p": "nosotros", "2p": "vosotros", "3p": "ellos/ellas"}
_IMP_PERSONS = ("2s", "3s", "1p", "2p", "3p")
_IMP_LABELS = {"2s": "tú", "3s": "usted", "1p": "nosotros",
               "2p": "vosotros", "3p": "ustedes"}
_REFLEXIVE = {"1s": "me", "2s": "te", "3s": "se", "1p": "nos", "2p": "os", "3p": "se"}
_OBJECT_CLITICS = ("los", "las", "les", "lo", "la", "le")

_VOWELS = set("aeiouáéíóúü")
_STRONG = set("aeoáéó")
_ACCENT = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}
_UNACCENT = {v: k for k, v in _ACCENT.items()}


# ---------------------------------------------------------------------------
# Stress and enclitic pronouns (quéjate, hagámoslo, vámonos)
# ---------------------------------------------------------------------------

def _is_vowel(word: str, i: int) -> bool:
    c = word[i]
    if c not in _VOWELS:
        return False
    if c == "u" and i > 0 and word[i - 1] in "qg" and i + 1 < len(word) and word[i + 1] in "eiéí":
        return False  # silent u in que/qui/gue/gui
    return True


def _nuclei(word: str) -> list[list[int]]:
    """Syllable nuclei as lists of letter indices. Two strong vowels or an
    accented í/ú next to another vowel split; everything else is one nucleus."""
    groups: list[list[int]] = []
    prev = False
    for i in range(len(word)):
        if not _is_vowel(word, i):
            prev = False
            continue
        if prev:
            a, b = word[groups[-1][-1]], word[i]
            hiatus = (a in _STRONG and b in _STRONG) or a in "íú" or b in "íú"
            if not hiatus:
                groups[-1].append(i)
                continue
        groups.append([i])
        prev = True
    return groups


def _stressed_nucleus(word: str, nuclei: list[list[int]]) -> int:
    for n, group in enumerate(nuclei):
        if any(word[i] in "áéíóú" for i in group):
            return n
    if len(nuclei) >= 2 and word[-1] in "aeiouns":
        return len(nuclei) - 2
    return len(nuclei) - 1


def _stress_vowel(word: str, group: list[int]) -> int:
    strong = [i for i in group if word[i] in _STRONG]
    return strong[0] if strong else group[-1]


def _attach(form: str, clitic: str) -> str:
    """Imperative + enclitic pronoun with Spanish accent rules: quejad+os ->
    quejaos (id+os -> idos), quejemos+nos -> quejémonos, ve+te -> vete,
    vaya+se -> váyase, divertid+os -> divertíos, reíd+os -> reíos."""
    nuclei = _nuclei(form)
    if not nuclei:
        return form + clitic
    # the stressed vowel is fixed by the full form (despertad: ta), before
    # the -d/-s is dropped
    v = _stress_vowel(form, nuclei[_stressed_nucleus(form, nuclei)])
    if clitic == "os" and form.endswith("d") and form != "id":
        form = form[:-1]
    if clitic == "nos" and form.endswith("mos"):
        form = form[:-1]  # quejemos+nos -> quejémonos
    plain = "".join(_UNACCENT.get(c, c) for c in form)
    word = plain + clitic
    if form == "id":
        return word  # idos
    all_nuclei = _nuclei(word)
    default = len(all_nuclei) - 2 if len(all_nuclei) >= 2 else 0  # clitics end in vowel/s
    idx = next((n for n, g in enumerate(all_nuclei) if v in g), None)
    if idx is None:
        return word
    if idx == default and _stress_vowel(word, all_nuclei[idx]) == v:
        return word
    return word[:v] + _ACCENT.get(word[v], word[v]) + word[v + 1:]


# ---------------------------------------------------------------------------
# Lemma analysis
# ---------------------------------------------------------------------------

def _analyse(lemma: str) -> dict | None:
    """{verb, reflexive, clitic, rest} or None if the lexicon can't conjugate it."""
    words = (lemma or "").strip().lower().split()
    if not words:
        return None
    head, rest = words[0], " ".join(words[1:])
    candidates = [(head, False, "")]
    if head.endswith("se") and len(head) > 3:
        candidates.append((head[:-2], True, ""))
    for cl in _OBJECT_CLITICS:
        if head.endswith(cl) and len(head) > len(cl) + 2:
            candidates.append((head[:-len(cl)], False, cl))
    for verb, reflexive, clitic in candidates:
        if verb[-2:] not in ("ar", "er", "ir", "ír"):
            continue
        if lexicon.form_for(verb, "subj_impf", "1s", LANG):
            return {"verb": verb, "reflexive": reflexive, "clitic": clitic, "rest": rest}
    return None


_PLURAL = {"1p", "2p", "3p"}


def _rest_for(a: dict, person: str) -> str:
    """ser/estar + one adjective agrees in number: estemos orgullosos."""
    rest = a["rest"]
    if (person in _PLURAL and a["verb"] in ("ser", "estar")
            and " " not in rest and rest.endswith(("o", "a"))):
        return rest + "s"
    return rest


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def subj_impf_row(lemma: str) -> list[tuple[str, str]]:
    """[(person label, form)] for the six persons, [] if unavailable."""
    a = _analyse(lemma)
    if not a:
        return []
    row = []
    for p in _PERSONS:
        form = lexicon.form_for(a["verb"], "subj_impf", p, LANG)
        if not form:
            return []
        pron = _REFLEXIVE[p] if a["reflexive"] else a["clitic"]
        row.append((_PERSON_LABELS[p], _join(pron, form, _rest_for(a, p))))
    return row


def imperative_row(lemma: str) -> list[tuple[str, str]]:
    """[(person label, form)] for tú/usted/nosotros/vosotros/ustedes."""
    a = _analyse(lemma)
    if not a:
        return []
    row = []
    for p in _IMP_PERSONS:
        if a["reflexive"]:
            # the lexicon has some attached forms itself (quéjate)
            form = lexicon.form_for(a["verb"] + "se", "imp_aff", p, LANG)
            if not form:
                base = lexicon.form_for(a["verb"], "imp_aff", p, LANG)
                form = _attach(base, _REFLEXIVE[p]) if base else None
        else:
            form = lexicon.form_for(a["verb"], "imp_aff", p, LANG)
            if form and a["clitic"]:
                form = _attach(form, a["clitic"])
        if not form:
            return []
        row.append((_IMP_LABELS[p], _join(form, _rest_for(a, p))))
    return row


# ---------------------------------------------------------------------------
# Table completion
# ---------------------------------------------------------------------------

def _has_tense(rows: list[dict], code: str) -> bool:
    for r in rows:
        label = r.get("tense")
        if code == "imp_aff":
            s = verb_slots._norm_label(label)
            if s.startswith("imperativo") and "negativ" not in s:
                return True
        elif verb_slots.tense_code_for_label(label) == code:
            return True
    return False


def _insert_after(rows: list[dict], code: str) -> int:
    """Index after the last row of tense `code`, else before participio/gerundio,
    else the end."""
    last = None
    for i, r in enumerate(rows):
        if verb_slots.tense_code_for_label(r.get("tense")) == code:
            last = i
    if last is not None:
        return last + 1
    for i, r in enumerate(rows):
        s = verb_slots._norm_label(r.get("tense"))
        if s.startswith("participio") or s.startswith("gerundio"):
            return i
    return len(rows)


_BASE_TENSES = (("ind_pres", "presente"), ("ind_pret", "indefinido"),
                ("ind_impf", "imperfecto"), ("ind_fut", "futuro"),
                ("ind_cond", "condicional"), ("subj_pres", "presente de subjuntivo"))


def lexicon_table(lemma: str) -> list[dict]:
    """Ganze Tabelle aus dem Wortschatz, wenn der Eintrag keine gespeicherte
    Konjugation hat (z. B. Formkarte zu »ir« ohne AI-Tabelle). Nie Ausnahme."""
    try:
        out = []
        for code, label in _BASE_TENSES:
            for p in _PERSONS:
                f = lexicon.form_for(lemma, code, p, LANG)
                if f:
                    out.append({"tense": label, "person": _PERSON_LABELS[p], "form": f, "position": None})
        for code, label in (("part", "participio"), ("ger", "gerundio")):
            f = lexicon.form_for(lemma, code, "", LANG)
            if f:
                out.append({"tense": label, "person": "", "form": f, "position": None})
        return out
    except Exception as e:
        logger.warning("lexicon table failed for %r — %s", lemma, e)
        return []


def complete_conjugations(lemma: str, rows: list[dict], lang: str) -> list[dict]:
    """Return `rows` plus the missing Imperfecto de subjuntivo (after the
    Presente de subjuntivo) and Imperativo (right after it). Rows look like
    get_word_conjugations(): {tense, person, form, position}. Only for Spanish
    tables that already exist; the input is never mutated. Never raises."""
    try:
        if lang != LANG or not lexicon.available(LANG):
            return rows
        out = list(rows) if rows else lexicon_table(lemma)
        if not out:
            return rows
        added = not rows
        if not _has_tense(out, "subj_impf"):
            new = [{"tense": SUBJ_IMPF_LABEL, "person": pl, "form": f, "position": None}
                   for pl, f in subj_impf_row(lemma)]
            if new:
                i = _insert_after(out, "subj_pres")
                out[i:i] = new
                added = True
        if not _has_tense(out, "imp_aff"):
            new = [{"tense": IMPERATIVE_LABEL, "person": pl, "form": f, "position": None}
                   for pl, f in imperative_row(lemma)]
            if new:
                i = _insert_after(out, "subj_impf")
                out[i:i] = new
                added = True
        if added:
            out = [{**r, "position": n} for n, r in enumerate(out)]
        return out
    except Exception as e:
        logger.warning("conjugation completion failed for %r — %s", lemma, e)
        return rows

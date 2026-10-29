"""Bundled per-language lexicon: surface form -> lemma (#1293).

Same family as word_frequency.py (#1246): a static resource shipped in the
repo, read from disk, zero network and zero AI at runtime. A language "has a
lexicon" exactly when resources/{lang}-lexicon.json.gz exists — today only
Spanish; French gets one by dropping a file next to it, no code change.

File format (written by scripts/build_{lang}_lexicon.py, see
resources/README.md):

    {"version": 1,
     "lemmas": {lemma: {"r": rank, "p": pos, "g": gender, "en": gloss}},
     "forms":  {form: [lemma, ...]},        # lemma list ordered by frequency
     "verbs":  {form: [[lemma, tense, person], ...]}}

`forms` omits the trivial case "form is a lemma and nothing else", so
lemmas_of() falls back to the lemma table itself.

Every public function is total: a missing or corrupt file degrades to "no
lexicon" (empty answers), never to an exception. The annotators call these on
every token of every page, and losing a page over a lookup table would be
absurd. A missing file is the normal state for languages without a lexicon,
so it only logs at debug; a file that exists but cannot be read is a real
problem and warns.
"""
import gzip
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tests point this at a tmp dir (and call clear_cache()).
_RESOURCE_DIR = Path(__file__).parent / "resources"

_EMPTY = {"lemmas": {}, "forms": {}, "verbs": {}}
_cache: dict[str, dict] = {}

_TENSE_LABELS = {
    "ind_pres": "Presente",
    "ind_pret": "Indefinido",
    "ind_impf": "Imperfecto",
    "ind_fut": "Futuro",
    "ind_cond": "Condicional",
    "subj_pres": "Subjuntivo presente",
    "subj_impf": "Subjuntivo imperfecto",
    "subj_fut": "Subjuntivo futuro",
    "imp_aff": "Imperativo",
    "imp_neg": "Imperativo negativo",
    "ger": "Gerundio",
    "part": "Participio",
    # Composed tense (haber + participle), not a lexicon code: only used by the
    # story verb-slot rotation (#1295, verb_slots.py).
    "perf": "Perfecto",
}
_PERSON_LABELS = {"1s": "1. Sg.", "2s": "2. Sg.", "3s": "3. Sg.",
                  "1p": "1. Pl.", "2p": "2. Pl.", "3p": "3. Pl."}
_MAX_TAG_GROUPS = 3


def clear_cache() -> None:
    """Forget every loaded lexicon (test hook)."""
    _cache.clear()
    _form_index.clear()
    _forms_of_index.clear()


def inject(lang: str, data: dict | None) -> None:
    """Install `data` as the lexicon for `lang` (test hook); None = no lexicon."""
    _cache[lang] = _normalise(data) if data is not None else _EMPTY
    _form_index.pop(lang, None)
    _forms_of_index.pop(lang, None)


def _normalise(data) -> dict:
    if not isinstance(data, dict):
        return _EMPTY
    return {key: (data.get(key) if isinstance(data.get(key), dict) else {})
            for key in ("lemmas", "forms", "verbs")}


def _load(lang: str) -> dict:
    cached = _cache.get(lang)
    if cached is not None:
        return cached
    path = Path(_RESOURCE_DIR) / f"{lang}-lexicon.json.gz"
    data = _EMPTY
    if not path.exists():
        logger.debug("lexicon: no %s", path)
    else:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = _normalise(json.load(f))
        except Exception as e:
            logger.warning("lexicon: cannot read %s — %s", path, e)
    _cache[lang] = data
    return data


def available(lang: str) -> bool:
    try:
        return bool(_load(lang)["lemmas"])
    except Exception:
        return False


def lemmas_of(form: str, lang: str) -> list[str]:
    """Candidate lemmas of `form`, most frequent first; [] when unknown.

    A form that is itself a lemma and nothing else is not stored in `forms`
    (the build script drops it), so it answers [form] from the lemma table."""
    try:
        lex = _load(lang)
        form = (form or "").lower()
        listed = lex["forms"].get(form)
        if listed:
            return list(listed)
        return [form] if form in lex["lemmas"] else []
    except Exception:
        return []


def lemma_info(lemma: str, lang: str) -> dict | None:
    try:
        entry = _load(lang)["lemmas"].get((lemma or "").lower())
        if not isinstance(entry, dict):
            return None
        return {"rank": entry.get("r"), "pos": entry.get("p"),
                "gender": entry.get("g"), "gloss_en": entry.get("en")}
    except Exception:
        return None


def verb_tags(form: str, lang: str) -> list[tuple[str, str, str]]:
    try:
        raw = _load(lang)["verbs"].get((form or "").lower()) or []
        return [(t[0], t[1], t[2]) for t in raw]
    except Exception:
        return []


def slot_label(tense: str, person: str) -> str:
    """Label of one (tense, person) slot: ("ind_pret", "3s") -> "Indefinido · 3. Sg.".

    Same maps as grammar_label() (#1295) so the card tag and the tap panel
    never name a tense differently. Person "" (participle/gerund) -> tense only."""
    label = _TENSE_LABELS.get(tense, tense)
    person_label = _PERSON_LABELS.get(person)
    return f"{label} · {person_label}" if person_label else label


# Lazy reverse indexes per language (#1295). Built once from the loaded
# lexicon; tests that inject()/clear_cache() must also drop these — both hooks do.
_form_index: dict[str, dict] = {}
_forms_of_index: dict[str, dict] = {}


def form_for(lemma: str, tense: str, person: str, lang: str) -> str | None:
    """The conjugated form of `lemma` for (tense, person), or None. Never raises.

    Reverse of verb_tags(): when several forms share a slot (voseo variants,
    alternative spellings) the first one in file order wins."""
    try:
        index = _form_index.get(lang)
        if index is None:
            index = {}
            for form, tags in _load(lang)["verbs"].items():
                for t in tags:
                    index.setdefault((t[0], t[1], t[2]), form)
            _form_index[lang] = index
        return index.get(((lemma or "").lower(), tense, person))
    except Exception:
        return None


def forms_of(lemma: str, lang: str) -> list[str]:
    """Every known form of `lemma` (including the lemma itself). Never raises."""
    try:
        lemma = (lemma or "").lower()
        index = _forms_of_index.get(lang)
        if index is None:
            index = {}
            lex = _load(lang)
            for form, lemmas in lex["forms"].items():
                for lem in lemmas or []:
                    index.setdefault(lem, []).append(form)
            for form, tags in lex["verbs"].items():
                for t in tags:
                    bucket = index.setdefault(t[0], [])
                    if form not in bucket:
                        bucket.append(form)
            _forms_of_index[lang] = index
        out = list(index.get(lemma, []))
        if lemma and lemma in _load(lang)["lemmas"] and lemma not in out:
            out.insert(0, lemma)
        return out
    except Exception:
        return []


def verb_lemmas(lang: str) -> list[str]:
    """Verb lemmas (pos 'v') that have at least one conjugated form in the
    verbs index, most frequent first (rank ascending, unranked last). Never
    raises; [] without a lexicon (#1296)."""
    try:
        lex = _load(lang)
        conjugated = set()
        for tags in lex["verbs"].values():
            for t in tags:
                conjugated.add(t[0])
        out = []
        for lemma in conjugated:
            entry = lex["lemmas"].get(lemma)
            if isinstance(entry, dict) and entry.get("p") == "v":
                rank = entry.get("r")
                out.append((rank is None, rank if rank is not None else 0, lemma))
        out.sort()
        return [lemma for _, _, lemma in out]
    except Exception:
        return []


def grammar_label(form: str, lang: str) -> str | None:
    """Short human label for the tap panel: "ir/ser · Indefinido · 1. Sg.".

    The lemma part appears only when it tells something (it differs from the
    form, or the form carries verb tags); the tag part lists up to three
    distinct (tense, person) readings. A plain lemma with no tags -> None."""
    try:
        form = (form or "").lower()
        lemmas = lemmas_of(form, lang)
        tags = verb_tags(form, lang)

        groups: list[str] = []
        for _lemma, tense, person in tags:
            text = slot_label(tense, person)
            if text not in groups:
                groups.append(text)
        groups = groups[:_MAX_TAG_GROUPS]

        parts = []
        if lemmas and (tags or lemmas != [form]):
            parts.append("/".join(lemmas))
        if groups:
            parts.append("; ".join(groups))
        return " · ".join(parts) if parts else None
    except Exception:
        return None

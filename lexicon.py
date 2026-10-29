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
}
_PERSON_LABELS = {"1s": "1. Sg.", "2s": "2. Sg.", "3s": "3. Sg.",
                  "1p": "1. Pl.", "2p": "2. Pl.", "3p": "3. Pl."}
_MAX_TAG_GROUPS = 3


def clear_cache() -> None:
    """Forget every loaded lexicon (test hook)."""
    _cache.clear()


def inject(lang: str, data: dict | None) -> None:
    """Install `data` as the lexicon for `lang` (test hook); None = no lexicon."""
    _cache[lang] = _normalise(data) if data is not None else _EMPTY


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
            label = _TENSE_LABELS.get(tense, tense)
            person_label = _PERSON_LABELS.get(person)
            text = f"{label} · {person_label}" if person_label else label
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

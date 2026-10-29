"""Verb slot rotation for story sentences (#1295).

For Chinese, knowing a word = being able to use it. For Spanish, knowing "ir"
is not knowing "fui": a story that always shows verbs as infinitives never
practises conjugation. So each time a story sentence is generated for a verb
card we pick a (tense, person) SLOT and require the sentence to use exactly
that form. The slot is the one used least so far across ALL verbs (history =
story_sentences.verb_slot, no extra table), so the whole grid gets covered
over time. Verbs keep their three cards; nothing new is scheduled.

Slot codes are "tense:person" ("ind_pret:3s"). Tense codes are the lexicon's
(lexicon.py, #1293) plus the composed "perf" (haber + participle). Which
tenses/persons are enabled lives in languages.py ("verb_slots").

Known limit: reflexive infinitives (quejarse) are skipped — the pronoun has to
move in front of the conjugated form ("me quejé"), which the plain form table
cannot produce.

Everything on the generation path is total: failures log a warning and degrade
to "no slots" (the story is generated as before).
"""
import logging
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
}
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
        if w.endswith("se"):  # reflexive — known limit, see module docstring
            return False
        info = lexicon.lemma_info(w, lang)
        if not info or info.get("pos") != "v":
            return False
        return lexicon.form_for(w, "ind_pres", "1s", lang) is not None
    except Exception:
        return False


def target_form(lemma: str, tense: str, person: str, lang: str) -> str | None:
    """The exact string a sentence must contain for this slot, or None."""
    try:
        if tense == "perf":
            aux = lexicon.form_for("haber", "ind_pres", person, lang) or _HABER_PRES.get(person)
            part = lexicon.form_for(lemma, "part", "", lang)
            return f"{aux} {part}" if aux and part else None
        return lexicon.form_for(lemma, tense, person, lang)
    except Exception:
        return None


def describe(slot: str) -> str:
    """English description for the prompt: "Indefinido (preterite), 3rd person singular"."""
    tense, _, person = (slot or "").partition(":")
    return f"{_TENSE_EN.get(tense, tense)}, {_PERSON_EN.get(person, person)}"


def assign_slots(cards: list[dict], lang: str, usage: dict | None = None,
                 rng: random.Random | None = None) -> dict[int, dict]:
    """Pick a slot for every slottable verb card: {word_id: {slot, form, label}}.

    Least-used slot first (count across all stories), ties -> least recently
    used (never used = oldest), remaining ties -> random. The local count is
    bumped after each pick so one story spreads over several slots. Never raises.
    """
    try:
        cfg = config(lang)
        if not cfg:
            return {}
        if usage is None:
            usage = database.verb_slot_usage(lang)
        rng = rng or random.Random()
        counts = {k: v[0] for k, v in usage.items()}
        last = {k: (v[1] or "") for k, v in usage.items()}
        slots = [f"{t}:{p}" for t in cfg["tenses"] for p in cfg["persons"]]

        result: dict[int, dict] = {}
        for card in cards:
            word = card.get("word_zh") or ""
            if not is_slottable_verb(word, lang):
                continue
            lemma = word.strip().lower()
            options = []
            for slot in slots:
                tense, _, person = slot.partition(":")
                form = target_form(lemma, tense, person, lang)
                if form:
                    options.append((slot, form))
            if not options:
                continue
            best = min((counts.get(s, 0), last.get(s, "")) for s, _ in options)
            tied = [o for o in options if (counts.get(o[0], 0), last.get(o[0], "")) == best]
            slot, form = rng.choice(tied)
            counts[slot] = counts.get(slot, 0) + 1
            tense, _, person = slot.partition(":")
            result[card["word_id"]] = {"slot": slot, "form": form,
                                       "label": lexicon.slot_label(tense, person)}
        return result
    except Exception as e:
        logger.warning("verb slot assignment failed — %s", e)
        return {}

"""Verb slot rotation for Spanish story sentences (#1295)."""
import random
from unittest.mock import patch

import pytest

import ai
import database
import lexicon
import verb_slots


def _verb(lemma, rank=10):
    return {"r": rank, "p": "v", "en": lemma}


LEXICON = {
    "lemmas": {
        "hablar": _verb("to speak"), "comer": _verb("to eat"),
        "haber": _verb("to have"), "ir": _verb("to go"),
        "quejar": _verb("to complain"),
        "casa": {"r": 50, "p": "n", "g": "f", "en": "house"},
    },
    "forms": {"habló": ["hablar"], "hablo": ["hablar"], "fui": ["ir", "ser"]},
    "verbs": {
        "hablo": [["hablar", "ind_pres", "1s"]],
        "hablas": [["hablar", "ind_pres", "2s"]],
        "habla": [["hablar", "ind_pres", "3s"]],
        "hablamos": [["hablar", "ind_pres", "1p"]],
        "habláis": [["hablar", "ind_pres", "2p"]],
        "hablan": [["hablar", "ind_pres", "3p"]],
        "habló": [["hablar", "ind_pret", "3s"]],
        "hablaba": [["hablar", "ind_impf", "1s"]],
        "hablado": [["hablar", "part", ""]],
        "como": [["comer", "ind_pres", "1s"]],
        "he": [["haber", "ind_pres", "1s"]],
        "has": [["haber", "ind_pres", "2s"]],
        "ha": [["haber", "ind_pres", "3s"]],
        "hemos": [["haber", "ind_pres", "1p"]],
        "habéis": [["haber", "ind_pres", "2p"]],
        "han": [["haber", "ind_pres", "3p"]],
        "voy": [["ir", "ind_pres", "1s"]],
        "fui": [["ir", "ind_pret", "1s"]],
        "quejo": [["quejar", "ind_pres", "1s"]],
        "quejé": [["quejar", "ind_pret", "1s"]],
        "quejado": [["quejar", "part", ""]],
    },
}


@pytest.fixture(autouse=True)
def _lex():
    lexicon.inject("es", LEXICON)
    lexicon.inject("fr", None)
    yield
    lexicon.clear_cache()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _card(wid, word):
    return {"word_id": wid, "word_zh": word, "pinyin": "", "definition": "", "pos": ""}


# --- lexicon additions -------------------------------------------------------

def test_form_for_and_forms_of_and_label():
    assert lexicon.form_for("hablar", "ind_pret", "3s", "es") == "habló"
    assert lexicon.form_for("hablar", "ind_pret", "1p", "es") is None
    assert lexicon.form_for("hablar", "ind_pret", "3s", "fr") is None
    forms = lexicon.forms_of("hablar", "es")
    assert {"hablar", "hablo", "habló", "hablado", "habla"} <= set(forms)
    assert lexicon.forms_of("hablar", "fr") == []
    assert lexicon.slot_label("ind_pret", "3s") == "Indefinido · 3. Sg."
    assert lexicon.slot_label("perf", "1p") == "Perfecto · 1. Pl."
    assert lexicon.grammar_label("fui", "es").endswith("Indefinido · 1. Sg.")


# --- verb_slots helpers ------------------------------------------------------

def test_target_form_simple_and_perfect():
    assert verb_slots.target_form("hablar", "ind_pret", "3s", "es") == "habló"
    assert verb_slots.target_form("hablar", "perf", "3s", "es") == "ha hablado"
    assert verb_slots.target_form("hablar", "perf", "1p", "es") == "hemos hablado"
    assert verb_slots.target_form("comer", "perf", "3s", "es") is None  # no participle


def test_is_slottable_verb():
    assert verb_slots.is_slottable_verb("hablar", "es")
    assert not verb_slots.is_slottable_verb("casa", "es")
    assert verb_slots.is_slottable_verb("quejarse", "es")  # reflexive (#1301)
    assert not verb_slots.is_slottable_verb("casarse", "es")  # base not a known verb
    assert not verb_slots.is_slottable_verb("ir a casa", "es")
    assert not verb_slots.is_slottable_verb("hablar", "fr")
    assert verb_slots.config("fr") is None


def test_target_form_reflexive():
    assert verb_slots.target_form("quejarse", "ind_pret", "1s", "es") == "me quejé"
    assert verb_slots.target_form("quejarse", "perf", "1s", "es") == "me he quejado"
    assert verb_slots.target_form("quejarse", "ind_pret", "3s", "es") is None  # no such form


def test_assign_slots_reflexive():
    usage = _full_usage(**{"ind_pret:1s": (0, None)})
    out = verb_slots.assign_slots([_card(1, "quejarse")], "es", usage=usage)
    assert out[1]["form"] == "me quejé"


def test_config_enables_future_and_subjunctive():
    tenses = verb_slots.config("es")["tenses"]
    assert "ind_fut" in tenses and "subj_pres" in tenses and "subj_impf" in tenses
    assert "subjunctive" in verb_slots.describe("subj_pres:1s")


@pytest.mark.skipif(not (lexicon._RESOURCE_DIR / "es-lexicon.json.gz").exists(),
                    reason="bundled es lexicon missing")
def test_real_lexicon_has_haber_supplement():
    lexicon.clear_cache()
    assert lexicon.form_for("haber", "ind_pret", "1s", "es") == "hube"
    assert lexicon.form_for("haber", "part", "", "es") == "habido"
    cands = verb_slots.form_card_candidates("es", max_verbs=200, limit=1000)
    assert "haber" in {c["lemma"] for c in cands}


def _full_usage(n=1, **overrides):
    usage = {}
    for t in ("ind_pres", "ind_pret", "ind_impf", "perf"):
        for p in ("1s", "2s", "3s", "1p", "2p", "3p"):
            usage[f"{t}:{p}"] = (n, "2026-01-02T00:00:00")
    usage.update(overrides)
    return usage


def test_assign_slots_least_used_first():
    usage = _full_usage(**{"ind_pret:3s": (0, None)})
    out = verb_slots.assign_slots([_card(1, "hablar")], "es", usage=usage)
    assert out[1] == {"slot": "ind_pret:3s", "form": "habló", "label": "Indefinido · 3. Sg."}


def test_assign_slots_ties_least_recent():
    usage = _full_usage(**{"ind_impf:1s": (1, "2025-01-01T00:00:00")})
    out = verb_slots.assign_slots([_card(1, "hablar")], "es", usage=usage)
    assert out[1]["slot"] == "ind_impf:1s"


def test_assign_slots_spreads_within_batch_and_skips_non_verbs():
    usage = _full_usage(**{"ind_pret:3s": (0, None), "ind_impf:1s": (0, None)})
    cards = [_card(1, "hablar"), _card(2, "casa"), _card(3, "hablar")]
    out = verb_slots.assign_slots(cards, "es", usage=usage)
    assert set(out) == {1, 3}
    assert {out[1]["slot"], out[3]["slot"]} == {"ind_pret:3s", "ind_impf:1s"}


def test_assign_slots_seeded_rng_deterministic_and_error_safe():
    cards = [_card(1, "hablar")]
    a = verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(7))
    b = verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(7))
    assert a == b and a
    assert verb_slots.assign_slots(cards, "es", usage=42) == {}
    assert verb_slots.assign_slots(cards, "fr", usage={}) == {}


# --- database ----------------------------------------------------------------

def test_verb_slot_usage_and_round_trip(tmp_db):
    deck_id = database.get_or_create_deck("T")
    sentences = [
        {"position": 1, "sentence_zh": "Ella habló.", "word_ids": [],
         "verb_slot": "ind_pret:3s", "grammar_hint": "Indefinido · 3. Sg."},
        {"position": 2, "sentence_zh": "Ella habló otra vez.", "word_ids": [],
         "verb_slot": "ind_pret:3s", "grammar_hint": "Indefinido · 3. Sg."},
        {"position": 3, "sentence_zh": "La casa.", "word_ids": []},
    ]
    story_id = database.create_story("2026-01-01", "reading", deck_id, sentences, lang="es")
    database.create_story("2026-01-01", "reading", deck_id,
                          [{"position": 1, "sentence_zh": "Il parle.", "word_ids": [],
                            "verb_slot": "ind_pres:3s"}], lang="fr")

    usage = database.verb_slot_usage("es")
    assert set(usage) == {"ind_pret:3s"}
    count, last = usage["ind_pret:3s"]
    assert count == 2 and last
    assert database.verb_slot_usage("zh") == {}

    got = database.get_story_sentences(story_id)
    assert got[0]["verb_slot"] == "ind_pret:3s"
    assert got[0]["grammar_hint"] == "Indefinido · 3. Sg."
    assert got[2]["verb_slot"] is None


def test_surface_forms_includes_lexicon_forms():
    forms = database.surface_forms(None, "hablar", "es")
    assert forms == ["hablar"]  # no word_id -> unchanged short-circuit
    forms = database.surface_forms(999, "hablar", "es")
    assert {"hablar", "habló", "hablado"} <= set(forms)
    assert forms == sorted(forms, key=len, reverse=True)


# --- generate_story ----------------------------------------------------------

_FIXED = {1: {"slot": "ind_pret:3s", "form": "habló", "label": "Indefinido · 3. Sg."}}


@pytest.fixture
def _no_translation(monkeypatch):
    monkeypatch.setattr(ai, "_fill_translations", lambda parsed, progress_key=None, lang="zh": None)


def test_generate_story_es_uses_slot(tmp_db, _no_translation, monkeypatch):
    monkeypatch.setattr(verb_slots, "assign_slots", lambda cards, lang, **kw: dict(_FIXED))
    cards = [_card(1, "hablar"), _card(2, "casa")]
    with patch("ai._call_api", return_value="1. Ayer ella habló con su madre.\n2. La casa es grande."):
        sentences, prompt = ai.generate_story(cards, lang="es")
    assert 'hablar → write exactly "habló" (Indefinido (preterite), 3rd person singular)' in prompt
    assert "marked with →" in prompt
    assert sentences[0]["verb_slot"] == "ind_pret:3s"
    assert sentences[0]["grammar_hint"] == "Indefinido · 3. Sg."
    assert "verb_slot" not in sentences[1]


def test_generate_story_es_wrong_form_keeps_card_without_slot(tmp_db, _no_translation, monkeypatch):
    monkeypatch.setattr(verb_slots, "assign_slots", lambda cards, lang, **kw: dict(_FIXED))
    cards = [_card(1, "hablar"), _card(2, "casa")]
    with patch("ai._call_api", return_value="1. Ella habla mucho.\n2. La casa es grande."):
        sentences, _prompt = ai.generate_story(cards, lang="es")
    assert sentences[0]["word_ids"] == [1]
    assert "verb_slot" not in sentences[0] and "grammar_hint" not in sentences[0]


def test_generate_story_fr_prompt_has_no_slot_lines(_no_translation):
    cards = [_card(1, "parler"), _card(2, "maison")]
    with patch("ai._call_api", return_value="1. Il aime parler.\n2. La maison est grande."):
        sentences, prompt = ai.generate_story(cards, lang="fr")
    assert "→" not in prompt and "write exactly" not in prompt
    assert all("verb_slot" not in s for s in sentences)

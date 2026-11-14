"""Imperativo as a verb-slot tense (#1540): uses the real es-lexicon."""
import random

import pytest

import conj_patterns
import lexicon
import verb_slots

pytestmark = pytest.mark.skipif(not lexicon.available("es"), reason="no es lexicon")


@pytest.mark.parametrize("lemma,person,form", [
    ("hablar", "2s", "habla"), ("hablar", "3s", "hable"), ("hablar", "2p", "hablad"),
    ("tener", "2s", "ten"), ("hacer", "2s", "haz"), ("decir", "2s", "di"),
    ("ir", "1p", "vamos"),
    # Wiktionary lists pronoun-attached variants first; the plain form must win.
    ("estar", "2s", "está"), ("estar", "3s", "esté"), ("dar", "3s", "dé"),
    ("mantener", "2s", "mantén"),
    # Reflexive: the pronoun is attached, not put in front.
    ("quejarse", "2s", "quéjate"), ("quejarse", "1p", "quejémonos"),
])
def test_target_form(lemma, person, form):
    assert verb_slots.target_form(lemma, "imp_aff", person, "es") == form


def test_no_yo_imperative():
    assert verb_slots.target_form("hablar", "imp_aff", "1s", "es") is None
    assert conj_patterns.pattern_for("hablar", "imp_aff", "1s") is None


@pytest.mark.parametrize("lemma,person,key", [
    ("hablar", "2s", "reg:imp_aff:ar"), ("hablar", "3p", "reg:imp_aff:ar"),
    ("comer", "2p", "reg:imp_aff:er_ir"), ("vivir", "3s", "reg:imp_aff:er_ir"),
    ("tener", "2s", "irr:imp_aff:short_tu"), ("salir", "2s", "irr:imp_aff:short_tu"),
    ("pensar", "2s", "irr:imp_aff:stem"), ("pedir", "2s", "irr:imp_aff:stem"),
    ("tener", "3s", "irr:imp_aff:subj"), ("buscar", "3p", "irr:imp_aff:subj"),
    ("ir", "2s", "verb:ir:imp_aff"),
])
def test_patterns(lemma, person, key):
    pat = conj_patterns.pattern_for(lemma, "imp_aff", person)
    assert pat["key"] == key
    assert conj_patterns.label_for_key(key) == pat["label"]


def test_catalog_and_weights():
    keys = {p["key"] for p in conj_patterns.catalog() if p["tense"] == "imp_aff"}
    assert {"reg:imp_aff:ar", "reg:imp_aff:er_ir", "irr:imp_aff:short_tu",
            "irr:imp_aff:stem", "irr:imp_aff:subj"} <= keys
    assert verb_slots.default_weights("es")["tenses"]["imp_aff"] == 8


def test_labels_name_the_addressee():
    assert lexicon.slot_label("imp_aff", "3s") == "Imperativo · usted"
    assert lexicon.slot_label("ind_pres", "3s") == "Presente · 3. Sg."
    assert "usted" in verb_slots.describe("imp_aff:3s")
    assert verb_slots.tense_context("imp_aff:2s") and verb_slots.tense_cue_de("imp_aff:2s")


def test_regular_verb_gets_imperative_by_weight():
    """A regular verb lands on the imperative about as often as its weight share."""
    w = verb_slots.default_weights("es")
    cards = [{"word_id": i, "word_zh": "hablar", "category": "reading"} for i in range(400)]
    res = verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(1), weights=w)
    imp = sum(1 for e in res.values() if e["slot"].startswith("imp_aff:"))
    share = w["tenses"]["imp_aff"] / sum(w["tenses"].values())
    # 1s has no imperative, so the expected share is a bit lower than the tense share
    assert 0.5 * share * 400 < imp < 1.3 * share * 400
    assert all(e["slot"] != "imp_aff:1s" for e in res.values())

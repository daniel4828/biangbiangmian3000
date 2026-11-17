"""Reflexive irregular verbs get form cards like their base verb."""
import pytest

import lexicon
import verb_slots

pytestmark = pytest.mark.skipif(not lexicon.available("es"), reason="no es lexicon")


@pytest.mark.parametrize("lemma", ["rendirse", "divertirse"])
def test_reflexive_irregular_verb_gets_form_cards(lemma):
    plan = verb_slots.irregular_card_plan(lemma, "es")
    assert plan == [c for c in plan if c["lemma"] == lemma]
    assert {c["tense"] for c in plan} >= {"ind_pres", "ind_pret"}
    for c in plan:
        assert verb_slots.parse_form_card(c["word_zh"]) == (lemma, c["tense"])
    assert verb_slots.verb_regularity(lemma, "es") == "irregular"


def test_reflexive_nonfinite_forms():
    t = verb_slots.target_form
    assert t("rendirse", "ind_pres", "1s", "es") == "me rindo"
    assert t("rendirse", "imp_aff", "2s", "es") == "ríndete"
    assert t("divertirse", "ger", "", "es") == "divirtiéndose"
    assert t("rendirse", "part", "", "es") == "rendido"
    assert verb_slots.irregular_card_plan("quejarse", "es") == []

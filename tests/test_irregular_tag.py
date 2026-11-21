"""Etikett „irreg.“ für alle unregelmäßigen Verben (echtes Lexikon)."""
import pytest

import lexicon
import verb_slots


@pytest.fixture(autouse=True)
def _real_lexicon():
    lexicon.clear_cache()
    if not lexicon.available("es"):
        pytest.skip("kein es-Lexikon")
    yield
    lexicon.clear_cache()


@pytest.mark.parametrize("lemma", ["irse", "reírse", "oírse", "acostarse", "acostar", "errar", "colar"])
def test_irregular_edge_cases(lemma):
    assert verb_slots.verb_regularity(lemma, "es") == "irregular"


@pytest.mark.parametrize("lemma", ["hablar", "quejarse", "comer"])
def test_regular_stays_regular(lemma):
    assert verb_slots.verb_regularity(lemma, "es") == "regular"


def test_form_for_prefers_irregular_variant():
    assert lexicon.form_for("acostar", "ind_pres", "1s", "es") == "acuesto"
    assert lexicon.form_for("hablar", "ind_pres", "1s", "es") == "hablo"

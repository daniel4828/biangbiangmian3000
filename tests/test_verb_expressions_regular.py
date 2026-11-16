"""Mehrwortige Verbausdrücke zählen als regelmäßige Verben (#1563)."""
import conj_patterns
import lexicon
import pytest
import verb_slots

pytestmark = pytest.mark.skipif(not lexicon.available("es"), reason="kein es-Lexikon")


def test_verb_expressions_count_as_regular():
    """Mehrwortige Verbausdrücke (tener éxito, estar orgulloso, darse cuenta de)
    zählen als regelmäßig: keine Formkarten, Etikett reg., keine roten Punkte,
    normale Personenrotation."""
    for w in ("tener éxito", "estar orgulloso", "darse cuenta de", "tener prisa"):
        assert verb_slots.is_verb_expression(w, "es")
        assert verb_slots.irregular_card_plan(w, "es") == []
        assert verb_slots.verb_regularity(w, "es") == "regular"
        assert verb_slots.form_is_irregular(w, "Presente", "yo", "tengo", "es") is False
        assert conj_patterns.focus_groups(w, "ind_pres") == []
    assert not verb_slots.is_verb_expression("tener", "es")
    assert verb_slots.verb_regularity("tener", "es") == "irregular"
    assert verb_slots.irregular_card_plan("tener", "es")

"""Imperativo and Imperfecto de subjuntivo appended to stored tables (#1547)."""
import pytest

import conj_table
import lexicon
import verb_slots

pytestmark = pytest.mark.skipif(not lexicon.available("es"), reason="no es lexicon")


def _imp(lemma):
    return [f for _, f in conj_table.imperative_row(lemma)]


def _subj(lemma):
    return [f for _, f in conj_table.subj_impf_row(lemma)]


@pytest.mark.parametrize("lemma,forms", [
    ("tener", ["ten", "tenga", "tengamos", "tened", "tengan"]),
    ("hacer", ["haz", "haga", "hagamos", "haced", "hagan"]),
    ("decir", ["di", "diga", "digamos", "decid", "digan"]),
    ("ir", ["ve", "vaya", "vamos", "id", "vayan"]),
    ("ser", ["sé", "sea", "seamos", "sed", "sean"]),
    ("poner", ["pon", "ponga", "pongamos", "poned", "pongan"]),
    ("salir", ["sal", "salga", "salgamos", "salid", "salgan"]),
    ("oír", ["oye", "oiga", "oigamos", "oíd", "oigan"]),
])
def test_irregular_imperatives(lemma, forms):
    assert _imp(lemma) == forms


@pytest.mark.parametrize("lemma,forms", [
    ("despertarse", ["despiértate", "despiértese", "despertémonos", "despertaos", "despiértense"]),
    ("divertirse", ["diviértete", "diviértase", "divirtámonos", "divertíos", "diviértanse"]),
    ("irse", ["vete", "váyase", "vámonos", "idos", "váyanse"]),
    ("reírse", ["ríete", "ríase", "riámonos", "reíos", "ríanse"]),
    ("ponerse", ["ponte", "póngase", "pongámonos", "poneos", "pónganse"]),
    ("hacerlo", ["hazlo", "hágalo", "hagámoslo", "hacedlo", "háganlo"]),
    ("darse cuenta de", ["date cuenta de", "dese cuenta de", "démonos cuenta de",
                         "daos cuenta de", "dense cuenta de"]),
])
def test_enclitic_imperatives(lemma, forms):
    assert _imp(lemma) == forms


def test_subjunctive_imperfect():
    assert _subj("tener") == ["tuviera", "tuvieras", "tuviera", "tuviéramos", "tuvierais", "tuvieran"]
    assert _subj("ser")[0] == "fuera"
    assert _subj("quejarse")[3] == "nos quejáramos"
    assert _subj("estar orgulloso")[3] == "estuviéramos orgullosos"
    assert _subj("tener éxito")[3] == "tuviéramos éxito"


def test_unknown_lemma_adds_nothing():
    assert _imp("y explícaselo") == [] and _subj("y explícaselo") == []


def _stored(lemma):
    labels = {"1s": "yo", "2s": "tú", "3s": "él/ella", "1p": "nosotros",
              "2p": "vosotros", "3p": "ellos/ellas"}
    rows = []
    for tense, code in (("presente", "ind_pres"), ("presente de subjuntivo", "subj_pres")):
        for p in verb_slots.PERSONS:
            rows.append({"tense": tense, "person": labels[p],
                         "form": lexicon.form_for(lemma, code, p, "es"), "position": len(rows)})
    rows.append({"tense": "participio", "person": "", "form": "x", "position": len(rows)})
    return rows


def test_complete_inserts_after_subjunctive_and_renumbers():
    out = conj_table.complete_conjugations("tener", _stored("tener"), "es")
    tenses = [r["tense"] for r in out]
    assert tenses[12:17] == ["imperfecto de subjuntivo"] * 5 and tenses[17] == "imperfecto de subjuntivo"
    assert tenses[18:23] == ["imperativo"] * 5
    assert tenses[-1] == "participio"
    assert [r["position"] for r in out] == list(range(len(out)))


def test_existing_tense_is_not_duplicated():
    rows = _stored("tener") + [{"tense": "Imperativo", "person": "tú", "form": "ten", "position": 99}]
    out = conj_table.complete_conjugations("tener", rows, "es")
    assert sum(1 for r in out if r["tense"].lower() == "imperativo") == 1
    assert any(r["tense"] == "imperfecto de subjuntivo" for r in out)


def test_non_spanish_and_empty_untouched():
    rows = [{"tense": "présent", "person": "je", "form": "parle", "position": 0}]
    assert conj_table.complete_conjugations("parler", rows, "fr") is rows
    # Ohne gespeicherte Tabelle: ganze Tabelle aus dem Wortschatz
    full = conj_table.complete_conjugations("ir", [], "es")
    assert ("presente", "yo", "voy") in [(r["tense"], r["person"], r["form"]) for r in full]
    assert any(r["tense"] == "imperativo" for r in full)
    assert conj_table.complete_conjugations("xyzzar", [], "es") == []


def test_reflexive_imperative_not_marked_irregular():
    assert verb_slots.form_is_irregular("quejarse", "imperativo", "tú", "quéjate", "es") is False
    assert verb_slots.form_is_irregular("despertarse", "imperativo", "tú", "despiértate", "es") is True
    assert verb_slots.form_is_irregular("tener", "imperativo", "tú", "ten", "es") is True

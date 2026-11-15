"""Multi-word Spanish verb expressions get a (tense, person) slot (#1546)."""
import pytest

import conj_patterns
import lexicon
import verb_slots


def _lex():
    verbs_by_lemma = {
        "echar": {("ind_pres", "1s"): "echo", ("ind_pret", "1s"): "eché",
                  ("imp_aff", "2s"): "echa"},
        "tener": {("ind_pres", "1s"): "tengo", ("ind_pres", "2s"): "tienes",
                  ("ind_pres", "3s"): "tiene", ("ind_pres", "1p"): "tenemos",
                  ("ind_pres", "2p"): "tenéis", ("ind_pres", "3p"): "tienen"},
        "dar": {("ind_pres", "1s"): "doy", ("ind_pret", "1s"): "di",
                ("ind_pret", "3s"): "dio", ("part", ""): "dado"},
        "volver": {("ind_pres", "1s"): "vuelvo", ("ind_pret", "1s"): "volví"},
        "pasar": {("ind_pres", "1s"): "paso", ("ind_pret", "1s"): "pasé",
                  ("ind_pret", "1p"): "pasamos"},
        "ir": {("ind_pres", "1s"): "voy", ("ind_pret", "1s"): "fui",
               ("ind_pret", "3s"): "fue"},
        "estar": {("ind_pres", "1s"): "estoy", ("ind_pres", "1p"): "estamos"},
        "hacer": {("ind_pres", "1s"): "hago", ("ind_pres", "3s"): "hace"},
        "poner": {("ind_pres", "1s"): "pongo"},
        "haber": {("ind_pres", "1s"): "he", ("ind_pres", "2s"): "has",
                  ("ind_pres", "3s"): "ha", ("ind_pres", "1p"): "hemos",
                  ("ind_pres", "2p"): "habéis", ("ind_pres", "3p"): "han"},
    }
    lemmas = {l: {"r": 10, "p": "v", "en": l} for l in verbs_by_lemma}
    lemmas.update({
        "lindo": {"r": 100, "p": "adj", "en": "nice"},
        "orgulloso": {"r": 100, "p": "adj", "en": "proud"},
        "nervioso": {"r": 100, "p": "adj", "en": "nervous"},
        "mismo": {"r": 100, "p": "adj", "en": "same"},
        "significado": {"r": 100, "p": "n", "g": "m", "en": "meaning"},
        "casa": {"r": 50, "p": "n", "g": "f", "en": "house"},
        "éxito": {"r": 50, "p": "n", "g": "m", "en": "success"},
        "menos": {"r": 50, "p": "adv", "en": "less"},
        "bien": {"r": 50, "p": "adv", "en": "well"},
    })
    verbs: dict = {}
    for lemma, forms in verbs_by_lemma.items():
        for (tense, person), form in forms.items():
            verbs.setdefault(form, []).append([lemma, tense, person])
    return {"lemmas": lemmas, "forms": {}, "verbs": verbs}


@pytest.fixture(autouse=True)
def _lexicon():
    lexicon.inject("es", _lex())
    yield
    lexicon.clear_cache()


def test_target_forms():
    tf = verb_slots.target_form
    assert tf("echar de menos", "ind_pret", "1s", "es") == "eché de menos"
    assert tf("tener éxito", "ind_pres", "3p", "es") == "tienen éxito"
    assert tf("darse cuenta de", "ind_pret", "1s", "es") == "me di cuenta"
    assert tf("darse cuenta de", "perf", "3s", "es") == "se ha dado cuenta"
    assert tf("volver a", "ind_pret", "1s", "es") == "volví a"
    assert tf("pasarla lindo", "ind_pret", "1s", "es") == "la pasé lindo"
    assert tf("pasarla lindo", "ind_pret", "1p", "es") is None  # adjective: singular only
    assert tf("irle bien a alguien", "ind_pret", "3s", "es") == "le fue bien"
    assert tf("irle bien a alguien", "ind_pret", "1s", "es") is None  # le: 3rd persons only
    assert tf("estar orgulloso", "ind_pres", "1s", "es") == "estoy orgulloso"
    assert tf("estar orgulloso", "ind_pres", "1p", "es") is None
    # adjective inside a noun phrase does not restrict the person
    assert tf("tener el mismo significado", "ind_pres", "3p", "es") == "tienen el mismo significado"
    assert tf("dar un paseo", "ind_pret", "3s", "es") == "dio un paseo"
    assert tf("hacer falta", "ind_pres", "3s", "es") == "hace falta"
    assert tf("echar de menos", "imp_aff", "2s", "es") == "echa de menos"
    assert tf("pasarla lindo", "imp_aff", "2s", "es") is None  # fixed clitic
    assert tf("echar de menos", "ind_pret", "", "es") is None


def test_is_slottable_verb():
    for w in ("echar de menos", "tener éxito", "darse cuenta de", "pasarla lindo",
              "volver a"):
        assert verb_slots.is_slottable_verb(w, "es"), w
    for w in ("sin embargo", "el medio ambiente", "como si", "y explícaselo",
              "poner a alguien nervioso"):
        assert not verb_slots.is_slottable_verb(w, "es"), w
    assert not verb_slots.is_slottable_verb("echar de menos", "fr")


def test_expression_parts():
    p = verb_slots.expression_parts("darse cuenta de algo", "es")
    assert p == {"head": "darse", "clitic": "", "rest": "cuenta", "persons": None}
    p = verb_slots.expression_parts("irle bien a alguien", "es")
    assert p["head"] == "ir" and p["clitic"] == "le" and p["rest"] == "bien"
    assert p["persons"] == ["3s", "3p"]
    assert verb_slots.expression_parts("hablar", "es") is None
    assert verb_slots.expression_parts("tener · Indefinido", "es") is None
    assert verb_slots.head_verb("tener éxito") == "tener"
    assert verb_slots.head_verb("tener") == "tener"


def test_assign_slots_uses_expression_form():
    tenses = ("ind_pres", "ind_pret", "ind_impf", "perf", "ind_fut", "subj_pres",
              "subj_impf", "ind_cond", "imp_aff")
    persons = ("1s", "2s", "3s", "1p", "2p", "3p")
    weights = {"tenses": {t: 1.0 for t in tenses}, "persons": {p: 1.0 for p in persons}}
    out = verb_slots.assign_slots(
        [{"word_id": 1, "word_zh": "echar de menos", "category": "reading"}],
        "es", usage={}, weights=weights)
    assert out[1]["form"].endswith(" de menos")


def test_slot_lemma_and_pattern_use_head_verb():
    assert verb_slots._slot_lemma("darse cuenta de", "es") == "dar"
    assert (conj_patterns.pattern_for("tener éxito", "ind_pres", "1s", "es")
            == conj_patterns.pattern_for("tener", "ind_pres", "1s", "es"))


def test_no_irregular_form_cards_for_expressions():
    assert verb_slots.irregular_card_plan("tener éxito", "es") == []

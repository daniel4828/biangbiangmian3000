"""Spanish irregular-verb form cards (#1296)."""
import random

import pytest
from fastapi import HTTPException

import ai
import database
import lexicon
import verb_slots
from routes import imports as imports_routes

PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")

TABLE = {
    "ser": (1, {
        "ind_pret": "fui fuiste fue fuimos fuisteis fueron",
        "ind_pres": "soy eres es somos sois son",
        "ind_impf": "era eras era éramos erais eran",
        "ind_fut": "seré serás será seremos seréis serán",
    }),
    "haber": (2, {"ind_pres": "he has ha hemos habéis han"}),
    "tener": (3, {
        "ind_pret": "tuve tuviste tuvo tuvimos tuvisteis tuvieron",
        "ind_pres": "tengo tienes tiene tenemos tenéis tienen",
        "ind_fut": "tendré tendrás tendrá tendremos tendréis tendrán",
        "ind_impf": "tenía tenías tenía teníamos teníais tenían",
    }),
    "ir": (6, {
        "ind_pret": "fui fuiste fue fuimos fuisteis fueron",
        "ind_pres": "voy vas va vamos vais van",
        "ind_impf": "iba ibas iba íbamos ibais iban",
        "ind_fut": "iré irás irá iremos iréis irán",
    }),
    "ver": (7, {"ind_pret": "vi viste vio vimos visteis vieron"}),
    "dar": (8, {"ind_pret": "di diste dio dimos disteis dieron"}),
    "parecer": (9, {"ind_pres": "parezco pareces parece parecemos parecéis parecen"}),
    "hacer": (10, {"ind_pret": "hice hiciste hizo hicimos hicisteis hicieron"}),
    "decir": (11, {"ind_pret": "dije dijiste dijo dijimos dijisteis dijeron"}),
    "hablar": (4, {
        "ind_pret": "hablé hablaste habló hablamos hablasteis hablaron",
        "ind_pres": "hablo hablas habla hablamos habláis hablan",
        "ind_fut": "hablaré hablarás hablará hablaremos hablaréis hablarán",
        "ind_impf": "hablaba hablabas hablaba hablábamos hablabais hablaban",
    }),
    "buscar": (5, {
        "ind_pret": "busqué buscaste buscó buscamos buscasteis buscaron",
        "ind_pres": "busco buscas busca buscamos buscáis buscan",
    }),
}


def _lexicon():
    lemmas, verbs = {}, {}
    for lemma, (rank, tenses) in TABLE.items():
        lemmas[lemma] = {"r": rank, "p": "v", "en": f"to {lemma}"}
        for tense, row in tenses.items():
            for person, form in zip(PERSONS, row.split()):
                verbs.setdefault(form, []).append([lemma, tense, person])
    return {"lemmas": lemmas, "forms": {}, "verbs": verbs}


@pytest.fixture(autouse=True)
def _lex():
    lexicon.inject("es", _lexicon())
    yield
    lexicon.clear_cache()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def test_regular_form_endings():
    assert verb_slots.regular_form("hablar", "ind_pres", "1p") == "hablamos"
    assert verb_slots.regular_form("comer", "ind_pres", "2p") == "coméis"
    assert verb_slots.regular_form("vivir", "ind_pres", "2p") == "vivís"
    assert verb_slots.regular_form("hablar", "ind_pret", "3s") == "habló"
    assert verb_slots.regular_form("comer", "ind_pret", "3p") == "comieron"
    assert verb_slots.regular_form("vivir", "ind_impf", "1p") == "vivíamos"
    assert verb_slots.regular_form("hablar", "ind_impf", "1p") == "hablábamos"
    assert verb_slots.regular_form("comer", "ind_fut", "3p") == "comerán"
    assert verb_slots.regular_form("quejarse", "ind_pres", "1s") is None
    assert verb_slots.regular_form("hablar", "ind_cond", "1s") is None
    assert verb_slots.regular_form("x", "ind_pres", "1s") is None


def test_irregular_tenses():
    assert verb_slots.irregular_tenses("hablar", "es") == []
    assert verb_slots.irregular_tenses("tener", "es") == ["ind_pret", "ind_pres", "ind_fut"]
    assert verb_slots.irregular_tenses("ser", "es") == ["ind_pret", "ind_pres", "ind_impf"]  # seré is regular
    # spelling change only counts
    assert verb_slots.irregular_tenses("buscar", "es") == ["ind_pret"]
    assert verb_slots.irregular_tenses("nope", "es") == []


def test_ir_two_letter_infinitive():
    assert verb_slots.regular_form("ir", "ind_pres", "1p") == "imos"
    assert verb_slots.regular_form("ir", "ind_pret", "3s") == "ió"
    assert verb_slots.regular_form("ir", "ind_fut", "1s") == "iré"
    assert verb_slots.irregular_tenses("ir", "es") == ["ind_pret", "ind_pres", "ind_impf"]
    words = [c["word_zh"] for c in verb_slots.form_card_candidates("es")]
    for w in ("ir · Indefinido", "ir · Presente", "ir · Imperfecto"):
        assert w in words


def _note(lemma, tense):
    row = TABLE[lemma][1][tense].split()
    return verb_slots.pattern_note(lemma, tense, row, "es")


def test_pret_notes_strong_only_for_real_strong_forms():
    assert "Starkes Indefinido" not in _note("ser", "ind_pret")
    assert _note("ir", "ind_pret").startswith("ser und ir haben dasselbe Indefinido")
    assert "Einsilbige Formen ohne Akzent (vi, vio)" in _note("ver", "ind_pret")
    dar = _note("dar", "ind_pret")
    assert "(di, dio)" in dar and "dar konjugiert wie ein -er/-ir-Verb" in dar
    assert "Starkes Indefinido" in _note("hacer", "ind_pret")
    assert "3. Pl. auf -eron" in _note("decir", "ind_pret")


def test_zco_pattern():
    assert "yo-Form auf -zco." in _note("parecer", "ind_pres")


def test_no_double_periods_and_wie_only_for_real_patterns():
    cands = verb_slots.form_card_candidates("es", max_verbs=50, limit=200)
    assert cands
    for c in cands:
        assert ".." not in c["note_de"], c["note_de"]
        if c["note_de"].startswith(("Unregelmäßig in", "Einsilbige", "ser und ir")):
            assert "Wie:" not in c["note_de"], c["note_de"]
    by = {c["word_zh"]: c["note_de"] for c in cands}
    assert "Wie:" in by["tener · Indefinido"]  # shares the strong pattern with hacer
    assert "hacer" in by["tener · Indefinido"]


def test_word_roundtrip_and_non_matching():
    word = verb_slots.form_card_word("tener", "ind_pret")
    assert word == "tener · Indefinido"
    assert verb_slots.parse_form_card(word) == ("tener", "ind_pret")
    assert verb_slots.parse_form_card("hacer · Futuro") is None  # lexicon lacks hacer
    assert verb_slots.parse_form_card("tener · Subjuntivo presente") is None
    assert verb_slots.parse_form_card("tener") is None
    assert verb_slots.parse_form_card("") is None
    assert verb_slots.parse_form_card(None) is None


def test_pattern_notes():
    row = TABLE["tener"][1]["ind_pret"].split()
    assert "Starkes Indefinido" in verb_slots.pattern_note("tener", "ind_pret", row, "es")
    row = TABLE["tener"][1]["ind_pres"].split()
    assert "-go" in verb_slots.pattern_note("tener", "ind_pres", row, "es")
    assert "e→ie" in verb_slots.pattern_note("tener", "ind_pres", row, "es")
    row = TABLE["tener"][1]["ind_fut"].split()
    assert "Futurstamm: tendr-" in verb_slots.pattern_note("tener", "ind_fut", row, "es")
    row = TABLE["ser"][1]["ind_pret"].split()
    assert "ser und ir" in verb_slots.pattern_note("ser", "ind_pret", row, "es")
    row = TABLE["buscar"][1]["ind_pret"].split()
    assert "Unregelmäßig in: 1. Sg." in verb_slots.pattern_note("buscar", "ind_pret", row, "es")


def test_candidates_order_and_caps():
    cands = verb_slots.form_card_candidates("es")
    words = [c["word_zh"] for c in cands]
    assert words[0] == "ser · Indefinido"
    assert "hablar · Indefinido" not in words
    assert "buscar · Indefinido" in words
    assert all(len(c["row"]) == 6 for c in cands)
    ser = [c for c in cands if c["lemma"] == "ser"]
    assert len(ser) == 3  # max_tenses
    capped = verb_slots.form_card_candidates("es", max_verbs=1)
    assert {c["lemma"] for c in capped} == {"ser"}
    assert len(verb_slots.form_card_candidates("es", limit=2)) == 2
    assert verb_slots.form_card_candidates("fr") == []


def test_assign_slots_fixes_tense_and_rotates_persons():
    cards = [{"word_id": 1, "word_zh": "tener · Indefinido"},
             {"word_id": 2, "word_zh": "tener · Indefinido"}]
    uniform = {"tenses": {}, "persons": {p: 1 for p in PERSONS}}  # #1306: no weighting
    res = verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(0), weights=uniform)
    assert res[1]["slot"].startswith("ind_pret:")
    assert res[2]["slot"].startswith("ind_pret:")
    assert res[1]["slot"] != res[2]["slot"]  # local count bumped
    usage = {f"ind_pret:{p}": (5, "2026-01-01") for p in PERSONS if p != "3s"}
    res = verb_slots.assign_slots([cards[0]], "es", usage=usage, rng=random.Random(0))
    assert res[1]["slot"] == "ind_pret:3s"
    assert res[1]["form"] == "tuvo"


def test_surface_forms_for_form_card(tmp_db):
    forms = database.surface_forms(1, "tener · Indefinido", "es")
    assert {"tuve", "tuviste", "tuvo", "tuvimos", "tuvisteis", "tuvieron"} <= set(forms)


def test_seed_creates_suspended_creating_cards_once(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("AI must not be called")
    monkeypatch.setattr(ai, "_call_api", boom)

    res = imports_routes.seed_es_irregular_form_cards()
    assert res["errors"] == []
    assert "tener · Indefinido" in res["created"]
    assert res["skipped"] == []

    saved = database.get_or_create_saved_deck("es")
    entry = database.get_word_by_zh("tener · Indefinido")
    assert entry["lang"] == "es" and entry["note_type"] == "grammar"
    assert entry["definition_de"] == "tuve, tuviste, tuvo, tuvimos, tuvisteis, tuvieron"
    assert "Starkes Indefinido" in entry["notes"]
    conn = database.get_db()
    cards = conn.execute("SELECT category, state, deck_id FROM cards WHERE word_id=?",
                         (entry["id"],)).fetchall()
    conn.close()
    assert len(cards) == 1
    assert (cards[0]["category"], cards[0]["state"], cards[0]["deck_id"]) == ("creating", "suspended", saved)

    again = imports_routes.seed_es_irregular_form_cards()
    assert again["created"] == []
    assert len(again["skipped"]) == len(res["created"])


def test_endpoint_requires_lexicon(tmp_db):
    lexicon.inject("es", None)
    with pytest.raises(HTTPException) as exc:
        imports_routes.create_es_irregular_form_cards()
    assert exc.value.status_code == 400


def test_verb_regularity_tag():
    # #1315：卡片上的 reg./irreg. 标签
    import verb_slots
    assert verb_slots.verb_regularity("hablar", "es") == "regular"
    assert verb_slots.verb_regularity("tener", "es") == "irregular"
    assert verb_slots.verb_regularity("casa", "es") is None
    # #1352：反身动词按原形判定
    assert verb_slots.verb_regularity("hablarse", "es") == "regular"
    assert verb_slots.verb_regularity("buscarse", "es") == "irregular"
    assert verb_slots.verb_regularity("casarse", "es") is None  # casar fehlt
    assert verb_slots.verb_regularity("hablar", "fr") is None

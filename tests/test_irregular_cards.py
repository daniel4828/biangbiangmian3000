"""Irregular-verb form cards judged by pronunciation (#1539): which persons/tenses
are irregular, card rotation, migration of old cards, cards for new verbs."""
import pytest

import database
import irregular_cards
import lexicon
import verb_slots
from routes import imports as imports_routes

PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")
IMP_PERSONS = ("2s", "3s", "1p", "2p", "3p")

ROWS = {
    "traer": {
        "ind_pres": "traigo traes trae traemos traéis traen",
        "ind_pret": "traje trajiste trajo trajimos trajisteis trajeron",
        "ind_impf": "traía traías traía traíamos traíais traían",
        "ind_fut": "traeré traerás traerá traeremos traeréis traerán",
        "imp_aff": "trae traiga traigamos traed traigan",
        "part": "traído", "ger": "trayendo",
    },
    "subir": {
        "ind_pres": "subo subes sube subimos subís suben",
        "ind_pret": "subí subiste subió subimos subisteis subieron",
        "ind_impf": "subía subías subía subíamos subíais subían",
        "ind_fut": "subiré subirás subirá subiremos subiréis subirán",
        "imp_aff": "sube suba subamos subid suban",
        "part": "subido", "ger": "subiendo",
    },
    "resolver": {
        "ind_pres": "resuelvo resuelves resuelve resolvemos resolvéis resuelven",
        "ind_pret": "resolví resolviste resolvió resolvimos resolvisteis resolvieron",
        "ind_impf": "resolvía resolvías resolvía resolvíamos resolvíais resolvían",
        "ind_fut": "resolveré resolverás resolverá resolveremos resolveréis resolverán",
        "imp_aff": "resuelve resuelva resolvamos resolved resuelvan",
        "part": "resuelto", "ger": "resolviendo",
    },
    "buscar": {
        "ind_pres": "busco buscas busca buscamos buscáis buscan",
        "ind_pret": "busqué buscaste buscó buscamos buscasteis buscaron",
        "ind_impf": "buscaba buscabas buscaba buscábamos buscabais buscaban",
        "ind_fut": "buscaré buscarás buscará buscaremos buscaréis buscarán",
        "imp_aff": "busca busque busquemos buscad busquen",
        "part": "buscado", "ger": "buscando",
    },
    "coger": {
        "ind_pres": "cojo coges coge cogemos cogéis cogen",
        "ind_pret": "cogí cogiste cogió cogimos cogisteis cogieron",
        "ind_impf": "cogía cogías cogía cogíamos cogíais cogían",
        "ind_fut": "cogeré cogerás cogerá cogeremos cogeréis cogerán",
        "imp_aff": "coge coja cojamos coged cojan",
        "part": "cogido", "ger": "cogiendo",
    },
    "leer": {
        "ind_pres": "leo lees lee leemos leéis leen",
        "ind_pret": "leí leíste leyó leímos leísteis leyeron",
        "ind_impf": "leía leías leía leíamos leíais leían",
        "ind_fut": "leeré leerás leerá leeremos leeréis leerán",
        "imp_aff": "lee lea leamos leed lean",
        "part": "leído", "ger": "leyendo",
    },
}


def _lexicon():
    lemmas, verbs = {}, {}
    for lemma, tenses in ROWS.items():
        lemmas[lemma] = {"r": 10, "p": "v", "en": f"to {lemma}", "de": f"{lemma} (de)"}
        for tense, forms in tenses.items():
            if tense in ("part", "ger"):
                verbs.setdefault(forms, []).append([lemma, tense, ""])
                continue
            persons = IMP_PERSONS if tense == "imp_aff" else PERSONS
            for person, form in zip(persons, forms.split()):
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


UNIFORM = {"tenses": {}, "persons": {p: 1.0 for p in PERSONS}}


# --- which persons / tenses are irregular ---------------------------------------------

def test_traer():
    assert verb_slots.irregular_persons("traer", "ind_pres", "es") == ["1s"]
    assert verb_slots.irregular_persons("traer", "ind_pret", "es") == list(PERSONS)
    assert verb_slots.irregular_persons("traer", "imp_aff", "es") == ["3s", "1p", "3p"]
    assert verb_slots.irregular_persons("traer", "part", "es") == []  # traído: accent only
    assert verb_slots.irregular_persons("traer", "ger", "es") == []   # trayendo: y = i
    assert verb_slots.irregular_form_tenses("traer", "es") == ["ind_pres", "ind_pret", "imp_aff"]


def test_resolver():
    assert verb_slots.irregular_persons("resolver", "ind_pres", "es") == ["1s", "2s", "3s", "3p"]
    assert verb_slots.irregular_persons("resolver", "imp_aff", "es") == ["2s", "3s", "3p"]
    assert verb_slots.irregular_persons("resolver", "part", "es") == [""]
    assert verb_slots.irregular_form_tenses("resolver", "es") == ["ind_pres", "imp_aff", "part"]


@pytest.mark.parametrize("lemma", ["subir", "buscar", "coger", "leer"])
def test_spelling_changes_and_regular_verbs_have_no_irregular_tense(lemma):
    assert verb_slots.irregular_form_tenses(lemma, "es") == []
    assert verb_slots.verb_regularity(lemma, "es") == "regular"
    assert verb_slots.irregular_card_plan(lemma, "es") == []


def test_irregular_persons_never_raises_and_skips_unknowns():
    assert verb_slots.irregular_persons("nope", "ind_pres", "es") == []
    assert verb_slots.irregular_persons("traer", "ind_cond", "es") == []  # no card tense
    assert verb_slots.irregular_persons("traer", "ind_pres", "fr") == []
    assert verb_slots.irregular_persons("", "ind_pres", "es") == []


def test_tense_is_irregular_uses_phonetics():
    assert verb_slots.tense_is_irregular("resolver", "perf", "es") is True   # resuelto
    assert verb_slots.tense_is_irregular("traer", "perf", "es") is False
    assert verb_slots.tense_is_irregular("buscar", "ind_pret", "es") is False
    assert verb_slots.tense_is_irregular("traer", "imp_aff", "es") is True
    assert verb_slots.tense_is_irregular("traer", "subj_pres", "es") is True  # follows ind_pres


def test_form_card_persons():
    assert verb_slots.form_card_persons("traer", "ind_pres", "es") == ["1s"]
    assert verb_slots.form_card_persons("resolver", "part", "es") == [""]
    # old card in a regular tense: every person with a form
    assert verb_slots.form_card_persons("traer", "ind_impf", "es") == list(PERSONS)
    assert verb_slots.form_card_persons("traer", "imp_aff", "es") == ["3s", "1p", "3p"]


def test_parse_form_card_new_tenses():
    assert verb_slots.parse_form_card("traer · Imperativo") == ("traer", "imp_aff")
    assert verb_slots.parse_form_card("resolver · Participio") == ("resolver", "part")
    assert verb_slots.parse_form_card("traer · Gerundio") == ("traer", "ger")
    assert verb_slots.parse_form_card("traer · Perfecto") == ("traer", "perf")  # old cards parse
    assert verb_slots.parse_form_card("subir · Imperativo") == ("subir", "imp_aff")
    assert verb_slots.parse_form_card("nope · Imperativo") is None  # no form
    assert verb_slots.split_form_card_word("nope · Imperativo") == ("nope", "imp_aff")
    assert verb_slots.split_form_card_word("traer") is None


def test_plan_notes():
    plan = {c["tense"]: c for c in verb_slots.irregular_card_plan("traer", "es")}
    assert plan["imp_aff"]["note_de"] == (
        "Unregelmäßiger Imperativ: traiga (usted), traigamos (nosotros), traigan (ustedes).")
    assert plan["ind_pres"]["persons"] == ["1s"]
    res = {c["tense"]: c for c in verb_slots.irregular_card_plan("resolver", "es")}
    assert res["part"]["note_de"] == "Unregelmäßiges Partizip: resuelto (he resuelto …)."
    assert verb_slots.irregular_card_plan("quejarse", "es") == []


# --- rotation -----------------------------------------------------------------------------

def test_assign_slots_form_card_uses_only_irregular_persons():
    cards = [{"word_id": i, "word_zh": "traer · Presente"} for i in range(1, 5)]
    res = verb_slots.assign_slots(cards, "es", usage={}, weights=UNIFORM)
    assert {v["slot"] for v in res.values()} == {"ind_pres:1s"}
    assert {v["form"] for v in res.values()} == {"traigo"}

    imp = [{"word_id": i, "word_zh": "traer · Imperativo"} for i in range(1, 4)]
    res = verb_slots.assign_slots(imp, "es", usage={}, weights=UNIFORM)
    assert {v["slot"] for v in res.values()} == {"imp_aff:3s", "imp_aff:1p", "imp_aff:3p"}


def test_assign_slots_participle_card_has_no_person():
    res = verb_slots.assign_slots([{"word_id": 1, "word_zh": "resolver · Participio"}],
                                  "es", usage={}, weights=UNIFORM)
    assert res[1]["slot"] == "part:" and res[1]["form"] == "resuelto"
    assert verb_slots.describe("part:") == "Participio (past participle)"
    assert verb_slots.tense_context("part:")
    assert verb_slots.tense_cue_de("imp_aff:3s")


def test_focus_mode_keeps_form_card_inside_its_persons(tmp_db, monkeypatch):
    monkeypatch.setattr(verb_slots, "_focus_tense", lambda: "ind_pret")
    cards = [{"word_id": 1, "word_zh": "traer · Presente", "category": "creating"},
             {"word_id": 2, "word_zh": "resolver · Participio", "category": "creating"}]
    res = verb_slots.assign_slots(cards, "es", usage={}, weights=UNIFORM)
    assert res[1]["slot"] == "ind_pres:1s"
    assert res[2]["slot"] == "part:"


def test_surface_forms_of_form_cards(tmp_db):
    assert {"traigo", "traer"} <= set(database.surface_forms(1, "traer · Presente", "es"))
    assert "traes" not in database.surface_forms(1, "traer · Presente", "es")
    assert "resuelto" in database.surface_forms(1, "resolver · Participio", "es")
    row = database.form_card_view({"word_zh": "traer · Imperativo", "lang": "es",
                                   "note_type": "grammar", "definition_de": ""})
    assert row["conjugation_row"] == "traiga, traigamos, traigan"


# --- migration --------------------------------------------------------------------------

def _form_card(word, deck, state, due):
    wid = database.insert_word({"word_zh": word, "lang": "es", "note_type": "grammar",
                                "pos": "Verbform", "source": "irregular-forms",
                                "notes": "alt"})
    return wid, database.insert_card(wid, "creating", deck, state=state, due=due)


@pytest.fixture
def world(tmp_db):
    saved = database.get_or_create_saved_deck("es")
    daily, _ = database.get_or_create_daily_deck("2026-03-05", "es")
    leaf = database.get_or_create_category_decks(daily, "2026-03-05")["creating"]
    w = {"saved": saved, "leaf": leaf}
    traer = database.insert_word({"word_zh": "traer", "lang": "es"})
    database.insert_card(traer, "creating", leaf, state="new", due="2026-03-09")
    resolver = database.insert_word({"word_zh": "resolver", "lang": "es"})
    database.insert_card(resolver, "creating", saved, state="suspended", due="2026-03-20")
    w["traer"], w["resolver"] = traer, resolver
    w["keep"] = _form_card("traer · Presente", leaf, "review", "2026-03-02")
    w["drop"] = _form_card("traer · Imperfecto", leaf, "new", "2026-03-04")
    w["perf"] = _form_card("resolver · Perfecto", saved, "suspended", "2026-03-07")
    w["drop2"] = _form_card("resolver · Futuro", saved, "suspended", "2026-03-08")
    conn = database.get_db()
    conn.execute("INSERT INTO review_log (card_id, rating) VALUES (?, 3)", (w["drop"][1],))
    conn.commit()
    conn.close()
    return w


def _by_word(items, key="word_zh"):
    return {it[key]: it for it in items}


def test_plan_migration(world):
    plan = irregular_cards.plan_migration("2026-03-10")
    assert [it["word_zh"] for it in plan["keep"]] == ["traer · Presente"]
    assert [(it["word_zh"], it["new_word_zh"]) for it in plan["rename"]] == [
        ("resolver · Perfecto", "resolver · Participio")]
    assert {it["word_zh"] for it in plan["delete"]} == {"traer · Imperfecto", "resolver · Futuro"}
    created = _by_word(plan["create"])
    assert set(created) == {"traer · Indefinido", "traer · Imperativo",
                            "resolver · Presente", "resolver · Imperativo"}
    # copies deck + the EARLIEST due of the verb's old form cards
    for w in ("traer · Indefinido", "traer · Imperativo"):
        assert created[w]["due"] == "2026-03-02" and created[w]["state"] == "new"
        assert created[w]["deck_id"] == world["leaf"]
    # parked in Saved -> stays parked
    for w in ("resolver · Presente", "resolver · Imperativo"):
        assert created[w]["due"] == "2026-03-07" and created[w]["state"] == "suspended"
        assert created[w]["deck_id"] == world["saved"]
    assert plan["skipped"] == []


def test_apply_migration_and_idempotence(world):
    counts = irregular_cards.apply_migration(irregular_cards.plan_migration("2026-03-10"))
    assert counts["errors"] == []
    assert (counts["kept"], counts["renamed"], counts["deleted"], counts["created"]) == (1, 1, 2, 4)

    conn = database.get_db()
    gone = conn.execute("SELECT deleted_at FROM cards WHERE id=?", (world["drop"][1],)).fetchone()
    logs = conn.execute("SELECT COUNT(*) n FROM review_log WHERE card_id=?",
                        (world["drop"][1],)).fetchone()["n"]
    kept = conn.execute("SELECT state, due, deck_id FROM cards WHERE id=?",
                        (world["keep"][1],)).fetchone()
    conn.close()
    assert gone["deleted_at"] is not None and logs == 1  # soft delete, history stays
    assert tuple(kept) == ("review", "2026-03-02", world["leaf"])  # progress untouched

    assert database.get_word_by_zh("resolver · Perfecto", "es") is None
    renamed = database.get_word_by_zh("resolver · Participio", "es")
    assert renamed["id"] == world["perf"][0]
    assert renamed["notes"] == "Unregelmäßiges Partizip: resuelto (he resuelto …)."
    assert database.get_word_by_zh("traer · Presente", "es")["notes"] == "yo-Form auf -go."

    new = database.live_creating_card(database.get_word_by_zh("traer · Indefinido", "es")["id"])
    assert (new["state"], new["due"], new["deck_id"]) == ("new", "2026-03-02", world["leaf"])
    new = database.live_creating_card(database.get_word_by_zh("resolver · Presente", "es")["id"])
    assert (new["state"], new["due"], new["deck_id"]) == ("suspended", "2026-03-07", world["saved"])

    again = irregular_cards.plan_migration("2026-03-10")
    assert again["create"] == [] and again["delete"] == [] and again["rename"] == []
    assert len(again["keep"]) == 2 + 4


def test_migration_reports_what_it_cannot_copy(tmp_db):
    database.insert_word({"word_zh": "traer", "lang": "es"})  # no card at all
    plan = irregular_cards.plan_migration("2026-03-10")
    assert plan["create"] == []
    assert len(plan["skipped"]) == 3 and "no old form card" in plan["skipped"][0]["reason"]


def test_apply_reports_entry_without_live_card(world):
    plan = irregular_cards.plan_migration("2026-03-10")
    # an entry with the wanted headword exists, but its card was deleted earlier
    wid, cid = _form_card("traer · Indefinido", world["leaf"], "new", "2026-01-01")
    database.soft_delete_card(cid)
    counts = irregular_cards.apply_migration({"create": plan["create"]})
    assert any("traer · Indefinido" in e and "without a live card" in e for e in counts["errors"])
    assert counts["created"] == 3


def test_seed_runs_only_the_create_part(world):
    res = imports_routes.seed_es_irregular_form_cards()
    assert res["errors"] == []
    assert len(res["created"]) == 4
    # nothing deleted or renamed by the button
    for word in ("traer · Imperfecto", "resolver · Perfecto", "resolver · Futuro"):
        assert database.live_creating_card(database.get_word_by_zh(word, "es")["id"])


# --- new verbs ---------------------------------------------------------------------------

def test_create_cards_for_verb_mirrors_the_verb_card(world):
    created = irregular_cards.create_cards_for_verb(world["traer"])
    assert sorted(created) == ["traer · Imperativo", "traer · Indefinido"]  # Presente exists
    for word in created:
        card = database.live_creating_card(database.get_word_by_zh(word, "es")["id"])
        assert (card["state"], card["due"], card["deck_id"]) == ("new", "2026-03-09", world["leaf"])
    assert irregular_cards.create_cards_for_verb(world["traer"]) == []  # idempotent

    created = irregular_cards.create_cards_for_verb(world["resolver"])
    assert "resolver · Participio" in created
    card = database.live_creating_card(database.get_word_by_zh("resolver · Participio", "es")["id"])
    assert (card["state"], card["due"], card["deck_id"]) == ("suspended", "2026-03-20", world["saved"])


def test_create_cards_for_verb_ignores_regular_verbs_and_other_entries(tmp_db):
    saved = database.get_or_create_saved_deck("es")
    subir = database.insert_word({"word_zh": "subir", "lang": "es"})
    database.insert_card(subir, "creating", saved, state="suspended")
    assert irregular_cards.create_cards_for_verb(subir) == []
    assert irregular_cards.create_cards_for_verb(99999) == []
    fr = database.insert_word({"word_zh": "traer", "lang": "fr"})
    assert irregular_cards.create_cards_for_verb(fr) == []


def test_promote_form_cards_with_the_verb(world):
    irregular_cards.create_cards_for_verb(world["resolver"])
    res = imports_routes.promote_saved(world["resolver"])
    assert res["status"] == "promoted"
    for word in ("resolver · Presente", "resolver · Imperativo", "resolver · Participio"):
        card = database.live_creating_card(database.get_word_by_zh(word, "es")["id"])
        assert card["deck_name"] != "Saved" and card["state"] == "new", word
    verb = database.live_creating_card(world["resolver"])
    assert verb["deck_name"] != "Saved" and verb["state"] == "new"


# --- tense-of-the-day gate ------------------------------------------------------------

def test_gate_never_pauses_imperative_participle_gerund(tmp_db):
    leaf = database.get_or_create_category_decks(
        database.get_or_create_daily_deck("2026-03-05", "es")[0], "2026-03-05")["creating"]
    for word in ("traer · Presente", "traer · Imperativo", "resolver · Participio",
                 "traer · Gerundio"):
        _form_card(word, leaf, "new", "2026-03-05")
    changed = database.sync_form_card_pause("ind_pret")  # today: Indefinido
    assert changed == 1  # only Presente is gated

    conn = database.get_db()
    states = {r["word_zh"]: r["state"] for r in conn.execute(
        """SELECT e.word_zh, c.state FROM cards c JOIN entries e ON e.id=c.word_id""")}
    conn.close()
    assert states["traer · Presente"] == "suspended"
    for word in ("traer · Imperativo", "resolver · Participio", "traer · Gerundio"):
        assert states[word] == "new"

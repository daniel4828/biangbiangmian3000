"""Irregular form cards of other tenses are paused on a tense day (#1438)."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import ai
import database
import lexicon
import tense_focus
import verb_slots
from routes import imports as imports_routes

pytestmark = pytest.mark.tense_focus  # conftest switches the focus off otherwise

PERSONS = ("1s", "2s", "3s", "1p", "2p", "3p")
TENER = {
    "ind_pres": "tengo tienes tiene tenemos tenéis tienen",
    "ind_pret": "tuve tuviste tuvo tuvimos tuvisteis tuvieron",
    "ind_impf": "tenía tenías tenía teníamos teníais tenían",
    "ind_fut": "tendré tendrás tendrá tendremos tendréis tendrán",
    "subj_pres": "tenga tengas tenga tengamos tengáis tengan",
    "subj_impf": "tuviera tuvieras tuviera tuviéramos tuvierais tuvieran",
}
HACER = {
    "ind_pres": "hago haces hace hacemos hacéis hacen",
    "ind_pret": "hice hiciste hizo hicimos hicisteis hicieron",
}
PARTICIPLES = {"tener": "tenido", "hacer": "hecho"}
TABLE = {"tener": (1, TENER), "hacer": (2, HACER)}


def _lexicon():
    lemmas, verbs = {}, {}
    for lemma, (rank, tenses) in TABLE.items():
        lemmas[lemma] = {"r": rank, "p": "v", "en": f"to {lemma}"}
        for tense, row in tenses.items():
            for person, form in zip(PERSONS, row.split()):
                verbs.setdefault(form, []).append([lemma, tense, person])
        verbs.setdefault(PARTICIPLES[lemma], []).append([lemma, "part", ""])
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
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("AI must not be called")))


@pytest.fixture
def activated(tmp_db):
    """All form cards moved out of Saved; today's tense is Presente."""
    res = imports_routes.seed_es_irregular_form_cards(activate=True)
    assert res["errors"] == []
    return res


def _row(word):
    conn = database.get_db()
    r = conn.execute(
        """SELECT c.id, c.state, c.pre_suspend_state, d.name AS deck FROM cards c
           JOIN entries e ON e.id=c.word_id JOIN decks d ON d.id=c.deck_id
           WHERE e.word_zh=?""", (word,)).fetchone()
    conn.close()
    return dict(r)


def _update(word, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    conn = database.get_db()
    conn.execute(f"UPDATE cards SET {sets} WHERE word_id=(SELECT id FROM entries WHERE word_zh=?)",
                 (*cols.values(), word))
    conn.commit()
    conn.close()


def _marker():
    return set(json.loads(database.get_app_setting("es_tense_focus_paused") or "[]"))


PRES = ("tener · Presente", "hacer · Presente")
PRET = ("tener · Indefinido", "hacer · Indefinido")


# --- candidates: every tense ------------------------------------------------------

def test_one_verb_yields_seven_cards_with_composed_perfecto():
    cands = verb_slots.form_card_candidates("es")
    tener = {c["tense"]: c for c in cands if c["lemma"] == "tener"}
    assert list(tener) == ["ind_pres", "ind_pret", "ind_impf", "perf", "ind_fut",
                           "subj_pres", "subj_impf"]
    assert tener["perf"]["row"][0] == "he tenido" and tener["perf"]["row"][5] == "han tenido"
    assert tener["ind_impf"]["note_de"].startswith("Regelmäßig in dieser Zeit")
    assert "tener · Imperfecto" in {c["word_zh"] for c in cands}
    assert "hacer · Perfecto" in {c["word_zh"] for c in cands}


def test_condicional_form_card(monkeypatch):
    tener = dict(TENER, ind_cond="tendría tendrías tendría tendríamos tendríais tendrían")
    monkeypatch.setitem(TABLE, "tener", (1, tener))
    lexicon.inject("es", _lexicon())
    assert verb_slots.parse_form_card("tener · Condicional") == ("tener", "ind_cond")
    cands = {c["word_zh"]: c for c in verb_slots.form_card_candidates("es")}
    card = cands["tener · Condicional"]
    assert card["row"] == ["tendría", "tendrías", "tendría", "tendríamos",
                           "tendríais", "tendrían"]
    assert "tendr-" in card["note_de"]


def test_subjunctive_notes_differ():
    cands = {c["tense"]: c for c in verb_slots.form_card_candidates("es")
             if c["lemma"] == "tener"}
    assert "1. Sg. (yo)" in cands["subj_pres"]["note_de"]
    assert "3. Pl. Indefinido" in cands["subj_impf"]["note_de"]
    assert "1. Sg. (yo)" not in cands["subj_impf"]["note_de"]


def _clock(monkeypatch):
    from datetime import date
    state = {"d": date(2026, 3, 10)}
    monkeypatch.setattr(database, "anki_today", lambda: state["d"])
    return state


def _walk(state, days_total):
    """Sync once per mocked day; returns the 'enabled' flag after each day."""
    from datetime import date, timedelta
    out = []
    for n in range(days_total):
        state["d"] = date(2026, 3, 10) + timedelta(days=n)
        tense_focus.sync_form_cards()
        out.append(tense_focus.settings()["enabled"])
    return out


def test_focus_ends_itself_after_the_round(tmp_db, monkeypatch):
    enabled = _walk(_clock(monkeypatch), 9)
    assert enabled == [True] * 8 + [False]  # day 9 would wrap to Presente: off instead
    assert tense_focus.settings()["enabled"] is False
    assert _released_list() == tense_focus.canonical_tenses()


def test_focus_with_two_day_period_stays_on_for_both_days_of_last_tense(tmp_db, monkeypatch):
    state = _clock(monkeypatch)
    tense_focus.save(tense_focus.validate({"days": 2}))
    enabled = _walk(state, 17)
    assert enabled == [True] * 16 + [False]
    assert _released_list() == tense_focus.canonical_tenses()


def _released_list():
    return json.loads(database.get_app_setting("es_form_intro_released") or "[]")


def test_parse_form_card_roundtrips_all_tenses():
    # ind_cond: the TENER fixture has no Condicional row, covered by test_condicional_form_card
    for tense in (t for t in verb_slots.FORM_CARD_ALL_TENSES if t != "ind_cond"):
        word = verb_slots.form_card_word("tener", tense)
        assert verb_slots.parse_form_card(word) == ("tener", tense)
    assert verb_slots.parse_form_card("hacer · Futuro") is None  # no forms


def test_surface_forms_of_perfecto_card(tmp_db):
    forms = database.surface_forms(1, "tener · Perfecto", "es")
    assert "he tenido" in forms and "han tenido" in forms


# --- sync_form_card_pause ----------------------------------------------------------

def test_off_tense_paused_on_tense_untouched(activated):
    assert len(activated["activated"]) == 10
    assert tense_focus.sync_form_cards() == 8
    for w in PRES:
        assert _row(w)["state"] == "new"
    r = _row("tener · Indefinido")
    assert r["state"] == "suspended" and r["pre_suspend_state"] == "new"
    assert len(_marker()) == 8


def _released():
    return set(json.loads(database.get_app_setting("es_form_intro_released") or "[]"))


def test_introduction_is_a_one_way_gate(activated):
    tense_focus.sync_form_cards()  # day 1: Presente released
    assert _released() == {"ind_pres"}
    tense_focus.step(1)  # day 2: Indefinido
    # only the 2 Indefinido cards change (restored); Presente stays active
    assert tense_focus.sync_form_cards() == 2
    assert _released() == {"ind_pres", "ind_pret"}
    for w in PRES + PRET:
        assert _row(w)["state"] == "new"  # unreviewed Presente cards stay in the queue
    r = _row("tener · Imperfecto")
    assert r["state"] == "suspended" and r["pre_suspend_state"] == "new"
    tense_focus.step(-1)  # stepping back does not un-release anything
    assert tense_focus.sync_form_cards() == 0
    assert _released() == {"ind_pres", "ind_pret"}
    assert _row("tener · Indefinido")["state"] == "new"


def test_started_cards_are_never_paused(activated):
    _update("tener · Imperfecto", state="review")
    _update("hacer · Indefinido", state="learning")
    tense_focus.sync_form_cards()
    assert _row("tener · Imperfecto")["state"] == "review"
    assert _row("hacer · Indefinido")["state"] == "learning"
    assert _row("tener · Futuro")["state"] == "suspended"


def test_focus_off_restores_all_and_keeps_released(activated):
    tense_focus.sync_form_cards()
    tense_focus.save(tense_focus.validate({"enabled": False}))
    assert tense_focus.sync_form_cards() == 8
    r = _row("tener · Indefinido")
    assert r["state"] == "new" and r["pre_suspend_state"] is None
    assert _marker() == set()
    assert _released() == {"ind_pres"}


def test_second_call_is_idempotent(activated):
    assert tense_focus.sync_form_cards() == 8
    assert tense_focus.sync_form_cards() == 0
    tense_focus.save(tense_focus.validate({"enabled": False}))
    assert tense_focus.sync_form_cards() == 8
    assert tense_focus.sync_form_cards() == 0


def test_manual_suspension_is_never_touched(activated):
    _update("tener · Presente", state="suspended")
    tense_focus.sync_form_cards()
    assert _row("tener · Presente")["state"] == "suspended"
    tense_focus.save(tense_focus.validate({"enabled": False}))
    tense_focus.sync_form_cards()
    assert _row("tener · Presente")["state"] == "suspended"  # not in the marker


def test_card_moved_to_saved_is_not_restored(activated):
    tense_focus.sync_form_cards()
    saved = database.get_or_create_saved_deck("es")
    _update("tener · Indefinido", deck_id=saved)
    tense_focus.step(1)  # Indefinido day
    tense_focus.sync_form_cards()
    r = _row("tener · Indefinido")
    assert r["state"] == "suspended" and r["deck"] == "Saved"
    assert r["id"] not in _marker()


def test_failing_current_does_not_mass_restore(activated, monkeypatch):
    tense_focus.sync_form_cards()
    monkeypatch.setattr(tense_focus, "current", lambda today=None: None)  # focus is enabled
    assert tense_focus.sync_form_cards() == 0
    assert _row("tener · Indefinido")["state"] == "suspended"


def test_ensure_daily_targets_syncs_for_es_only(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(tense_focus, "sync_form_cards", lambda: calls.append(1) or 3)
    assert database.ensure_daily_targets("es") >= 3
    assert len(calls) == 1
    database.ensure_daily_targets("zh")
    assert len(calls) == 1
    database.ensure_daily_targets(None)
    assert len(calls) == 2


# --- activate -------------------------------------------------------------------------

def test_activate_moves_cards_out_of_saved_and_pauses_off_tense(tmp_db):
    res = imports_routes.create_es_irregular_form_cards({"activate": True})
    assert len(res["activated"]) == 10 and res["errors"] == []
    saved = database.get_or_create_saved_deck("es")
    assert _row("tener · Presente")["deck"] != "Saved"
    assert _row("tener · Presente")["state"] == "new"
    assert _row("tener · Indefinido")["state"] == "suspended"
    assert _row("tener · Indefinido")["deck"] != "Saved"
    conn = database.get_db()
    left = conn.execute("SELECT COUNT(*) n FROM cards WHERE deck_id=?", (saved,)).fetchone()["n"]
    conn.close()
    assert left == 0


def test_default_does_not_activate(tmp_db):
    res = imports_routes.seed_es_irregular_form_cards()
    assert res["activated"] == [] and len(res["created"]) == 10
    assert _row("tener · Presente")["deck"] == "Saved"


def test_endpoint_without_body_and_bad_activate(tmp_db):
    import main
    client = TestClient(main.app)
    r = client.post("/api/es/irregular-form-cards")
    assert r.status_code == 200 and r.json()["activated"] == []
    assert client.post("/api/es/irregular-form-cards", json={"activate": "yes"}).status_code == 400
    r = client.post("/api/es/irregular-form-cards", json={"activate": True})
    assert r.status_code == 200 and len(r.json()["activated"]) == 10
    with pytest.raises(HTTPException):
        imports_routes.create_es_irregular_form_cards({"activate": 1})

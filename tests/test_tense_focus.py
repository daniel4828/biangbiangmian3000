"""Zeit des Tages: one Spanish tense per day (#1434)."""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import conj_patterns
import database
import lexicon
import tense_focus
import verb_slots

pytestmark = pytest.mark.tense_focus

_REAL_LEXICON = lexicon._RESOURCE_DIR / "es-lexicon.json.gz"
DAY0 = date(2026, 3, 10)
ORDER = ["ind_pres", "ind_pret", "ind_impf", "perf", "ind_fut", "subj_pres", "subj_impf"]


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def clock(tmp_db, monkeypatch):
    state = {"d": DAY0}
    monkeypatch.setattr(database, "anki_today", lambda: state["d"])
    return state


@pytest.fixture
def real_lex(clock):
    if not _REAL_LEXICON.exists():
        pytest.skip("bundled es lexicon missing")
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


def _card(wid, word, category="reading"):
    return {"word_id": wid, "word_zh": word, "category": category}


def _set(**kw):
    database.set_app_setting(tense_focus.KEY, json.dumps(kw))


# --- current / settings ----------------------------------------------------------

def test_default_starts_with_presente_today(clock):
    cur = tense_focus.current()
    assert cur["tense"] == "ind_pres" and cur["index"] == 0
    assert cur["count"] == 7 and cur["label"] == "Presente"
    assert cur["next_change"] == (DAY0 + timedelta(days=1)).isoformat()
    s = tense_focus.settings()  # anchor was persisted
    assert s["anchor_date"] == DAY0.isoformat() and s["anchor_index"] == 0


def test_index_advances_daily_and_wraps(clock):
    tense_focus.settings()
    for n, code in enumerate(ORDER):
        assert tense_focus.current(DAY0 + timedelta(days=n))["tense"] == code
    assert tense_focus.current(DAY0 + timedelta(days=7))["tense"] == "ind_pres"
    assert tense_focus.current(DAY0 + timedelta(days=3))["label"] == "Perfecto"


def test_days_per_tense(clock):
    tense_focus.save(tense_focus.validate({"days": 3}))
    assert tense_focus.current(DAY0 + timedelta(days=2))["tense"] == "ind_pres"
    cur = tense_focus.current(DAY0 + timedelta(days=3))
    assert cur["tense"] == "ind_pret"
    assert cur["next_change"] == (DAY0 + timedelta(days=6)).isoformat()


def test_disabled_and_corrupt_setting(clock):
    tense_focus.save(tense_focus.validate({"enabled": False}))
    assert tense_focus.current() is None
    database.set_app_setting(tense_focus.KEY, "{not json")
    assert tense_focus.current()["tense"] == "ind_pres"  # defaults, no exception


def test_step_moves_and_wraps(clock):
    assert tense_focus.step(1)["tense"] == "ind_pret"
    assert tense_focus.step(-1)["tense"] == "ind_pres"
    assert tense_focus.step(-1)["tense"] == "subj_impf"
    with pytest.raises(ValueError):
        tense_focus.step(2)


# --- validate / save -------------------------------------------------------------

def test_validate_errors():
    for bad in ([], "x", {"nope": 1}, {"enabled": 1}, {"days": 0}, {"days": 31},
                {"days": True}, {"days": "2"}, {"tenses": []}, {"tenses": ["xx"]},
                {"tenses": "ind_pres"}):
        with pytest.raises(ValueError):
            tense_focus.validate(bad)


def test_validate_dedupes_and_orders_canonically():
    out = tense_focus.validate({"tenses": ["ind_fut", "ind_pres", "ind_fut"]})
    assert out == {"tenses": ["ind_pres", "ind_fut"]}


def test_save_keeps_current_tense_when_tenses_change(clock):
    tense_focus.step(1)  # today = Indefinido
    tense_focus.save(tense_focus.validate({"tenses": ["ind_pret", "ind_fut"]}))
    assert tense_focus.current()["tense"] == "ind_pret"
    assert tense_focus.current(DAY0 + timedelta(days=1))["tense"] == "ind_fut"
    # current tense dropped out -> restarts with the first remaining one
    tense_focus.save(tense_focus.validate({"tenses": ["ind_fut", "subj_pres"]}))
    assert tense_focus.current()["tense"] == "ind_fut"


# --- assign_slots ----------------------------------------------------------------

def test_assign_slots_focus_rotates_persons_in_order(real_lex):
    cards = [_card(i, w) for i, w in enumerate(
        ["hablar", "comer", "vivir", "trabajar", "estudiar", "caminar", "cantar"], 1)]
    out = verb_slots.assign_slots(cards, "es", usage={})
    persons = [out[c["word_id"]]["slot"] for c in cards]
    assert persons == ["ind_pres:1s", "ind_pres:2s", "ind_pres:3s", "ind_pres:1p",
                       "ind_pres:2p", "ind_pres:3p", "ind_pres:1s"]
    assert out[1]["form"] == "hablo" and out[5]["form"] == "estudiáis"


def test_assign_slots_focus_follows_the_day_tense(real_lex):
    tense_focus.step(1)
    out = verb_slots.assign_slots([_card(1, "hablar")], "es", usage={})
    assert out[1]["slot"] == "ind_pret:1s" and out[1]["form"] == "hablé"


def test_assign_slots_focus_reserves_persons_for_followups(real_lex):
    cards = [_card(1, "hablar", "creating"), _card(2, "comer", "creating")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    # default prod_per_verb 3 -> 2 persons reserved after the first card
    assert out[1]["slot"] == "ind_pres:1s"
    assert out[2]["slot"] == "ind_pres:1p"
    assert out[1]["pattern"] == "reg:ind_pres:ar"
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "0")
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[2]["slot"] == "ind_pres:2s"


def test_assign_slots_form_card_keeps_its_tense(real_lex):
    word = verb_slots.form_card_word("tener", "ind_pret")
    out = verb_slots.assign_slots([_card(1, word)], "es", usage={})
    assert out[1]["slot"] == "ind_pret:1s" and out[1]["form"] == "tuve"


def test_assign_slots_focus_off_uses_normal_rotation(real_lex):
    tense_focus.save(tense_focus.validate({"enabled": False}))
    out = verb_slots.assign_slots([_card(1, "hablar")], "es", usage={"ind_pret:3p": (0, "")},
                                  weights={"tenses": {}, "persons": {}})
    assert 1 in out  # normal logic ran (no focus pointer); slot is some enabled slot


# --- plan_followups --------------------------------------------------------------

def test_plan_followups_focus_uses_next_persons(real_lex):
    entries = [(_card(1, "hablar", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert [f["slot"] for f in out] == ["ind_pres:2s", "ind_pres:3s"]
    assert [f["form"] for f in out] == ["hablas", "habla"]
    assert all(f["pattern"] == "reg:ind_pres:ar" for f in out)  # no pattern dedupe


def test_plan_followups_focus_wraps_persons_and_respects_cap(real_lex):
    entries = [(_card(1, "hablar", "creating"), "ind_pres:3p"),
               (_card(2, "comer", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert [(f["word_id"], f["slot"]) for f in out] == [
        (1, "ind_pres:1s"), (1, "ind_pres:2s"), (2, "ind_pres:2s"), (2, "ind_pres:3s")]
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "3")
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert len(out) == 3


def test_plan_followups_focus_skips_non_creating_and_no_main_slot(real_lex):
    entries = [(_card(1, "hablar", "reading"), "ind_pres:1s"),
               (_card(2, "comer", "creating"), None)]
    assert conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(),
                                        conj_states={}) == []


# --- person types by irregularity (#1440) ------------------------------------------

def test_focus_persons_by_irregularity_type(real_lex):
    assert conj_patterns.focus_persons("pensar", "ind_pres") == ["1s", "1p"]
    assert conj_patterns.focus_persons("salir", "ind_pres") == ["1s", "2s"]
    assert conj_patterns.focus_persons("tener", "ind_pres") == ["1s", "2s", "1p"]
    assert conj_patterns.focus_persons("ser", "ind_pres") == list(verb_slots.PERSONS)
    assert conj_patterns.focus_persons("hablar", "ind_pres") == []


def test_assign_slots_creating_irregular_keeps_pointer(real_lex):
    cards = [_card(1, "salir", "creating"), _card(2, "pensar", "creating"),
             _card(3, "hablar", "creating")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[1]["slot"] == "ind_pres:1s" and out[1]["form"] == "salgo"
    assert out[2]["slot"] == "ind_pres:1s"
    assert out[3]["slot"] == "ind_pres:1s"  # pointer was not moved


def test_assign_slots_reading_irregular_uses_pointer(real_lex):
    cards = [_card(1, "hablar"), _card(2, "salir")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[1]["slot"] == "ind_pres:1s" and out[2]["slot"] == "ind_pres:2s"


def test_plan_followups_form_card_ser_gets_five_followups(real_lex):
    word = verb_slots.form_card_word("ser", "ind_pres")
    entries = [(_card(1, word, "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert [f["slot"] for f in out] == [
        "ind_pres:2s", "ind_pres:3s", "ind_pres:1p", "ind_pres:2p", "ind_pres:3p"]


def test_plan_followups_pensar_only_nosotros(real_lex):
    entries = [(_card(1, "pensar", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert [f["slot"] for f in out] == ["ind_pres:1p"]


def test_plan_followups_cap_is_spread_round_robin(real_lex):
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "2")
    entries = [(_card(1, "ser", "creating"), "ind_pres:1s"),
               (_card(2, "salir", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert sorted(f["word_id"] for f in out) == [1, 2]


# --- API -------------------------------------------------------------------------

def test_api_roundtrip(clock):
    import main
    client = TestClient(main.app)
    r = client.get("/api/es/tense-focus")
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["tense"] == "ind_pres"
    assert [a["code"] for a in body["available"]] == ORDER
    r = client.put("/api/es/tense-focus", json={"days": 2})
    assert r.status_code == 200 and r.json()["settings"]["days"] == 2
    assert client.put("/api/es/tense-focus", json={"days": 99}).status_code == 400
    assert client.put("/api/es/tense-focus", json={"tenses": []}).status_code == 400
    r = client.post("/api/es/tense-focus/step", json={"delta": 1})
    assert r.status_code == 200 and r.json()["current"]["tense"] == "ind_pret"
    assert client.post("/api/es/tense-focus/step", json={"delta": 5}).status_code == 400

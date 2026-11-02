"""Conjugation patterns and their scheduler (#1395)."""
import random
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import conj_patterns
import database
import lexicon
import verb_slots

_REAL_LEXICON = lexicon._RESOURCE_DIR / "es-lexicon.json.gz"


def _verb(lemma, rank=10):
    return {"r": rank, "p": "v", "en": lemma}


# Tiny lexicon for the DB / API tests (classification tests use the real one).
SMALL_LEXICON = {
    "lemmas": {"hablar": _verb("to speak")},
    "forms": {},
    "verbs": {
        "hablo": [["hablar", "ind_pres", "1s"]],
        "hablas": [["hablar", "ind_pres", "2s"]],
        "habla": [["hablar", "ind_pres", "3s"]],
        "hablamos": [["hablar", "ind_pres", "1p"]],
        "habláis": [["hablar", "ind_pres", "2p"]],
        "hablan": [["hablar", "ind_pres", "3p"]],
        "hablé": [["hablar", "ind_pret", "1s"]],
        "hablaste": [["hablar", "ind_pret", "2s"]],
        "habló": [["hablar", "ind_pret", "3s"]],
        "hablaba": [["hablar", "ind_impf", "1s"], ["hablar", "ind_impf", "3s"]],
        "hablabas": [["hablar", "ind_impf", "2s"]],
    },
}


@pytest.fixture
def real_lex():
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


@pytest.fixture
def small_lex():
    lexicon.inject("es", SMALL_LEXICON)
    yield
    lexicon.clear_cache()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def clock(monkeypatch):
    """Controllable Anki day: clock["d"] = date(...)."""
    state = {"d": date(2026, 3, 10)}
    monkeypatch.setattr(database, "anki_today", lambda: state["d"])
    return state


# --- classification ------------------------------------------------------------

CASES = [
    ("hablar", "ind_pres", "1s", "reg:ind_pres:ar"),
    ("comer", "ind_pret", "3s", "reg:ind_pret:er_ir"),
    ("vivir", "ind_pres", "1p", "reg:ind_pres:ir"),
    ("hablar", "ind_fut", "2s", "reg:ind_fut:all"),
    ("tener", "ind_pres", "1s", "irr:ind_pres:yo_go"),
    ("tener", "ind_pres", "2s", "irr:ind_pres:e_ie"),
    ("tener", "ind_pres", "1p", "reg:ind_pres:er"),
    ("tener", "ind_pret", "1s", "irr:ind_pret:strong_u"),
    ("hacer", "ind_pret", "3s", "irr:ind_pret:strong_i"),
    ("decir", "ind_pret", "1s", "irr:ind_pret:strong_j"),
    ("pedir", "ind_pret", "3s", "irr:ind_pret:stem_3rd"),
    ("pedir", "ind_pres", "3s", "irr:ind_pres:e_i"),
    ("leer", "ind_pret", "3s", "irr:ind_pret:y_3rd"),
    ("buscar", "ind_pret", "1s", "irr:ind_pret:ortho"),
    ("buscar", "ind_pret", "3s", "reg:ind_pret:ar"),
    ("conocer", "ind_pres", "1s", "irr:ind_pres:yo_zco"),
    ("tener", "ind_fut", "1s", "irr:ind_fut:stem"),
    ("tener", "subj_pres", "1p", "irr:subj_pres:yo_stem"),
    ("tener", "subj_impf", "1s", "irr:subj_impf:strong_u"),
    ("hacer", "perf", "1s", "irr:perf:participle"),
    ("ir", "ind_pres", "1s", "verb:ir:ind_pres"),
    ("ir", "ind_pret", "1s", "verb:ser_ir:ind_pret"),
    ("ser", "ind_pret", "1s", "verb:ser_ir:ind_pret"),
    ("ir", "ind_impf", "1s", "verb:ir:ind_impf"),
    ("quejarse", "ind_pres", "1s", "reg:ind_pres:ar"),
]


@pytest.mark.skipif(not _REAL_LEXICON.exists(), reason="bundled es lexicon missing")
@pytest.mark.parametrize("lemma,tense,person,key", CASES)
def test_pattern_for_real_lexicon(real_lex, lemma, tense, person, key):
    pat = conj_patterns.pattern_for(lemma, tense, person, "es")
    assert pat is not None, (lemma, tense, person)
    assert pat["key"] == key
    assert pat["kind"] == key.split(":")[0].replace("reg", "regular").replace(
        "irr", "group")
    assert pat["label"] and conj_patterns.label_for_key(key) == pat["label"]


def test_pattern_for_none_cases(small_lex):
    assert conj_patterns.pattern_for("hablar", "ind_pres", "1s", "fr") is None
    assert conj_patterns.pattern_for("hablar", "imp_aff", "2s", "es") is None  # unknown tense
    assert conj_patterns.pattern_for("casa", "ind_pres", "1s", "es") is None  # no verb
    assert conj_patterns.pattern_for("hablar", "ind_fut", "1s", "es") is None  # no form
    assert conj_patterns.pattern_for(None, "ind_pres", "1s", "es") is None


def test_labels_are_german():
    assert conj_patterns.label_for_key("reg:ind_pret:er_ir") == "Indefinido · -er/-ir (regelmäßig)"
    assert conj_patterns.label_for_key("reg:ind_fut:all") == "Futuro (regelmäßig)"
    assert conj_patterns.label_for_key("verb:ser_ir:ind_pret") == "ser/ir · Indefinido"
    assert conj_patterns.label_for_key("verb:oler:ind_pres").endswith("(Sonderform)")


def test_slot_pattern(small_lex):
    assert conj_patterns.slot_pattern("hablar", "ind_pres:1s", "es")["key"] == "reg:ind_pres:ar"
    assert conj_patterns.slot_pattern("casa", "ind_pres:1s", "es") is None
    assert conj_patterns.slot_pattern("hablar", "", "es") is None


def test_mode_for_category():
    assert conj_patterns.mode_for_category("creating") == "prod"
    assert conj_patterns.mode_for_category("reading") == "recog"
    assert conj_patterns.mode_for_category("listening") == "recog"
    assert conj_patterns.mode_for_category(None) == "recog"


@pytest.mark.skipif(not _REAL_LEXICON.exists(), reason="bundled es lexicon missing")
def test_catalog_deterministic_and_complete(real_lex):
    cat = conj_patterns.catalog("es")
    assert cat == conj_patterns.catalog("es")
    keys = [c["key"] for c in cat]
    assert len(keys) == len(set(keys))
    assert {"reg:ind_pres:ar", "irr:ind_pret:strong_u", "verb:ser_ir:ind_pret"} <= set(keys)
    assert all({"key", "label", "kind", "tense"} <= set(c) for c in cat)
    # tense order of the config
    order = verb_slots.config("es")["tenses"]
    tenses = [c["tense"] for c in cat]
    assert tenses == sorted(tenses, key=order.index)
    assert conj_patterns.catalog("fr") == []


# --- settings ------------------------------------------------------------------

def test_settings_defaults_and_validation(tmp_db):
    assert conj_patterns.settings() == {"enabled": True, "new_per_day": 3,
                                        "desired_retention": 0.90, "pull_per_day": 10,
                                        "irregular_creating_unburied": True}
    clean = conj_patterns.validate_settings({"new_per_day": 5})
    assert clean == {"new_per_day": 5}
    conj_patterns.save_settings({"enabled": False, "new_per_day": 5, "desired_retention": 0.85})
    assert conj_patterns.settings() == {"enabled": False, "new_per_day": 5,
                                        "desired_retention": 0.85}
    for bad in ({"new_per_day": 51}, {"new_per_day": -1}, {"new_per_day": 1.5},
                {"new_per_day": True}, {"desired_retention": 0.79},
                {"desired_retention": 0.98}, {"desired_retention": "x"},
                {"enabled": "yes"}, {"nope": 1}, []):
        with pytest.raises(ValueError):
            conj_patterns.validate_settings(bad)


# --- scheduler -----------------------------------------------------------------

P = "reg:ind_pres:ar"


def _rec(ok, mode="recog", pattern=P, **kw):
    return conj_patterns.record("es", pattern, mode, ok, **kw)


def test_record_new_ok(tmp_db, clock):
    r = _rec(True, card_id=1, sentence_id=2, slot="ind_pres:1s", lemma="hablar", form="hablo")
    row = database.get_conj_state("es", P, "recog")
    assert r["state"] == "review" and row["state"] == "review"
    assert r["due"] == (clock["d"] + timedelta(days=3)).isoformat()  # S=3.173, R=0.9
    assert row["reps"] == 1 and row["lapses"] == 0 and row["last_review"] == "2026-03-10"
    assert r["pattern"] == P and r["mode"] == "recog" and r["ok"] is True and r["label"]
    assert database.conj_log_stats("es")[(P, "recog")] == {"total": 1, "correct": 1}


def test_record_new_fail(tmp_db, clock):
    r = _rec(False)
    row = database.get_conj_state("es", P, "recog")
    assert r["state"] == "learning" and row["lapses"] == 0
    assert r["due"] == (clock["d"] + timedelta(days=1)).isoformat()
    assert database.conj_log_stats("es")[(P, "recog")] == {"total": 1, "correct": 0}


def test_review_ok_grows_interval(tmp_db, clock):
    first = _rec(True)
    clock["d"] = date.fromisoformat(first["due"])
    second = _rec(True)
    gap1 = (date.fromisoformat(first["due"]) - date(2026, 3, 10)).days
    gap2 = (date.fromisoformat(second["due"]) - clock["d"]).days
    assert gap2 > gap1
    assert database.get_conj_state("es", P, "recog")["reps"] == 2


def test_review_fail_lapses_and_due_tomorrow(tmp_db, clock):
    first = _rec(True)
    clock["d"] = date.fromisoformat(first["due"])
    r = _rec(False)
    row = database.get_conj_state("es", P, "recog")
    assert row["lapses"] == 1 and row["state"] == "learning"
    assert r["due"] == (clock["d"] + timedelta(days=1)).isoformat()


def test_same_day_ok_then_fail_recomputes_from_snapshot(tmp_db, clock):
    _rec(True)
    r = _rec(False)  # first rating of the day was ok -> corrected once
    row = database.get_conj_state("es", P, "recog")
    assert r["state"] == "learning" and row["day_failed"] == 1
    assert row["reps"] == 1 and row["lapses"] == 0  # as if the first rating were a fail
    assert r["due"] == (clock["d"] + timedelta(days=1)).isoformat()
    before = dict(row)
    _rec(False)  # second failure the same day: log only
    assert database.get_conj_state("es", P, "recog") == before
    assert database.conj_log_stats("es")[(P, "recog")]["total"] == 3


def test_same_day_ok_after_ok_and_fail_after_fail_only_log(tmp_db, clock):
    _rec(True)
    before = database.get_conj_state("es", P, "recog")
    _rec(True)
    assert database.get_conj_state("es", P, "recog") == before
    _rec(False, pattern="reg:ind_pret:ar")
    b2 = database.get_conj_state("es", "reg:ind_pret:ar", "recog")
    _rec(False, pattern="reg:ind_pret:ar")
    assert database.get_conj_state("es", "reg:ind_pret:ar", "recog") == b2


def test_same_day_fail_recompute_uses_pre_day_state(tmp_db, clock):
    first = _rec(True)
    clock["d"] = date.fromisoformat(first["due"])
    _rec(True)  # review ok today
    _rec(False)  # corrected: as if today's first rating were a fail
    row = database.get_conj_state("es", P, "recog")
    assert row["lapses"] == 1 and row["reps"] == 2 and row["state"] == "learning"


def test_undo_restores_and_deletes_log(tmp_db, clock):
    r1 = _rec(True)
    conj_patterns.undo_record(r1["undo"])
    assert database.get_conj_state("es", P, "recog") is None
    assert database.conj_log_stats("es") == {}

    r1 = _rec(True)
    clock["d"] = date.fromisoformat(r1["due"])
    saved = database.get_conj_state("es", P, "recog")
    r2 = _rec(False)
    conj_patterns.undo_record(r2["undo"])
    assert database.get_conj_state("es", P, "recog") == saved
    assert database.conj_log_stats("es")[(P, "recog")]["total"] == 1


def test_prod_and_recog_independent(tmp_db, clock):
    _rec(True, mode="recog")
    assert database.get_conj_state("es", P, "prod") is None
    _rec(False, mode="prod")
    assert database.get_conj_state("es", P, "recog")["state"] == "review"
    assert database.get_conj_state("es", P, "prod")["state"] == "learning"
    with pytest.raises(ValueError):
        _rec(True, mode="nope")


def test_conj_introduced_today(tmp_db, clock):
    _rec(True)
    _rec(True, pattern="reg:ind_pret:ar")
    _rec(True, mode="prod")
    today = clock["d"].isoformat()
    assert database.conj_introduced_today("es", "recog", today) == 2
    assert database.conj_introduced_today("es", "prod", today) == 1
    clock["d"] = date(2026, 3, 11)
    _rec(True)  # not new any more
    assert database.conj_introduced_today("es", "recog", "2026-03-11") == 0


def test_pattern_status(tmp_db, clock):
    r = _rec(True)
    _rec(False, mode="prod")
    states = database.get_conj_states("es")
    stats = database.conj_log_stats("es")
    st = conj_patterns.pattern_status("es", P, "recog", states, stats, "2026-03-10")
    assert st["state"] == "review" and st["due"] == r["due"] and st["due_in_days"] == 3
    assert st["interval_days"] == 3 and st["reps"] == 1 and st["total"] == 1 and st["correct"] == 1
    prod = conj_patterns.pattern_status("es", P, "prod", states, stats, "2026-03-10")
    assert prod["state"] == "learning" and prod["lapses"] == 0 and prod["correct"] == 0
    new = conj_patterns.pattern_status("es", "reg:ind_fut:all", "recog", states, stats, "2026-03-10")
    assert new["state"] == "new" and new["due"] is None and new["due_in_days"] is None


# --- slot priority -------------------------------------------------------------

TODAY = "2026-03-10"
LONG_AGO = "2026-01-01"
LATER = "2026-04-01"


def _state(pattern, mode, due, state="review"):
    return {"lang": "es", "pattern": pattern, "mode": mode, "state": state,
            "due": due, "last_review": LONG_AGO, "reps": 1, "lapses": 0}


def _card(wid, category, word="hablar"):
    return {"word_id": wid, "word_zh": word, "category": category, "pinyin": "",
            "definition": "", "pos": ""}


@pytest.fixture
def real_conj(tmp_db):
    if not _REAL_LEXICON.exists():
        pytest.skip("bundled es lexicon missing")
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


def _assign(cards, states, budget, seed=1):
    return verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(seed),
                                   conj_states=states, new_budget=budget, today=TODAY)


def test_due_pattern_wins_over_rotation(real_conj):
    states = {("reg:ind_pret:ar", "recog"): _state("reg:ind_pret:ar", "recog", LONG_AGO)}
    out = _assign([_card(1, "reading")], states, {"recog": 0, "prod": 0})
    assert out[1]["slot"] == "ind_pret:3s"  # most frequent person
    assert out[1]["pattern"] == "reg:ind_pret:ar"


def test_due_prod_pattern_does_not_affect_reading_card(real_conj):
    states = {
        ("reg:ind_pret:ar", "prod"): _state("reg:ind_pret:ar", "prod", LONG_AGO),
        ("reg:ind_impf:ar", "recog"): _state("reg:ind_impf:ar", "recog", LATER),  # not due
    }
    budget = {"recog": 0, "prod": 0}
    reading = _assign([_card(1, "reading")], states, budget)
    assert reading[1]["slot"].startswith("ind_impf:")  # rotation limited to introduced
    creating = _assign([_card(1, "creating")], states, budget)
    assert creating[1]["slot"] == "ind_pret:3s"


def test_each_due_pattern_used_once_per_call(real_conj):
    states = {("reg:ind_pret:ar", "recog"): _state("reg:ind_pret:ar", "recog", LONG_AGO),
              ("reg:ind_impf:ar", "recog"): _state("reg:ind_impf:ar", "recog", LONG_AGO)}
    out = _assign([_card(1, "reading"), _card(2, "reading")], states, {"recog": 0, "prod": 0})
    assert {out[1]["slot"].split(":")[0], out[2]["slot"].split(":")[0]} == {"ind_pret", "ind_impf"}


def test_most_overdue_wins(real_conj):
    states = {("reg:ind_pret:ar", "recog"): _state("reg:ind_pret:ar", "recog", "2026-03-01"),
              ("reg:ind_impf:ar", "recog"): _state("reg:ind_impf:ar", "recog", "2026-02-01")}
    out = _assign([_card(1, "reading")], states, {"recog": 0, "prod": 0})
    assert out[1]["pattern"] == "reg:ind_impf:ar"


def test_new_budget_introduces_highest_weight_pattern(real_conj):
    out = _assign([_card(1, "reading")], {}, {"recog": 1, "prod": 0})
    assert out[1]["slot"] == "ind_pres:3s"
    assert out[1]["pattern"] == "reg:ind_pres:ar"


def test_budget_zero_restricts_to_introduced_patterns(real_conj):
    states = {("reg:ind_fut:all", "recog"): _state("reg:ind_fut:all", "recog", LATER)}
    for seed in range(5):
        out = _assign([_card(1, "reading")], states, {"recog": 0, "prod": 0}, seed=seed)
        assert out[1]["slot"].startswith("ind_fut:")


def test_nothing_introduced_and_no_budget_is_plain_rotation(real_conj):
    out = _assign([_card(1, "reading")], {}, {"recog": 0, "prod": 0})
    assert out[1]["slot"]  # still gets a slot


def test_disabled_is_identical_to_old_behaviour(real_conj):
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    states = {("reg:ind_pret:ar", "recog"): _state("reg:ind_pret:ar", "recog", LONG_AGO)}
    cards = [_card(i, "reading") for i in range(1, 6)]
    with_states = _assign(cards, states, {"recog": 3, "prod": 3}, seed=7)
    old = verb_slots.assign_slots(cards, "es", usage={}, rng=random.Random(7))
    assert with_states == old
    assert all("pattern" not in v for v in with_states.values())


def test_non_spanish_unchanged(real_conj):
    assert verb_slots.assign_slots([_card(1, "reading")], "fr", usage={},
                                   conj_states={}, new_budget=1) == {}


# --- hydration + review API ----------------------------------------------------

@pytest.fixture
def client(tmp_db):
    import main
    return TestClient(main.app)


def _setup_sentence(category="reading"):
    deck_id = database.get_or_create_deck("ConjDeck")
    word_id = database.insert_word({"word_zh": "hablar", "lang": "es", "pos": "v"})
    card_id = database.insert_card(word_id, category, deck_id)
    story_id = database.create_story("2026-03-10", category, deck_id, [
        {"position": 1, "sentence_zh": "Yo hablo mucho.", "word_ids": [word_id],
         "verb_slot": "ind_pres:1s", "grammar_hint": "Presente · 1. Sg."}], lang="es")
    sentence = database.get_story_sentences(story_id)[0]
    return card_id, sentence


def test_sentence_hydration(small_lex, client):
    _, sentence = _setup_sentence()
    assert sentence["conj_pattern"] == "reg:ind_pres:ar"
    assert sentence["conj_pattern_label"] == "Presente · -ar (regelmäßig)"
    assert sentence["conj_rating"] is True
    single = database.get_story_sentence(sentence["id"])
    assert single["conj_pattern"] == "reg:ind_pres:ar" and single["lang"] == "es"
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    assert database.get_story_sentence(sentence["id"])["conj_rating"] is False


def test_review_with_conj_ok_logs_and_undo_removes(small_lex, client, clock):
    card_id, sentence = _setup_sentence("reading")
    r = client.post("/api/review", params={"card_id": card_id, "rating": 3, "conj_ok": 1,
                                           "sentence_id": sentence["id"]})
    assert r.status_code == 200, r.text
    conj = r.json()["conj"]
    assert conj["pattern"] == "reg:ind_pres:ar" and conj["mode"] == "recog" and conj["ok"] is True
    assert "undo" not in conj
    assert database.conj_log_stats("es")[("reg:ind_pres:ar", "recog")] == {"total": 1, "correct": 1}
    assert database.get_conj_state("es", "reg:ind_pres:ar", "recog")["state"] == "review"

    u = client.post("/api/review/undo")
    assert u.status_code == 200, u.text
    assert database.conj_log_stats("es") == {}
    assert database.get_conj_state("es", "reg:ind_pres:ar", "recog") is None


def test_review_creating_card_rates_prod_mode(small_lex, client, clock):
    card_id, sentence = _setup_sentence("creating")
    r = client.post("/api/review", params={"card_id": card_id, "rating": 3, "conj_ok": 0,
                                           "sentence_id": sentence["id"]})
    assert r.json()["conj"]["mode"] == "prod" and r.json()["conj"]["ok"] is False
    assert database.get_conj_state("es", "reg:ind_pres:ar", "recog") is None
    assert database.get_conj_state("es", "reg:ind_pres:ar", "prod")["state"] == "learning"
    client.post("/api/review/undo")


def test_review_without_conj_params_or_when_disabled(small_lex, client, clock):
    card_id, sentence = _setup_sentence("reading")
    r = client.post("/api/review", params={"card_id": card_id, "rating": 3})
    assert r.json()["conj"] is None
    client.post("/api/review/undo")
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    r = client.post("/api/review", params={"card_id": card_id, "rating": 3, "conj_ok": 1,
                                           "sentence_id": sentence["id"]})
    assert r.status_code == 200 and r.json()["conj"] is None
    assert database.conj_log_stats("es") == {}
    client.post("/api/review/undo")


def test_review_conj_failure_never_fails_vocab_rating(small_lex, client, clock, monkeypatch):
    card_id, sentence = _setup_sentence("reading")

    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(conj_patterns, "record", boom)
    r = client.post("/api/review", params={"card_id": card_id, "rating": 3, "conj_ok": 1,
                                           "sentence_id": sentence["id"]})
    assert r.status_code == 200
    assert r.json()["conj"] is None and "db gone" in r.json()["conj_error"]
    client.post("/api/review/undo")


# --- settings / patterns endpoints ---------------------------------------------

def test_conj_settings_endpoints(client):
    assert client.get("/api/es/conj-settings").json() == {
        "enabled": True, "new_per_day": 3, "desired_retention": 0.9,
        "pull_per_day": 10, "irregular_creating_unburied": True}
    r = client.put("/api/es/conj-settings", json={"new_per_day": 7})
    assert r.status_code == 200 and r.json()["new_per_day"] == 7
    assert r.json()["enabled"] is True
    r = client.put("/api/es/conj-settings", json={"enabled": False, "desired_retention": 0.85})
    assert r.json() == {"enabled": False, "new_per_day": 7, "desired_retention": 0.85,
                        "pull_per_day": 10, "irregular_creating_unburied": True}
    for bad in ({"new_per_day": 99}, {"desired_retention": 0.5}, {"enabled": "yes"},
                {"new_per_day": True}):
        assert client.put("/api/es/conj-settings", json=bad).status_code == 400
    assert client.get("/api/es/conj-settings").json()["new_per_day"] == 7


def test_conj_patterns_endpoint_shape(client, clock):
    conj_patterns.record("es", "reg:ind_pres:ar", "recog", True)
    conj_patterns.record("es", "verb:oler:ind_pres", "prod", False)  # not in the catalog
    body = client.get("/api/es/conj-patterns").json()
    assert body["today"] == "2026-03-10"
    by_key = {p["key"]: p for p in body["patterns"]}
    assert len(by_key) == len(body["patterns"])
    reg = by_key["reg:ind_pres:ar"]
    assert {"key", "label", "kind", "tense", "recog", "prod"} <= set(reg)
    assert reg["recog"]["state"] == "review" and reg["recog"]["due_in_days"] == 3
    assert reg["recog"]["total"] == 1 and reg["prod"]["state"] == "new"
    assert set(reg["recog"]) == {"state", "due", "due_in_days", "interval_days", "reps",
                                 "lapses", "total", "correct"}
    odd = by_key["verb:oler:ind_pres"]
    assert odd["prod"]["state"] == "learning" and odd["label"].endswith("(Sonderform)")


def test_word_detail_has_patterns(small_lex, client, clock):
    from routes import browse
    conj_patterns.record("es", "reg:ind_pres:ar", "recog", True)
    word = {"word_zh": "hablar", "lang": "es", "conjugations": [
        {"tense": "Presente", "person": "yo", "form": "hablo"},
        {"tense": "Presente", "person": "él/ella/usted", "form": "habla"},
        {"tense": "Participio", "person": "", "form": "hablado"},
    ]}
    browse._add_conjugation_patterns(word)
    first = word["conjugations"][0]["pattern"]
    assert first["key"] == "reg:ind_pres:ar" and first["kind"] == "regular"
    assert first["recog"]["state"] == "review" and first["prod"]["state"] == "new"
    assert "pattern" in word["conjugations"][1]
    assert "pattern" not in word["conjugations"][2]
    assert "practice_count" not in first and "conjugation_stats" not in word


def test_word_detail_noun_and_chinese_have_no_patterns(small_lex, client):
    noun = database.insert_word({"word_zh": "casa", "lang": "es"})
    zh = database.insert_word({"word_zh": "你好", "lang": "zh"})
    for wid in (noun, zh):
        body = client.get(f"/api/word/{wid}").json()
        assert "conjugation_stats" not in body
        assert not any("pattern" in c for c in body.get("conjugations") or [])

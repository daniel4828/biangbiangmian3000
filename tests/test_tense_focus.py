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


@pytest.fixture(autouse=True)
def even_persons(request, monkeypatch):
    # Die meisten Tests prüfen die gleichmäßige Rotation; die gewichtete
    # (#1477) hat eigene Tests mit dem Marker "weighted_persons".
    if "weighted_persons" not in request.keywords:
        monkeypatch.setattr(verb_slots, "focus_person_weights", lambda lang: None)


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


def test_assign_slots_focus_pointer_continues_from_history(real_lex, monkeypatch):
    # Kleine Geschichten (ein Verb) dürfen nicht immer bei yo anfangen.
    hist = {"ind_pres:1s": (3, "2026-11-05"), "ind_pres:2s": (2, "2026-11-05"),
            "ind_pres:3s": (2, "2026-11-04")}
    monkeypatch.setattr(verb_slots.database, "verb_slot_usage", lambda lang: hist)
    out = verb_slots.assign_slots([_card(1, "hablar"), _card(2, "comer")], "es", usage={})
    assert out[1]["slot"] == "ind_pres:1p" and out[2]["slot"] == "ind_pres:2p"


def test_assign_slots_focus_follows_the_day_tense(real_lex):
    tense_focus.step(1)
    out = verb_slots.assign_slots([_card(1, "hablar")], "es", usage={})
    assert out[1]["slot"] == "ind_pret:1s" and out[1]["form"] == "hablé"


def test_assign_slots_focus_creating_regular_moves_one_person(real_lex):
    cards = [_card(1, "hablar", "creating"), _card(2, "comer", "creating")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[1]["slot"] == "ind_pres:1s"
    assert out[2]["slot"] == "ind_pres:2s"
    assert out[1]["pattern"] == "reg:ind_pres:ar"


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

def test_plan_followups_focus_regular_verb_gets_none(real_lex):
    entries = [(_card(1, "hablar", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert out == []


def test_plan_followups_form_card_regular_tense_gets_none(real_lex):
    word = verb_slots.form_card_word("tener", "ind_impf")
    entries = [(_card(1, word, "creating"), "ind_impf:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert out == []


def test_plan_followups_focus_skips_non_creating_and_no_main_slot(real_lex):
    entries = [(_card(1, "hablar", "reading"), "ind_pres:1s"),
               (_card(2, "comer", "creating"), None)]
    assert conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(),
                                        conj_states={}) == []


# --- person types by irregularity (#1440) ------------------------------------------

def test_focus_persons_by_irregularity_type(real_lex):
    # #1470: stem change only (regular endings) -> one group with all persons
    assert conj_patterns.focus_persons("pensar", "ind_pres") == ["1s"]
    assert conj_patterns.focus_persons("salir", "ind_pres") == ["1s"]
    assert conj_patterns.focus_persons("tener", "ind_pres") == ["1s"]
    assert conj_patterns.focus_persons("estar", "ind_pres") == list(verb_slots.PERSONS)
    assert conj_patterns.focus_persons("ser", "ind_pres") == list(verb_slots.PERSONS)
    assert conj_patterns.focus_persons("hablar", "ind_pres") == []


def test_assign_slots_creating_irregular_keeps_pointer(real_lex):
    cards = [_card(1, "salir", "creating"), _card(2, "pensar", "creating"),
             _card(3, "hablar", "creating")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[1]["slot"] == "ind_pres:1s" and out[1]["form"] == "salgo"
    # pensar (nur Stammwechsel, eine Gruppe) weicht der von salir belegten Person aus
    assert out[2]["slot"] == "ind_pres:2s"
    assert out[3]["slot"] == "ind_pres:3s"  # weicht den beiden belegten Personen aus


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


def test_plan_followups_pensar_stem_change_only_gets_none(real_lex):
    # #1470: pensar ind_pres is a pure stem change -> no Folgesätze
    entries = [(_card(1, "pensar", "creating"), "ind_pres:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert out == []


def test_plan_followups_cap_is_spread_round_robin(real_lex):
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "2")
    entries = [(_card(1, "ser", "creating"), "ind_pres:1s"),
               (_card(2, "estar", "creating"), "ind_pres:1s")]  # estar: irregular endings (#1470)
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert sorted(f["word_id"] for f in out) == [1, 2]


# --- groups + rotation by history (#1440) -------------------------------------------

def _add_verb(word, lang="es"):
    return database.insert_word({"word_zh": word, "lang": lang, "note_type": "vocabulary"})


def _story(deck_id, sentences):
    return database.create_story(DAY0.isoformat(), "creating", deck_id, sentences, lang="es")


def test_focus_groups_contrast_and_exceptions(real_lex):
    fg = conj_patterns.focus_groups
    # y_3rd: only forms with a new y are the exception, accent-only forms join the contrast group
    assert fg("creer", "ind_pret") == [["3s", "3p"], ["1s", "2s", "1p", "2p"]]
    # #1470: stem change only (sintió = sint + io) -> one group
    assert fg("sentir", "ind_pret") == [list(verb_slots.PERSONS)]
    assert len(fg("creer", "subj_impf")) == 1  # every person has the y
    # #1470: spelling changes are stem + regular ending (llegu+é) -> one group
    assert fg("llegar", "ind_pret") == [list(verb_slots.PERSONS)]
    assert fg("buscar", "ind_pret") == [list(verb_slots.PERSONS)]
    # one pattern for all persons
    assert fg("tener", "ind_pret") == [list(verb_slots.PERSONS)]
    # single-verb patterns: every person on its own
    assert fg("ser", "ind_pres") == [[p] for p in verb_slots.PERSONS]
    assert fg("hablar", "ind_pres") == []


def test_focus_groups_perf_special_verbs_use_participle_group(real_lex):
    # #1468: ver (visto) must not become six one-person groups in Perfecto
    fg = conj_patterns.focus_groups
    assert fg("ver", "perf") == [["1s", "2s", "3s", "1p", "2p", "3p"]]
    assert fg("ver", "perf") == fg("hacer", "perf")


def test_pick_person_order():
    pp = conj_patterns.pick_person
    group = ["1s", "2s", "3s"]
    assert pp(group, "ind_pret", None) == "1s"
    assert pp(group, "ind_pret", {}) == "1s"
    assert pp(group, "ind_pret", {"ind_pret:1s": (1, "2026-01-02")}) == "2s"
    # equal counts: the oldest last use wins, never used counts as oldest
    usage = {"ind_pret:1s": (1, "2026-01-03"), "ind_pret:2s": (1, "2026-01-01"),
             "ind_pret:3s": (1, "2026-01-02")}
    assert pp(group, "ind_pret", usage) == "2s"
    usage["ind_pret:2s"] = (2, "2026-01-01")
    assert pp(group, "ind_pret", usage) == "3s"
    # other tenses' usage is ignored
    assert pp(group, "ind_pres", usage) == "1s"


def test_focus_persons_uses_history(real_lex):
    assert conj_patterns.focus_persons("creer", "ind_pret") == ["3s", "1s"]
    usage = {"ind_pret:3s": (1, "2026-01-01"), "ind_pret:1s": (1, "2026-01-01")}
    assert conj_patterns.focus_persons("creer", "ind_pret", usage=usage) == ["3p", "2s"]


def test_verb_slot_usage_by_word_counts_main_and_followups(real_lex):
    deck_id = database.get_or_create_deck("T")
    w1, w2 = _add_verb("tener"), _add_verb("hablar")
    _story(deck_id, [
        {"position": 1, "sentence_zh": "Tuve.", "word_ids": [w1], "verb_slot": "ind_pret:1s"},
        {"position": 2, "sentence_zh": "Tuviste.", "word_ids": [],
         "followup_word_id": w1, "verb_slot": "ind_pret:2s"},
        {"position": 3, "sentence_zh": "Tuve otra vez.", "word_ids": [],
         "followup_word_id": w1, "verb_slot": "ind_pret:1s"},
        {"position": 4, "sentence_zh": "Sin slot.", "word_ids": [w1]},
        {"position": 5, "sentence_zh": "Habló.", "word_ids": [w2], "verb_slot": "ind_pret:3s"},
    ])
    assert database.verb_slot_usage_by_word([]) == {}
    out = database.verb_slot_usage_by_word([w1, w2, 99999])
    assert set(out) == {w1, w2}
    assert out[w1]["ind_pret:1s"][0] == 2 and out[w1]["ind_pret:2s"][0] == 1
    assert out[w1]["ind_pret:1s"][1]
    assert set(out[w2]) == {"ind_pret:3s"}


def test_assign_slots_tener_indefinido_rotates_with_history(real_lex):
    tense_focus.step(1)  # Indefinido
    deck_id = database.get_or_create_deck("T")
    wid = _add_verb("tener")
    cards = [_card(wid, "tener", "creating")]
    assert verb_slots.assign_slots(cards, "es", usage={})[wid]["slot"] == "ind_pret:1s"
    _story(deck_id, [{"position": 1, "sentence_zh": "Tuve.", "word_ids": [wid],
                      "verb_slot": "ind_pret:1s"}])
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[wid]["slot"] == "ind_pret:2s" and out[wid]["form"] == "tuviste"


def test_assign_slots_form_card_regular_tense_rotates_without_pointer(real_lex):
    tense_focus.step(1)
    deck_id = database.get_or_create_deck("T")
    word = verb_slots.form_card_word("tener", "ind_impf")
    wid = _add_verb(word)
    _story(deck_id, [{"position": 1, "sentence_zh": "Tenía.", "word_ids": [wid],
                      "verb_slot": "ind_impf:1s"}])
    cards = [_card(wid, word, "creating"), _card(2, "hablar", "reading")]
    out = verb_slots.assign_slots(cards, "es", usage={})
    assert out[wid]["slot"] == "ind_impf:2s"
    assert out[2]["slot"] == "ind_pret:1s"  # pointer did not move


def test_form_cards_with_same_history_spread_over_persons(real_lex):
    # Formkarten, die am selben Tag eingeführt wurden, haben dieselbe Historie.
    # Früher bekamen sie deshalb alle dieselbe Person (an einem Tag alle tú).
    tense_focus.step(1)
    deck_id = database.get_or_create_deck("T")
    verbs = ["venir", "tener", "poner"]
    wids = [_add_verb(verb_slots.form_card_word(v, "ind_impf")) for v in verbs]
    _story(deck_id, [{"position": i, "sentence_zh": "x.", "word_ids": [w],
                      "verb_slot": "ind_impf:1s"} for i, w in enumerate(wids, 1)])
    cards = [_card(w, verb_slots.form_card_word(v, "ind_impf"), "creating")
             for w, v in zip(wids, verbs)]
    out = verb_slots.assign_slots(cards, "es", usage={})
    slots = [out[w]["slot"] for w in wids]
    assert slots == ["ind_impf:2s", "ind_impf:3s", "ind_impf:1p"]


def test_pick_person_local_spreads_ties():
    pp = conj_patterns.pick_person
    group = ["1s", "2s", "3s"]
    assert pp(group, "ind_pret", None, {"ind_pret:1s": 1}) == "2s"
    # history still comes first
    assert pp(group, "ind_pret", {"ind_pret:2s": (1, "x"), "ind_pret:3s": (1, "x")},
              {"ind_pret:1s": 3}) == "1s"


def test_each_card_rotates_on_its_own_history(real_lex):
    # #1475: jede Karte rotiert für sich, auch Lese-/Hörkarten und normale Verben.
    deck_id = database.get_or_create_deck("T")
    w1, w2 = _add_verb("hablar"), _add_verb("comer")
    _story(deck_id, [
        {"position": 1, "sentence_zh": "Hablo.", "word_ids": [w1], "verb_slot": "ind_pres:1s"},
        {"position": 2, "sentence_zh": "Hablas.", "word_ids": [w1], "verb_slot": "ind_pres:2s"},
        {"position": 3, "sentence_zh": "Como.", "word_ids": [w2], "verb_slot": "ind_pres:1s"},
    ])
    out = verb_slots.assign_slots([_card(w1, "hablar"), _card(w2, "comer")], "es", usage={})
    assert out[w1]["slot"] == "ind_pres:3s"
    assert out[w2]["slot"] == "ind_pres:1p"  # 2s global schon geübt, 3s hier vergeben


def test_plan_followups_sentir_ind_pret_stem_only_gets_none(real_lex):
    # #1470: sentir Indefinido is stem change only -> no Folgesätze
    tense_focus.step(1)
    wid = _add_verb("sentir")
    entries = [(_card(wid, "sentir", "creating"), "ind_pret:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert out == []


def test_plan_followups_llegar_spelling_change_gets_none(real_lex):
    # #1470: llegué = llegu + regular ending -> no Folgesätze
    tense_focus.step(1)
    wid = _add_verb("llegar")
    entries = [(_card(wid, "llegar", "creating"), "ind_pret:1s")]
    out = conj_patterns.plan_followups(entries, "es", today=DAY0.isoformat(), conj_states={})
    assert out == []


ALL_PERSONS = [["1s", "2s", "3s", "1p", "2p", "3p"]]


@pytest.mark.parametrize("verb,tense", [
    ("haber", "ind_fut"),
    ("ser", "subj_pres"), ("estar", "subj_pres"), ("haber", "subj_pres"),
    ("ir", "subj_pres"), ("ver", "subj_pres"), ("saber", "subj_pres"),
    ("dar", "subj_pres"), ("sentir", "subj_pres"), ("poder", "subj_pres"),
    ("querer", "subj_pres"), ("pensar", "subj_pres"), ("encontrar", "subj_pres"),
    ("volver", "subj_pres"),
    ("ser", "subj_impf"), ("ir", "subj_impf"), ("estar", "subj_impf"),
    ("haber", "subj_impf"), ("dar", "subj_impf"),
])
def test_focus_groups_stem_only_is_one_group(real_lex, verb, tense):
    out = conj_patterns.focus_groups(verb, tense)
    assert len(out) == 1
    assert out == ALL_PERSONS


@pytest.mark.parametrize("verb,tense", [
    ("ser", "ind_pres"), ("ir", "ind_pres"), ("estar", "ind_pres"),
    ("dar", "ind_pres"), ("haber", "ind_pres"),
    ("ser", "ind_pret"), ("ir", "ind_pret"),
])
def test_focus_groups_irregular_endings_stay_multi_group(real_lex, verb, tense):
    assert len(conj_patterns.focus_groups(verb, tense)) > 1


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


# --- Personen nach Häufigkeit gewichten (#1477) ---------------------------------

@pytest.mark.weighted_persons
def test_weighted_default_on_and_switchable(real_lex):
    assert tense_focus.settings()["weighted_persons"] is True
    w = verb_slots.focus_person_weights("es")
    assert w["3s"] > w["1s"] > w["2s"] > w["2p"]
    tense_focus.save(tense_focus.validate({"weighted_persons": False}))
    assert verb_slots.focus_person_weights("es") is None
    with pytest.raises(ValueError):
        tense_focus.validate({"weighted_persons": "ja"})


@pytest.mark.weighted_persons
def test_weighted_story_follows_real_frequency(real_lex):
    verbs = ["hablar", "comer", "vivir", "trabajar", "estudiar", "caminar",
             "cantar", "bailar", "mirar", "llamar", "tomar", "llevar"]
    cards = [_card(i, w) for i, w in enumerate(verbs, 1)]
    out = verb_slots.assign_slots(cards, "es", usage={})
    persons = [out[c["word_id"]]["slot"].split(":")[1] for c in cards]
    assert persons[0] == "3s"                     # häufigste Person zuerst
    assert persons.count("3s") > persons.count("1s") > persons.count("1p")
    assert "2p" not in persons                    # vosotros ist sehr selten
    assert len(set(persons)) >= 4                 # trotzdem gemischt


@pytest.mark.weighted_persons
def test_weighted_card_rotates_on_its_own(real_lex):
    tense_focus.step(1)
    deck_id = database.get_or_create_deck("T")
    wid = _add_verb("hablar")
    _story(deck_id, [{"position": 1, "sentence_zh": "Habló.", "word_ids": [wid],
                      "verb_slot": "ind_pret:3s"}])
    out = verb_slots.assign_slots([_card(wid, "hablar")], "es", usage={})
    assert out[wid]["slot"] == "ind_pret:3s"      # einmal geübt: 2/47 < 1/22.5
    _story(deck_id, [{"position": 1, "sentence_zh": "Habló otra vez.", "word_ids": [wid],
                      "verb_slot": "ind_pret:3s"}])
    out = verb_slots.assign_slots([_card(wid, "hablar")], "es", usage={})
    assert out[wid]["slot"] == "ind_pret:1s"      # zweimal: 3/47 > 1/22.5


def test_pick_person_weighted():
    pp = conj_patterns.pick_person
    w = {"1s": 22.5, "2s": 12, "3s": 47, "1p": 7, "2p": 0.4, "3p": 11}
    assert pp(["1s", "2s", "3s"], "ind_pres", None, None, w) == "3s"
    assert pp(["1s", "2s"], "ind_pres", None, None, w) == "1s"                    # 1/22.5 < 1/12
    assert pp(["1s", "2s"], "ind_pres", {"ind_pres:1s": (1, "x")}, None, w) == "2s"  # 2/22.5 > 1/12
    assert pp(["1s", "2s"], "ind_pres", None, {"ind_pres:1s": 1}, w) == "2s"        # local zählt mit
    # Gewicht 0 = nie, außer die ganze Gruppe ist 0
    assert pp(["2p", "1p"], "ind_pres", None, None, {"2p": 0, "1p": 0.1}) == "1p"
    assert pp(["2p"], "ind_pres", None, None, {"2p": 0}) == "2p"

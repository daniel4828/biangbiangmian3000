"""Folgesätze: extra conjugation sentences after a creating card (#1401)."""
import random
from datetime import date

import pytest
from fastapi.testclient import TestClient

import ai
import conj_patterns
import database
import lexicon
import verb_slots
from routes import story as story_routes

from tests.test_conj_patterns import SMALL_LEXICON

_REAL_LEXICON = lexicon._RESOURCE_DIR / "es-lexicon.json.gz"
TODAY = "2026-03-10"
LONG_AGO = "2026-01-01"
LATER = "2026-04-01"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def clock(monkeypatch):
    state = {"d": date(2026, 3, 10)}
    monkeypatch.setattr(database, "anki_today", lambda: state["d"])
    return state


@pytest.fixture
def real_lex(tmp_db, clock):
    if not _REAL_LEXICON.exists():
        pytest.skip("bundled es lexicon missing")
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


@pytest.fixture
def small_lex(tmp_db, clock):
    lexicon.inject("es", SMALL_LEXICON)
    yield
    lexicon.clear_cache()


@pytest.fixture
def client(tmp_db):
    import main
    return TestClient(main.app)


def _card(wid, category, word):
    return {"word_id": wid, "word_zh": word, "category": category, "pinyin": "",
            "definition": "", "pos": ""}


def _state(pattern, mode, due, state="review"):
    return {"lang": "es", "pattern": pattern, "mode": mode, "state": state,
            "due": due, "last_review": LONG_AGO, "reps": 1, "lapses": 0}


def _plan(entries, states=None, seed=1):
    return conj_patterns.plan_followups(
        entries, "es", today=TODAY, conj_states=states if states is not None else {},
        rng=random.Random(seed))


# --- plan_followups --------------------------------------------------------------

def test_per_verb_limit(real_lex):
    entries = [(_card(1, "creating", "hablar"), "ind_pres:3s")]
    out = _plan(entries)  # default prod_per_verb = 3 -> at most 2 follow-ups
    assert [f["pattern"] for f in out] == ["reg:ind_pret:ar", "reg:ind_impf:ar"]
    first = out[0]
    assert first["word_id"] == 1 and first["lemma"] == "hablar"
    assert first["slot"] == "ind_pret:3s" and first["form"] == "habló"
    assert first["label"] == "Indefinido · 3. Sg."
    assert first["pattern_label"] == conj_patterns.label_for_key("reg:ind_pret:ar")

    database.set_app_setting(conj_patterns.KEY_PROD_PER_VERB, "2")
    assert len(_plan(entries)) == 1
    database.set_app_setting(conj_patterns.KEY_PROD_PER_VERB, "1")
    assert _plan(entries) == []
    database.set_app_setting(conj_patterns.KEY_PROD_PER_VERB, "5")
    assert len(_plan(entries)) == 4  # 5 per verb = main sentence + 4 follow-ups


def test_due_before_new(real_lex):
    database.set_app_setting(conj_patterns.KEY_PROD_PER_VERB, "2")
    states = {("reg:perf:ar", "prod"): _state("reg:perf:ar", "prod", LONG_AGO)}
    out = _plan([(_card(1, "creating", "hablar"), "ind_pres:3s")], states)
    assert [f["pattern"] for f in out] == ["reg:perf:ar"]  # the low-weight due one wins


def test_oldest_due_first_and_not_due_excluded(real_lex):
    states = {
        ("reg:ind_pret:ar", "prod"): _state("reg:ind_pret:ar", "prod", "2026-03-01"),
        ("reg:ind_impf:ar", "prod"): _state("reg:ind_impf:ar", "prod", "2026-02-01"),
        ("reg:perf:ar", "prod"): _state("reg:perf:ar", "prod", LATER),  # introduced, not due
    }
    out = _plan([(_card(1, "creating", "hablar"), "ind_pres:3s")], states)
    assert [f["pattern"] for f in out] == ["reg:ind_impf:ar", "reg:ind_pret:ar"]
    # recognition state of a pattern says nothing about production
    only_recog = {("reg:perf:ar", "recog"): _state("reg:perf:ar", "recog", LONG_AGO)}
    out = _plan([(_card(1, "creating", "hablar"), "ind_pres:3s")], only_recog)
    assert [f["pattern"] for f in out] == ["reg:ind_pret:ar", "reg:ind_impf:ar"]


def test_main_pattern_is_excluded(real_lex):
    states = {("reg:ind_pret:ar", "prod"): _state("reg:ind_pret:ar", "prod", LONG_AGO)}
    out = _plan([(_card(1, "creating", "hablar"), "ind_pret:3s")], states)
    assert "reg:ind_pret:ar" not in [f["pattern"] for f in out]
    assert len(out) == 2


def test_no_pattern_twice_in_one_story(real_lex):
    entries = [(_card(1, "creating", "hablar"), None),
               (_card(2, "creating", "hablar"), None)]
    out = _plan(entries)
    patterns = [f["pattern"] for f in out]
    assert len(patterns) == len(set(patterns)) == 4
    assert {f["word_id"] for f in out} == {1, 2}


def test_pattern_of_another_cards_main_sentence_is_excluded(real_lex):
    entries = [(_card(1, "creating", "hablar"), None),
               (_card(2, "reading", "hablar"), "ind_pret:3s")]  # reading card drills pret
    out = _plan(entries)
    assert "reg:ind_pret:ar" not in [f["pattern"] for f in out]
    assert {f["word_id"] for f in out} == {1}


def test_global_daily_cap(real_lex, clock):
    entries = [(_card(1, "creating", "hablar"), None), (_card(2, "creating", "hablar"), None)]
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "1")
    assert len(_plan(entries)) == 1
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "3")
    assert len(_plan(entries)) == 3
    # follow-ups already written into today's stories count against the cap
    deck_id = database.get_or_create_deck("CapDeck")
    database.create_story(TODAY, "creating", deck_id, [
        {"position": 0, "sentence_zh": "Ayer hablé.", "word_ids": [],
         "followup_word_id": 1, "verb_slot": "ind_pret:1s"},
        {"position": 1, "sentence_zh": "Ayer hablaste.", "word_ids": [],
         "followup_word_id": 1, "verb_slot": "ind_pret:2s"}], lang="es")
    assert database.count_followups_for_date(TODAY) == 2
    assert len(_plan(entries)) == 1
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "2")
    assert _plan(entries) == []
    # another day starts with a fresh cap
    other = conj_patterns.plan_followups(entries, "es", today="2026-03-11",
                                         conj_states={}, rng=random.Random(1))
    assert len(other) == 2


def test_off_switches(real_lex):
    entries = [(_card(1, "creating", "hablar"), None)]
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "0")
    assert _plan(entries) == []
    database.set_app_setting(conj_patterns.KEY_FOLLOWUPS_PER_DAY, "30")
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    assert _plan(entries) == []


def test_only_creating_spanish_verb_cards(real_lex):
    assert _plan([(_card(1, "reading", "hablar"), "ind_pres:3s"),
                  (_card(2, "listening", "hablar"), "ind_pres:3s")]) == []
    assert _plan([(_card(3, "creating", "perro"), None)]) == []     # no verb
    assert _plan([(_card(4, "creating", "hablar"), None)], states=None) != []
    zh = _card(5, "creating", "说话")
    assert _plan([(zh, None)]) == []
    # multi-language story: only cards whose own language is Spanish
    es = {**_card(6, "creating", "hablar"), "word_lang": "es"}
    other = {**_card(7, "creating", "hablar"), "word_lang": "zh"}
    out = conj_patterns.plan_followups([(es, None), (other, None)], "multi", today=TODAY,
                                       conj_states={}, rng=random.Random(1))
    assert {f["word_id"] for f in out} == {6}


def test_irregular_verb(real_lex):
    out = _plan([(_card(1, "creating", "tener"), "ind_pres:1s")])
    assert len(out) == 2
    main = conj_patterns.pattern_for("tener", "ind_pres", "1s")["key"]
    assert main == "irr:ind_pres:yo_go"
    for f in out:
        tense, _, person = f["slot"].partition(":")
        assert f["pattern"] != main
        assert f["form"] == verb_slots.target_form("tener", tense, person, "es")
        assert conj_patterns.pattern_for("tener", tense, person)["key"] == f["pattern"]
    assert len({f["pattern"] for f in out}) == 2


def test_form_card_keeps_its_tense(real_lex):
    card = _card(1, "creating", "tener · Indefinido")
    out = _plan([(card, None)])
    assert len(out) == 1  # all six persons share one pattern (strong -u- preterite)
    assert out[0]["pattern"] == "irr:ind_pret:strong_u"
    assert out[0]["slot"] == "ind_pret:3s" and out[0]["form"] == "tuvo"
    assert out[0]["lemma"] == "tener"
    # ... and that one pattern is the main sentence's -> nothing left
    assert _plan([(card, "ind_pret:1s")]) == []


# --- ai.generate_conj_followups ---------------------------------------------------

ITEMS = [{"form": "tuve", "lemma": "tener", "label": "Indefinido · 1. Sg."},
         {"form": "hablé", "lemma": "hablar", "label": "Indefinido · 1. Sg."}]


def test_generate_drops_invalid_forms(monkeypatch):
    seen = {}

    def fake(model, messages, max_tokens, purpose, **kw):
        seen["purpose"] = purpose
        seen["prompt"] = messages[0]["content"]
        return ('```json\n[{"index": 1, "sentence_es": "Ayer tuve mucho trabajo.", '
                '"sentence_de": "Gestern hatte ich viel Arbeit."},'
                '{"index": 2, "sentence_es": "Ella habló conmigo.", "sentence_de": "x"}]\n```')
    monkeypatch.setattr(ai, "_call_api", fake)
    out = ai.generate_conj_followups(ITEMS)
    assert out == [{"index": 0, "sentence_es": "Ayer tuve mucho trabajo.",
                    "sentence_de": "Gestern hatte ich viel Arbeit."}]
    assert seen["purpose"] == "conj_followups"
    assert '"tuve"' in seen["prompt"] and '"hablé"' in seen["prompt"]


def test_generate_prompt_has_tense_cues(monkeypatch):
    """#1481: with slots the prompt asks for a tense-fixing context and German
    signal words (only for the tenses present); without slots it does not."""
    monkeypatch.setattr(ai, "_tense_cues_active", lambda: True)
    seen = []
    monkeypatch.setattr(ai, "_call_api", lambda m, msgs, *a, **k: seen.append(msgs[0]["content"]) or "[]")
    ai.generate_conj_followups([{**ITEMS[0], "slot": "ind_impf:1s"}])
    assert "— context: a past habit" in seen[0]
    assert "früher" in seen[0] and "gestern, letzte Woche" not in seen[0]
    ai.generate_conj_followups(ITEMS)  # no slots -> old prompt
    assert "context:" not in seen[1] and '"why"' not in seen[1]
    monkeypatch.setattr(ai, "_tense_cues_active", lambda: False)
    ai.generate_conj_followups([{**ITEMS[0], "slot": "ind_impf:1s"}])
    assert "context:" not in seen[2]


def test_generate_returns_tense_reason(monkeypatch):
    monkeypatch.setattr(ai, "_tense_cues_active", lambda: True)
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: (
        '[{"index": 1, "sentence_es": "Ayer tuve suerte.", "sentence_de": "Gestern hatte ich Glück.", '
        '"why": "ayer = Zeitpunkt"}]'))
    out = ai.generate_conj_followups([{**ITEMS[0], "slot": "ind_pret:1s"}])
    assert out[0]["tense_reason"] == "ayer = Zeitpunkt"
    monkeypatch.setattr(ai, "_tense_cues_active", lambda: False)
    out = ai.generate_conj_followups([{**ITEMS[0], "slot": "ind_pret:1s"}])
    assert "tense_reason" not in out[0]


def test_generate_form_must_be_a_whole_word(monkeypatch):
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: (
        '[{"index": 1, "sentence_es": "Ellos tuvieron suerte.", "sentence_de": "a"},'
        ' {"index": 1, "sentence_es": "Yo TUVE suerte.", "sentence_de": "b"},'
        ' {"index": 1, "sentence_es": "Yo tuve suerte otra vez.", "sentence_de": "c"},'
        ' {"index": 9, "sentence_es": "Yo tuve suerte.", "sentence_de": "d"},'
        ' {"index": 2, "sentence_es": "", "sentence_de": "e"}]'))
    out = ai.generate_conj_followups(ITEMS)
    # case-insensitive whole word; the first valid answer per index wins
    assert out == [{"index": 0, "sentence_es": "Yo TUVE suerte.", "sentence_de": "b"}]


def test_generate_failures_return_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(ai, "_call_api", boom)
    assert ai.generate_conj_followups(ITEMS) == []
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: "sorry, no JSON here")
    assert ai.generate_conj_followups(ITEMS) == []
    monkeypatch.setattr(ai, "_call_api", lambda *a, **k: '{"index": 1}')
    assert ai.generate_conj_followups(ITEMS) == []
    assert ai.generate_conj_followups([]) == []


# --- storage + reading back --------------------------------------------------------

def _story_with_followup(category="creating"):
    deck_id = database.get_or_create_deck("FuDeck")
    word_id = database.insert_word({"word_zh": "hablar", "lang": "es", "pos": "v"})
    card_id = database.insert_card(word_id, category, deck_id)
    story_id = database.create_story(TODAY, category, deck_id, [
        {"position": 0, "sentence_zh": "Yo hablo mucho.", "sentence_de": "Ich rede viel.",
         "word_ids": [word_id], "verb_slot": "ind_pres:1s",
         "grammar_hint": "Presente · 1. Sg."},
        {"position": 1, "sentence_zh": "Ayer hablé con ella.",
         "sentence_de": "Gestern sprach ich mit ihr.", "sentence_en": "", "word_ids": [],
         "followup_word_id": word_id, "verb_slot": "ind_pret:1s",
         "grammar_hint": "Indefinido · 1. Sg.", "lang": "es"}], lang="es")
    return deck_id, word_id, card_id, story_id


def test_followups_are_not_story_sentences(small_lex):
    deck_id, word_id, card_id, story_id = _story_with_followup()
    sentences = database.get_story_sentences(story_id)
    assert [s["sentence_zh"] for s in sentences] == ["Yo hablo mucho."]
    assert len(database.get_story_sentences(story_id, include_followups=True)) == 2
    # card -> sentence lookups never see the follow-up
    assert database.get_sentence_for_word(story_id, word_id)["sentence_zh"] == "Yo hablo mucho."
    assert database.get_latest_sentence_for_word(word_id)["sentence_zh"] == "Yo hablo mucho."

    followups = database.get_story_followups(story_id)
    assert len(followups) == 1
    f = followups[0]
    assert f["followup_word_id"] == word_id and f["word_ids"] == []
    assert f["sentence_zh"] == "Ayer hablé con ella." and f["sentence_de"].startswith("Gestern")
    assert f["form"] == "hablé" and f["lemma"] == "hablar"
    assert f["verb_slot"] == "ind_pret:1s" and f["grammar_hint"] == "Indefinido · 1. Sg."
    assert f["conj_pattern"] == "reg:ind_pret:ar" and f["conj_pattern_label"]
    assert database.count_followups_for_date(TODAY) == 1
    assert database.count_followups_for_date("2026-03-09") == 0


def test_story_endpoints_separate_followups(small_lex, client):
    _, _, _, story_id = _story_with_followup()
    body = client.get(f"/api/stories/{story_id}").json()
    assert len(body["sentences"]) == 1 and len(body["followups"]) == 1
    assert body["followups"][0]["form"] == "hablé"
    listed = [s for s in client.get("/api/stories").json()["stories"] if s["id"] == story_id]
    assert listed[0]["sentence_count"] == 1


def test_append_followups_into_story_sentences(small_lex, monkeypatch):
    monkeypatch.setattr(story_routes, "ai_disabled", lambda: False)
    calls = {}

    def fake(items, model=None):
        calls["items"] = items
        return [{"index": 0, "sentence_es": "Ayer habló mucho.", "sentence_de": "Gestern sprach er viel."}]
    monkeypatch.setattr(ai, "generate_conj_followups", fake)
    cards = [_card(1, "creating", "hablar")]
    sentences = [{"word_ids": [1], "sentence_zh": "Ella habla.", "verb_slot": "ind_pres:3s",
                  "grammar_hint": "Presente · 3. Sg."}]
    story_routes._append_followups(sentences, cards, lang="es", model="m", progress_key=None)
    assert len(sentences) == 2
    f = sentences[1]
    assert f["followup_word_id"] == 1 and f["word_ids"] == []
    assert f["sentence_zh"] == "Ayer habló mucho." and f["verb_slot"] == "ind_pret:3s"
    assert f["lang"] == "es" and f["grammar_hint"] == "Indefinido · 3. Sg."
    assert calls["items"][0]["form"] == "habló"


def test_append_followups_never_breaks_the_story(small_lex, monkeypatch):
    monkeypatch.setattr(story_routes, "ai_disabled", lambda: False)

    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(ai, "generate_conj_followups", boom)
    cards = [_card(1, "creating", "hablar")]
    sentences = [{"word_ids": [1], "sentence_zh": "Ella habla.", "verb_slot": "ind_pres:3s"}]
    story_routes._append_followups(sentences, cards, lang="es", model="m", progress_key=None)
    assert len(sentences) == 1
    # other languages / AI off: nothing happens, nothing is called
    monkeypatch.setattr(ai, "generate_conj_followups", lambda *a, **k: pytest.fail("called"))
    story_routes._append_followups(sentences, cards, lang="zh", model="m", progress_key=None)
    monkeypatch.setattr(story_routes, "ai_disabled", lambda: True)
    story_routes._append_followups(sentences, cards, lang="es", model="m", progress_key=None)
    assert len(sentences) == 1


# --- POST /api/conj/followup-rate ---------------------------------------------------

def test_followup_rate_writes_prod_log(small_lex, client):
    _, _, card_id, story_id = _story_with_followup()
    sentence_id = database.get_story_followups(story_id)[0]["id"]
    r = client.post("/api/conj/followup-rate",
                    json={"sentence_id": sentence_id, "ok": True, "card_id": card_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pattern"] == "reg:ind_pret:ar" and body["mode"] == "prod" and body["ok"] is True
    assert "undo" not in body
    assert database.conj_log_stats("es")[("reg:ind_pret:ar", "prod")] == {"total": 1, "correct": 1}
    assert database.get_conj_state("es", "reg:ind_pret:ar", "prod")["state"] == "review"
    assert database.get_conj_state("es", "reg:ind_pret:ar", "recog") is None


def test_followup_rate_failure_is_a_failed_rating(small_lex, client):
    _, _, _, story_id = _story_with_followup()
    sentence_id = database.get_story_followups(story_id)[0]["id"]
    r = client.post("/api/conj/followup-rate", json={"sentence_id": sentence_id, "ok": False})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert database.get_conj_state("es", "reg:ind_pret:ar", "prod")["state"] == "learning"


def test_followup_rate_rejects_normal_sentences_and_bad_input(small_lex, client):
    _, _, _, story_id = _story_with_followup()
    normal = database.get_story_sentences(story_id)[0]
    r = client.post("/api/conj/followup-rate", json={"sentence_id": normal["id"], "ok": True})
    assert r.status_code == 400
    assert client.post("/api/conj/followup-rate",
                       json={"sentence_id": 999999, "ok": True}).status_code == 404
    fid = database.get_story_followups(story_id)[0]["id"]
    assert client.post("/api/conj/followup-rate",
                       json={"sentence_id": fid, "ok": "yes"}).status_code == 400
    assert client.post("/api/conj/followup-rate", json={"ok": True}).status_code == 400
    assert client.post("/api/conj/followup-rate",
                       json={"sentence_id": fid, "ok": True, "card_id": "x"}).status_code == 400
    assert database.conj_log_stats("es") == {}
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    assert client.post("/api/conj/followup-rate",
                       json={"sentence_id": fid, "ok": True}).status_code == 400

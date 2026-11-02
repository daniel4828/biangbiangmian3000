"""Conjugation pull-forward and the irregular-verb bury rule (#1395)."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import conj_patterns
import database
import lexicon
from test_browse_lang import _entry

if not (lexicon._RESOURCE_DIR / "es-lexicon.json.gz").exists():
    pytest.skip("bundled es lexicon missing", allow_module_level=True)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "pull.db"))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


def _today():
    return database.anki_today()


def _iso(days):
    return (_today() + timedelta(days=days)).isoformat()


def _deck(category):
    parent = database.get_or_create_deck_path("Español::Practice", lang="es")
    return database.get_or_create_category_decks(parent, "Practice")[category]


def _card(word, category, days, state="review", due=None):
    """Spanish verb card with `days` offset from today (word entry is reused)."""
    conn = database.get_db()
    row = conn.execute("SELECT id FROM entries WHERE word_zh = ? AND lang = 'es'", (word,)).fetchone()
    conn.close()
    wid = row["id"] if row else _entry(word, "es")
    return database.insert_card(wid, category, _deck(category), state=state,
                                due=due or _iso(days))


def _pattern(key, mode, days_ago=1):
    database.save_conj_state({
        "lang": "es", "pattern": key, "mode": mode, "state": "review",
        "due": _iso(-days_ago), "last_review": _iso(-30), "reps": 1, "lapses": 0,
        "stability": 5.0, "difficulty": 5.0})


def _due(cid):
    return database.get_card(cid)["due"]


# --- pull-forward ---------------------------------------------------------------

def test_pulls_card_for_due_prod_pattern_once():
    cid = _card("ir", "creating", 20)
    _pattern("verb:ir:ind_pres", "prod")
    assert conj_patterns.ensure_pullforward() == 1
    card = database.get_card(cid)
    assert card["due"] == _iso(0) and card["state"] == "review"
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(cid) == _iso(0)


def test_database_entry_point_pulls_and_adds_to_total():
    cid = _card("ir", "creating", 20)
    _pattern("verb:ir:ind_pres", "prod")
    assert database.ensure_daily_targets("es") >= 1
    assert _due(cid) == _iso(0)


def test_covered_pattern_pulls_nothing():
    _card("hablar", "creating", 0)             # due today, produces the pattern
    other = _card("comprar", "creating", 20)
    _pattern("reg:ind_pres:ar", "prod")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(other) == _iso(20)


def test_mode_must_match_category():
    ir_creating = _card("ir", "creating", 20)
    _pattern("verb:ir:ind_pres", "recog")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(ir_creating) == _iso(20)

    ir_listening = _card("estar", "listening", 20)
    _pattern("verb:estar:ind_pres", "prod")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(ir_listening) == _iso(20)

    _pattern("verb:estar:ind_pres", "recog")
    assert conj_patterns.ensure_pullforward() == 1
    assert _due(ir_listening) == _iso(0)


def test_new_and_learning_cards_are_never_pulled():
    new = _card("ir", "creating", 20, state="new")
    learning = _card("estar", "creating", 0, state="learning",
                     due=(_today() + timedelta(days=3)).isoformat() + "T10:00:00")
    _pattern("verb:ir:ind_pres", "prod")
    _pattern("verb:estar:ind_pres", "prod")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(new) == _iso(20)
    assert database.get_card(learning)["state"] == "learning"


def test_suspended_and_buried_cards_are_not_pulled():
    sus = _card("ir", "creating", 20, state="suspended")
    buried = _card("estar", "creating", 20)
    conn = database.get_db()
    conn.execute("UPDATE cards SET buried_until = ? WHERE id = ?", (_iso(0), buried))
    conn.commit()
    conn.close()
    _pattern("verb:ir:ind_pres", "prod")
    _pattern("verb:estar:ind_pres", "prod")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(sus) == _iso(20) and _due(buried) == _iso(20)


def test_budget_respected_and_prod_goes_first():
    database.set_app_setting(conj_patterns.KEY_PULL_PER_DAY, "1")
    estar = _card("estar", "listening", 10)
    ir = _card("ir", "creating", 20)
    _pattern("verb:estar:ind_pres", "recog", days_ago=5)   # older, but recog
    _pattern("verb:ir:ind_pres", "prod", days_ago=1)
    assert conj_patterns.ensure_pullforward() == 1
    assert _due(ir) == _iso(0) and _due(estar) == _iso(10)
    assert conj_patterns.ensure_pullforward() == 0          # budget used up


def test_budget_two_patterns_pull_one():
    database.set_app_setting(conj_patterns.KEY_PULL_PER_DAY, "1")
    ir = _card("ir", "creating", 20)
    estar = _card("estar", "creating", 20)
    _pattern("verb:ir:ind_pres", "prod", days_ago=3)
    _pattern("verb:estar:ind_pres", "prod", days_ago=1)
    assert conj_patterns.ensure_pullforward() == 1
    assert _due(ir) == _iso(0) and _due(estar) == _iso(20)


def test_off_when_zero_or_disabled():
    cid = _card("ir", "creating", 20)
    _pattern("verb:ir:ind_pres", "prod")
    database.set_app_setting(conj_patterns.KEY_PULL_PER_DAY, "0")
    assert conj_patterns.ensure_pullforward() == 0
    database.set_app_setting(conj_patterns.KEY_PULL_PER_DAY, "10")
    database.set_app_setting(conj_patterns.KEY_ENABLED, "0")
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(cid) == _iso(20)
    database.set_app_setting(conj_patterns.KEY_ENABLED, "1")
    database.set_app_setting("disabled_langs", '["es"]')
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(cid) == _iso(20)


def test_earliest_due_candidate_wins_for_regular_pattern():
    far = _card("hablar", "creating", 30)
    first = _card("comprar", "creating", 10)
    _card("cantar", "creating", 10)
    _pattern("reg:ind_pres:ar", "prod")
    assert conj_patterns.ensure_pullforward() == 1
    assert _due(first) == _iso(0)
    assert _due(far) == _iso(30)


def test_future_patterns_are_not_due():
    cid = _card("ir", "creating", 20)
    _pattern("verb:ir:ind_pres", "prod", days_ago=-2)
    assert conj_patterns.ensure_pullforward() == 0
    assert _due(cid) == _iso(20)


# --- irregular verbs: reading/listening do not bury creating -------------------

def _word_cards(word):
    ids = {c: _card(word, c, 0) for c in ("listening", "reading", "creating")}
    return ids


def _buried(cid):
    return database.get_card(cid)["buried_until"] is not None


def test_irregular_listening_does_not_bury_creating():
    ids = _word_cards("tener")
    wid = database.get_card(ids["listening"])["word_id"]
    database.bury_siblings(wid, "listening", bury_new=True, bury_review=True, bury_learning=True)
    assert not _buried(ids["creating"])
    assert _buried(ids["reading"])


def test_irregular_creating_still_buries_others():
    ids = _word_cards("tener")
    wid = database.get_card(ids["creating"])["word_id"]
    database.bury_siblings(wid, "creating", bury_new=True, bury_review=True, bury_learning=True)
    assert _buried(ids["listening"]) and _buried(ids["reading"])


def test_irregular_reading_and_listening_bury_each_other():
    ids = _word_cards("tener")
    wid = database.get_card(ids["reading"])["word_id"]
    database.bury_siblings(wid, "reading", bury_new=True, bury_review=True, bury_learning=True)
    assert _buried(ids["listening"]) and not _buried(ids["creating"])


def test_regular_verb_still_buries_creating():
    ids = _word_cards("hablar")
    wid = database.get_card(ids["listening"])["word_id"]
    database.bury_siblings(wid, "listening", bury_new=True, bury_review=True, bury_learning=True)
    assert _buried(ids["creating"]) and _buried(ids["reading"])


def test_setting_off_restores_old_bury_behaviour():
    database.set_app_setting(conj_patterns.KEY_NO_BURY, "0")
    ids = _word_cards("tener")
    wid = database.get_card(ids["listening"])["word_id"]
    database.bury_siblings(wid, "listening", bury_new=True, bury_review=True, bury_learning=True)
    assert _buried(ids["creating"]) and _buried(ids["reading"])


def test_story_list_suppression_follows_the_rule():
    from database.cards import _get_virtually_buried_word_ids
    ids = _word_cards("tener")
    wid = database.get_card(ids["creating"])["word_id"]
    today, now = _iso(0), _today().isoformat() + "T23:59:59"
    conn = database.get_db()
    try:
        assert _get_virtually_buried_word_ids({wid}, "creating", conn, today, now) == set()
        # reading is still suppressed by listening
        assert _get_virtually_buried_word_ids({wid}, "reading", conn, today, now) == {wid}
        database.set_app_setting(conj_patterns.KEY_NO_BURY, "0")
        assert _get_virtually_buried_word_ids({wid}, "creating", conn, today, now) == {wid}
    finally:
        conn.close()


# --- settings API ----------------------------------------------------------------

def test_settings_validation_for_new_keys():
    import main
    client = TestClient(main.app)
    s = client.get("/api/es/conj-settings").json()
    assert s["pull_per_day"] == 10 and s["irregular_creating_unburied"] is True
    for bad in ({"pull_per_day": 51}, {"pull_per_day": -1}, {"pull_per_day": True},
                {"pull_per_day": "5"}, {"pull_per_day": 2.5},
                {"irregular_creating_unburied": "yes"}):
        assert client.put("/api/es/conj-settings", json=bad).status_code == 400
    r = client.put("/api/es/conj-settings",
                   json={"pull_per_day": 0, "irregular_creating_unburied": False})
    assert r.status_code == 200
    assert r.json()["pull_per_day"] == 0 and r.json()["irregular_creating_unburied"] is False
    assert client.get("/api/es/conj-settings").json()["pull_per_day"] == 0

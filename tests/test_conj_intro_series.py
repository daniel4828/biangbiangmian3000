"""Intro series of a NEW irregular verb (#1498)."""
import random
from datetime import date

import pytest

import conj_patterns
import database
import lexicon

_REAL_LEXICON = lexicon._RESOURCE_DIR / "es-lexicon.json.gz"
TODAY = "2026-03-10"

TENER_ALL = ["1s", "2s", "3s", "1p", "2p", "3p"]
TENER_GROUPS = {
    "ind_pres": [["1s"], ["2s", "3s", "3p"], ["1p", "2p"]],
    "ind_pret": [TENER_ALL],
    "ind_impf": [TENER_ALL],
    "perf": [TENER_ALL],
    "ind_fut": [TENER_ALL],
    "subj_pres": [TENER_ALL],
    "subj_impf": [TENER_ALL],
}


@pytest.fixture
def real_lex(tmp_path, monkeypatch):
    if not _REAL_LEXICON.exists():
        pytest.skip("bundled es lexicon missing")
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(database, "anki_today", lambda: date(2026, 3, 10))
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


def _card(wid, category, word, state="new"):
    return {"word_id": wid, "word_zh": word, "category": category, "pinyin": "",
            "definition": "", "pos": "", "state": state}


def _plan(entries, seed=1):
    return conj_patterns.plan_followups(entries, "es", today=TODAY, conj_states={},
                                        rng=random.Random(seed))


@pytest.mark.parametrize("tense", list(TENER_GROUPS))
def test_intro_groups_tener(real_lex, tense):
    assert conj_patterns.intro_groups("tener", tense) == TENER_GROUPS[tense]


def test_intro_groups_pensar(real_lex):
    assert conj_patterns.intro_groups("pensar", "ind_pres") == [
        ["1s", "2s", "3s", "3p"], ["1p", "2p"]]


def test_intro_groups_regular_verb_is_one_group(real_lex):
    for tense in TENER_GROUPS:
        assert conj_patterns.intro_groups("hablar", tense) == [TENER_ALL]


def test_intro_groups_never_raises(real_lex):
    assert conj_patterns.intro_groups("xyz", "ind_pres") == []
    assert conj_patterns.intro_groups("tener", "nonsense") == []
    assert conj_patterns.intro_groups("tener", "ind_pres", "fr") == []


def test_intro_series_with_main_slot(real_lex):
    items, covered = conj_patterns.plan_intro_series(
        [(_card(1, "creating", "tener"), "ind_pres:3s")], "es")
    assert covered == {1}
    assert len(items) == 9
    assert all(i["intro"] and i["word_id"] == 1 and i["lemma"] == "tener" for i in items)
    slots = [i["slot"] for i in items]
    pres = [s for s in slots if s.startswith("ind_pres:")]
    assert pres[0] == "ind_pres:1s"
    assert pres[1] in ("ind_pres:1p", "ind_pres:2p")
    assert not {"ind_pres:2s", "ind_pres:3s", "ind_pres:3p"} & set(slots)
    for tense in ("ind_pret", "ind_impf", "perf", "ind_fut", "subj_pres", "subj_impf", "ind_cond"):
        assert sum(s.startswith(tense + ":") for s in slots) == 1


def test_intro_series_without_main_slot(real_lex):
    items, _ = conj_patterns.plan_intro_series([(_card(1, "creating", "tener"), None)], "es")
    assert len(items) == 10
    forms = {i["form"] for i in items}
    assert "tengo" in forms and "tuviera" in forms


def test_intro_series_skips_non_qualifying(real_lex):
    def run(card):
        return conj_patterns.plan_intro_series([(card, None)], "es")

    assert run(_card(1, "creating", "hablar")) == ([], set())
    assert run(_card(1, "creating", "tener", state="review")) == ([], set())
    assert run(_card(1, "reading", "tener")) == ([], set())
    assert run(_card(1, "creating", "tener · Indefinido")) == ([], set())


def test_plan_followups_includes_intro(real_lex):
    out = _plan([(_card(1, "creating", "tener"), None)])
    assert len(out) == 10 and all(i.get("intro") for i in out)


def test_intro_setting_off(real_lex):
    conj_patterns.save_settings({"intro_series": False})
    out = _plan([(_card(1, "creating", "tener"), None)])
    assert not any(i.get("intro") for i in out)


def test_followups_per_day_zero_switches_intro_off(real_lex):
    conj_patterns.save_settings({"followups_per_day": 0})
    assert _plan([(_card(1, "creating", "tener"), None)]) == []


def test_intro_not_truncated_by_daily_cap(real_lex):
    conj_patterns.save_settings({"followups_per_day": 2})
    out = _plan([(_card(1, "creating", "tener"), None)])
    assert len(out) == 10 and all(i.get("intro") for i in out)


def test_regular_followups_unchanged_for_non_new_card(real_lex):
    out = _plan([(_card(1, "creating", "tener", state="review"), "ind_pres:3s")])
    assert not any(i.get("intro") for i in out)


def test_intro_series_setting_roundtrip(real_lex):
    assert conj_patterns.settings()["intro_series"] is True
    clean = conj_patterns.validate_settings({"intro_series": False})
    assert clean == {"intro_series": False}
    conj_patterns.save_settings(clean)
    assert conj_patterns.settings()["intro_series"] is False
    conj_patterns.save_settings(conj_patterns.validate_settings({"intro_series": 1}))
    assert conj_patterns.settings()["intro_series"] is True
    with pytest.raises(ValueError):
        conj_patterns.validate_settings({"intro_series": "no"})

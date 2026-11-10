"""Conjugated forms of multi-word verb expressions (#1508)."""
import re
from unittest.mock import patch

import pytest

import ai
import database
import expressions
import lexicon
import verb_slots


@pytest.fixture(autouse=True)
def _real_lexicon():
    lexicon.clear_cache()
    if not lexicon.available("es"):
        pytest.skip("resources/es-lexicon.json.gz missing")
    yield
    lexicon.clear_cache()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _matches(forms, sentence):
    return any(re.search(rf"(?<!\w){re.escape(f)}(?!\w)", sentence, re.IGNORECASE)
               for f in forms)


def test_tener_prisa():
    forms = expressions.expression_forms("tener prisa", "es")
    assert "tengo prisa" in forms and "tenía prisa" in forms
    assert _matches(forms, "Tengo prisa.")
    assert forms == sorted(forms, key=len, reverse=True)


def test_irle_bien_a_alguien():
    forms = expressions.expression_forms("irle bien a alguien", "es")
    assert {"le va bien", "les fue bien", "va bien"} <= set(forms)


def test_darse_cuenta_de_algo():
    forms = expressions.expression_forms("darse cuenta de algo", "es")
    assert {"me di cuenta", "se dio cuenta"} <= set(forms)


def test_non_expressions_give_nothing():
    assert expressions.expression_forms("tener", "es") == []
    assert expressions.expression_forms("生态", "zh") == []
    assert expressions.expression_forms("xyzzy prisa", "es") == []
    assert expressions.expression_forms("", "es") == []


def test_french_without_lexicon_does_not_raise():
    assert expressions.expression_forms("avoir besoin de qch", "fr") == []
    assert expressions.expression_forms("se souvenir de qn", "fr") == []


def test_surface_forms_includes_expression_forms(tmp_db):
    forms = database.surface_forms(999, "tener prisa", "es")
    assert "tengo prisa" in forms
    assert "tener prisa" in forms


def test_generate_story_matches_conjugated_expression(tmp_db, monkeypatch):
    monkeypatch.setattr(ai, "_fill_translations", lambda parsed, progress_key=None, lang="zh": None)
    monkeypatch.setattr(verb_slots, "assign_slots", lambda cards, lang, **kw: {})
    cards = [{"word_id": 1, "word_zh": "tener prisa", "pinyin": "", "definition": "", "pos": ""}]
    with patch("ai._call_api", return_value="1. Tengo prisa hoy."):
        sentences, prompt = ai.generate_story(cards, lang="es")
    assert sentences[0]["word_ids"] == [1]
    assert sentences[0]["sentence_zh"] == "Tengo prisa hoy."
    assert "multi-word expressions" in prompt

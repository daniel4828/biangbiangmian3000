"""Spanish A1 baseline + noun articles (#1294)."""
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

import database
import languages


# ── languages.article_for ──────────────────────────────────────────────────

def test_article_masculine_and_feminine():
    assert languages.article_for("clima", "m", "es") == "el"
    assert languages.article_for("cuenta", "f", "es") == "la"


def test_article_stressed_a_feminine_takes_el():
    assert languages.article_for("agua", "f", "es") == "el"
    assert languages.article_for("Águila", "f", "es") == "el"
    # unstressed initial a keeps "la"
    assert languages.article_for("aguja", "f", "es") == "la"


def test_article_both_genders():
    assert languages.article_for("estudiante", "mf", "es") == "el/la"


def test_article_none_cases():
    assert languages.article_for("de", "m", "zh") is None
    assert languages.article_for("maison", "f", "fr") is None
    assert languages.article_for("clima", None, "es") is None
    assert languages.article_for("clima", "", "es") is None
    assert languages.article_for("", "m", "es") is None
    assert languages.article_for(None, None, None) is None
    assert languages.article_for("clima", "x", "es") is None


# ── GET /api/word/{id} ─────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path


def test_word_detail_article(tmp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    es_id = database.insert_word({"word_zh": "clima", "lang": "es", "gender": "m"})
    zh_id = database.insert_word({"word_zh": "生态", "lang": "zh"})

    es = client.get(f"/api/word/{es_id}").json()
    assert es["article"] == "el"
    assert es["word_zh"] == "clima"  # stored headword stays bare

    zh = client.get(f"/api/word/{zh_id}").json()
    assert "article" not in zh


# ── scripts/build_es_baseline.py ───────────────────────────────────────────

def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "build_es_baseline.py"
    spec = importlib.util.spec_from_file_location("build_es_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_baseline_script(tmp_path):
    mod = _load_script()
    lexicon = {"lemmas": {
        "ser": {"r": 2, "p": "verb"},
        "de": {"r": 1, "p": "prep"},
        "madrid": {"r": 3, "p": "prop"},
        "casa": {"r": 4, "p": "noun", "g": "f"},
        "perro": {"r": 5, "p": "noun", "g": "m"},
        "gato": {"p": "noun"},
    }}
    lex_path = tmp_path / "lex.json.gz"
    with gzip.open(lex_path, "wt", encoding="utf-8") as f:
        json.dump(lexicon, f)
    out = tmp_path / "baseline_es.txt"

    assert mod.main(["--top", "4", "--lexicon", str(lex_path), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("#")
    words = [l for l in lines if l and not l.startswith("#")]
    assert words == ["de", "ser", "casa"]  # rank order, prop excluded, rank 5 cut


def test_article_for_skips_headword_with_article():
    # 生产里出现 "la la ventaja"：词头已带冠词时不再重复
    assert languages.article_for("la ventaja", "f", "es") is None
    assert languages.article_for("El clima", "m", "es") is None
    assert languages.article_for("lado", "m", "es") == "el"

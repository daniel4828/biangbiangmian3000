"""
Entry-level etymology for Romance languages (issue #906).

The French/Spanish review card has no character breakdown to show, so the
"Word Analysis" slot carries the word's origin instead — a new
`entries.etymology` column, filled by the entry prompt at import time and
regenerated on demand through the `entry_etymology` field of
/api/word/{id}/regenerate-fields.

What these tests guard:
  - the column round-trips through the importer for fr/es and stays NULL for zh
  - the regen endpoint routes `entry_etymology` to ai.generate_entry_etymology()
    and never to the Chinese character prompt
  - the frontend renders exactly one of the two blocks per language
"""
import os
import re
import sys
import uuid

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ai
import database
import database.core as db_core
import importer

APP_JS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "app.js")

_ETYM_FR = "Vom lateinischen *tripalium*, einem Folterinstrument."


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = os.path.join(tmp_path, f"test_etym_{uuid.uuid4().hex}.db")
    monkeypatch.setattr(db_core, "DB_PATH", db_file)
    database.init_db()
    yield db_file
    if os.path.exists(db_file):
        os.remove(db_file)


def _import(tmp_path, entries, lang, deck):
    doc = {"entries": entries}
    if lang != "zh":
        doc["lang"] = lang
    deck_dir = tmp_path / deck
    deck_dir.mkdir(parents=True, exist_ok=True)
    path = deck_dir / "1_1.yaml"
    path.write_text(yaml.dump(doc, allow_unicode=True), encoding="utf-8")
    return importer.import_yaml_file(str(path), [deck])


class TestImport:
    def test_french_etymology_is_stored(self, tmp_db, tmp_path):
        _import(tmp_path, [{
            "type": "word",
            "word": "travailler",
            "english": "to work",
            "german": "arbeiten",
            "level": "A1",
            "etymology": _ETYM_FR,
        }], "fr", "Francais")

        entry = database.get_word_by_zh("travailler")
        assert entry["lang"] == "fr"
        assert entry["etymology"] == _ETYM_FR

    def test_chinese_entries_keep_null_etymology(self, tmp_db, tmp_path):
        # The Chinese format has no top-level `etymology:` key — its etymology
        # lives per character. Even if a model emitted one, it must not land in
        # the entry column, where the frontend would render it as a Romance
        # block for a Chinese word.
        _import(tmp_path, [{
            "type": "word",
            "simplified": "生态",
            "english": "ecology",
            "etymology": "should be ignored",
        }], "zh", "Kouyu")

        assert database.get_word_by_zh("生态")["etymology"] is None

    def test_reimport_backfills_missing_etymology(self, tmp_db, tmp_path):
        """Entries imported before #906 must be able to gain the column later."""
        base = {"type": "word", "word": "travailler", "english": "to work",
                "german": "arbeiten", "level": "A1"}
        _import(tmp_path, [base], "fr", "Francais")
        assert database.get_word_by_zh("travailler")["etymology"] is None

        _import(tmp_path, [{**base, "etymology": _ETYM_FR}], "fr", "Francais")
        assert database.get_word_by_zh("travailler")["etymology"] == _ETYM_FR

    def test_update_word_can_write_etymology(self, tmp_db, tmp_path):
        _import(tmp_path, [{"type": "word", "word": "travailler",
                            "english": "to work", "level": "A1"}], "fr", "Francais")
        wid = database.get_word_by_zh("travailler")["id"]
        database.update_word(wid, {"etymology": _ETYM_FR})
        assert database.get_word_full(wid)["etymology"] == _ETYM_FR


class TestRegenEndpoint:
    def _client(self):
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    def test_entry_etymology_field_saves_to_entries(self, tmp_db, tmp_path, monkeypatch):
        _import(tmp_path, [{"type": "word", "word": "travailler",
                            "english": "to work", "level": "A1"}], "fr", "Francais")
        wid = database.get_word_by_zh("travailler")["id"]

        calls = []

        def fake_etym(word, model=None):
            calls.append(word["word_zh"])
            return _ETYM_FR

        def boom(*a, **kw):
            raise AssertionError("the Chinese character prompt must not run for a French entry")

        monkeypatch.setattr(ai, "generate_entry_etymology", fake_etym)
        monkeypatch.setattr(ai, "regenerate_entry_fields", boom)

        r = self._client().post(f"/api/word/{wid}/regenerate-fields",
                                json={"fields": ["entry_etymology"]})
        assert r.status_code == 200, r.text
        assert calls == ["travailler"]
        assert database.get_word_full(wid)["etymology"] == _ETYM_FR

    def test_preview_returns_the_prose_without_saving(self, tmp_db, tmp_path, monkeypatch):
        _import(tmp_path, [{"type": "word", "word": "travailler",
                            "english": "to work", "level": "A1"}], "fr", "Francais")
        wid = database.get_word_by_zh("travailler")["id"]
        monkeypatch.setattr(ai, "generate_entry_etymology", lambda word, model=None: _ETYM_FR)

        r = self._client().post(f"/api/word/{wid}/regenerate-fields",
                                json={"fields": ["entry_etymology"], "preview": True})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["entry_etymology"] == _ETYM_FR
        assert database.get_word_full(wid)["etymology"] is None

    def test_apply_regen_result_writes_the_edited_text(self, tmp_db, tmp_path):
        _import(tmp_path, [{"type": "word", "word": "travailler",
                            "english": "to work", "level": "A1"}], "fr", "Francais")
        wid = database.get_word_by_zh("travailler")["id"]

        r = self._client().post(f"/api/word/{wid}/apply-regen-result", json={
            "fields": ["entry_etymology"],
            "result": {"entry_etymology": "Hand-edited."},
        })
        assert r.status_code == 200, r.text
        assert database.get_word_full(wid)["etymology"] == "Hand-edited."


class TestPrompts:
    @pytest.mark.parametrize("lang", ["fr", "es"])
    def test_romance_prompt_asks_for_a_separate_etymology_field(self, lang):
        prompt, example = ai._ENTRY_YAML_TEMPLATES[lang]
        assert "- etymology:" in prompt
        # It must no longer be smuggled into `note` — that is exactly what made
        # it unrenderable before #906.
        assert "belongs in the note" not in prompt
        assert "\n  etymology: |" in example

    def test_chinese_prompt_is_untouched(self):
        prompt, example = ai._ENTRY_YAML_TEMPLATES["zh"]
        # Chinese etymology stays nested under word_analyses, per character.
        assert "\n  etymology: |" not in example
        assert "word_analyses" in prompt


class TestFrontend:
    """The two renderers share one panel slot and must not both fill it."""

    def test_word_analysis_bails_out_for_non_chinese(self):
        src = open(APP_JS, encoding="utf-8").read()
        body = src[src.index("function renderWordAnalysis("):]
        assert "_entryLang(wd) !== 'zh'" in body[:1200]

    def test_etymology_bails_out_for_chinese(self):
        src = open(APP_JS, encoding="utf-8").read()
        body = src[src.index("function renderEtymologySection("):]
        assert "_entryLang(wd) === 'zh'" in body[:800]

    def test_regen_all_picks_fields_by_language(self):
        src = open(APP_JS, encoding="utf-8").read()
        body = src[src.index("function _allRegenFields("):]
        body = body[:body.index("function regenAllFields(")]
        # Character-level fields for Chinese, the entry-level column otherwise.
        assert "'compounds'" in body and "'entry_etymology'" in body
        assert re.search(r"_entryLang\(wordData\) === 'zh'", body)

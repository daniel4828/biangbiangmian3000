"""Words Daniel already has an entry for get a word_id in the reader (#1042).

The reader's second pass (#1018) glosses every word of the page, not just the
new ones — but a word he has actually studied is exactly the one he wants to
open again, and until now it was display-only. The route tags those words with
their entry id so the frontend can link them to the detail page.

The lookup itself must not stem or guess: a wrong id sends him to another
word's entry, which is worse than no link at all.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database
import database.core
from main import app

client = TestClient(app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Patch database.core.DB_PATH, never database.DB_PATH (#615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _add_fr_word(word: str, forms: list[str]) -> int:
    wid = database.insert_word({"word_zh": word, "definition": word, "lang": "fr"})
    for i, f in enumerate(forms):
        database.insert_word_form(wid, "conjugation", "présent", "nous", f, i)
    return wid


class TestEntryIdsForForms:

    def test_headword_matches(self, tmp_db):
        wid = database.insert_word({"word_zh": "生态", "definition": "ecology"})
        assert database.entry_ids_for_forms(["生态", "重要"], "zh") == {"生态": wid}

    def test_empty_input(self, tmp_db):
        assert database.entry_ids_for_forms([], "zh") == {}

    def test_other_language_is_not_matched(self, tmp_db):
        database.insert_word({"word_zh": "chat", "definition": "cat", "lang": "fr"})
        assert database.entry_ids_for_forms(["chat"], "es") == {}

    def test_inflected_form_resolves_to_its_entry(self, tmp_db):
        wid = _add_fr_word("manger", ["mangeons"])
        assert database.entry_ids_for_forms(["mangeons"], "fr") == {"mangeons": wid}

    def test_no_stemming(self, tmp_db):
        """A form nobody stored stays unmatched — see get_word_by_form."""
        _add_fr_word("manger", ["mangeons"])
        assert database.entry_ids_for_forms(["mangeaient"], "fr") == {}

    def test_headword_wins_over_another_entrys_inflection(self, tmp_db):
        head = database.insert_word({"word_zh": "porte", "definition": "Tür", "lang": "fr"})
        _add_fr_word("porter", ["porte"])
        assert database.entry_ids_for_forms(["porte"], "fr") == {"porte": head}


class TestNewWordsAllMode:

    def test_all_mode_tags_words_that_have_an_entry(self, tmp_db):
        wid = database.insert_word({"word_zh": "生态", "definition": "ecology"})
        with patch("annotate.all_words", return_value=[
            {"word": "生态", "pinyin": "shēngtài", "definition_de": "Ökologie"},
            {"word": "很", "pinyin": "hěn", "definition_de": "sehr"},
        ]):
            words = client.post("/api/new-words",
                                json={"text": "生态很重要", "mode": "all"}).json()["words"]

        assert words[0]["word_id"] == wid
        assert "word_id" not in words[1]

    def test_new_mode_is_untouched(self, tmp_db):
        """A *new* word has no entry by definition — no lookup, no key."""
        with patch("annotate.annotate_summary",
                   return_value=("t", [{"word": "生态", "definition_de": "Ökologie"}])):
            words = client.post("/api/new-words", json={"text": "生态"}).json()["words"]
        assert "word_id" not in words[0]

    def test_lookup_failure_costs_the_links_not_the_glosses(self, tmp_db):
        with patch("annotate.all_words", return_value=[{"word": "生态", "definition_de": "Ökologie"}]), \
             patch("database.entry_ids_for_forms", side_effect=RuntimeError("db gone")):
            r = client.post("/api/new-words", json={"text": "生态", "mode": "all"})

        assert r.status_code == 200
        assert r.json()["words"][0]["definition_de"] == "Ökologie"

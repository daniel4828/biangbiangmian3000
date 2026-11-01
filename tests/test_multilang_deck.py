"""🌐 Languages session: every enabled language in one review + one story (#1378).

The pseudo language code "multi" travels as the `lang` param. Scheduling is
untouched; only the review view / story generation differ.
"""
from unittest.mock import patch

import pytest

import ai
import database
import routes.story as story_routes


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def no_translation(monkeypatch):
    monkeypatch.setattr(ai, "_fill_translations",
                        lambda parsed, progress_key=None, lang="zh": None)


def _card(wid, word, lang):
    return {"word_id": wid, "word_zh": word, "word_lang": lang,
            "pinyin": "", "definition": "", "pos": ""}


def _tree():
    """All -> Daily (zh) / Español (es) / Français (fr), one leaf each."""
    all_id = database.get_all_deck_id()
    leaves = {}
    for lang, root, leaf, cat in (("zh", "Daily", "Daily · Listening", "listening"),
                                  ("es", "Español", "Español · Creating", "creating"),
                                  ("fr", "Français", "Français · Reading", "reading")):
        root_id = database.get_or_create_deck(root, parent_id=all_id, lang=lang)
        leaves[lang] = database.get_or_create_deck(leaf, parent_id=root_id,
                                                   category=cat, lang=lang)
    return all_id, leaves


# --- deck selection ----------------------------------------------------------

def test_multi_leaf_ids_cover_enabled_languages(tmp_db):
    all_id, leaves = _tree()
    # French is disabled by default (#1252)
    ids = set(database.get_descendant_leaf_deck_ids(all_id, lang="multi"))
    assert ids == {leaves["zh"], leaves["es"]}


def test_multi_leaf_ids_exclude_disabled_language(tmp_db):
    all_id, leaves = _tree()
    database.set_lang_enabled("fr", True)
    database.set_lang_enabled("es", False)
    ids = set(database.get_descendant_leaf_deck_ids(all_id, lang="multi"))
    assert ids == {leaves["zh"], leaves["fr"]}


def test_single_language_filter_unchanged(tmp_db):
    all_id, leaves = _tree()
    assert database.get_descendant_leaf_deck_ids(all_id, lang="es") == [leaves["es"]]


# --- generate_story ----------------------------------------------------------

def test_multi_story_matches_each_card_in_its_language(tmp_db, no_translation):
    cards = [_card(1, "生态", "zh"), _card(2, "casa", "es")]
    raw = "1. 这里的生态很好。\n2. La casa es muy grande."
    with patch("ai._call_api", return_value=raw) as call:
        sentences, prompt = ai.generate_story(cards, lang="multi")
    assert "[Chinese]" in prompt and "[Spanish]" in prompt
    assert call.call_count == 1
    assert [s["word_ids"] for s in sentences] == [[1], [2]]
    assert [s["lang"] for s in sentences] == ["zh", "es"]


def test_multi_zh_card_does_not_match_latin_sentence(tmp_db, no_translation):
    cards = [_card(1, "生态", "zh")]
    raw = "1. The word 生态 means ecology in English, as used in many articles today."
    with patch("ai._call_api", return_value=raw):
        with pytest.raises(RuntimeError):
            ai.generate_story(cards, lang="multi")


def test_multi_unmatched_sentence_gets_a_language(tmp_db, no_translation):
    cards = [_card(1, "生态", "zh"), _card(2, "casa", "es")]
    raw = "1. 这里的生态很好。\n2. La casa es muy grande.\n3. Hoy hace sol."
    with patch("ai._call_api", return_value=raw):
        sentences, _ = ai.generate_story(cards, lang="multi")
    assert sentences[2]["word_ids"] == []
    assert sentences[2]["lang"] == "es"


def test_multi_patch_missing_uses_card_language():
    cards = {1: _card(1, "生态", "zh"), 2: _card(2, "casa", "es")}
    sentences: list = []
    ai._patch_missing(sentences, [1, 2], cards, lang="multi")
    assert [s["lang"] for s in sentences] == ["zh", "es"]
    assert "生态" in sentences[0]["sentence_zh"]
    assert "casa" in sentences[1]["sentence_zh"]


def test_single_language_patch_missing_has_no_lang():
    sentences: list = []
    ai._patch_missing(sentences, [1], {1: _card(1, "生态", "zh")}, lang="zh")
    assert "lang" not in sentences[0]


def test_single_language_prompt_unchanged(tmp_db, no_translation):
    cards = [{"word_id": 1, "word_zh": "生态", "pinyin": "", "definition": "", "pos": ""}]
    with patch("ai._call_api", return_value="1. 这里的生态很好。"):
        sentences, prompt = ai.generate_story(cards, lang="zh")
    assert "[Chinese]" not in prompt and "[Spanish]" not in prompt
    assert "lang" not in sentences[0]


def test_fill_translations_multi_groups_by_sentence_language(monkeypatch):
    import translator
    seen = []

    def fake_batch(texts, target="de", source="auto", on_progress=None):
        seen.append((source, list(texts)))
        return [f"de:{t}" for t in texts]

    monkeypatch.setattr(translator, "translate_batch", fake_batch)
    sentences = [{"sentence_zh": "你好", "lang": "zh"},
                 {"sentence_zh": "Hola", "lang": "es"},
                 {"sentence_zh": "再见", "lang": "zh"}]
    ai._fill_translations(sentences, lang="multi")
    assert [s["sentence_de"] for s in sentences] == ["de:你好", "de:Hola", "de:再见"]
    assert {src for src, _ in seen} == {"zh-CN", "es"}


# --- route-level guards ------------------------------------------------------

def test_multi_rejects_zh_only_modes(tmp_db):
    result = story_routes._generate_and_store_body(
        1, "unified", "2026-01-01", [_card(1, "生态", "zh")],
        topic=None, max_hsk=2, model="m", grammar_focus=None, grammar_pct=75,
        mode="kahneman", chapter_ids=None, articles=None, progress_key="k",
        lang="multi", origin=None, episode_ids=None)
    assert result["error"] is True
    assert "not available for the multi-language deck" in result["reason"]


def test_add_tokens_uses_sentence_language():
    sentences = [{"sentence_zh": "你好吗", "lang": "zh", "tokens": []},
                 {"sentence_zh": "Hola amigo", "lang": "es", "tokens": []},
                 {"sentence_zh": "Hola amigo", "tokens": []}]
    story_routes._add_tokens(sentences, lang="multi")
    assert sentences[1]["tokens"] == [["Hola", None], [" ", None], ["amigo", None]]
    assert sentences[2]["tokens"] == [["Hola", None], [" ", None], ["amigo", None]]
    assert all(t[1] is None for t in sentences[0]["tokens"])


# --- storage -----------------------------------------------------------------

def test_story_sentence_lang_round_trips_and_counts_verb_slots(tmp_db):
    all_id, _ = _tree()
    zh_id = database.insert_word({"word_zh": "生态", "lang": "zh"})
    es_id = database.insert_word({"word_zh": "hablar", "lang": "es"})
    sentences = [
        {"position": 0, "sentence_zh": "这里的生态很好。", "word_ids": [zh_id], "lang": "zh"},
        {"position": 1, "sentence_zh": "Ayer hablé mucho.", "word_ids": [es_id],
         "lang": "es", "verb_slot": "ind_pret:1s"},
    ]
    story_id = database.create_story("2026-01-01", "unified", all_id, sentences, lang="multi")
    stored = database.get_story_sentences(story_id)
    assert [s["lang"] for s in stored] == ["zh", "es"]
    # The Spanish drill inside the multi story counts for the es rotation
    assert database.verb_slot_usage("es")["ind_pret:1s"][0] == 1
    assert "ind_pret:1s" not in database.verb_slot_usage("multi")


def test_story_sentence_without_lang_stays_null(tmp_db):
    all_id, _ = _tree()
    zh_id = database.insert_word({"word_zh": "生态", "lang": "zh"})
    story_id = database.create_story(
        "2026-01-01", "unified", all_id,
        [{"position": 0, "sentence_zh": "这里的生态很好。", "word_ids": [zh_id]}], lang="zh")
    assert database.get_story_sentences(story_id)[0]["lang"] is None

"""Tests for issue #806: knowledge-mode sentence generation in French/Spanish,
and one scheduling preset per language.

Knowledge mode is what Daniel actually uses day to day, and it was hardwired
to Chinese: ai.generate_briefing_sentences/generate_podcast_sentences had no
lang parameter and their prompts were written in Chinese. The material's
language never mattered — only the output language does.

The preset half guards a specific past mistake (CLAUDE.md's #629 postmortem):
every Chinese deck is bound to preset id=2, and retuning the wrong preset once
already cost three weeks. French/Spanish decks therefore get their own preset
instead of sharing Chinese's.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ai
import database
import languages


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


# ---------------------------------------------------------------------------
# Knowledge mode in French
# ---------------------------------------------------------------------------

_SOURCE = {
    "title": "Le climat en 2026",
    "kind": "article",
    "url": "https://example.com/a",
    "material": "En 2026, la France a réduit ses émissions de 12 pour cent.",
}


def _cards(*words):
    return [{"word_id": i + 1, "word_zh": w, "pinyin": "", "definition": w,
             "definition_de": w} for i, w in enumerate(words)]


def test_knowledge_mode_french_prompt_and_coverage(tmp_db, monkeypatch):
    """Every target word gets exactly one sentence, and the prompt asks for
    French — not Chinese.

    "réduire" appears in the reply as "a réduit": the prompt explicitly lets
    the model adapt a word's form, so matching has to accept the conjugation
    stored in entry_forms (#803). Without that the sentence would be dropped
    and replaced by a fallback — which is exactly why add-word generates the
    full conjugation table.
    """
    word_id = database.insert_word({
        "word_zh": "réduire", "lang": "fr", "pinyin": None, "definition": "reduce",
        "pos": "verb", "hsk_level": 3, "traditional": None, "definition_zh": None,
        "source": "test", "note_type": "vocabulary", "notes": None, "date_yaml": None,
        "source_sentence": None, "grammar_notes": None, "register": None,
        "definition_de": "reduzieren", "definition_fr": None, "gender": None,
    })
    database.set_entry_forms(word_id, [
        {"kind": "conjugation", "paradigm": "passé composé", "slot": "il/elle",
         "form": "a réduit", "position": 0},
    ])

    cards = [
        {"word_id": word_id, "word_zh": "réduire", "pinyin": "", "definition": "reduce",
         "definition_de": "reduzieren"},
        {"word_id": word_id + 1000, "word_zh": "les émissions", "pinyin": "",
         "definition": "emissions", "definition_de": "Emissionen"},
    ]
    seen = {}

    def fake_call(model, messages, max_tokens, purpose=None, **kw):
        seen["prompt"] = messages[0]["content"]
        return json.dumps([
            {"reasoning_zh": "Fact: -12%", "sentence_zh": "La France a réduit son empreinte.",
             "target_word": "réduire"},
            {"reasoning_zh": "Fact: 2026", "sentence_zh": "Les émissions ont baissé en 2026.",
             "target_word": "les émissions"},
        ])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    sentences, prompt = ai.generate_podcast_sentences(cards, _SOURCE, lang="fr")

    assert len(sentences) == 2
    assert {s["word_ids"][0] for s in sentences} == {word_id, word_id + 1000}
    # Neither card fell back (a fallback would be the bare word + a period).
    assert all(len(s["sentence_zh"]) > 12 for s in sentences)
    assert "French" in seen["prompt"]
    assert "HSK" not in seen["prompt"]
    assert prompt


def test_knowledge_mode_french_matches_inflected_forms(monkeypatch):
    """The AI is told it may adapt a word's form; matching must tolerate that
    (article dropped, plural added) or every sentence would be discarded and
    replaced by a fallback."""
    cards = _cards("le chat")

    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: json.dumps([
        {"reasoning_zh": "Fact: x", "sentence_zh": "Les chats dorment beaucoup.",
         "target_word": "le chat"},
    ]))
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    sentences, _ = ai.generate_podcast_sentences(cards, _SOURCE, lang="fr")
    assert sentences[0]["sentence_zh"] == "Les chats dorment beaucoup."


def test_knowledge_mode_chinese_prompt_unchanged(monkeypatch):
    """zh keeps going through the user-editable prompt template."""
    cards = _cards("生态")
    seen = {}

    def fake_call(model, messages, max_tokens, purpose=None, **kw):
        seen["prompt"] = messages[0]["content"]
        return json.dumps([{"reasoning_zh": "事实：…", "sentence_zh": "生态很重要。",
                            "target_word": "生态"}])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    ai.generate_podcast_sentences(cards, _SOURCE)
    assert "目标词汇" in seen["prompt"]


def test_knowledge_mode_non_chinese_prompt_excludes_ads(monkeypatch):
    cards = _cards("réduire")
    seen = {}

    def fake_call(model, messages, max_tokens, purpose=None, **kw):
        seen["prompt"] = messages[0]["content"]
        return json.dumps([{
            "reasoning_zh": "Fakt: Der Inhalt nennt 2026.",
            "sentence_zh": "La France réduira ses émissions en 2026.",
            "target_word": "réduire",
        }])

    monkeypatch.setattr(ai, "_call_api", fake_call)
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    ai.generate_podcast_sentences(cards, _SOURCE, lang="fr")

    assert "Advertisements, sponsorship messages, affiliate promotions" in seen["prompt"]
    assert "must not be used for card sentences or reasoning_zh" in seen["prompt"]


def test_french_fallback_sentence_is_not_chinese(monkeypatch):
    """When every round fails, the filler must not be the Chinese
    我学了X这个词。in a French deck."""
    cards = _cards("réduire")
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: "not json at all")
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **kw: None)

    sentences, _ = ai.generate_podcast_sentences(cards, _SOURCE, lang="fr")
    assert len(sentences) == 1
    assert "我学了" not in sentences[0]["sentence_zh"]
    assert "réduire" in sentences[0]["sentence_zh"]


# ---------------------------------------------------------------------------
# Knowledge mode stays available for non-Chinese decks
# ---------------------------------------------------------------------------

def test_knowledge_story_mode_enabled_for_every_language():
    for code in languages.LANGUAGES:
        feats = languages.get_lang_config(code)["features"]
        assert feats["knowledge_story_mode"] is True, code
    # ...while the Chinese-only modes stay gated as before.
    assert languages.get_lang_config("fr")["features"]["extended_story_modes"] is False
    assert languages.get_lang_config("zh")["features"]["extended_story_modes"] is True


# ---------------------------------------------------------------------------
# One scheduling preset per language
# ---------------------------------------------------------------------------

def _preset_name(deck_id):
    deck = database.get_deck(deck_id)
    return next(p["name"] for p in database.list_presets() if p["id"] == deck["preset_id"])


def test_french_decks_get_their_own_preset(tmp_db):
    zh_deck, _ = database.get_or_create_daily_deck("2026-08-19", "zh")
    fr_deck, _ = database.get_or_create_daily_deck("2026-08-19", "fr")
    es_deck, _ = database.get_or_create_daily_deck("2026-08-19", "es")

    zh_preset = database.get_deck(zh_deck)["preset_id"]
    fr_preset = database.get_deck(fr_deck)["preset_id"]
    es_preset = database.get_deck(es_deck)["preset_id"]

    assert fr_preset != zh_preset
    assert es_preset != zh_preset
    assert fr_preset != es_preset
    assert _preset_name(fr_deck) == "Français"
    assert _preset_name(es_deck) == "Español"

    # The Chinese deck still uses the default preset — nothing about zh's
    # scheduling binding may change (#629).
    assert database.get_deck(zh_deck)["preset_id"] == zh_preset
    zh_deck2, _ = database.get_or_create_daily_deck("2026-08-20", "zh")
    assert database.get_deck(zh_deck2)["preset_id"] == zh_preset


def test_language_preset_is_created_once(tmp_db):
    a, _ = database.get_or_create_daily_deck("2026-08-19", "fr")
    b, _ = database.get_or_create_daily_deck("2026-08-20", "fr")
    saved = database.get_or_create_saved_deck("fr")

    ids = {database.get_deck(d)["preset_id"] for d in (a, b, saved)}
    assert len(ids) == 1
    assert sum(1 for p in database.list_presets() if p["name"] == "Français") == 1

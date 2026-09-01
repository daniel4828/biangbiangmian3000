"""Gloss reading mode for the knowledge listen bar (issue #1017).

The second mode reads the German definition of every unknown word, then the
word itself. That needs a German voice, which must NOT come from the learning
language registry — see the comment on tts.GLOSS_VOICES.
"""
import asyncio
import os
import re

import languages
import tts


def _voice_used(monkeypatch, lang):
    seen = {}

    async def fake_ensure(text, voice=tts.VOICE):
        seen["voice"] = voice
        return "/tmp/x.mp3"

    monkeypatch.setattr(tts, "_ensure_cached", fake_ensure)
    asyncio.run(tts.get_cached_path("hallo", lang=lang))
    return seen["voice"]


def test_gloss_lang_de_uses_a_german_voice(monkeypatch):
    assert _voice_used(monkeypatch, "de").startswith("de-")


def test_learning_languages_are_unaffected(monkeypatch):
    assert _voice_used(monkeypatch, "zh") == languages.LANGUAGES["zh"]["tts_voice"]
    assert _voice_used(monkeypatch, "fr") == languages.LANGUAGES["fr"]["tts_voice"]


def test_unknown_lang_still_falls_back_to_the_default(monkeypatch):
    assert _voice_used(monkeypatch, "xx") == languages.LANGUAGES["zh"]["tts_voice"]


def test_german_is_not_a_learning_language():
    """It must not show up in /api/langs, the deck trees or the tab bar."""
    assert "de" not in languages.LANGUAGES
    assert not languages.is_valid_lang("de")


def test_german_audio_gets_its_own_cache_key():
    """The zh key formula stays bare so the existing cache survives (#1017
    reuses _cache_path's voice prefixing) — but German must not collide."""
    assert tts._cache_path("Ökologie", tts.GLOSS_VOICES["de"]) != tts._cache_path("Ökologie")


def _app_js():
    path = os.path.join(os.path.dirname(__file__), "..", "static", "app.js")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_playback_reads_each_part_in_its_own_language():
    """A chunk is {text, lang} now: the gloss parts are German while the rest
    of the sentence is the reading language, so playback may not fall back to
    one language for the whole list."""
    js = _app_js()
    assert "_ttsUrl(_kTts.chunks[idx].text, _kTts.chunks[idx].lang)" in js
    assert re.search(r"function _kTtsPlayAt[^}]*?const lang = activeLang\(\)", js) is None


def test_mode_is_part_of_the_playback_cache_key():
    """Switching mode changes what the parts are — reusing the old list would
    resume in the middle of a different sentence."""
    js = _app_js()
    m = re.search(r"const key = `\$\{ep\.id\}[^`]*`", js)
    assert m and "_kTtsMode" in m.group(0)

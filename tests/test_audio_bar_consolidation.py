"""Tests for issue #1135: the knowledge-item detail page used to stack three
separate "listen" rows — _kTtsBarHtml (chunked synthetic reading, plain/gloss),
_knowledgeListenBarHtml (build a read-along from the item's own recording,
#1074) and _raBarHtml (the read-along player, #1048) — one under the other,
with two of the three idle buttons both labeled "Listen". #1135 collapses the
idle state of all three into a single _audioBarHtml(): one 🎧 Listen button
plus one "how to listen" dropdown (original / voice / gloss). Active states
(building, erroring, playing, already-built) are still handed off untouched
to the three original functions.

This repo's CI has no JS runtime (no node), so — same idiom as
tests/test_readalong_alignment.py and tests/test_add_word.py — these are
textual guards against static/app.js: they confirm the new entry point
exists and is wired up, and that the old three-parallel-calls shape is gone,
without actually executing any JS.
"""
import os
import pathlib
import re

APP_JS_PATH = pathlib.Path(os.path.dirname(os.path.dirname(__file__))) / "static" / "app.js"


def _app_js() -> str:
    return APP_JS_PATH.read_text(encoding="utf-8")


def _function_body(app_js: str, name: str) -> str:
    """Extract a top-level function's source, from its `function NAME(` line
    up to (and including) the first newline immediately followed by a
    column-0 `}` — the closing brace of a top-level function in this file's
    style (every nested block's closing brace is indented)."""
    m = re.search(r"function %s\(.*?\{" % re.escape(name), app_js, re.S)
    assert m, f"{name}() not found in static/app.js"
    start = m.end()
    end_m = re.search(r"\n\}", app_js[start:])
    assert end_m, f"could not find the end of {name}() in static/app.js"
    return app_js[start:start + end_m.start()]


# ---------------------------------------------------------------------------
# 1. The new entry point exists and is what _renderKnowledgeDetail calls.
# ---------------------------------------------------------------------------

def test_audio_bar_html_exists():
    app_js = _app_js()
    assert "function _audioBarHtml(ep, lang)" in app_js


def test_render_knowledge_detail_calls_audio_bar_html():
    app_js = _app_js()
    render_body = _function_body(app_js, "_renderKnowledgeDetail")
    assert "${_audioBarHtml(ep, lang)}" in render_body


# ---------------------------------------------------------------------------
# 2. The old three-parallel-rows shape is gone from the render template.
# ---------------------------------------------------------------------------

# The exact three lines _renderKnowledgeDetail used to render one under the
# other, before #1135. If this substring is still present anywhere, the
# consolidation didn't actually happen at the call site.
_OLD_TRIPLE_STACK = (
    "${_kTtsBarHtml(ep, lang)}\n"
    "      ${isZh ? _knowledgeListenBarHtml(ep) : ''}\n"
    "      ${_raBarHtml(_raOwnerForEpisode(ep, lang))}"
)


def test_old_three_row_stack_is_gone():
    assert _OLD_TRIPLE_STACK not in _app_js()


def test_render_knowledge_detail_no_longer_calls_the_old_three_directly():
    app_js = _app_js()
    render_body = _function_body(app_js, "_renderKnowledgeDetail")
    # None of the three old bars are invoked directly from the render
    # template anymore — only _audioBarHtml is, and it delegates to them
    # internally once something is actually built or playing.
    assert "_kTtsBarHtml(ep, lang)" not in render_body
    assert "_knowledgeListenBarHtml(" not in render_body
    assert "_raBarHtml(_raOwnerForEpisode(ep, lang))" not in render_body
    # ...but the highlight-map wiring that depends on the owner descriptor
    # must survive untouched (CLAUDE.md: "别的一字节都不许动").
    assert "_raAfterRender(_raOwnerForEpisode(ep, lang));" in render_body


def test_knowledge_listen_bar_html_function_was_removed():
    """Its idle "press to build" branch is superseded by the merged
    dropdown; only the busy/error half survives, renamed _listenBusyBarHtml
    (see next test) — the old function itself should no longer exist."""
    app_js = _app_js()
    assert "function _knowledgeListenBarHtml(" not in app_js


def test_listen_busy_bar_html_still_covers_building_and_error_states():
    app_js = _app_js()
    assert "function _listenBusyBarHtml(ep, lang)" in app_js
    body = _function_body(app_js, "_listenBusyBarHtml")
    assert "_listenBuildingId" in body
    assert "_listenErrors" in body


# ---------------------------------------------------------------------------
# 3. The three modes exist as option values and are all dispatched.
# ---------------------------------------------------------------------------

def test_three_mode_values_present():
    app_js = _app_js()
    assert "'original'" in app_js
    assert "'voice'" in app_js
    assert "'gloss'" in app_js


def test_do_audio_listen_dispatches_all_three_players():
    app_js = _app_js()
    assert "function doAudioListen(episodeId)" in app_js
    body = _function_body(app_js, "doAudioListen")
    assert "doStartListen(" in body        # 'original' -> #1074's own-recording read-along
    assert "doGenerateReadalong(" in body  # 'voice' -> #1048's synthetic read-along
    assert "toggleKnowledgeTts(" in body   # 'gloss' -> the chunked reader


# ---------------------------------------------------------------------------
# 4. New localStorage key, without clobbering the old one.
# ---------------------------------------------------------------------------

def test_new_audio_mode_storage_key_does_not_replace_the_old_tts_mode_key():
    app_js = _app_js()
    assert "knowledgeAudioMode" in app_js
    # knowledgeTtsMode is a different axis (chunked reader's own plain/gloss
    # setting) and must still be read/written independently.
    assert "knowledgeTtsMode" in app_js
    assert "localStorage.getItem('knowledgeAudioMode')" in app_js
    assert "localStorage.setItem('knowledgeAudioMode', value)" in app_js

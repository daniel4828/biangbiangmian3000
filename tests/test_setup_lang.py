"""Story-setup modal resolves the language like the server does (#908).

The aggregating root deck 'All' is lang='zh' in the database yet reviews every
language under the tab bar, so reading its own lang made a French session
render as Chinese: the editable Chinese prompt-template button appeared (edits
there are dead — non-zh generation goes through ai._KNOWLEDGE_PROMPT_NON_ZH,
#806), the difficulty slider read "HSK", and the zh-only modes stayed
selectable until the backend rejected them.

These are static checks on app.js: there is no build step and no JS test
runner in this project, and the bug was a wrong *source* expression, not a
runtime state the Python API can observe.
"""

import pathlib
import re


def _app_js() -> str:
    return pathlib.Path("static/app.js").read_text(encoding="utf-8")


def test_setup_lang_prefers_the_active_tab():
    """Same condition as _langQ(): the lang parameter wins whenever it is sent,
    and the deck's own lang is only the fallback."""
    app_js = _app_js()
    assert (
        "return _availableLangs.length > 1 ? activeLang() : (_deckLangById[deckId] || 'zh');"
        in app_js
    )


def test_setup_modal_does_not_read_the_decks_own_lang():
    """Every story-setup decision must go through setupLang(). A stray
    `_deckLangById[deckId] || 'zh'` is exactly the bug this issue fixed."""
    app_js = _app_js()
    # The single legitimate occurrence is inside setupLang() itself.
    assert len(re.findall(r"_deckLangById\[deckId\]", app_js)) == 1


def test_prompt_editor_button_is_chinese_only():
    """Non-zh decks have no editable template at all (#806), so offering the
    button would show a prompt whose edits do nothing."""
    app_js = _app_js()
    assert "&& setupLang() === 'zh';" in app_js


def test_current_card_lang_still_uses_the_cards_own_deck():
    """#726: the long-press add-word menu follows the card in front of the
    user, not the tab — that one deliberately does NOT use setupLang()."""
    app_js = _app_js()
    assert "return _deckLangById[id] || 'zh';" in app_js

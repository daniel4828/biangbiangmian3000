"""Gemischte Sitzung (All / 🌐): Ladebildschirm hat „Continue in background“ wie die Kategorien."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")


def _func_body(name: str) -> str:
    start = APP_JS.index(f"async function {name}(")
    nxt = APP_JS.index("\n}\n", start)
    return APP_JS[start:nxt]


def test_mixed_start_uses_background_poll_and_button():
    body = _func_body("_doStartReviewMixed")
    assert "_showLoadingBgButton()" in body
    assert "background=true" in body
    assert "_BG_LEFT" in body
    assert "isMixed: true" in body


def test_mixed_start_reopens_running_background_story():
    body = _func_body("startReviewMixed")
    assert "_bgStories[_mixedBgKey(id)]" in body
    assert "_resumeBackgroundReview" in body


def test_resume_handles_mixed_context():
    m = re.search(r"function _resumeBackgroundReview\(ctx\) \{(.*?)\n\}\n", APP_JS, re.S)
    assert m and "ctx.isMixed" in m.group(1) and "_doStartReviewMixed(" in m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_review_header_stays_neutral_after_deck_render():
    start = APP_JS.index("function _applyLangTheme(")
    end = APP_JS.index("function flatten(", start)
    script = APP_JS[start:end] + """
const assert = require('node:assert/strict');
const tabs = { style: {}, innerHTML: '' };
const document = { body: { dataset: {} }, getElementById: () => tabs };
const _availableLangs = ['es', 'zh'];
const _LANG_TAB_LABELS = { es: 'Español', zh: '中文' };
const activeLang = () => 'zh';
let _currentView = 'browse';
let _multiLangSession = false;
_renderHeaderLangTabs();
assert.equal(tabs.style.display, 'flex');
assert.equal(document.body.dataset.lang, 'zh');
_currentView = 'review';
_multiLangSession = true;
// renderDecks can refresh the header directly during an active review.
_renderHeaderLangTabs();
assert.equal(tabs.style.display, 'none');
assert.equal(tabs.innerHTML, '');
assert.equal(document.body.dataset.lang, undefined);
_currentView = 'browse';
_multiLangSession = false;
_renderHeaderLangTabs();
assert.equal(tabs.style.display, 'flex');
assert.equal(document.body.dataset.lang, 'zh');
"""
    subprocess.run(["node", "-e", script], check=True)

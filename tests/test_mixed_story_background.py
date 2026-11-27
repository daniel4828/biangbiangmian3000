"""Gemischte Sitzung (All / 🌐): Ladebildschirm hat „Continue in background“ wie die Kategorien."""
import re
from pathlib import Path

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

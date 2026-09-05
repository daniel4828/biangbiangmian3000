"""Guards for the shared dictionary-result rendering (#1055).

renderResult()/renderOption()/makeStarButton() used to be defined only in
static/dict.html. The header search box needs the exact same rendering, so
they were extracted into static/shared.js as renderDictResult() (#643/#668's
rule: one implementation per feature — a second copy would drift, and a fix
to how an option/example/star button renders would only land in whichever
page someone happened to touch).
"""

import pathlib


def _static(name):
    return pathlib.Path(f"static/{name}").read_text(encoding="utf-8")


def test_dict_result_rendering_lives_only_in_shared_js():
    """The rendering internals must not be redefined in dict.html — that
    would give the feature two implementations to keep in sync.

    dict.html keeps a thin renderResult() wrapper on purpose: it is where the
    page binds its own ↻ Repeat callback and language picker to the shared
    renderer. What must not come back are the parts that actually build the
    DOM."""
    dict_html = _static("dict.html")
    shared_js = _static("shared.js")

    assert "function renderDictResult(" in shared_js
    for fn in ("function renderOption(", "function makeStarButton("):
        assert fn not in dict_html, f"{fn} must be removed from dict.html — use shared.js's renderDictResult()"


def test_dict_html_uses_the_shared_renderer():
    dict_html = _static("dict.html")
    assert "renderDictResult(resultEl" in dict_html
    assert "/static/shared.js" in dict_html
    assert "/static/dict-result.css" in dict_html


def test_dict_result_css_is_scoped_and_self_contained():
    """Every rule must live under .dr-root with dr- prefixed class names —
    otherwise this stylesheet fights static/style.css the moment both are
    loaded on the same page (the whole reason for extracting it, #1055)."""
    css = _static("dict-result.css")
    assert "--dr-bg" in css and "--dr-accent" in css
    assert "@media (prefers-color-scheme: dark)" in css
    # A couple of the generic names that would collide if left unprefixed.
    for bare in (".zh {", ".body {", ".key {", ".option {", ".example {"):
        assert bare not in css, f"unprefixed selector {bare!r} left in dict-result.css"

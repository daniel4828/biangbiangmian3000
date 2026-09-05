"""Guards for the header search box (#1055).

It replaced the header ＋ (#829/#958). The pieces live in three files that
cannot import each other — index.html declares the ids, style.css styles them,
app.js wires them — so a rename in one of them fails silently in the browser
and nowhere else. These checks are the only thing that notices.
"""

import pathlib

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def _static(name):
    return pathlib.Path(f"static/{name}").read_text(encoding="utf-8")


def test_header_holds_the_search_box_and_no_stray_add_button():
    index = _static("index.html")
    assert 'id="header-search-input"' in index
    assert 'id="header-search-panel"' in index
    # Every reference had to go together: a leftover style rule or a
    # getElementById on a removed id is a silent no-op nobody ever notices.
    for name in ("index.html", "app.js", "style.css"):
        assert "header-add-btn" not in _static(name), f"stray header-add-btn in {name}"


def test_add_word_modal_survives_the_swap():
    """＋ moved, it did not disappear: the deck list's nav row and ⌘A still
    open the add-word modal, and the panel keeps a direct add row because the
    header ＋ was the only add-word entry point on a phone (#829/#958)."""
    app_js = _static("app.js")
    assert "function toggleAddWordModal()" in app_js
    assert "function submitAddWord()" in app_js
    assert 'onclick="openAddWordModal()"' in app_js  # nav row on the deck list
    assert "function headerSearchAdd(" in app_js


def test_search_panel_is_wired_and_initialised():
    app_js = _static("app.js")
    for fn in ("function initHeaderSearch()", "function onHeaderSearchInput()",
               "function onHeaderSearchKey(", "function closeHeaderSearchPanel()"):
        assert fn in app_js, fn
    # Declared but never called is exactly the failure this file exists for.
    assert "\ninitHeaderSearch();" in app_js


def test_lookup_reuses_the_shared_renderer_not_a_second_copy():
    """The dictionary result must be drawn by shared.js's renderDictResult —
    the same function /dict uses (#643/#668: one implementation per feature).
    Its ★ buttons are what keeps adding on the single add-word pipeline."""
    app_js = _static("app.js")
    assert "renderDictResult(body, record" in app_js
    assert "/api/dict/lookup" in app_js
    assert "function renderDictResult(" not in app_js, "app.js must not define its own copy"


def test_index_loads_the_shared_dictionary_stylesheet():
    """renderDictResult draws .dr-* classes; without the sheet the popup is
    unstyled text and nothing errors."""
    assert "/static/dict-result.css" in _static("index.html")


def test_dict_result_palette_is_fully_remapped_for_the_light_only_app():
    """dict-result.css ships a dark palette for the theme-aware /dict page;
    this app is light-only. Every one of its variables must be remapped, or
    the ones left out paint dark inside a light popup on a dark device."""
    style = _static("style.css")
    block = style[style.index("#dict-result-body.dr-root"):]
    block = block[:block.index("}")]
    # Read the variable names straight out of the light .dr-root block of the
    # shared sheet, so adding one there fails here instead of in the browser.
    css = _static("dict-result.css")
    light = css[css.index(".dr-root {"):css.index("@media")]
    declared = {
        tok[tok.index("--dr-"):].split(":")[0].strip()
        for tok in light.split(";") if "--dr-" in tok
    }
    missing = sorted(v for v in declared if v not in block)
    assert not missing, f"unmapped dict-result variables: {missing}"


def test_word_search_endpoint_is_reachable():
    r = client.get("/api/word-search", params={"q": ""})
    assert r.status_code == 200
    assert r.json()["words"] == []

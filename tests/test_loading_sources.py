"""Source buttons on the story loading screen (issue #929).

Knowledge mode generates from one or more source items (#752) and takes
minutes. During that wait the loading screen now lists those items; tapping one
opens its summary in a popup and closing it lands back on the loading screen,
generation untouched.

Frontend-only feature, so these are static checks on static/. They guard the
three things that would silently rot: the single shared summary renderer, the
snapshot (not a live read of a Map that gets cleared), and the clearing of the
list when the loading screen is left.
"""
import pathlib

APP_JS = pathlib.Path("static/app.js").read_text(encoding="utf-8")
INDEX_HTML = pathlib.Path("static/index.html").read_text(encoding="utf-8")
STYLE_CSS = pathlib.Path("static/style.css").read_text(encoding="utf-8")


def test_loading_view_has_the_sources_container():
    assert 'id="loading-sources"' in INDEX_HTML
    # Inside the loading view, not floating somewhere else in the page.
    view = INDEX_HTML[INDEX_HTML.index('<div id="view-loading">'):]
    view = view[:view.index('<div id="view-decks">')]
    assert 'id="loading-sources"' in view
    assert "#loading-sources" in STYLE_CSS
    assert ".loading-source-btn" in STYLE_CSS


def test_summary_block_has_exactly_one_renderer():
    """The detail view and the popup must render the same block. A second copy
    would drift the moment one of the zh / rendition branches changes — the
    same single-pipeline reasoning as #643's one add-word entry point."""
    assert APP_JS.count("function _knowledgeSummaryHtml(ep)") == 1
    # Both call sites go through it, and neither rebuilds the branches itself.
    assert APP_JS.count("_knowledgeSummaryHtml(ep)") == 3  # 1 definition + 2 calls
    assert APP_JS.count('id="podcast-summary-rendition"') == 1
    assert APP_JS.count('id="podcast-summary-zh"') == 1


def test_sources_are_snapshotted_not_read_live():
    """_setupSelectedEpisodes is cleared the next time the setup modal opens,
    so the loading screen has to hold its own copy of {id, title, kind}."""
    setup = APP_JS[APP_JS.index("function confirmStorySetup()"):]
    setup = setup[:setup.index("\n}\n")]
    assert "_storyLoadingSources = mode === 'knowledge'" in setup
    assert "Array.from(_setupSelectedEpisodes.values())" in setup
    # Non-knowledge modes must end up with an empty list, never the previous run's.
    assert ": [];" in setup


def test_leaving_the_loading_view_clears_the_sources():
    """Otherwise the buttons of a finished generation reappear on the next
    unrelated setLoading() ("Loading audio…", opening a knowledge item)."""
    show = APP_JS[APP_JS.index("function showView(name)"):]
    show = show[:show.index("\n// Show the loading view.")]
    assert "if (name !== 'loading') _storyLoadingSources = [];" in show


def test_popup_reuses_the_kahneman_modal():
    """Esc / ✕ / the overlay already close that modal, and closing it leaves
    the loading screen exactly as it was — nothing about the run is touched."""
    popup = APP_JS[APP_JS.index("async function openKnowledgeSummaryPopup("):]
    popup = popup[:popup.index("\n}\n")]
    assert "kahneman-examples-modal" in popup
    assert "kahneman-examples-overlay" in popup
    # Titles come from podcast feeds / YouTube / arbitrary web pages.
    assert "titleEl.textContent" in popup
    assert "titleEl.innerHTML" not in popup


def test_source_button_labels_are_textcontent():
    render = APP_JS[APP_JS.index("function _renderLoadingSources()"):]
    render = render[:render.index("\n}\n")]
    assert "btn.textContent" in render
    assert "innerHTML +=" not in render

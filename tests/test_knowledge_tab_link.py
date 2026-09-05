"""Tests for the bookmarkable knowledge-tab links (issue #704).

/knowledge/videos is the browsing counterpart to /add (#668) and /save (#681):
a clean URL for the phone's home screen that lands on one sub-tab. Unlike those
two it is not a standalone page — it redirects into the app's hash route, so
these tests cover both halves: the server redirect and the client-side hash
patterns in app.js that have to recognize the target.
"""
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app)


@pytest.mark.parametrize("path,tab", [
    ("/knowledge/videos", "video"),
    ("/knowledge/video", "video"),
    ("/knowledge/articles", "article"),
    ("/knowledge/article", "article"),
    ("/knowledge/podcasts", "podcast"),
    ("/knowledge/podcast", "podcast"),
    ("/knowledge/VIDEOS", "video"),
    # Reels (#764) are a frontend-only split of kind='video', but the URL
    # has to work like any other tab.
    ("/knowledge/reels", "reel"),
    ("/knowledge/reel", "reel"),
    ("/knowledge/instagram", "reel"),
    ("/knowledge/newsletter", "newsletter"),
    ("/knowledge/newsletters", "newsletter"),
    ("/knowledge/audiobook", "audiobook"),
    ("/knowledge/audiobooks", "audiobook"),
])
def test_tab_link_redirects_to_the_matching_hash(client, path, tab):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/#knowledge-{tab}"


def test_unknown_kind_falls_back_instead_of_404ing(client):
    """Same reasoning as the `day` parameter in #686: reaching the knowledge
    base is the point — a typo in the URL must not turn into a dead end."""
    resp = client.get("/knowledge/videoss", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/#knowledge-podcast"


# ---------------------------------------------------------------------------
# Client side: app.js must recognize what the redirect points at
# ---------------------------------------------------------------------------

APP_JS = pathlib.Path("static/app.js").read_text(encoding="utf-8")

# Every hash pattern app.js tests at boot / in _openKnowledgeFromHash.
HASH_PATTERNS = [
    re.compile(r"^#(?:podcast|knowledge)-feed-\d+$"),
    re.compile(r"^#(?:podcast|knowledge)-\d+$"),
    re.compile(r"^#knowledge-(?:podcast|video|reel|article|newsletter|audiobook)$"),
]


def test_app_js_declares_the_tab_hash_pattern():
    """The boot branch decides between "open the knowledge view" and "load the
    deck list"; if it doesn't know the tab form, a bookmarked tab link opens
    the deck list instead."""
    assert APP_JS.count("#knowledge-(?:podcast|video|reel|article|newsletter|audiobook)$") == 1
    assert "/^#knowledge-(podcast|video|reel|article|newsletter|audiobook)$/" in APP_JS


def test_tab_hash_is_matched_by_exactly_one_pattern():
    """The tab form is letters-only and the item/feed forms are digits-only,
    so an item link can never be mistaken for a tab link or vice versa."""
    for tab in ("podcast", "video", "reel", "article", "newsletter", "audiobook"):
        matched = [p for p in HASH_PATTERNS if p.match(f"#knowledge-{tab}")]
        assert len(matched) == 1, tab
    # The legacy links that already went out in podcast emails/Signal messages.
    for legacy in ("#podcast-12", "#knowledge-12", "#podcast-feed-3", "#knowledge-feed-3"):
        assert len([p for p in HASH_PATTERNS if p.match(legacy)]) == 1, legacy


def test_nothing_in_the_app_writes_a_hash():
    """#792 reversed the #704 contract. The tab bar used to write the tab into
    the address bar so a reload stayed put — but that is exactly what made
    *every* later reload land in the knowledge base instead of the home screen,
    since the hash outlived the visit. Navigation inside the app now leaves the
    address bar alone; the sub-tab is remembered in localStorage instead, and
    the bookmarkable entry points (/knowledge/videos, notification links) are
    server-generated, so they do not depend on the frontend writing anything."""
    assert "location.hash =" not in APP_JS


def test_boot_consumes_the_hash_so_later_reloads_go_home():
    """A direct link is a one-shot instruction to open one item, not a sticky
    location: after acting on it the boot code must clear it, otherwise the
    next reload reopens the same thing instead of the deck list (#792)."""
    boot = APP_JS[APP_JS.index("// \u2500\u2500 Boot"):]
    boot = boot[:boot.index("_loadVersionBadge();")]
    assert "_openKnowledgeFromHash();" in boot
    # The clear has to sit in the branch that consumed the hash, before the
    # else-branch that just loads the decks.
    # history.state, not null (#1057): boot's replaceState has to KEEP the
    # navIdx set a few lines above it — passing null there would wipe the nav
    # history index on every load with a hash. The assertion below tracked the
    # old literal and had been failing on main ever since.
    clear_call = "history.replaceState(history.state, '', location.pathname + location.search);"
    assert clear_call in boot
    # Match the FULL call, not the bare "history.replaceState" prefix: the boot
    # block opens with a different replaceState (the navIdx seed, #1002) that
    # sits before _openKnowledgeFromHash(), so a prefix search finds that one
    # and the ordering assertions below compare the wrong two positions.
    assert boot.index("_openKnowledgeFromHash();") < boot.index(clear_call)
    assert boot.index(clear_call) < boot.index("loadDecks();")

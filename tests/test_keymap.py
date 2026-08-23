"""Tests for the expanded, scoped keymap system (#856).

Pure text checks against static/app.js — no browser involved, same style as
tests/test_add_word.py and tests/test_anki_day_semantics.py. The goal is to
catch two classes of regression cheaply:

1. An action gets added to KEYMAP_ACTIONS but nobody wires it up to the
   keydown handler with _key('<id>') — the settings page would then let
   Daniel "rebind" a shortcut that does nothing.
2. A shortcut regresses back to a hardcoded key literal (e.g. `e.key === 'w'`)
   instead of going through _key(...) — silently making it unconfigurable
   again and out of sync with the settings page.
"""

import pathlib
import re

APP_JS = pathlib.Path("static/app.js").read_text(encoding="utf-8")


def _extract(pattern, text=APP_JS):
    m = re.search(pattern, text, re.DOTALL)
    assert m, f"pattern not found: {pattern!r}"
    return m.group(0)


def _keymap_actions_block():
    return _extract(r"const KEYMAP_ACTIONS = \[.*?\n\];")


def _keymap_defaults_block():
    return _extract(r"const KEYMAP_DEFAULTS = \{.*?\n\};")


def _action_ids():
    block = _keymap_actions_block()
    return re.findall(r"\{\s*id:\s*'([^']+)'", block)


def _keydown_handler_body():
    """The big global `document.addEventListener('keydown', async e => { ... })`
    handler (~line 10951). There's exactly one line that is *only* `});` inside
    it (verified by hand against the current file), so cutting at the first
    occurrence of a line containing just that is safe and doesn't require a
    real JS parser."""
    start_marker = "document.addEventListener('keydown', async e => {"
    start = APP_JS.index(start_marker)
    end_marker = "\n});\n"
    end = APP_JS.index(end_marker, start)
    return APP_JS[start:end]


def test_every_keymap_action_is_wired_into_the_keydown_handler():
    """Every action id declared in KEYMAP_ACTIONS must be read via _key('<id>')
    somewhere in the global keydown handler (or the story-modal branch it
    contains) — otherwise adding it to the settings page is a lie: rebinding
    it would have no effect."""
    body = _keydown_handler_body()
    ids = _action_ids()
    assert len(ids) >= 26, "expected the #856 expansion to have ~26+ actions"
    missing = [i for i in ids if f"_key('{i}')" not in body]
    assert not missing, f"actions not read via _key(...) in the keydown handler: {missing}"


# Keys/comparisons that are allowed to stay hardcoded in the keydown handler:
# rating keys, fixed editing/navigation keys, arrow keys (book reader), and
# anything gated behind a Ctrl/Cmd/Alt modifier (those are explicitly kept
# hardcoded per #856 — Cmd+I, Cmd+A, Cmd+Enter, Alt+L, book reader arrows).
_ALLOWED_LITERAL_KEYS = {"1", "2", "3", "4", "Enter", "Tab", "Escape", "ArrowLeft", "ArrowRight"}


def test_keydown_handler_has_no_stray_hardcoded_review_shortcut_literals():
    """Regression guard: a shortcut that regresses from _key('id') back to a
    bare `e.key === 'x'` literal becomes unconfigurable again and silently
    falls out of sync with the settings page / KEYMAP_DEFAULTS. This scans for
    `e.key === '<single char>'` / `e.code === 'Key<X>'` comparisons that
    aren't on the small whitelist above, and aren't guarded by a modifier key
    check on the same line (Ctrl/Cmd/Alt-gated combos are intentionally left
    hardcoded, e.g. Cmd+I, Cmd+A, Alt+L).
    """
    body = _keydown_handler_body()

    offenders = []
    for m in re.finditer(r"e\.key === '([^']+)'", body):
        key = m.group(1)
        if key in _ALLOWED_LITERAL_KEYS:
            continue
        # Look at the rest of the line (and a little context before it) for a
        # modifier-key guard — those are the intentionally-hardcoded combos.
        line_start = body.rfind("\n", 0, m.start()) + 1
        line_end = body.find("\n", m.end())
        line = body[line_start:line_end if line_end != -1 else len(body)]
        if "metaKey" in line or "ctrlKey" in line or "altKey" in line:
            continue
        offenders.append(key)

    assert not offenders, (
        f"hardcoded e.key literals found outside the allowed set: {offenders} — "
        "these should read from _key('<action-id>') instead so they're configurable"
    )

    # e.code === 'KeyX' comparisons: only the Ctrl/Cmd/Alt-gated ones (Cmd+I,
    # Cmd+A, Alt+L) may remain — everything review/nav/story-related must have
    # moved to e.key + _key(...).
    for m in re.finditer(r"code === 'Key([A-Z])'", body):
        line_start = body.rfind("\n", 0, m.start()) + 1
        line_end = body.find("\n", m.end())
        line = body[line_start:line_end if line_end != -1 else len(body)]
        assert "metaKey" in line or "ctrlKey" in line or "altKey" in line, (
            f"unguarded e.code === 'Key{m.group(1)}' comparison found — "
            "should be e.key + _key('<action-id>') instead"
        )


def test_keymap_reserved_and_defaults_do_not_overlap():
    """KEYMAP_RESERVED keys (rating 1-4, Enter, Tab, Escape) must never also be
    a default binding for some action — that would make the action
    permanently unrebindable-away-from and reject-on-load inconsistent."""
    reserved = set(re.findall(r"'([^']+)'", _extract(r"const KEYMAP_RESERVED = \[.*?\];")))
    defaults_block = _keymap_defaults_block()
    # Values are the part after each `key:` or `'key':` — grab everything that
    # looks like a JS object value string literal on the right of a colon.
    default_keys = set(re.findall(r":\s*'([^']*)'", defaults_block))
    overlap = reserved & default_keys
    assert not overlap, f"KEYMAP_RESERVED overlaps with KEYMAP_DEFAULTS values: {overlap}"


def test_keymap_actions_all_declare_a_known_scope():
    """Every action must declare one of the scopes KEYMAP_SCOPES knows about,
    otherwise _scopeSet() silently falls back to a single-item set built from
    the (nonexistent) scope name and conflict detection goes quiet instead of
    catching real overlaps."""
    scopes_block = _extract(r"const KEYMAP_SCOPES = \{.*?\n\};")
    known_scopes = set(re.findall(r"^\s*(?:'([^']+)'|(\w[\w-]*)):", scopes_block, re.MULTILINE))
    known_scopes = {a or b for a, b in known_scopes}
    assert known_scopes, "could not parse KEYMAP_SCOPES keys"

    actions_block = _keymap_actions_block()
    action_scopes = set(re.findall(r"scope:\s*'([^']+)'", actions_block))
    unknown = action_scopes - known_scopes
    assert not unknown, f"KEYMAP_ACTIONS reference unknown scopes: {unknown}"


def test_global_scope_excludes_story():
    """#856: `global`'s scope set must NOT include 'story' — the story-modal
    keydown branch runs first and returns early whenever the modal is open, so
    nav-back/story-next sharing the default key 'd' is fine in practice, but
    only because global doesn't claim to cover the story context too."""
    scopes_block = _extract(r"const KEYMAP_SCOPES = \{.*?\n\};")
    global_line = _extract(r"global:\s*\[[^\]]*\]", scopes_block)
    assert "'story'" not in global_line

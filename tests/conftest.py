"""Shared pytest setup (issue #615).

The safety net here exists because a wrong monkeypatch is invisible: for a long
time two test modules patched `database.DB_PATH` instead of
`database.core.DB_PATH`. `database/__init__.py` does `from .core import *`, so
the package-level name is only a copy — get_db() kept connecting to the real
data/srs.db, and the tests quietly wrote to it. The symptom was a confusing
`UNIQUE constraint failed: decks.name` from leftover rows, not an obvious
"you are writing to production" error.

Pointing DB_PATH at a throwaway file before any test imports a database module
means a missing patch can, at worst, produce a stray file in a temp directory.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Must be set before database.core is imported: it reads DB_PATH at import time.
_FALLBACK_DB = os.path.join(tempfile.mkdtemp(prefix="biangbiangmian3000-tests-"), "fallback.db")
os.environ["DB_PATH"] = _FALLBACK_DB

# DISABLE_AI is deliberately NOT set here: routes.utils reads it at import time,
# and tests like test_api's story suite mock the AI layer and assert it was
# called. Modules that want it (test_offline_sync, test_prompt_templates) set it
# themselves before importing.


import pytest


def pytest_configure(config):
    # #1054: test_youtube_audiobook.py's autouse "no real metadata/download"
    # fixture stubs knowledge.youtube.fetch_duration/download_audio as
    # tripwires — but the tests exercising those two functions directly need
    # to opt out of that tripwire (they stub subprocess.run instead, one
    # level lower). Registering the marker here avoids a
    # PytestUnknownMarkWarning on every one of those tests.
    config.addinivalue_line(
        "markers",
        "real_youtube_calls: test exercises knowledge.youtube's own "
        "fetch_duration/download_audio (stubs subprocess.run instead) and "
        "must skip the autouse tripwire that stubs those functions themselves",
    )


@pytest.fixture(autouse=True)
def _no_real_tts(monkeypatch, tmp_path_factory):
    """Never let a test open an edge-tts connection.

    Once the DB isolation above was fixed, the story endpoint tests started
    actually reaching the TTS preload step — one test file went from instant to
    85 seconds of real network calls, and it would fail outright on a machine
    with no internet. Audio generation is not what any of these tests are
    checking, so stub it at the single choke point every path goes through.
    """
    import tts

    cache = tmp_path_factory.mktemp("tts")

    async def _fake_ensure_cached(text, voice=tts.VOICE):
        path = cache / f"{abs(hash((text, voice)))}.mp3"
        path.write_bytes(b"")
        return str(path)

    monkeypatch.setattr(tts, "_ensure_cached", _fake_ensure_cached)

"""#1265: a Google door that hangs must fail fast, enter cooldown, and let the
fallback doors run."""
import socket

import pytest

import translator


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(translator, "_cooldowns", {})
    monkeypatch.setattr(translator, "_translators", {})


def test_hanging_google_door_falls_through_to_deepseek_and_cools_down(monkeypatch):
    import ai

    def hang(req, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setattr(ai, "_call_api", lambda *a, **kw: "Bonjour")
    monkeypatch.setattr(translator.urllib.request, "urlopen", hang)

    t = translator._load("de", "fr")
    assert t.translate("Hallo") == "Bonjour"

    left, reason = translator._cooldown_left("google-mobile")
    assert left > 0 and "timed out" in reason


def test_walk_deadline_covers_two_google_timeouts():
    assert translator._WALK_DEADLINE_SECONDS > 2 * translator._REQUEST_TIMEOUT_SECONDS

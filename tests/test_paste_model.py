"""Paste mode honours the model dropdown (#910).

Paste shares the briefing pipeline but not its reason for being OpenAI-only —
DeepSeek censors *news*, and pasted material is whatever went into the box, so
the lock was inherited, not justified (knowledge mode made the same move in
#561/#640). What must NOT change: briefing stays locked to BRIEFING_MODEL, and
paste still *defaults* to it, since that is the configuration this pipeline is
verified on.
"""

import pathlib
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from routes import story as story_routes


# ---------------------------------------------------------------------------
# _paste_model — the per-request resolution
# ---------------------------------------------------------------------------

class TestPasteModel:

    def test_honours_an_allowed_model(self):
        """The whole point of the issue: the dropdown's pick is used as-is, and
        BRIEFING_MODEL is never even resolved (that call probes the API)."""
        with patch.object(story_routes.ai, "resolve_briefing_model") as resolve:
            assert story_routes._paste_model("deepseek-v4-flash") == "deepseek-v4-flash"
        resolve.assert_not_called()

    def test_sentinel_resolves_briefing_model(self):
        with patch.object(story_routes.ai, "resolve_briefing_model",
                          return_value="gpt-5.6-luna") as resolve:
            assert story_routes._paste_model(story_routes.SERVER_MODEL_SENTINEL) == "gpt-5.6-luna"
        resolve.assert_called_once()

    def test_missing_model_resolves_briefing_model(self):
        """Old stories and iOS shortcuts send no model at all."""
        with patch.object(story_routes.ai, "resolve_briefing_model",
                          return_value="gpt-5.6-luna"):
            assert story_routes._paste_model(None) == "gpt-5.6-luna"

    def test_unknown_model_falls_back_to_briefing_model_and_warns(self, caplog):
        """#721's lesson: a dropdown value missing from ALLOWED_MODELS must be
        loud, not silently swapped for something else."""
        with patch.object(story_routes.ai, "resolve_briefing_model",
                          return_value="gpt-5.6-luna"):
            with caplog.at_level("WARNING"):
                assert story_routes._paste_model("gpt-9-imaginary") == "gpt-5.6-luna"
        assert "gpt-9-imaginary" in caplog.text

    def test_sentinel_is_not_a_model(self):
        """It must never pass validation as if it were one."""
        assert story_routes.SERVER_MODEL_SENTINEL not in story_routes.ALLOWED_MODELS


# ---------------------------------------------------------------------------
# _requested_model — surviving the routes' up-front validation
# ---------------------------------------------------------------------------

class TestRequestedModel:

    def test_sentinel_survives_for_paste_and_briefing(self):
        """The routes validate before _generate_and_store; _validated_model would
        turn the sentinel into DEFAULT_MODEL here and paste would silently run on
        DeepSeek instead of BRIEFING_MODEL."""
        for mode in ("paste", "briefing"):
            assert story_routes._requested_model(
                story_routes.SERVER_MODEL_SENTINEL, mode) == story_routes.SERVER_MODEL_SENTINEL

    def test_sentinel_is_rejected_for_every_other_mode(self):
        """No other branch knows how to read it, so it must not get through as a
        model name."""
        for mode in ("story", "qa", "expository", "kahneman", "knowledge", "book"):
            assert story_routes._requested_model(
                story_routes.SERVER_MODEL_SENTINEL, mode) != story_routes.SERVER_MODEL_SENTINEL

    def test_real_models_pass_through_for_paste(self):
        assert story_routes._requested_model("gpt-5-mini", "paste") == "gpt-5-mini"


# ---------------------------------------------------------------------------
# Frontend — static checks (no build step, no JS test runner)
# ---------------------------------------------------------------------------

def _app_js() -> str:
    return pathlib.Path("static/app.js").read_text(encoding="utf-8")


def test_frontend_sentinel_matches_the_backend():
    """Two spellings of this value would fail the way #721 did: the UI would
    look right and the server would quietly use another model."""
    assert f"const SERVER_MODEL_VALUE = '{story_routes.SERVER_MODEL_SENTINEL}';" in _app_js()


def test_only_briefing_disables_the_model_select():
    """Paste must be reachable; briefing must stay locked."""
    app_js = _app_js()
    assert "if (mode === 'briefing') {" in app_js
    assert "modelSel.disabled = true;" in app_js
    # The old lock covered both modes — that condition must be gone.
    assert "if (mode === 'briefing' || mode === 'paste') {" not in app_js


def test_paste_defaults_to_the_server_placeholder():
    assert "paste: SERVER_MODEL_VALUE," in _app_js()

"""Tests für GPT über das Codex-Abo (#1582)."""
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

import ai
import codex_sub
import database
import main
import database.stats

client = TestClient(main.app)
MSGS = [{"role": "user", "content": "x"}]


class _FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def _boom(*a, **k):
    raise AssertionError("must not be called")


@pytest.fixture(autouse=True)
def _db():
    database.init_db()
    database.set_app_setting("gpt_backend", "api")
    yield
    database.set_app_setting("gpt_backend", "api")


def test_build_args_pins_model():
    args = codex_sub.build_args("codex", "/tmp", "/tmp/o", "gpt-6-luna", "low")
    i = args.index("-m")
    assert args[i + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="low"' in args
    for bad in ("", "gpt;rm"):
        with pytest.raises(ValueError):
            codex_sub.build_args("codex", "/tmp", "/tmp/o", bad, "low")


def test_default_backend_is_api(monkeypatch):
    assert ai.gpt_backend() == "api"
    monkeypatch.setattr(codex_sub, "complete", _boom)
    monkeypatch.setattr(ai, "_openai_client", lambda m: _FakeOpenAI())
    assert ai._call_api("gpt-6-luna", MSGS, 100, "test") == "hi"


def test_subscription_uses_codex(monkeypatch):
    database.set_app_setting("gpt_backend", "subscription")
    calls = []

    def fake(model, messages, thinking=False, timeout=300):
        calls.append(model)
        return "from codex", {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 3}

    monkeypatch.setattr(codex_sub, "complete", fake)
    monkeypatch.setattr(ai, "_openai_client", _boom)
    assert ai._call_api("gpt-6-luna", MSGS, 100, "test") == "from codex"
    assert calls == ["gpt-6-luna"]
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM api_call_log WHERE model = ?", ("codex:gpt-6-luna",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_subscription_failure_falls_back(monkeypatch):
    database.set_app_setting("gpt_backend", "subscription")

    def fail(*a, **k):
        raise codex_sub.CodexError("boom")

    monkeypatch.setattr(codex_sub, "complete", fail)
    monkeypatch.setattr(ai, "_openai_client", lambda m: _FakeOpenAI())
    assert ai._call_api("gpt-6-luna", MSGS, 100, "test") == "hi"


def test_subscription_ignores_non_gpt(monkeypatch):
    database.set_app_setting("gpt_backend", "subscription")
    monkeypatch.setattr(codex_sub, "complete", _boom)
    monkeypatch.setattr(ai, "_openai_client", lambda m: _FakeOpenAI())
    assert ai._call_api("deepseek-v4-flash", MSGS, 100, "test") == "hi"


def test_stream_subscription(monkeypatch):
    database.set_app_setting("gpt_backend", "subscription")
    monkeypatch.setattr(codex_sub, "complete",
                        lambda *a, **k: ("from codex", {"input_tokens": 1, "cached_input_tokens": 0,
                                                        "output_tokens": 1}))
    assert list(ai._stream_api("gpt-6-luna", MSGS, 100, "test")) == ["from codex"]


def test_codex_pricing_is_zero():
    assert database.stats._lookup_pricing("codex:gpt-6-luna") == {
        "input": 0.0, "cached": 0.0, "output": 0.0}


def test_route_get_put(monkeypatch):
    monkeypatch.setattr(codex_sub, "login_status",
                        lambda: {"installed": True, "logged_in": True, "detail": "ok"})
    r = client.get("/api/ai-backend")
    assert r.status_code == 200
    assert r.json()["gpt_backend"] == "api"
    r = client.put("/api/ai-backend", json={"gpt_backend": "subscription"})
    assert r.status_code == 200
    assert database.get_app_setting("gpt_backend", "api") == "subscription"
    assert client.put("/api/ai-backend", json={"gpt_backend": "astra"}).status_code == 400

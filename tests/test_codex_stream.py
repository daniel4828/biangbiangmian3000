"""Tests für echtes Streaming über `codex app-server` (#1588). Kein echter Prozess, kein Netz."""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

import ai
import codex_sub
import database

MSGS = [{"role": "user", "content": "Hallo Welt"}]


class _FakeStdin:
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def messages(self):
        return [json.loads(l) for l in self.lines if l.strip()]


class _FakeProc:
    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = iter(l + "\n" for l in lines)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _j(**kw):
    return json.dumps(kw)


def _script(turn_status="completed", deltas=("Hal", "lo"), extra=()):
    lines = [
        _j(id=1, result={}),
        "not json at all",
        _j(method="mcpServer/startupStatus/updated", params={}),
        _j(id=2, result={"thread": {"id": "T1"}}),
        _j(id=3, result={}),
    ]
    lines += list(extra)
    for d in deltas:
        lines.append(_j(method="item/agentMessage/delta", params={"delta": d}))
    lines.append(_j(method="thread/tokenUsage/updated", params={"tokenUsage": {"last": {
        "inputTokens": 7, "cachedInputTokens": 2, "outputTokens": 4}}}))
    lines.append(_j(method="turn/completed", params={"turn": {
        "status": turn_status, "error": {"message": "kaputt"} if turn_status != "completed" else None}}))
    return lines


@pytest.fixture
def popen(monkeypatch):
    holder = {}

    def install(lines):
        proc = _FakeProc(lines)
        holder["proc"] = proc
        monkeypatch.setattr(codex_sub.subprocess, "Popen", lambda *a, **k: proc)
        holder["args"] = None
        return proc

    monkeypatch.setattr(codex_sub, "codex_bin", lambda: "/fake/codex")
    return install


def test_build_app_server_args():
    args = codex_sub.build_app_server_args("codex")
    assert args[:2] == ["codex", "app-server"]
    for f in codex_sub.CODEX_DISABLED_FEATURES:
        i = args.index(f)
        assert args[i - 1] == "--disable"
    assert "mcp_servers={}" in args


def test_invalid_model_raises_before_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(codex_sub.subprocess, "Popen", boom)
    with pytest.raises(ValueError):
        list(codex_sub.stream("gpt;rm", MSGS))


def test_stream_ok(popen):
    proc = popen(_script())
    usage = {}
    out = list(codex_sub.stream("gpt-6-luna", MSGS, usage_out=usage))
    assert out == ["Hal", "lo"]
    assert usage == {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 4}
    sent = proc.stdin.messages()
    methods = [m.get("method") for m in sent]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start"]
    assert sent[2]["params"]["model"] == "gpt-6-luna"
    turn = sent[3]["params"]
    assert turn["threadId"] == "T1"
    assert "Hallo Welt" in turn["input"][0]["text"]
    assert turn["effort"] == "low"
    assert proc.killed and proc.stdin.closed


def test_stream_turn_failed(popen):
    popen(_script(turn_status="failed"))
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


def test_stream_server_request(popen):
    popen(_script(extra=[_j(id=99, method="item/commandExecution/requestApproval", params={})]))
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


def test_stream_jsonrpc_error(popen):
    popen([_j(id=1, error={"code": -1, "message": "nope"})])
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


def test_stream_eof_before_completed(popen):
    popen(_script()[:-1])
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


def test_stream_zero_deltas(popen):
    popen(_script(deltas=()))
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


def test_stream_error_notification_no_retry(popen):
    popen(_script(extra=[_j(method="error", params={"error": {"message": "x"}, "willRetry": False})]))
    with pytest.raises(codex_sub.CodexError):
        list(codex_sub.stream("gpt-6-luna", MSGS))


# --- ai._stream_api im Abo-Modus -------------------------------------------

@pytest.fixture
def sub_mode(monkeypatch):
    database.init_db()
    database.set_app_setting("gpt_backend", "subscription")
    yield
    database.set_app_setting("gpt_backend", "api")


class _FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        def chunk(c):
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content=c), finish_reason=None)])
        return iter([chunk("api-"), chunk("antwort")])


def test_stream_api_uses_codex_stream(sub_mode, monkeypatch):
    def fake(model, messages, thinking=False, timeout=300, usage_out=None):
        usage_out.update({"input_tokens": 3, "cached_input_tokens": 1, "output_tokens": 2})
        yield "a"
        yield "b"

    logged = []
    monkeypatch.setattr(codex_sub, "stream", fake)
    monkeypatch.setattr(ai, "_openai_client", lambda m: (_ for _ in ()).throw(AssertionError("no API")))
    monkeypatch.setattr(database, "log_api_call", lambda **kw: logged.append(kw))
    assert list(ai._stream_api("gpt-6-luna", MSGS, 100, "test")) == ["a", "b"]
    assert len(logged) == 1
    assert logged[0]["model"] == "codex:gpt-6-luna"
    assert logged[0]["response"] == "ab"
    assert logged[0]["input_tokens"] == 3


def test_stream_api_falls_back_when_codex_fails_early(sub_mode, monkeypatch):
    def fail(*a, **k):
        raise codex_sub.CodexError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(codex_sub, "stream", fail)
    monkeypatch.setattr(ai, "_openai_client", lambda m: _FakeOpenAI())
    monkeypatch.setattr(database, "log_api_call", lambda **kw: None)
    assert "".join(ai._stream_api("gpt-6-luna", MSGS, 100, "test")) == "api-antwort"


def test_stream_api_reraises_after_first_delta(sub_mode, monkeypatch):
    def half(*a, **k):
        yield "a"
        raise codex_sub.CodexError("mitten drin")

    monkeypatch.setattr(codex_sub, "stream", half)
    monkeypatch.setattr(ai, "_openai_client", lambda m: (_ for _ in ()).throw(AssertionError("no API")))
    gen = ai._stream_api("gpt-6-luna", MSGS, 100, "test")
    assert next(gen) == "a"
    with pytest.raises(codex_sub.CodexError):
        next(gen)

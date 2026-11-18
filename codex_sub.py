"""GPT über das ChatGPT/Codex-Abo statt der bezahlten OpenAI-API (#1582).

Ruft die `codex exec`-CLI auf. 🔴 `-m <model>` wird IMMER explizit übergeben:
Codex' eigenes Standardmodell ist extrem teuer und darf nie greifen. Lehnt Codex
das Modell ab, ist das ein Fehler — kein Wiederholen ohne `-m`.
"""
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Alles, was Codex Werkzeuge oder Zugriff nach außen gäbe, wird abgeschaltet.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "remote_plugin",
    "multi_agent", "browser_use", "browser_use_external", "in_app_browser", "computer_use",
    "view_image", "image_generation", "hooks", "skill_search", "tool_suggest", "goals",
    "sleep_tool", "memories",
)

_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PROD_BIN = "/opt/node26/bin/codex"


class CodexError(Exception):
    pass


def codex_bin() -> str | None:
    env = os.environ.get("CODEX_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("codex")
    if found:
        return found
    if os.path.exists(_PROD_BIN):
        return _PROD_BIN
    return None


def _env(bin_path: str) -> dict:
    """codex ist ein Node-Skript; `node` liegt neben ihm."""
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(bin_path) + os.pathsep + env.get("PATH", "")
    return env


def build_args(bin_path: str, cwd: str, out_file: str, model: str, effort: str) -> list:
    if not model or not _SAFE_RE.match(model):
        raise ValueError(f"invalid codex model: {model!r}")
    if not effort or not _SAFE_RE.match(effort):
        raise ValueError(f"invalid codex effort: {effort!r}")
    args = [bin_path, "exec"]
    for f in CODEX_DISABLED_FEATURES:
        args += ["--disable", f]
    args += [
        "--sandbox", "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color", "never",
        "--json",
        "-C", cwd,
        "-o", out_file,
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-",
    ]
    return args


def parse_usage(stdout: str | None) -> dict:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            return {k: int(u.get(k) or 0)
                    for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "\n".join(out)
    return str(content or "")


def _build_prompt(messages: list) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "user")
        text = _content_text(m.get("content", ""))
        if role == "system":
            parts.append(f"<SYSTEM>\n{text}\n</SYSTEM>")
        elif role == "assistant":
            parts.append(f"[assistant]\n{text}")
        else:
            parts.append(text)
    return "Du hast keine Werkzeuge. Antworte direkt.\n\n" + "\n\n".join(parts)


def complete(model: str, messages: list, thinking: bool = False,
             timeout: float = 300) -> tuple[str, dict]:
    bin_path = codex_bin()
    if not bin_path:
        raise CodexError("codex CLI not found")
    effort = "medium" if thinking else "low"
    prompt = _build_prompt(messages)
    with tempfile.TemporaryDirectory(prefix="bbm-codex-") as cwd:
        out_file = os.path.join(cwd, "answer.txt")
        args = build_args(bin_path, cwd, out_file, model, effort)
        try:
            proc = subprocess.run(args, input=prompt, cwd=cwd, capture_output=True,
                                  text=True, timeout=timeout, env=_env(bin_path))
        except subprocess.TimeoutExpired:
            raise CodexError(f"codex timed out after {timeout:.0f}s")
        except OSError as e:
            raise CodexError(f"codex could not start: {e}")
        if proc.returncode != 0:
            raise CodexError(f"codex exit {proc.returncode}: {(proc.stderr or '')[-400:]}")
        try:
            with open(out_file, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        if not text:
            raise CodexError(f"codex returned no text: {(proc.stderr or '')[-400:]}")
        return text, parse_usage(proc.stdout)


def build_app_server_args(bin_path: str) -> list:
    """`codex app-server` (stdio JSON-RPC). Das Modell wird in `thread/start`
    übergeben (🔴 immer explizit). app-server kennt kein --ignore-user-config,
    darum werden MCP-Server des Nutzers per -c geleert."""
    args = [bin_path, "app-server"]
    for f in CODEX_DISABLED_FEATURES:
        args += ["--disable", f]
    args += ["-c", "mcp_servers={}"]
    return args


_EOF = object()


def _send(proc, obj: dict) -> None:
    try:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as e:
        raise CodexError(f"codex stdin write failed: {e}")


def stream(model: str, messages: list, thinking: bool = False, timeout: float = 300,
           usage_out: dict | None = None):
    """Echtes Streaming über `codex app-server`: yieldet Text-Deltas.

    Wirft CodexError bei jedem Fehler (auch leere Antwort). Beim Abbruch durch
    den Konsumenten wird der Prozess aufgeräumt."""
    if not model or not _SAFE_RE.match(model):
        raise ValueError(f"invalid codex model: {model!r}")
    effort = "medium" if thinking else "low"
    if not _SAFE_RE.match(effort):
        raise ValueError(f"invalid codex effort: {effort!r}")
    bin_path = codex_bin()
    if not bin_path:
        raise CodexError("codex CLI not found")
    prompt = _build_prompt(messages)
    args = build_app_server_args(bin_path)
    deadline = time.monotonic() + timeout
    tmp = tempfile.TemporaryDirectory(prefix="bbm-codex-")
    proc = None
    try:
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                    cwd=tmp.name, env=_env(bin_path))
        except OSError as e:
            raise CodexError(f"codex could not start: {e}")
        q: queue.Queue = queue.Queue()

        def _reader(stdout=proc.stdout):
            try:
                for line in stdout:
                    q.put(line)
            except (OSError, ValueError):
                pass
            q.put(_EOF)

        threading.Thread(target=_reader, daemon=True).start()

        _send(proc, {"id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "biangbiangmian3000", "version": "1"}}})
        tid = None
        deltas = 0
        last_usage: dict = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError(f"codex timed out after {timeout:.0f}s")
            try:
                line = q.get(timeout=remaining)
            except queue.Empty:
                raise CodexError(f"codex timed out after {timeout:.0f}s")
            if line is _EOF:
                raise CodexError("codex exited before the turn completed")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and "method" in msg:
                raise CodexError(f"codex server request refused: {msg.get('method')}")
            if "error" in msg and "method" not in msg:
                err = msg["error"]
                detail = err.get("message") if isinstance(err, dict) else err
                raise CodexError(f"codex JSON-RPC error: {detail}")
            method = msg.get("method")
            params = msg.get("params") or {}
            if method is None:
                rid = msg.get("id")
                result = msg.get("result") or {}
                if rid == 1:
                    _send(proc, {"method": "initialized"})
                    _send(proc, {"id": 2, "method": "thread/start", "params": {
                        "model": model, "ephemeral": True, "sandbox": "read-only",
                        "approvalPolicy": "never", "cwd": tmp.name}})
                elif rid == 2:
                    tid = (result.get("thread") or {}).get("id")
                    if not tid:
                        raise CodexError("codex thread/start returned no thread id")
                    _send(proc, {"id": 3, "method": "turn/start", "params": {
                        "threadId": tid, "input": [{"type": "text", "text": prompt}],
                        "effort": effort}})
                continue
            if method == "item/agentMessage/delta":
                d = params.get("delta")
                if isinstance(d, str) and d:
                    deltas += 1
                    yield d
            elif method == "thread/tokenUsage/updated":
                last = (params.get("tokenUsage") or {}).get("last")
                if isinstance(last, dict):
                    last_usage = last
            elif method == "error":
                err = params.get("error") or {}
                if not params.get("willRetry"):
                    raise CodexError(f"codex error: {err.get('message') if isinstance(err, dict) else err}")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    e = turn.get("error")
                    detail = e.get("message") if isinstance(e, dict) else e
                    raise CodexError(f"codex turn {turn.get('status')}: {detail}")
                break
        if deltas == 0:
            raise CodexError("codex returned no text")
        if usage_out is not None:
            usage_out.update({
                "input_tokens": int(last_usage.get("inputTokens") or 0),
                "cached_input_tokens": int(last_usage.get("cachedInputTokens") or 0),
                "output_tokens": int(last_usage.get("outputTokens") or 0),
            })
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            tmp.cleanup()
        except OSError:
            pass


def login_status() -> dict:
    bin_path = codex_bin()
    if not bin_path:
        return {"installed": False, "logged_in": False, "detail": "codex CLI not found"}
    try:
        proc = subprocess.run([bin_path, "login", "status"], capture_output=True, text=True,
                              timeout=15, env=_env(bin_path))
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"installed": True, "logged_in": False, "detail": str(e)}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    first = out.splitlines()[0].strip() if out else ""
    return {"installed": True, "logged_in": proc.returncode == 0, "detail": first}

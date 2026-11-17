"""GPT über das ChatGPT/Codex-Abo statt der bezahlten OpenAI-API (#1582).

Ruft die `codex exec`-CLI auf. 🔴 `-m <model>` wird IMMER explizit übergeben:
Codex' eigenes Standardmodell ist extrem teuer und darf nie greifen. Lehnt Codex
das Modell ab, ist das ein Fehler — kein Wiederholen ohne `-m`.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

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

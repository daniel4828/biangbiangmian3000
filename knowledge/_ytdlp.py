"""Shared yt-dlp subprocess plumbing (issue #1054).

knowledge/instagram.py (#750) and knowledge/youtube.py's audiobook download
(#1054) both shell out to the same system-level yt-dlp binary and need the
same "missing binary / timed out / non-zero exit" handling. This module is
the one place that knows how to invoke it, so a fix to that handling (or a
future flag every caller needs) lands once instead of twice.

Deliberately generic: each caller passes its own exception class so a
failure still raises the error type its own callers already catch
(InstagramError, knowledge.youtube's own error class, ...) rather than a
shared type nobody expects.
"""
from __future__ import annotations

import os
import subprocess


def yt_dlp_path() -> str:
    return os.environ.get("YT_DLP_PATH", "yt-dlp")


def run_yt_dlp(cmd: list[str], timeout: int, action: str, error_cls: type) -> subprocess.CompletedProcess:
    """Run `cmd`, turning a missing binary or a timeout into `error_cls`
    (raised with a readable message) instead of letting the raw
    FileNotFoundError/TimeoutExpired propagate — every caller only needs one
    except clause for "yt-dlp itself couldn't run", on top of its own check
    of `result.returncode`."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise error_cls(f"yt-dlp {action} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise error_cls(
            f"yt-dlp not found (YT_DLP_PATH={yt_dlp_path()!r}) — see scripts/README.md"
        ) from e


def format_error(stderr: str, action: str, hint: str = "") -> str:
    """Shared "yt-dlp <action> failed<hint>: <tail of stderr>" message shape.
    `hint` is caller-supplied (e.g. Instagram's cookie-expiry hint) so it
    stays out of this generic module."""
    tail = (stderr or "").strip()[-500:]
    return f"yt-dlp {action} failed{hint}: {tail}"

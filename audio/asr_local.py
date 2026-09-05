"""Phase 4 of #1047 (issue #1053): local ASR via whisper.cpp, running on this
machine's own CPU cores instead of a paid cloud API. Free, but slow — no GPU,
so large-v3 runs roughly 1-3x slower than realtime on 4 cores: an hour of
audio costs 1-3 hours of wall clock with all four cores pinned. That's why
this module is never called synchronously from an HTTP request — see
scripts/audio_worker.py, which only invokes it while Daniel is not using the
server (main.py's activity timestamp) and outside the morning pre-gen window.

Unlike audio/asr_cloud.py (Groq), there is NO chunking here. Groq's HTTP
endpoint caps uploads around 25MB, forcing asr_cloud.py to split long audio
into pieces and re-add each piece's own offset to its timestamps. whisper.cpp
is a local process reading a file straight off disk — there is no upload
limit, and it happily transcribes a multi-hour recording in one invocation,
handing back timestamps that are already absolute from the start of the
file. Re-introducing asr_cloud.py's chunking here would just be extra
complexity solving a problem this path doesn't have.

whisper.cpp only accepts 16kHz mono WAV, so this module always transcodes
the input with ffmpeg first, into a throwaway temp file that is deleted in a
`finally:` on every path (success or failure) — a leftover multi-gigabyte WAV
from an audiobook-length input is exactly the kind of leak this package must
never produce (same rule asr_cloud.py's chunk-file cleanup follows).
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

import podcast

from . import AudioTrackAborted, AudioTrackError, Cue, Track

logger = logging.getLogger(__name__)

# whisper.cpp's CLI executable. The upstream project's build produces a
# binary historically called "main" but newer releases name it "whisper-cli";
# either way this is a system-level tool (like ffmpeg), not a Python
# dependency — installed once per server, see scripts/README.md.
_DEFAULT_WHISPER_CPP_PATH = "whisper-cli"

# The quantized large-v3 model this project standardizes on (q5_0 keeps
# accuracy close to full precision at a fraction of the disk/RAM cost) —
# see scripts/README.md for the one-time download step.
_DEFAULT_WHISPER_CPP_MODEL = "/opt/whisper.cpp/models/ggml-large-v3-q5_0.bin"

# Leaves one of the server's 4 cores free for the actual web app (and the
# cron jobs that hit it every few minutes) instead of pinning all four —
# whisper.cpp running flat-out on every core would make the app itself
# sluggish even though scripts/audio_worker.py only starts this while Daniel
# is supposedly not using it (he might come back mid-transcription).
_THREADS = 3

_TIMEOUT_SECONDS = 6 * 60 * 60  # 6h hard cap — see scripts/audio_worker.py's own watchdog

# How often the run loop below checks whether it's been asked to step aside.
# Every few seconds is plenty: the thing being waited on takes hours, and the
# only cost of a late reaction is a few more seconds of CPU contention.
_ABORT_POLL_SECONDS = 5


def _priority_prefix() -> list[str]:
    """`nice`/`ionice` prefix so the app always outranks this.

    whisper.cpp is the lowest-priority thing on this machine by definition:
    it is free, it is not urgent, and it runs for hours. Even inside the
    quiet window scripts/audio_worker.py picks, the web app, the deploy cron
    and the mail/Signal pollers must never wait behind it — and when Daniel
    does come back, the seconds before the worker notices and stops it should
    not feel like the server died.

    `ionice` is Linux-only (the production server), so on macOS — where
    Daniel develops — it simply isn't prefixed rather than crashing the whole
    call. `nice` exists on both.
    """
    prefix = ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix


def _whisper_cpp_path() -> str:
    return os.environ.get("WHISPER_CPP_PATH", _DEFAULT_WHISPER_CPP_PATH)


def _whisper_cpp_model() -> str:
    return os.environ.get("WHISPER_CPP_MODEL", _DEFAULT_WHISPER_CPP_MODEL)


def _require_installed() -> tuple[str, str]:
    exe = _whisper_cpp_path()
    resolved = shutil.which(exe) if os.sep not in exe else (exe if os.path.exists(exe) else None)
    if not resolved:
        raise AudioTrackError(
            f"whisper.cpp executable {exe!r} was not found (WHISPER_CPP_PATH) — "
            "see scripts/README.md for the one-time build/install steps (#1053)")
    model = _whisper_cpp_model()
    if not os.path.exists(model):
        raise AudioTrackError(
            f"whisper.cpp model {model!r} was not found (WHISPER_CPP_MODEL) — "
            "see scripts/README.md for the one-time download step (#1053)")
    return resolved, model


def _transcode_to_wav16(audio_path: str) -> str:
    """ffmpeg -> 16kHz mono WAV, the only format whisper.cpp accepts. Caller
    is responsible for deleting the returned path."""
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, text=True, timeout=600,
        )
    except FileNotFoundError as e:
        os.remove(wav_path)
        raise AudioTrackError("ffmpeg is required for local ASR (#1053) but was not found") from e
    if result.returncode != 0:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        raise AudioTrackError(f"ffmpeg failed to transcode audio for whisper.cpp: {result.stderr.strip()[-500:]}")
    return wav_path


def _wait_or_abort(proc, should_abort) -> None:
    """Block until `proc` exits, killing it if `should_abort()` says to.

    subprocess.run() cannot do this: it hands control back only when the
    child is finished, and this child runs for hours. The whole point of
    #1053 is that the moment Daniel touches the server, whatever is chewing
    three cores gets out of the way — that requires staying awake while the
    child runs.

    Escalates SIGTERM -> SIGKILL: whisper.cpp normally exits promptly, but a
    worker that hangs waiting for a well-behaved shutdown would hold both the
    CPU and the PID lock, which is the exact situation this is meant to end.
    """
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while True:
        try:
            proc.wait(timeout=_ABORT_POLL_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        if should_abort is not None and should_abort():
            _kill(proc)
            raise AudioTrackAborted("local transcription was asked to stop (the server is in use again)")
        if time.monotonic() > deadline:
            _kill(proc)
            raise AudioTrackError(f"whisper.cpp did not finish within {_TIMEOUT_SECONDS}s")


def _kill(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("audio.asr_local: whisper.cpp survived SIGKILL, giving up on it")


def _run_whisper_cpp(wav_path: str, lang: str, should_abort=None) -> list:
    """Invoke whisper.cpp on the (already 16kHz mono) wav_path, requesting
    JSON output so segment-level timestamps survive — whisper.cpp's plain
    stdout transcript has no timing information at all. Returns the raw
    `transcription` array from the JSON file (offsets.from/to in ms, plus
    text), or raises AudioTrackError.
    """
    exe, model = _require_installed()
    fd, json_stub = tempfile.mkstemp(suffix="")
    os.close(fd)
    os.remove(json_stub)  # whisper.cpp appends ".json" itself to -of's path
    json_path = json_stub + ".json"
    cmd = _priority_prefix() + [
        exe, "-m", model, "-f", wav_path, "-l", lang, "-t", str(_THREADS),
        "--output-json", "-of", json_stub,
    ]
    # Output goes to a temp FILE, not subprocess.PIPE. whisper.cpp writes a
    # steady stream of progress lines to stderr for hours; with a pipe that
    # nobody drains, the OS buffer fills and the child blocks forever on its
    # next write — a deadlock that would only ever show up on long audio,
    # i.e. exactly the inputs this path exists for.
    log_fd, log_path = tempfile.mkstemp(suffix=".log")
    try:
        with os.fdopen(log_fd, "w") as log_file:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)
            _wait_or_abort(proc, should_abort)
        if proc.returncode != 0:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-500:]
            raise AudioTrackError(
                f"whisper.cpp exited with an error: {tail.strip()}")
        if not os.path.exists(json_path):
            raise AudioTrackError(
                "whisper.cpp finished but produced no JSON output file")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("transcription") or []
    except FileNotFoundError as e:
        # `nice`/`ionice`/the whisper binary itself missing from PATH.
        raise AudioTrackError(f"could not start whisper.cpp: {e}") from e
    finally:
        for path in (json_path, log_path):
            try:
                os.remove(path)
            except OSError:
                pass


def build(audio_path: str, lang: str = "zh", should_abort=None) -> Track:
    """audio_path -> Track, source='asr_local' (#1053).

    Raises AudioTrackError when the whisper.cpp binary/model is missing, the
    transcoding or transcription subprocess fails or times out, or the whole
    transcript is filtered out as hallucination/silence. Always cleans up the
    temporary transcoded WAV, on both the success and failure paths.

    `should_abort` is an optional zero-argument callback, polled every few
    seconds while whisper.cpp runs; returning True stops it and raises
    AudioTrackAborted — which is NOT a failure, see that class's docstring.
    scripts/audio_worker.py passes one so that Daniel touching the server
    frees the CPU within seconds instead of hours.
    """
    _require_installed()  # fail fast, before paying for the transcode

    wav_path = _transcode_to_wav16(audio_path)
    try:
        raw_segments = _run_whisper_cpp(wav_path, lang, should_abort=should_abort)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass

    # whisper.cpp's JSON segments use {"offsets": {"from": ms, "to": ms},
    # "text": str} — reshape into the {"text", "start", "end"} (seconds)
    # shape podcast._filter_whisper_segments/_seg_field already understand,
    # so the shared hallucination filter needs no whisper.cpp-specific branch.
    segments = []
    for seg in raw_segments:
        offsets = seg.get("offsets") or {}
        segments.append({
            "text": seg.get("text", ""),
            "start": (offsets.get("from", 0) or 0) / 1000.0,
            "end": (offsets.get("to", 0) or 0) / 1000.0,
        })

    kept = podcast._filter_whisper_segments(segments)
    if not kept:
        raise AudioTrackError("transcript was filtered out as hallucination/silence")

    # Segment-level cues, same construction as asr_cloud.build() — whisper.cpp
    # segments are already sentence-grained, so these are not re-split.
    cues: list[Cue] = []
    cursor = 0
    for seg in kept:
        text = (podcast._seg_field(seg, "text") or "").strip()
        if not text:
            continue
        if cues:
            cursor += 1  # join space between this segment and the last
        char_start = cursor
        char_end = cursor + len(text)
        cues.append(Cue(
            start_ms=round((podcast._seg_field(seg, "start", 0.0) or 0.0) * 1000),
            end_ms=round((podcast._seg_field(seg, "end", 0.0) or 0.0) * 1000),
            text=text, char_start=char_start, char_end=char_end,
        ))
        cursor = char_end

    if not cues:
        raise AudioTrackError("transcript was filtered out as hallucination/silence")

    duration_ms = cues[-1].end_ms if cues else None
    # Same single-space join the cursor above assumed — see asr_cloud.py.
    source_text = " ".join(c.text for c in cues)
    return Track(audio_path=audio_path, duration_ms=duration_ms,
                cues=cues, word_cues=[], source="asr_local", voice=None,
                source_text=source_text)

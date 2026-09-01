"""
Text-to-speech wrapper using edge-tts + afplay (macOS).

speak()/speak_sync()/speak_multi()/_play() — server-side playback via afplay/say (macOS only).
                      Only reachable through /api/speak, /api/speak-multi, /api/speak-stop,
                      which the frontend no longer calls (issue #418) — safe on non-macOS servers.
preload()          — pre-generate audio in background without playing
preload_all_async()— pre-generate a batch in parallel; awaitable

Cache strategy: persistent files in data/tts/<sha256(text)>.mp3
  - Survives server restarts — same sentence is never generated twice
  - Atomic write (tmp → rename) prevents partial files
  - No size limit (mp3 files are ~30–100 KB each)
  - Small in-memory set tracks which paths we've verified this process,
    to skip the os.path.exists call for hot items
"""

import asyncio
import hashlib
import logging
import os
import subprocess
import threading

import edge_tts

import languages
from offline import is_offline

# Bypass system proxy (e.g. Shadowrocket in China) for edge-tts WebSocket connections.
# The VPN tunnel routes traffic at the network level, so direct connections work fine.
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

logger = logging.getLogger(__name__)

VOICE = "zh-CN-XiaoxiaoNeural"
TTS_CACHE_DIR = "data/tts"

# In-process set of paths confirmed to exist — avoids repeated stat() calls
_hot: set[str] = set()

# Per-session TTS progress: key → {"done": int, "total": int}
_preload_progress: dict[str, dict] = {}


def _cache_path(text: str, voice: str = VOICE) -> str:
    # Keep the existing key formula for the zh default voice so the pre-existing
    # cache stays valid; other voices get a voice-prefixed key to avoid collisions.
    if voice == VOICE:
        key = hashlib.sha256(text.encode()).hexdigest()
    else:
        key = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()
    return os.path.join(TTS_CACHE_DIR, f"{key}.mp3")


class NotCachedOffline(Exception):
    """Offline mode + cache miss — the audio simply doesn't exist here (#612)."""


async def _ensure_cached(text: str, voice: str = VOICE) -> str:
    """Return path to mp3 for text, generating via edge-tts if not on disk.

    In offline mode a cache miss raises NotCachedOffline instead: opening an
    edge-tts WebSocket with no network would hang until its timeout and stall
    the request, so a miss has to fail immediately.
    """
    path = _cache_path(text, voice)
    if path in _hot or os.path.exists(path):
        _hot.add(path)
        return path

    if is_offline():
        logger.info("tts  offline cache MISS %r — no audio available", text[:30])
        raise NotCachedOffline(text)

    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    logger.debug("tts  generating %r → %s", text[:30], os.path.basename(path))
    communicate = edge_tts.Communicate(text, voice)
    try:
        await communicate.save(tmp)
        if not os.path.exists(tmp):
            raise RuntimeError("edge-tts produced no output")
        os.replace(tmp, path)   # atomic: no partial files visible to readers
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _hot.add(path)
    logger.debug("tts  cached     %s", os.path.basename(path))
    return path


_TTS_CONCURRENCY = 12   # max parallel edge-tts connections
_TTS_TIMEOUT = 20.0     # seconds per sentence before giving up


async def preload_all_async(texts: list[str], progress_key: str | None = None,
                            lang: str = "zh") -> None:
    """Pre-generate audio for all texts with bounded concurrency. Awaitable — blocks until done.

    If progress_key is given, updates _preload_progress[progress_key] as each
    sentence finishes so callers can poll for live progress.
    """
    voice = languages.get_lang_config(lang)["tts_voice"]
    total = len(texts)
    missing = [t for t in texts if _cache_path(t, voice) not in _hot
               and not os.path.exists(_cache_path(t, voice))]
    already_cached = total - len(missing)

    if progress_key is not None:
        _preload_progress[progress_key] = {"done": already_cached, "total": total}

    if not missing:
        logger.info("tts  all %d sentences already cached", total)
        if progress_key is not None:
            _preload_progress[progress_key]["done"] = total
        return

    if is_offline():
        # Nothing to generate without a network. Report the batch as finished
        # so the frontend's progress poll doesn't spin — the uncached
        # sentences just play silently (issue #612).
        logger.info("tts  offline — %d/%d sentences have no cached audio, skipping",
                    len(missing), total)
        if progress_key is not None:
            _preload_progress[progress_key]["done"] = total
        return

    logger.info("tts  generating %d/%d sentences (rest cached), concurrency=%d",
                len(missing), total, _TTS_CONCURRENCY)
    done_count = already_cached
    sem = asyncio.Semaphore(_TTS_CONCURRENCY)

    async def _cached_with_progress(text: str) -> str:
        nonlocal done_count
        async with sem:
            try:
                result = await asyncio.wait_for(_ensure_cached(text, voice), timeout=_TTS_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("tts  timeout after %.0fs for: %r", _TTS_TIMEOUT, text[:40])
                if progress_key is not None:
                    _preload_progress[progress_key]["error"] = "⚠ Audio timeout — skipped some sentences"
                done_count += 1
                if progress_key is not None:
                    _preload_progress[progress_key]["done"] = done_count
                return ""
            except Exception as e:
                logger.warning("tts  failed for sentence: %s", e)
                if progress_key is not None:
                    _preload_progress[progress_key]["error"] = f"⚠ Audio failed: {str(e)[:60]}"
                done_count += 1
                if progress_key is not None:
                    _preload_progress[progress_key]["done"] = done_count
                return ""
        done_count += 1
        if progress_key is not None:
            _preload_progress[progress_key]["done"] = done_count
        return result

    await asyncio.gather(*[_cached_with_progress(t) for t in missing])
    if progress_key is not None:
        _preload_progress[progress_key]["done"] = total


def preload(text: str, lang: str = "zh") -> None:
    """Fire-and-forget single-item preload (background thread)."""
    if is_offline():
        return
    voice = languages.get_lang_config(lang)["tts_voice"]
    threading.Thread(target=lambda: asyncio.run(_ensure_cached(text, voice)),
                     daemon=True).start()


_current_playback: subprocess.Popen | None = None
_playback_lock = threading.Lock()
_stop_requested = False
_multi_lock = threading.Lock()   # only one speak_multi runs at a time
_current_play_idx: int = -1
_is_multi_playing: bool = False


# Voices for languages that are NOT learning languages (issue #1017). German
# belongs here and not in `languages.py`: that registry drives /api/langs, the
# deck trees and the home tab bar, so adding de there would make a German deck
# tree appear. The gloss-reading mode needs a German voice and nothing else.
GLOSS_VOICES = {"de": "de-DE-KatjaNeural"}


async def get_cached_path(text: str, lang: str = "zh") -> str:
    """Return local mp3 path for text, generating via edge-tts if not cached."""
    voice = GLOSS_VOICES.get(lang) or languages.get_lang_config(lang)["tts_voice"]
    return await _ensure_cached(text, voice)


def get_status() -> dict:
    """Return current playback index and whether speak_multi is active."""
    return {"idx": _current_play_idx, "playing": _is_multi_playing}


def speak(text: str, lang: str = "zh") -> None:
    """Play audio, killing any ongoing playback first (fire-and-forget)."""
    threading.Thread(target=lambda: asyncio.run(_play(text, lang)),
                     daemon=True).start()


def speak_sync(text: str, lang: str = "zh") -> None:
    """Play audio and block until playback is complete."""
    asyncio.run(_play(text, lang))


def speak_multi(texts: list[str], start_idx: int = 0, lang: str = "zh") -> None:
    """Play texts[start_idx:] sequentially with minimal gap.

    Sets _stop_requested before acquiring _multi_lock so any in-progress
    speak_multi exits quickly, then starts fresh.
    """
    global _stop_requested
    _stop_requested = True   # interrupt any current playback fast
    with _multi_lock:
        _stop_requested = False  # safe to start now — old loop has exited
        asyncio.run(_play_multi(texts, start_idx, lang))


async def _play_multi(texts: list[str], start_idx: int = 0, lang: str = "zh") -> None:
    """Pre-cache all sentences in parallel, then play from start_idx."""
    global _stop_requested, _current_play_idx, _is_multi_playing
    await preload_all_async(texts, lang=lang)
    _is_multi_playing = True
    try:
        for i, text in enumerate(texts[start_idx:], start=start_idx):
            if _stop_requested:
                break
            _current_play_idx = i
            await _play(text, lang)
    finally:
        _is_multi_playing = False
        _current_play_idx = -1


def stop() -> None:
    """Stop any ongoing playback and cancel speak_multi loop."""
    global _current_playback, _stop_requested
    _stop_requested = True
    with _playback_lock:
        if _current_playback and _current_playback.poll() is None:
            _current_playback.kill()
            _current_playback.wait()


SAY_VOICE = "Tingting"  # macOS built-in zh_CN fallback


async def _play(text: str, lang: str = "zh") -> None:
    global _current_playback
    cfg = languages.get_lang_config(lang)
    try:
        path = await asyncio.wait_for(_ensure_cached(text, cfg["tts_voice"]), timeout=5.0)
        cmd = ["afplay", path]
    except Exception:
        logger.warning("tts  edge-tts failed, falling back to macOS say")
        cmd = ["say", "-v", cfg["say_voice"], text]

    with _playback_lock:
        if _current_playback and _current_playback.poll() is None:
            _current_playback.kill()
            _current_playback.wait()
        _current_playback = subprocess.Popen(cmd)
    _current_playback.wait()

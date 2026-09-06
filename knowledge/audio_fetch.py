"""Download a podcast episode's own audio for the "Listen" feature (#1074).

Unlike knowledge/instagram.py or the YouTube audiobook path, an RSS
enclosure (`podcast_episodes.audio_url`) is a plain HTTP(S) direct link to an
mp3 — no yt-dlp needed, the same "plain urllib" approach podcast.py's own
`_download_audio()` already uses for the Whisper/NotebookLM transcription
paths (#497 chose that deliberately, see that function's docstring). This
module exists separately (rather than reusing `_download_audio()`) because
that function transcodes into a throwaway tmp_dir the caller deletes —
#1074 needs the file to persist on disk afterwards (audio/anchored.py reads
it directly, and the whole point of keeping it is to avoid downloading it a
second time if the alignment is ever rebuilt).
"""
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Same "pretend to be a browser" reasoning as translator.py's _BROWSER_UA
# (#890): some CDNs serving podcast enclosures reject the default Python
# User-Agent outright. Keeping our own real UA string here (rather than one
# that just lies about being a browser) is enough to get past that without
# pretending to be something we're not.
_UA = "biangbiangmian3000/1.0 (+https://github.com/daniel4828/biangbiangmian3000)"

_TIMEOUT_SECONDS = 60
_CHUNK_BYTES = 1024 * 1024  # 1 MB — see the streaming-write note below

# An episode's audio is routinely 20-150 MB; this ceiling only exists to turn
# an obviously wrong URL (a redirect to something that isn't a podcast at
# all) into a clear, immediate error instead of a very slow one. Well above
# any real episode, well below "accidentally downloading a movie forever".
MAX_AUDIO_BYTES = 500 * 1024 * 1024  # 500 MB


class AudioFetchError(Exception):
    """The enclosure could not be downloaded — network failure, non-200
    response, or the size ceiling was exceeded. Callers must surface the
    reason; per this codebase's rule, nothing is left half-written on
    failure (see download_episode_audio's cleanup)."""


def download_episode_audio(url: str, dest_dir: str, filename: str) -> str:
    """Stream `url` (an RSS enclosure mp3 direct link) into
    `dest_dir/filename` and return the full path.

    Streamed in fixed-size chunks, never `.read()`ing the whole response into
    memory at once — an episode can be a hundred-plus MB, and the server has
    other things competing for RAM at the same time (same concern
    knowledge/audio_upload.py's chunked upload write documents).

    Raises AudioFetchError on any network failure or if MAX_AUDIO_BYTES is
    exceeded; in both cases the partial file is deleted before raising —
    never a truncated mp3 masquerading as a complete download.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    tmp_path = dest_path + ".part"

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            size = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise AudioFetchError(
                            f"audio at {url!r} exceeds the {MAX_AUDIO_BYTES // (1024 * 1024)} MB limit")
                    out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _cleanup(tmp_path)
        raise AudioFetchError(f"failed to download audio from {url!r}: {e}") from e
    except AudioFetchError:
        _cleanup(tmp_path)
        raise

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        _cleanup(tmp_path)
        raise AudioFetchError(f"downloaded nothing from {url!r}")

    os.replace(tmp_path, dest_path)  # atomic: no reader ever sees a partial file at dest_path
    return dest_path


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

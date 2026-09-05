"""Direct audio file upload for the knowledge base (#1068).

2026-09-05: YouTube blocks Contabo's (and every other cloud provider's) IP
range outright ("Sign in to confirm you're not a bot") — #1054's audiobook
download and #1067's cookie workaround both depend on YouTube letting the
server through, and cookies expire besides. This module is the one entry
point that depends on no external site at all and therefore can never go
stale: Daniel uploads an audio file he already has (an audiobook mp3, a
recording, ...) straight from his phone or laptop.

Deliberately NOT routed through knowledge/files.py's extract_file_text() ->
ingest_text(): that path turns a file into TEXT and stores it as a
transcript immediately. An audio file has no text yet — producing one is
exactly what queueing local ASR (audio/asr_local.py via database.audio_jobs,
picked up later by scripts/audio_worker.py during a quiet window) does on
the existing #1054 audiobook path. So this module builds the row and queues
the job the exact same way _ingest_audiobook does, via
knowledge.ingest._store_audiobook_episode — there must only ever be one
"build this row + enqueue this job" implementation (#643/#836).
"""
import hashlib
import logging
import os

import database
from audio import asr_cloud

logger = logging.getLogger(__name__)


class AudioUploadError(Exception):
    """An uploaded file could not be turned into a queued transcription job
    (unsupported extension, over the size limit, ...)."""


# What Daniel actually has lying around: exported/ripped audiobook and
# recording files. Not the wider set knowledge/files.py accepts — those are
# TEXT documents, a different shape of material entirely.
SUPPORTED_EXTENSIONS = (".mp3", ".m4a", ".wav")

# An audiobook is routinely several hundred MB; this ceiling exists only to
# turn an obvious mistake (wrong file, someone uploading a movie) into a
# clear error instead of a very slow failure partway through. Deliberately
# far above knowledge/files.py's 10 MB (that path is for text documents) and
# above routes/books.py's 80 MB book ceiling — audio is far less
# information-dense per byte, so a proportionally bigger ceiling here is not
# a proportionally bigger risk of an accidental multi-GB upload being
# something other than what it looks like.
MAX_AUDIO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Same parent directory _ingest_audiobook (#1054) downloads YouTube audio
# into (data/audio/source/<video_id>/) — one place on disk for "source audio
# queued for/awaiting local ASR", regardless of whether it arrived by
# download or by upload. Uploads get their own subdirectory only so an
# upload's content-hash filename can never collide with a YouTube video id.
UPLOAD_AUDIO_DIR = os.path.join("data", "audio", "source", "uploads")

_CHUNK_BYTES = 1024 * 1024  # 1 MB


def _save_upload_stream(fileobj, filename: str) -> tuple[str, str, int]:
    """Stream `fileobj` (a file-like object — routes/knowledge.py passes
    UploadFile.file) into UPLOAD_AUDIO_DIR one chunk at a time, hashing it in
    the same pass. Returns (tmp_path, sha256_hex, size_bytes).

    This is the one place in this module that touches the disk-vs-memory
    tradeoff: an audiobook can be several hundred MB, and the server has
    only 7.8 GB of RAM with other things potentially running at the same
    time (CLAUDE.md's note on the read-along audio jobs already competing
    for that). Reading `await file.read()` (whole-file-into-memory, what
    routes/books.py does for EPUB/PDF uploads that are capped at 80 MB) is
    not an option at this ceiling — hence the chunked read/write/hash loop
    instead.

    Raises AudioUploadError for an unsupported extension or a file over
    MAX_AUDIO_BYTES; in the latter case the partial file is deleted before
    raising, never left on disk.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise AudioUploadError(
            f"unsupported audio type '{ext or filename}' — supported: "
            + ", ".join(SUPPORTED_EXTENSIONS)
        )

    os.makedirs(UPLOAD_AUDIO_DIR, exist_ok=True)
    tmp_path = os.path.join(UPLOAD_AUDIO_DIR, f".tmp-{os.urandom(8).hex()}{ext}")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = fileobj.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise AudioUploadError(
                        "file is too large (limit "
                        f"{MAX_AUDIO_BYTES // (1024 * 1024 * 1024)} GB)"
                    )
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return tmp_path, digest.hexdigest(), size


def ingest_audio_upload(fileobj, filename: str, title: str | None = None,
                        author: str | None = None,
                        china_critical: bool = False) -> dict:
    """Save an uploaded audio file and queue it for local ASR transcription.

    Returns {"episode_id": ..., "queued": True} for a new upload, or
    {"status": "already_exists", "episode_id": ...} when the exact same
    audio content was uploaded before — deduped by a sha256 of the bytes
    (video_id = f"upload:{hash}"), same "dedup key when there's no URL to
    hash" approach ingest_text() uses for pasted bodies. A dedup hit deletes
    the just-written temp file: re-uploading the same file must never
    occupy a second few hundred MB of disk, and must never queue a second
    multi-hour transcription.

    Raises AudioUploadError on an unsupported extension or an over-limit
    file (from _save_upload_stream). A duration ffprobe can't determine is
    NOT a failure here — duration is a nice-to-have for the list view, the
    transcription job doesn't need it (contrast with _ingest_audiobook's
    _MAX_UNCONFIRMED_SECONDS guard, which exists only because that path
    downloads the audio itself and wants to ask before committing to a
    possibly-huge download; there's no equivalent question here, the file
    is already fully on disk by the time this runs).

    This function (and the streaming write above it) runs synchronously in
    the request — no background thread / job_id polling, unlike
    routes/books.py's upload flow. The one truly slow part (moving the
    bytes off the wire and onto disk) already has to happen before this
    function is even called; everything this function itself does — a
    sha256 lookup, ffprobe reading a few metadata bytes, two small INSERTs —
    is on the order of milliseconds, so introducing a job dict and a polling
    endpoint here would just be a second thing to keep in sync for no
    benefit.
    """
    tmp_path, digest, _size = _save_upload_stream(fileobj, filename)
    video_id = f"upload:{digest[:16]}"

    existing = database.get_episode_by_video_id(video_id)
    if existing:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {"status": "already_exists", "episode_id": existing["id"]}

    ext = os.path.splitext(tmp_path)[1]
    final_path = os.path.join(UPLOAD_AUDIO_DIR, f"{digest[:16]}{ext}")
    os.replace(tmp_path, final_path)

    try:
        duration = asr_cloud._probe_duration_seconds(final_path)
        duration = int(duration) if duration is not None else None
    except Exception as e:
        logger.warning("audio_upload: ffprobe could not read a duration for %s — %s",
                       final_path, e)
        duration = None

    title = (title or "").strip()
    if not title:
        title = os.path.splitext(os.path.basename(filename or ""))[0].strip() or video_id

    import knowledge.ingest as ingest
    return ingest._store_audiobook_episode(
        video_id=video_id, title=title, audio_path=final_path, duration=duration,
        author=(author or "").strip() or None, platform="upload",
        china_critical=china_critical,
    )

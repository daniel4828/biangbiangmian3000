"""Tests for direct audio file upload (#1068) — the one knowledge-base
ingestion path that depends on no external site (YouTube blocks the
server's IP outright, #1054/#1067's workarounds both eventually fail or
expire).

CLAUDE.md's hard rules for this suite apply: isolated db only via
database.core.DB_PATH (never database.DB_PATH, a wildcard-import copy,
#615); never actually shell out to ffprobe (monkeypatched wherever a test
reaches for it).
"""
import io
import os

import pytest

import ai
import database
import database.core
import knowledge.audio_upload as audio_upload
from audio import asr_cloud


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def tmp_audio_dir(tmp_path, monkeypatch):
    """Redirect the upload directory to a throwaway path — nothing here may
    ever write into the real data/audio/source/uploads/."""
    monkeypatch.setattr(audio_upload, "UPLOAD_AUDIO_DIR", str(tmp_path / "audio_uploads"))


@pytest.fixture(autouse=True)
def _no_title_translation(monkeypatch):
    monkeypatch.setattr(ai, "translate_title", lambda title: None)


@pytest.fixture(autouse=True)
def _fake_ffprobe(monkeypatch):
    """Default stub so tests that don't care about duration don't need to
    stub it themselves — tests exercising ffprobe failure override this."""
    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", lambda path: 123.0)


def _uploaded_dir_files(base_dir):
    out = []
    for root, _dirs, files in os.walk(base_dir):
        out.extend(files)
    return out


# ---------------------------------------------------------------------------
# 1. same file uploaded twice: one row, one job, one file on disk
# ---------------------------------------------------------------------------

def test_duplicate_upload_creates_only_one_row_and_file(tmp_path):
    content = b"fake mp3 bytes " * 1000

    first = audio_upload.ingest_audio_upload(io.BytesIO(content), "book.mp3", title="My Book")
    assert "episode_id" in first
    episode_id = first["episode_id"]

    jobs = database.list_audio_jobs(statuses=("pending", "running"))
    assert len(jobs) == 1

    upload_dir = audio_upload.UPLOAD_AUDIO_DIR
    files_after_first = _uploaded_dir_files(upload_dir)
    assert len(files_after_first) == 1

    second = audio_upload.ingest_audio_upload(io.BytesIO(content), "book.mp3", title="My Book")
    assert second == {"status": "already_exists", "episode_id": episode_id}

    # still exactly one job queued, one file on disk — the duplicate upload
    # must not have re-queued a transcription or occupied a second copy.
    assert database.list_audio_jobs(statuses=("pending", "running")) == jobs
    assert _uploaded_dir_files(upload_dir) == files_after_first


# ---------------------------------------------------------------------------
# 2. unsupported extension
# ---------------------------------------------------------------------------

def test_unsupported_extension_is_rejected_and_creates_no_row():
    with pytest.raises(audio_upload.AudioUploadError):
        audio_upload.ingest_audio_upload(io.BytesIO(b"whatever"), "archive.zip")

    upload_dir = audio_upload.UPLOAD_AUDIO_DIR
    assert not os.path.isdir(upload_dir) or _uploaded_dir_files(upload_dir) == []
    assert database.list_audio_jobs(statuses=("pending", "running", "done", "error")) == []


# ---------------------------------------------------------------------------
# 3. successful upload queues an audio_job pointing at the stored file
# ---------------------------------------------------------------------------

def test_successful_upload_enqueues_an_audio_job():
    res = audio_upload.ingest_audio_upload(io.BytesIO(b"fake wav bytes"), "recording.wav")
    episode_id = res["episode_id"]
    assert res["queued"] is True

    jobs = database.list_audio_jobs(statuses=("pending", "running"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["owner_kind"] == "episode"
    assert job["owner_id"] == episode_id
    assert job["status"] == "pending"

    episode = database.get_episode(episode_id)
    assert job["audio_path"] == episode["audio_url"]
    assert os.path.isfile(job["audio_path"])
    assert episode["kind"] == "video"
    assert episode["platform"] == "upload"


# ---------------------------------------------------------------------------
# 4. ffprobe failing to read a duration must not fail the upload
# ---------------------------------------------------------------------------

def test_ffprobe_failure_does_not_fail_the_upload(monkeypatch):
    def fail_probe(path):
        raise asr_cloud.AudioTrackError("ffprobe returned no readable duration")

    monkeypatch.setattr(asr_cloud, "_probe_duration_seconds", fail_probe)

    res = audio_upload.ingest_audio_upload(io.BytesIO(b"fake m4a bytes"), "book.m4a")
    assert "episode_id" in res
    assert res["queued"] is True

    episode = database.get_episode(res["episode_id"])
    assert episode["duration_seconds"] is None
    assert len(database.list_audio_jobs(statuses=("pending", "running"))) == 1


# ---------------------------------------------------------------------------
# 5. over the size limit: rejected, and no file left behind
# ---------------------------------------------------------------------------

def test_oversized_upload_is_rejected_and_leaves_no_file(monkeypatch):
    monkeypatch.setattr(audio_upload, "MAX_AUDIO_BYTES", 10)

    with pytest.raises(audio_upload.AudioUploadError):
        audio_upload.ingest_audio_upload(io.BytesIO(b"x" * 1000), "huge.mp3")

    upload_dir = audio_upload.UPLOAD_AUDIO_DIR
    assert _uploaded_dir_files(upload_dir) == []
    assert database.list_audio_jobs(statuses=("pending", "running", "done", "error")) == []


# ---------------------------------------------------------------------------
# 6. title fallback to filename, and default platform/kind
# ---------------------------------------------------------------------------

def test_title_falls_back_to_filename_when_not_given():
    res = audio_upload.ingest_audio_upload(io.BytesIO(b"fake mp3"), "my_audiobook.mp3")
    episode = database.get_episode(res["episode_id"])
    assert episode["title"] == "my_audiobook"

"""Tests for issue #1053: local whisper.cpp transcription + the idle-time
worker that decides when it may run.

whisper.cpp is never actually invoked here — what's under test is the
decision logic around it, which is where this feature can fail silently:

  1. Polling endpoints must NOT count as "Daniel is using the server".
     /api/tasks alone is hit every few seconds by every open tab; counting
     it would mean a tab left open forever keeps whisper.cpp locked out and
     the queue simply never drains, with nothing anywhere saying why.
  2. The worker steps aside while he IS using the server, and outside the
     morning pre-generation window (server local time — Asia/Shanghai, not
     German time; getting that wrong shifts the window by 6-7 hours).
  3. An interrupted job goes back to 'pending', never 'error': transcription
     is idempotent, so re-running it tomorrow costs nothing, whereas marking
     it failed would permanently write off whichever job was running every
     single time he sat down at his laptop.
  4. claim_next_audio_job() hands one job to exactly one caller.
  5. init_db() recovers 'running' rows left by a killed process, idempotently
     (production restarts every ~2 minutes, so every migration runs again).
  6. A missing whisper.cpp binary is a readable error, not a crash.
  7. The multi-gigabyte transcoded WAV is deleted on both the success and the
     failure path.

Each DB test patches database.core.DB_PATH — never database.DB_PATH, which
is only a wildcard-import copy (#615).
"""
import importlib.util
import os
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio
import audio.asr_local as asr_local
import database
import database.core
import main

_WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "audio_worker.py")
_spec = importlib.util.spec_from_file_location("audio_worker", _WORKER_PATH)
audio_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audio_worker)

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


# ── 1. What counts as "he is using the server" ──────────────────────────────

def test_polling_endpoints_do_not_count_as_activity(tmp_db, monkeypatch):
    """The whole feature hinges on this one: /api/tasks is polled by every
    open tab every few seconds, so if it counted, `last_user_activity` would
    never go stale and whisper.cpp would never get a turn."""
    written = []
    monkeypatch.setattr(database, "set_app_setting",
                        lambda k, v: written.append(k))
    # Reset the once-per-minute throttle so this test isn't at the mercy of
    # whatever ran before it.
    main._last_activity_write[0] = 0.0

    client.get("/api/tasks")
    assert "last_user_activity" not in written


def test_a_real_request_does_count_as_activity(tmp_db, monkeypatch):
    written = []
    monkeypatch.setattr(database, "set_app_setting",
                        lambda k, v: written.append(k))
    main._last_activity_write[0] = 0.0

    client.get("/api/decks")
    assert "last_user_activity" in written


def test_this_servers_own_cron_jobs_do_not_count_as_activity(tmp_db, monkeypatch):
    """#1071: scripts/due_check.py (every 5 min) and podcast_check.py (every
    15) reach this app over HTTP with Basic Auth. Their heartbeat used to keep
    `last_user_activity` permanently fresh, so the worker's "30 minutes idle"
    gate never opened once — and the log said "1 分钟前还有动作", which reads
    like everything is fine.

    Deliberately tested through the auth *mechanism*, not through those two
    paths: adding them to a blocklist would fix today and let the next
    HTTP-based cron silently reintroduce it."""
    written = []
    monkeypatch.setattr(database, "set_app_setting", lambda k, v: written.append(k))
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    # No session cookie on the request => a script, not a browser.
    monkeypatch.setattr(main, "_session_cookie_valid", lambda c: False)
    main._last_activity_write[0] = 0.0

    client.get("/api/decks")
    assert "last_user_activity" not in written


def test_a_browser_session_cookie_does_count_as_activity(tmp_db, monkeypatch):
    """The other half of #1071: a real person in a browser must still park the
    worker, or it would happily eat three cores mid-review."""
    written = []
    monkeypatch.setattr(database, "set_app_setting", lambda k, v: written.append(k))
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "_session_cookie_valid", lambda c: True)
    main._last_activity_write[0] = 0.0

    # Set on the client, not per-request: starlette deprecated the latter.
    client.cookies.set(main._SESSION_COOKIE, "pretend-valid")
    try:
        client.get("/api/decks")
    finally:
        client.cookies.clear()
    assert "last_user_activity" in written


# ── 2. The worker's gates ───────────────────────────────────────────────────

def test_worker_does_not_start_while_he_is_using_the_server(tmp_db, monkeypatch):
    database.enqueue_audio_job("episode", 1, "zh", "/tmp/a.mp3")
    monkeypatch.setattr(audio_worker, "_seconds_since_activity", lambda: 5 * 60)

    claimed = []
    monkeypatch.setattr(database, "claim_next_audio_job",
                        lambda: claimed.append(1))
    assert audio_worker.main() == 0
    assert claimed == [], "claimed a job while he was still at the keyboard"


def test_worker_exits_immediately_with_an_empty_queue(tmp_db, monkeypatch):
    checked = []
    monkeypatch.setattr(audio_worker, "_seconds_since_activity",
                        lambda: checked.append(1) or 10_000)
    assert audio_worker.main() == 0
    assert checked == [], "asked about activity before checking there was work"


def test_quiet_window_uses_server_local_time(tmp_db):
    """05:30-09:30 belongs to scripts/morning_pregen.py. The server runs on
    Asia/Shanghai, so this must be plain local time — computing it in German
    time would move the window by 6-7 hours and let the two fight for the
    CPU exactly when he opens the app."""
    assert audio_worker._in_quiet_window(datetime(2026, 9, 5, 6, 0))
    assert audio_worker._in_quiet_window(datetime(2026, 9, 5, 5, 30))
    assert not audio_worker._in_quiet_window(datetime(2026, 9, 5, 9, 30))
    assert not audio_worker._in_quiet_window(datetime(2026, 9, 5, 14, 0))
    assert not audio_worker._in_quiet_window(datetime(2026, 9, 5, 3, 0))


def test_unparseable_activity_timestamp_keeps_the_worker_out(tmp_db):
    """Garbage in the setting means "we don't know whether he's here" — and
    the safe answer to that is not to pin three cores."""
    database.set_app_setting("last_user_activity", "not-a-number")
    assert audio_worker._seconds_since_activity() == 0.0


# ── 3. Interruption ─────────────────────────────────────────────────────────

def test_interrupted_job_goes_back_to_pending_not_error(tmp_db, monkeypatch):
    job_id = database.enqueue_audio_job("episode", 7, "zh", "/tmp/a.mp3")
    job = database.claim_next_audio_job()
    assert job["id"] == job_id

    def _abort(**kwargs):
        raise audio.AudioTrackAborted("he came back")

    monkeypatch.setattr(audio, "build_track", _abort)
    assert audio_worker._run_one_job(job) == 0

    rows = database.list_audio_jobs(statuses=("pending", "running", "error"))
    assert [r["status"] for r in rows] == ["pending"]


def test_a_real_failure_marks_the_job_error(tmp_db, monkeypatch):
    """The other half of the previous test: a genuine failure must NOT be
    retried forever, or a permanently broken file would occupy every quiet
    night from now on."""
    database.enqueue_audio_job("episode", 8, "zh", "/tmp/a.mp3")
    job = database.claim_next_audio_job()

    def _fail(**kwargs):
        raise audio.AudioTrackError("whisper.cpp exploded")

    monkeypatch.setattr(audio, "build_track", _fail)
    assert audio_worker._run_one_job(job) == 1

    rows = database.list_audio_jobs(statuses=("error",))
    assert len(rows) == 1
    assert "exploded" in rows[0]["error"]


def test_aborted_is_a_subclass_of_audio_track_error():
    """Callers that only care about "did it work" keep working; the worker
    tells the two apart deliberately."""
    assert issubclass(audio.AudioTrackAborted, audio.AudioTrackError)


# ── 4/5. Queue bookkeeping ──────────────────────────────────────────────────

def test_claim_does_not_hand_the_same_job_to_two_callers(tmp_db):
    first_id = database.enqueue_audio_job("episode", 1, "zh", "/tmp/a.mp3")
    second_id = database.enqueue_audio_job("episode", 2, "zh", "/tmp/b.mp3")

    got = [database.claim_next_audio_job(), database.claim_next_audio_job()]
    assert sorted(j["id"] for j in got) == sorted([first_id, second_id])
    assert database.claim_next_audio_job() is None


def test_init_db_recovers_running_jobs_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "recover.db"))
    database.init_db()
    database.enqueue_audio_job("episode", 1, "zh", "/tmp/a.mp3")
    database.claim_next_audio_job()          # -> running
    assert [j["status"] for j in database.list_audio_jobs()] == ["running"]

    # Production restarts every ~2 minutes, so this runs again and again.
    database.init_db()
    database.init_db()
    assert [j["status"] for j in database.list_audio_jobs()] == ["pending"]


# ── 6/7. asr_local's own guarantees ─────────────────────────────────────────

def test_missing_whisper_binary_is_a_readable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_CPP_PATH", str(tmp_path / "nope"))
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(tmp_path / "nope.bin"))
    with pytest.raises(audio.AudioTrackError) as e:
        asr_local.build(str(tmp_path / "x.mp3"))
    assert "README" in str(e.value) or "whisper" in str(e.value).lower()


def test_temp_wav_is_removed_on_both_success_and_failure(monkeypatch, tmp_path):
    """An audiobook transcodes to a multi-gigabyte WAV; leaking one per run
    fills a 55 GB disk fast, and the symptom would be some unrelated feature
    breaking."""
    made = []

    def _fake_transcode(path):
        wav = tmp_path / f"chunk{len(made)}.wav"
        wav.write_bytes(b"x")
        made.append(str(wav))
        return str(wav)

    monkeypatch.setattr(asr_local, "_require_installed", lambda: ("exe", "model"))
    monkeypatch.setattr(asr_local, "_transcode_to_wav16", _fake_transcode)

    # Long enough to clear the shared hallucination filter's minimum-word
    # check — a one-word transcript is thrown away as silence, which would
    # make this test fail for a reason that has nothing to do with cleanup.
    long_text = " ".join(f"wort{i}" for i in range(30))
    monkeypatch.setattr(asr_local, "_run_whisper_cpp",
                        lambda *a, **k: [{"offsets": {"from": 0, "to": 1000}, "text": long_text}])
    asr_local.build("/tmp/a.mp3")

    def _boom(*a, **k):
        raise audio.AudioTrackError("nope")

    monkeypatch.setattr(asr_local, "_run_whisper_cpp", _boom)
    with pytest.raises(audio.AudioTrackError):
        asr_local.build("/tmp/a.mp3")

    assert len(made) == 2
    assert not any(os.path.exists(p) for p in made)


def test_priority_prefix_always_nices_and_survives_a_mac(monkeypatch):
    """ionice is Linux-only; Daniel develops on macOS, where its absence must
    not blow up the call."""
    monkeypatch.setattr(asr_local.shutil, "which", lambda name: None)
    assert asr_local._priority_prefix() == ["nice", "-n", "19"]

    monkeypatch.setattr(asr_local.shutil, "which", lambda name: "/usr/bin/ionice")
    assert asr_local._priority_prefix() == ["nice", "-n", "19", "ionice", "-c", "3"]

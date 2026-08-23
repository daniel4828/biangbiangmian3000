"""Tests for the header background-task indicator (#821).

The central claim: /api/tasks reports what is *actually* running by reading the
progress state each subsystem already publishes. So every test here writes into
those real dicts (ai._story_progress, tts._preload_progress, …) rather than into
a registry of the endpoint's own — if a subsystem ever stops publishing there,
these tests must break rather than keep reporting a comfortable lie.
"""

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import ai
import database
import main
import tts
from routes import imports as import_routes
from routes import podcast as podcast_routes
from routes import story as story_routes
from routes import tasks as task_routes

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH, not database.DB_PATH
    — the latter is only a copy of the name (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def clean_state():
    """The aggregated dicts are module globals shared with the running app."""
    for d in (ai._story_progress, tts._preload_progress,
              import_routes._import_jobs, task_routes._ad_hoc):
        d.clear()
    podcast_routes._PROCESSING_IDS.clear()
    story_routes._generating.clear()
    yield
    for d in (ai._story_progress, tts._preload_progress,
              import_routes._import_jobs, task_routes._ad_hoc):
        d.clear()
    podcast_routes._PROCESSING_IDS.clear()
    story_routes._generating.clear()


def get_tasks():
    r = client.get("/api/tasks")
    assert r.status_code == 200
    return r.json()


def test_idle_reports_nothing(tmp_db):
    body = get_tasks()
    assert body["count"] == 0 and body["tasks"] == []


def test_story_generation_shows_up_with_phase_and_percent(tmp_db):
    deck_id = database.get_or_create_deck("Kouyu")
    key = f"{deck_id}/reading/zh"
    story_routes._generating.add(key)
    ai._story_progress[key] = {"phase": "request", "msg": "生成句子…", "percent": 40}

    tasks = get_tasks()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "story"
    assert "Kouyu" in tasks[0]["label"] and "reading" in tasks[0]["label"]
    assert tasks[0]["detail"] == "生成句子…"
    assert tasks[0]["percent"] == 40


def test_blocking_generation_without_the_generating_flag_still_shows(tmp_db):
    """A foreground (non-background) generation only writes _story_progress —
    it must not be invisible just because it never entered _generating."""
    deck_id = database.get_or_create_deck("Kouyu")
    ai._story_progress[f"{deck_id}/listening/zh"] = {
        "phase": "translating", "msg": "Translating…", "percent": 70}
    assert [t["kind"] for t in get_tasks()["tasks"]] == ["story"]


def test_finished_story_is_not_a_running_task(tmp_db):
    """Terminal states linger in _story_progress for the loading screen to read
    one last time; they are history, not work in progress."""
    deck_id = database.get_or_create_deck("Kouyu")
    for phase in ("done", "error", "idle"):
        ai._story_progress[f"{deck_id}/reading/zh"] = {"phase": phase, "percent": 100}
        assert get_tasks()["count"] == 0, phase


def test_tts_preload_reports_a_ratio_and_disappears_when_complete(tmp_db):
    deck_id = database.get_or_create_deck("Kouyu")
    key = f"{deck_id}/listening/zh"
    tts._preload_progress[key] = {"done": 3, "total": 12}
    task = get_tasks()["tasks"][0]
    assert task["kind"] == "audio" and task["percent"] == 25
    assert task["detail"] == "3/12 sentences"

    tts._preload_progress[key] = {"done": 12, "total": 12}
    assert get_tasks()["count"] == 0


def test_running_add_word_job_shows_up_but_a_finished_one_does_not(tmp_db):
    import_routes._import_jobs["abc123"] = {
        "status": "running", "message": "Generating entry for 生态…",
        "started_at": 1_700_000_000.0}
    task = get_tasks()["tasks"][0]
    assert task["kind"] == "word"
    assert task["started_at"] == 1_700_000_000.0

    import_routes._import_jobs["abc123"]["status"] = "done"
    assert get_tasks()["count"] == 0


def test_knowledge_item_being_processed_shows_its_title(tmp_db):
    episode_id = database.create_pending_episode(
        "abc", None, "Ein Podcast", "2026-08-19", "https://example.com/x")
    podcast_routes._PROCESSING_IDS.add(episode_id)
    task = get_tasks()["tasks"][0]
    assert task["kind"] == "knowledge" and task["label"] == "Ein Podcast"


def test_ad_hoc_registry_round_trip(tmp_db):
    """The Again single-sentence regeneration is the only job with no progress
    state of its own, so it registers here explicitly."""
    task_routes.register("sentence:1:reading", "sentence", "New sentence · 生态")
    assert [t["label"] for t in get_tasks()["tasks"]] == ["New sentence · 生态"]
    task_routes.finish("sentence:1:reading")
    assert get_tasks()["count"] == 0


def test_a_broken_collector_does_not_take_the_whole_list_down(tmp_db, monkeypatch):
    """A half-complete list still tells Daniel something is running; a 500
    tells him nothing at all."""
    monkeypatch.setattr(task_routes, "_story_tasks",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    task_routes.register("x", "sentence", "still here")
    assert [t["label"] for t in get_tasks()["tasks"]] == ["still here"]


def test_deleted_deck_falls_back_to_the_id(tmp_db):
    ai._story_progress["99999/reading/zh"] = {"phase": "request", "percent": 10}
    assert get_tasks()["tasks"][0]["label"] == "99999 · reading"


# ── 取消任务（议题 #877）────────────────────────────────────────────────────
# 只有故事生成真的能停下来（ai.request_cancel 设的标志由 _set_progress 在每个
# 阶段检查）。别的任务类型没有任何中断点——它们必须明确拒绝，而不是回一个
# 「取消成功」然后活儿照跑：那样 Daniel 会以为账单停了。

def _cancel(task_id):
    return client.post("/api/tasks/cancel", json={"id": task_id})


def test_cancel_story_sets_the_flag_the_generator_checks(tmp_db):
    deck_id = database.get_or_create_deck("Kouyu")
    key = f"{deck_id}/reading/zh"
    story_routes._generating.add(key)
    ai._story_progress[key] = {"phase": "request", "msg": "生成句子…", "percent": 40}
    try:
        assert get_tasks()["tasks"][0]["cancellable"] is True

        r = _cancel(f"story:{key}")
        assert r.status_code == 200 and r.json()["cancelled"] is True
        # 断言打在生成线程真正会读的那个标志上，而不是端点的返回值——
        # 返回 200 却没设标志，正是这条测试要挡住的谎。
        assert ai.is_cancelled(key)
    finally:
        ai.clear_cancel(key)


def test_cancel_rejects_a_task_kind_that_cannot_be_interrupted(tmp_db):
    import_routes._import_jobs["j1"] = {
        "status": "running", "message": "Generating entry for 生态…"}

    task = get_tasks()["tasks"][0]
    assert task["cancellable"] is False

    r = _cancel(task["id"])
    assert r.status_code == 400
    assert "cannot be cancelled" in r.json()["detail"]


def test_cancel_unknown_task_is_404_not_a_polite_success(tmp_db):
    r = _cancel("story:999/reading/zh")
    assert r.status_code == 404

"""Header background-task indicator (#821).

Daniel leaves the story loading screen ("continue in background") and lands on
the deck list with no sign that anything is still running — the AI call, the
translations and the TTS preload keep going for another minute, and adding a
word or processing a knowledge item takes 30s to 15min with the same silence.

This module answers one question: *what is running right now?*

It deliberately **aggregates the progress state that already exists** instead of
introducing a second bookkeeping layer next to it. Every long-running job in
this app already publishes its state somewhere (`ai._story_progress`,
`tts._preload_progress`, `routes.imports._import_jobs`,
`routes.podcast._PROCESSING_IDS`); a parallel registry would inevitably drift
out of sync with them and start lying about what the server is doing — the same
reason #643 insists on a single add-word pipeline.

The one exception is the Again single-sentence regeneration in
`routes/review.py`, which had no bookkeeping at all: `register()`/`finish()`
below are that missing minimal registry, and nothing else should use them
unless it, too, has no state of its own.
"""
import logging
import threading
import time

import ai
import database
import tts
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Minimal registry for jobs that publish no progress of their own ─────────
_ad_hoc: dict[str, dict] = {}
_ad_hoc_lock = threading.Lock()


def register(task_id: str, kind: str, label: str, detail: str = "") -> None:
    """Record an ad-hoc background job as running. Callers MUST call finish()
    in a `finally:` — a leaked entry shows as a task that never ends."""
    with _ad_hoc_lock:
        _ad_hoc[task_id] = {"kind": kind, "label": label, "detail": detail,
                            "started_at": time.time()}


def finish(task_id: str) -> None:
    with _ad_hoc_lock:
        _ad_hoc.pop(task_id, None)


# ── Aggregation ─────────────────────────────────────────────────────────────

# Terminal states that _story_progress keeps around for the loading screen to
# read one last time — they are history, not work in progress.
_STORY_DONE_PHASES = {"done", "error", "idle"}

_ICONS = {"story": "📖", "audio": "🔊", "word": "＋",
          "knowledge": "📄", "sentence": "↺", "import": "📥", "book": "📚"}


def _deck_label(deck_id: str | int) -> str:
    """Deck name for a progress key, falling back to the raw id — a task list
    that raises because a deck was deleted mid-generation is worse than one
    that shows a number."""
    try:
        deck = database.get_deck(int(deck_id))
    except Exception:
        return str(deck_id)
    return (deck or {}).get("name") or str(deck_id)


def _split_progress_key(key: str) -> tuple[str, str, str]:
    """`deck_id/category/lang` → its three parts (missing parts become '')."""
    parts = key.split("/")
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def _story_tasks() -> list[dict]:
    from . import story as story_routes

    with story_routes._gen_lock:
        generating = set(story_routes._generating)
    out = []
    # A key can be in _generating slightly before its progress entry exists,
    # and a *blocking* (foreground) generation only has the progress entry —
    # both must show up, so walk the union.
    for key in set(ai._story_progress) | generating:
        prog = ai._story_progress.get(key) or {}
        phase = prog.get("phase", "starting")
        if key not in generating and phase in _STORY_DONE_PHASES:
            continue
        deck_id, category, lang = _split_progress_key(key)
        out.append({
            "id": f"story:{key}",
            "kind": "story",
            "label": f"{_deck_label(deck_id)} · {category}" if category else _deck_label(deck_id),
            "detail": prog.get("msg") or phase,
            "percent": prog.get("percent"),
            "lang": lang or None,
            # The only kind that can actually be stopped (#877), via the flag
            # ai._set_progress() checks at every phase. See cancel_task below.
            "cancellable": True,
        })
    return out


def _audio_tasks() -> list[dict]:
    out = []
    for key, prog in list(tts._preload_progress.items()):
        total, done = prog.get("total") or 0, prog.get("done") or 0
        if total <= 0 or done >= total:
            continue
        deck_id, category, lang = _split_progress_key(key)
        out.append({
            "id": f"audio:{key}",
            "kind": "audio",
            "label": f"{_deck_label(deck_id)} · {category}" if category else _deck_label(deck_id),
            "detail": f"{done}/{total} sentences",
            "percent": round(done * 100 / total),
            "lang": lang or None,
        })
    return out


def _import_tasks() -> list[dict]:
    from . import imports as import_routes

    out = []
    for job_id, job in list(import_routes._import_jobs.items()):
        if job.get("status") != "running":
            continue
        # Both the add-word jobs (#627) and the YAML upload jobs (#458) live in
        # this dict; only the former carries a per-word message.
        message = job.get("message") or "Working…"
        kind = "word" if message.startswith("Generating entry") else "import"
        out.append({
            "id": f"import:{job_id}",
            "kind": kind,
            "label": message,
            "detail": "",
            "percent": None,
            "started_at": job.get("started_at"),
        })
    return out


def _book_tasks() -> list[dict]:
    """Book uploads being parsed and paginated (#836) — tens of seconds for a
    long book, with nothing else on screen to say so."""
    from . import books as book_routes

    out = []
    for job_id, job in list(book_routes._upload_jobs.items()):
        if job.get("status") != "running":
            continue
        out.append({
            "id": f"book:{job_id}",
            "kind": "book",
            "label": job.get("message") or "Reading book…",
            "detail": "",
            "percent": None,
            "started_at": job.get("started_at"),
        })
    return out


def _knowledge_tasks() -> list[dict]:
    from . import podcast as podcast_routes

    with podcast_routes._PROCESSING_LOCK:
        ids = list(podcast_routes._PROCESSING_IDS)
    out = []
    for episode_id in ids:
        try:
            episode = database.get_episode(episode_id) or {}
        except Exception:
            episode = {}
        out.append({
            "id": f"knowledge:{episode_id}",
            "kind": "knowledge",
            "label": episode.get("title") or f"Item {episode_id}",
            "detail": "Transcribing / summarising…",
            "percent": None,
            "episode_id": episode_id,
        })
    return out


def _audio_job_tasks() -> list[dict]:
    """Local whisper.cpp transcriptions (#1053).

    Reads the audio_jobs table's own status rather than keeping a parallel
    registry — #821's rule: a second set of books always drifts from the
    first and then starts lying. It also has to be the table here and not an
    in-process dict, because the work happens in a separate cron process
    (scripts/audio_worker.py), not in this one.

    Only 'running' rows are tasks; 'pending' ones are a queue, not work in
    progress. But the queue length goes in the detail line — a job that will
    not start until tonight should not look like nothing was requested.
    """
    jobs = database.list_audio_jobs(statuses=("pending", "running"))
    running = [j for j in jobs if j["status"] == "running"]
    waiting = len(jobs) - len(running)
    out = []
    for job in running:
        detail = "Transcribing locally…"
        if waiting:
            detail += f" ({waiting} queued)"
        out.append({
            "id": f"audiojob:{job['id']}",
            "kind": "audio",
            "label": f"{job['owner_kind']} {job['owner_id']} · {job['lang']}",
            "detail": detail,
            "percent": None,
            "started_at": job.get("started_at"),
        })
    return out


def _ad_hoc_tasks() -> list[dict]:
    with _ad_hoc_lock:
        items = list(_ad_hoc.items())
    return [{"id": task_id, "kind": t["kind"], "label": t["label"],
             "detail": t.get("detail", ""), "percent": None,
             "started_at": t["started_at"]} for task_id, t in items]


@router.get("/api/tasks")
def list_tasks():
    """Everything the server is currently working on, for the header indicator.

    Polled every few seconds by every open tab, so it must stay cheap: no AI,
    no writes, and the only DB reads are deck/episode names for the labels.
    A failing collector must never take the whole list down — a half-complete
    list still tells Daniel something is running.
    """
    tasks: list[dict] = []
    for collect in (_story_tasks, _audio_tasks, _audio_job_tasks, _import_tasks,
                    _knowledge_tasks, _book_tasks, _ad_hoc_tasks):
        try:
            tasks.extend(collect())
        except Exception as e:
            logger.warning("tasks: collector %s failed: %s", collect.__name__, e)
    for task in tasks:
        task.setdefault("started_at", None)
        task.setdefault("cancellable", False)
        task["icon"] = _ICONS.get(task["kind"], "⚙")
    return {"tasks": tasks, "count": len(tasks)}


@router.post("/api/tasks/cancel")
def cancel_task(body: dict):
    """Stop a running task from the header panel (#877).

    Only story generation can be stopped: ai.request_cancel() sets a flag that
    _set_progress() checks at every phase, so the run stops before the next AI
    call and before anything is written. TTS preload, add-word, knowledge
    processing and the Again regeneration have no interruption point at all —
    they return 400 here and show no button in the panel. A cross that quietly
    does nothing would be worse than none: Daniel would stop watching the bill.

    404 when the id is not in the current task list — a cancel reported for a
    task that does not exist tells him a run stopped when it did not.

    The id travels in the body, not the path: it carries the progress key
    verbatim ("story:12/unified/zh"), slashes and a possibly empty last segment
    included, which in the path would need a :path converter and still break on
    the trailing slash of a key with no lang.
    """
    task_id = (body or {}).get("id") or ""
    tasks = list_tasks()["tasks"]
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"No running task {task_id!r}")
    if not task.get("cancellable"):
        raise HTTPException(
            status_code=400,
            detail=f"{task['kind']} tasks cannot be cancelled — no interruption point exists")

    key = task_id.split(":", 1)[1]
    ai.request_cancel(key)
    logger.info("tasks: cancel requested for %s", task_id)
    return {"cancelled": True, "id": task_id}

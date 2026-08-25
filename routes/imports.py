import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import timedelta

import ai
import database
import importer
from fastapi import APIRouter, Form, HTTPException, UploadFile
from languages import DEFAULT_LANG, get_lang_config, is_valid_lang

from .utils import ai_disabled, queue_mgr

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Background import jobs (issue #458) — the previous /api/import/upload ran
# the AI-heavy import synchronously, blocking the browser for 1-2 minutes.
# Now the request just kicks off a daemon thread and returns a job id; the
# frontend polls /api/import/progress/{job_id} for status.
# ---------------------------------------------------------------------------
_import_jobs: dict[str, dict] = {}
_import_jobs_lock = threading.Lock()
_MAX_IMPORT_JOBS = 10


def _prune_import_jobs() -> None:
    """Keep at most _MAX_IMPORT_JOBS entries, oldest-first, never evicting a
    job that's still running."""
    with _import_jobs_lock:
        if len(_import_jobs) <= _MAX_IMPORT_JOBS:
            return
        for job_id in list(_import_jobs.keys()):
            if len(_import_jobs) <= _MAX_IMPORT_JOBS:
                break
            if _import_jobs[job_id]["status"] == "running":
                continue
            del _import_jobs[job_id]


@router.post("/api/import/preview")
async def preview_import(file: UploadFile):
    """Parse a YAML file and return a preview without writing to the DB."""
    content = (await file.read()).decode("utf-8")
    return importer.preview_yaml_content(content)


@router.post("/api/import/upload")
async def upload_import(
    file: UploadFile,
    deck_id: int | None = Form(None),
    deck_path: str | None = Form(None),
    deck_name: str | None = Form(None),
    resolutions: str | None = Form(None),    # JSON: {"word_zh": "keep"|"update"|"custom"}
    card_configs: str | None = Form(None),   # JSON: {word_zh: {include, deck_path, suspended, ai_fill}}
    custom_fields: str | None = Form(None),  # JSON: {word_zh: {pinyin, definition, traditional}}
):
    """Import a YAML file into a deck.

    Deck resolution order:
      1. deck_id   — existing deck id
      2. deck_path — Anki-style 'Parent::Child' path (creates hierarchy if needed)
      3. deck_name — creates a new top-level deck with this name
    """
    if deck_id is None and not deck_path and not deck_name:
        raise HTTPException(status_code=400, detail="Provide deck_id, deck_path, or deck_name")

    content = (await file.read()).decode("utf-8")

    if deck_id is None:
        if deck_path:
            try:
                deck_id = database.get_or_create_deck_path(deck_path)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            all_id = database.get_all_deck_id()
            preset_id = database.get_preset_for_deck(all_id)["id"]
            deck_id = database.insert_deck(deck_name, parent_id=all_id, preset_id=preset_id)

    if deck_id == database.get_all_deck_id():
        raise HTTPException(status_code=400, detail="Cannot import directly into 'All' — select a specific sub-deck")

    resolution_map: dict = {}
    if resolutions:
        try:
            resolution_map = json.loads(resolutions)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="resolutions must be valid JSON")

    card_configs_map: dict = {}
    if card_configs:
        try:
            card_configs_map = json.loads(card_configs)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="card_configs must be valid JSON")

    custom_fields_map: dict = {}
    if custom_fields:
        try:
            custom_fields_map = json.loads(custom_fields)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="custom_fields must be valid JSON")

    job_id = uuid.uuid4().hex[:8]
    with _import_jobs_lock:
        _import_jobs[job_id] = {
            "status": "running",
            "message": "Importing…",
            "started_at": time.time(),
        }
    _prune_import_jobs()

    def _run_import():
        try:
            result = importer.import_yaml_content(
                content, deck_id,
                resolutions=resolution_map,
                card_configs=card_configs_map,
                custom_fields=custom_fields_map,
            )
            with _import_jobs_lock:
                started_at = _import_jobs[job_id]["started_at"]
                _import_jobs[job_id] = {
                    "status": "done",
                    "message": "Import complete",
                    "summary": {"deck_id": deck_id, **result},
                    "started_at": started_at,
                }
        except Exception as e:
            logger.exception("Unhandled error during import (deck_id=%s): %s", deck_id, e)
            with _import_jobs_lock:
                started_at = _import_jobs.get(job_id, {}).get("started_at", time.time())
                _import_jobs[job_id] = {
                    "status": "error",
                    "message": "Import failed",
                    "error": str(e),
                    "started_at": started_at,
                }

    threading.Thread(target=_run_import, daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/import/progress/{job_id}")
def import_progress(job_id: str):
    """Poll status for a background import job started by /api/import/upload."""
    job = _import_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


@router.post("/api/import/directory")
async def import_from_directory(
    deck_id: int | None = Form(None),
    deck_path: str | None = Form(None),
    deck_name: str | None = Form(None),
    imports_dir: str = Form("imports"),
):
    """Scan the imports/ directory recursively and import all YAML files.

    Deck resolution order:
      1. deck_id   — existing deck id
      2. deck_path — Anki-style 'Parent::Child' path (creates hierarchy if needed)
      3. deck_name — creates a new top-level deck with this name
    """
    if deck_id is None and not deck_path and not deck_name:
        raise HTTPException(status_code=400, detail="Provide deck_id, deck_path, or deck_name")

    if deck_id is None:
        if deck_path:
            try:
                deck_id = database.get_or_create_deck_path(deck_path)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            all_id = database.get_all_deck_id()
            preset_id = database.get_preset_for_deck(all_id)["id"]
            deck_id = database.insert_deck(deck_name, parent_id=all_id, preset_id=preset_id)

    if deck_id == database.get_all_deck_id():
        raise HTTPException(status_code=400, detail="Cannot import directly into 'All' — select a specific sub-deck")

    # Collect all YAML files
    yaml_files = []
    if os.path.isdir(imports_dir):
        for dirpath, dirnames, filenames in os.walk(imports_dir):
            dirnames.sort()
            for fn in sorted(f for f in filenames if f.endswith((".yaml", ".yml"))):
                yaml_files.append(os.path.join(dirpath, fn))

    if not yaml_files:
        raise HTTPException(status_code=404, detail=f"No YAML files found in {imports_dir}/")

    total_imported = 0
    total_duplicate = 0
    total_invalid = 0
    errors = []

    for filepath in yaml_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            errors.append({"file": os.path.basename(filepath), "problem": str(e)})
            continue

        result = importer.import_yaml_content(content, deck_id)
        if result.get("yaml_error"):
            err = result["yaml_error"]
            err["file"] = os.path.relpath(filepath, imports_dir)
            errors.append(err)
            continue

        total_imported += result.get("imported", 0)
        total_duplicate += result.get("skipped_duplicate", 0)
        total_invalid += result.get("skipped_invalid", 0)

    return {
        "deck_id": deck_id,
        "imported": total_imported,
        "skipped_duplicate": total_duplicate,
        "skipped_invalid": total_invalid,
        "errors": errors,
        "files_processed": len(yaml_files),
    }


# ---------------------------------------------------------------------------
# In-app "add a word" (issue #627) — one button, one Chinese word, a full
# de-zh-bot style entry in today's Daily deck.
#
# Everything downstream is the ordinary import path: importer._create_cards
# dues cards at database.anki_today() and importer._make_leaf_decks builds the
# very same '<date> · Listening/Reading/Creating' children that
# database.get_or_create_category_decks does. So handing the generated YAML to
# import_yaml_content() with today's Daily deck yields cards indistinguishable
# from a hand-imported entry — no special-case card creation here.
# ---------------------------------------------------------------------------


def _card_deck_ids(entry_id: int) -> set[int]:
    """Deck ids holding this entry's live cards."""
    conn = database.get_db()
    rows = conn.execute(
        "SELECT DISTINCT deck_id FROM cards WHERE word_id = ? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    return {r["deck_id"] for r in rows}


def _total_repetitions(entry_id: int) -> int:
    """How many reviews this word's cards have accumulated — reported back when
    a reset throws that progress away (#675)."""
    conn = database.get_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(repetitions), 0) AS n FROM cards "
        "WHERE word_id = ? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchone()
    conn.close()
    return row["n"]


_HAN_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÿ]")


def _validate_word_for_lang(word: str, lang: str) -> None:
    """Reject input that plainly isn't the language the caller asked for.

    The add-word box takes no follow-up questions (unlike the de-zh-bot /
    de-fr-bot skills, which ask which meaning is wanted), so a German word
    typed under the French tab would silently become a wrong entry. Checking
    the script is the one cheap guard available: it can't tell French from
    German, but it does stop 生态 from being sent to the French prompt and
    séjour from being sent to the Chinese one.
    """
    if lang == "zh":
        if not _HAN_RE.search(word):
            raise HTTPException(status_code=400,
                                detail="Please enter the word in Chinese characters")
        return
    if _HAN_RE.search(word):
        raise HTTPException(status_code=400,
                            detail="That looks like Chinese — switch the language first")
    if not _LATIN_RE.search(word):
        lang_name = get_lang_config(lang)["name_en"]
        raise HTTPException(status_code=400, detail=f"Please enter the word in {lang_name}")


@router.post("/api/add-word-ai")
def add_word_ai(body: dict):
    """Add one word with an AI-generated entry.

    Body: { word_zh, day?: "today"|"tomorrow"|"list", lang?: "zh"|"fr",
            confirm?: bool }
      today/tomorrow → cards go into that day's daily deck, due then.
      list (#677)    → the entry is generated in full but its cards are parked
                       suspended in the Saved deck, so the word enters no review
                       queue until it is promoted from Browse.
      lang (#726)    → which language's prompt and deck tree to use; every
                       language owns a parallel tree ('Daily::…' for zh,
                       'Français::…' for fr) because the app's language filters
                       key off decks.lang.
      confirm (#888) → when the word already exists, re-adding it mutates real
                       state (moves cards, and for today/tomorrow irreversibly
                       wipes FSRS memory). Daniel wants a chance to see what is
                       about to move before that happens, so the first call for
                       an existing word (unless it's a no-op already_listed)
                       does no writes at all and instead reports what WOULD
                       happen; the caller re-POSTs with confirm=true to commit.
    Returns either {job_id, deck_path} — generation runs in the background,
    poll /api/add-word-ai/progress/{job_id} — or, when the word is already in
    the database, a finished {status, ...} with no AI call at all (status is
    "needs_confirmation" on the first call, unless already_listed).
    """
    word_zh = (body.get("word_zh") or "").strip()
    if not word_zh:
        raise HTTPException(status_code=400, detail="word_zh is required")

    lang = (body.get("lang") or DEFAULT_LANG).strip()
    if not is_valid_lang(lang):
        raise HTTPException(status_code=400, detail=f"Unknown language: {lang!r}")

    day = (body.get("day") or "today").strip()
    if day not in ("today", "tomorrow", "list"):
        raise HTTPException(status_code=400,
                            detail="day must be 'today', 'tomorrow' or 'list'")
    # 'list' (#677) = generate the full entry now but park it in the Saved deck
    # as suspended cards, so the word enters no queue until it is promoted from
    # Browse. The import itself still runs against today's Daily deck — that is
    # the one code path that builds a complete entry — and the cards are moved
    # afterwards. Importing straight into Saved would create Saved::listening
    # etc. leaf decks, and Browse's saved filter matches on deck_name == 'Saved'.
    to_list = day == "list"
    # A daily deck dated in the future is locked until its date arrives
    # (database.parse_daily_deck_date), which is exactly the "stage it for
    # tomorrow" semantics — the cards just have to be due then too (#636).
    due_offset_days = 1 if day == "tomorrow" else 0
    # anki_today(), not date.today() (#851): the due dates importer._create_cards
    # writes and the future-deck lock in parse_daily_deck_date both run on the
    # Anki day (5am boundary). Between midnight and the cutoff the two disagree,
    # and the card lands due-today inside a deck dated tomorrow — locked shut.
    target_day = (database.anki_today() + timedelta(days=due_offset_days)).isoformat()

    # Known word → don't pay for a second generation; the importer would skip
    # it as a duplicate anyway. `cards` has UNIQUE(word_id, category), so a word
    # owns exactly one card per category for its whole lifetime — there is no
    # "also add it to today", only moving the cards it already has.
    #
    # Daniel asked (2026-08-10, #675) for re-adding a known word to reset it to
    # new and pull it into today's/tomorrow's deck, whatever its progress. That
    # discards the word's FSRS memory (stability/difficulty/interval/lapses)
    # irreversibly, so the response says exactly what was thrown away rather
    # than reporting a bland success.
    existing = database.get_word_by_zh(word_zh)
    if existing is None and lang != "zh":
        # Typed an inflected form of a word already in the database (#924):
        # "mangeons" has to reach the existing `manger` entry. Without this the
        # word gets generated a second time under a second headword —
        # UNIQUE(word_zh, lang) does not catch it, the two spellings differ.
        # Exact match against entry_forms only, no stemming: a wrong guess here
        # would move some other word's cards.
        existing = database.get_word_by_form(word_zh, lang)
    if existing:
        # Act on and report the entry's own headword, not what was typed.
        word_zh = existing["word_zh"]
        # An existing word moves inside its OWN language's tree, whatever the
        # request said (#726): word_zh is globally unique, so a mistyped lang
        # would otherwise scatter one word's cards across two language trees
        # and make it invisible under both tabs.
        lang = existing["lang"] or DEFAULT_LANG
    else:
        _validate_word_for_lang(word_zh, lang)

    daily_deck_id, daily_path = database.get_or_create_daily_deck(target_day, lang)
    deck_id = daily_deck_id
    deck_path = "Saved" if to_list else daily_path

    if existing:
        card_decks = _card_deck_ids(existing["id"])
        saved_deck_id = database.get_or_create_saved_deck(lang)
        was_only_saved = bool(card_decks) and card_decks <= {saved_deck_id}
        # Count the progress about to be dropped *before* anything writes —
        # needed both for the real move and for the needs_confirmation preview.
        reps = _total_repetitions(existing["id"])
        deck_names = sorted(
            (database.get_deck(d) or {}).get("name") or f"deck {d}" for d in card_decks
        )
        if to_list:
            status = "already_listed" if was_only_saved else "listed"
        else:
            # Saved cards carry no progress, so that move is a promotion, not a
            # reset — the frontend words them differently.
            status = "promoted" if was_only_saved else "reset"

        # already_listed is a no-op (cards are already parked in Saved), so it
        # never needs a confirmation round-trip. Everything else mutates real
        # state — moves cards, and for reset/promoted wipes FSRS memory
        # irreversibly — so Daniel gets to see the preview first (#888).
        if status != "already_listed" and not body.get("confirm"):
            return {
                "status": "needs_confirmation",
                "action": status,
                "word_zh": word_zh, "entry_id": existing["id"],
                "deck_path": deck_path, "deck_id": deck_id,
                "previous_decks": deck_names,
                "reviews_discarded": 0 if to_list else reps,
            }

        if to_list:
            # Parking suspends the cards but leaves their scheduling alone, so
            # nothing is discarded here — promoting later is what resets them.
            moved = database.stage_word_in_saved(existing["id"], saved_deck_id)
            reps = 0
        else:
            leaf_decks = database.get_or_create_category_decks(deck_id, target_day)
            moved = database.reset_word_to_new(existing["id"], leaf_decks, target_day)
        # Same in-memory-queue staleness as promote_saved (#728): a card moved
        # into today has to reach a queue that may already have been built.
        queue_mgr.invalidate()
        return {
            "status": status,
            "word_zh": word_zh, "entry_id": existing["id"],
            "deck_path": deck_path, "deck_id": deck_id,
            "cards_moved": moved, "previous_decks": deck_names,
            "reviews_discarded": reps,
        }

    if ai_disabled():
        raise HTTPException(status_code=503,
                            detail="AI is disabled (offline mode) — cannot generate a new entry")

    job_id = uuid.uuid4().hex[:8]
    with _import_jobs_lock:
        _import_jobs[job_id] = {
            "status": "running",
            "message": f"Generating entry for {word_zh}…",
            "started_at": time.time(),
        }
    _prune_import_jobs()

    def _run():
        try:
            yaml_text = ai.generate_word_entry_yaml(word_zh, lang=lang)
            result = importer.import_yaml_content(yaml_text, deck_id,
                                                  due_offset_days=due_offset_days)
            if result.get("yaml_error"):
                raise ValueError(
                    f"AI returned invalid YAML: {result['yaml_error'].get('problem', 'parse error')}")
            # The headword actually written can differ from what Daniel typed:
            # the fr/es prompts normalise an inflected input to the lemma
            # (#924). Looking the entry up by the typed string would find
            # nothing and leave the cards behind in the daily deck.
            imported_words = result.get("imported_words") or []
            stored_word = imported_words[0] if imported_words else word_zh
            if to_list and result.get("imported"):
                # Only now do the freshly created cards exist to move (#677).
                entry = database.get_word_by_zh(stored_word)
                if entry:
                    database.stage_word_in_saved(
                        entry["id"], database.get_or_create_saved_deck(lang))
            if result.get("imported") and not to_list:
                # New cards due today must reach queues built earlier (#728).
                queue_mgr.invalidate()
            with _import_jobs_lock:
                started_at = _import_jobs[job_id]["started_at"]
                _import_jobs[job_id] = {
                    "status": "done",
                    "message": "Entry added",
                    "summary": {"word_zh": stored_word, "deck_id": deck_id,
                                "deck_path": deck_path, **result},
                    "started_at": started_at,
                }
        except Exception as e:
            logger.exception("add_word_ai failed for %r: %s", word_zh, e)
            with _import_jobs_lock:
                started_at = _import_jobs.get(job_id, {}).get("started_at", time.time())
                _import_jobs[job_id] = {
                    "status": "error",
                    "message": "Failed to add word",
                    "error": str(e),
                    "started_at": started_at,
                }

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "deck_path": deck_path}


@router.get("/api/add-word-ai/progress/{job_id}")
def add_word_ai_progress(job_id: str):
    """Poll status for a background add-word job started by /api/add-word-ai."""
    job = _import_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


# /api/quick-add-word was removed in #643. It only had the AI fill four fields
# (definition/definition_zh/definition_de/pos) — no examples, character
# breakdown, measure words or synonyms — and its "added_to_deck" branch claimed
# success while cards' UNIQUE(word_id, category) silently dropped every insert
# for a word already studied elsewhere. Both callers now use /api/add-word-ai.


@router.post("/api/save-word")
def save_word(body: dict):
    """Stage a compound word in the fixed 'Saved' deck as suspended cards.

    Unlike /api/add-word-ai this does NOT call the AI and does NOT activate
    the cards — content is generated later on demand, and the word only enters
    the study algorithm when promoted to a Daily deck (see /api/saved/{id}/promote).

    Body: { word_zh, pinyin?, meaning?, lang? }
    Returns: { status: "saved"|"already_saved"|"exists_elsewhere", entry_id, saved_deck_id }
    """
    word_zh = (body.get("word_zh") or "").strip()
    if not word_zh:
        raise HTTPException(status_code=400, detail="word_zh is required")

    pinyin = (body.get("pinyin") or "").strip()
    meaning = (body.get("meaning") or "").strip()
    # The word was picked out of a card, so it carries that card's language
    # (#726) — staging a French word in the Chinese Saved deck would hide it
    # under the fr tab and promote it into the Chinese daily deck later.
    lang = (body.get("lang") or DEFAULT_LANG).strip()
    if not is_valid_lang(lang):
        raise HTTPException(status_code=400, detail=f"Unknown language: {lang!r}")

    saved_deck_id = database.get_or_create_saved_deck(lang)

    existing = database.get_word_by_zh(word_zh)
    if existing is None and lang != "zh":
        # Same lemma resolution as /api/add-word-ai (#924): a conjugated form
        # of a known word must not become a second, contentless entry.
        existing = database.get_word_by_form(word_zh, lang)
    if existing:
        entry_id = existing["id"]
        conn = database.get_db()
        deck_ids = {
            r["deck_id"] for r in conn.execute(
                "SELECT deck_id FROM cards WHERE word_id=? AND deleted_at IS NULL",
                (entry_id,),
            ).fetchall()
        }
        conn.close()
        if saved_deck_id in deck_ids:
            return {"status": "already_saved", "entry_id": entry_id, "saved_deck_id": saved_deck_id}
        if deck_ids:
            # Word already lives in a real deck — nothing to stage.
            return {"status": "exists_elsewhere", "entry_id": entry_id, "saved_deck_id": saved_deck_id}
    else:
        entry_id = database.insert_word({
            "word_zh": word_zh,
            "pinyin": pinyin,
            "definition": meaning,
            "note_type": "vocabulary",
            "lang": lang,
        })

    for category in ("listening", "reading", "creating"):
        database.insert_card(entry_id, category, saved_deck_id, state="suspended")

    return {"status": "saved", "entry_id": entry_id, "saved_deck_id": saved_deck_id}


@router.post("/api/saved/{word_id}/promote")
def promote_saved(word_id: int, day: str = "today"):
    """Move a saved word's suspended cards into a daily deck as active new cards.

    Defaults to *today* (#728): ★ List is the "keep it for later" staging area
    (#715), so clicking promote means "I want to study this now" — landing the
    cards in tomorrow's deck instead hid them behind the future-daily-deck lock
    (parse_daily_deck_date) for the rest of the day. `day='tomorrow'` keeps the
    old behaviour; as everywhere else, the deck and the due date have to move
    together or a card sits due-today inside a locked deck (#636).

    Both decks are the ones belonging to the word's own language (#726) — the
    word knows its language, the caller doesn't have to.
    """
    entry = database.get_word(word_id)
    lang = (entry or {}).get("lang") or DEFAULT_LANG
    saved_deck_id = database.get_or_create_saved_deck(lang)
    target_day = (database.anki_today() + timedelta(days=1 if day == "tomorrow" else 0)).isoformat()
    daily_deck_id, deck_path = database.get_or_create_daily_deck(target_day, lang)
    leaf_decks = database.get_or_create_category_decks(daily_deck_id, target_day)

    count = database.promote_saved_word(word_id, leaf_decks, saved_deck_id, target_day)
    if not count:
        raise HTTPException(status_code=404, detail="No saved cards found for this word")
    # Session queues are built once per Anki day and kept in memory, so a card
    # that becomes due today after the build would not be served until tomorrow
    # (#728). Landing in a future deck used to make this moot.
    queue_mgr.invalidate()

    return {"status": "promoted", "count": count, "deck_path": deck_path, "deck_id": daily_deck_id}

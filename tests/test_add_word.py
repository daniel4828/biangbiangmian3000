"""Tests for the in-app "add a word" flow (issue #627).

The AI is stubbed at ai._call_api — the single choke point every provider goes
through. Patching a provider client instead would silently stop working the
next time DEFAULT_MODEL changes (issue #615).
"""

from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
from unittest.mock import patch

import ai
import database

# Anki days start at the preset's cutoff hour (4 a.m. by default), not at
# midnight, so between 00:00 and the cutoff date.today() is one day ahead of
# the day the app is on. Every "today"/"tomorrow" expectation in this file is
# compared against a deck name or due date that production code derived from
# database.anki_today(), so the tests derive theirs the same way — otherwise
# they fail on any run started before 4 a.m. (#810).
import main
import routes.imports

client = TestClient(main.app)


ENTRY_YAML = """- type: word
  date: "08/06"
  simplified: 生态
  traditional: 生態
  pinyin: shēngtài
  english: ecology / ecosystem
  german: Ökologie / Ökosystem
  definition_zh: 生物与环境相互作用形成的系统
  pos: noun
  hsk: "5"
  register: formal_written
  note: |
    Ein Substantiv aus der Biologie.
  examples:
    - zh: 保护生态环境是我们的责任。
      pinyin: Bǎohù shēngtài huánjìng shì wǒmen de zérèn.
      english: Protecting the ecological environment is our responsibility.
      de: Die ökologische Umwelt zu schützen ist unsere Verantwortung.
  synonyms:
    - simplified: 环境
      pinyin: huánjìng
      meaning: Umwelt, Umgebung
  word_analyses:
    - char_only: 生
      pinyin: shēng
      hsk: "1"
"""


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database. Patch database.core.DB_PATH — the package-level
    name is only a copy (issue #615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    # The route refuses to call the AI when DISABLE_AI is set; routes.utils
    # reads the env var at import time, so patch the resolved flag instead.
    monkeypatch.setattr(routes.imports, "ai_disabled", lambda: False)
    return tmp_path


def _run_add_word(word_zh, yaml_text=ENTRY_YAML, day=None, lang=None):
    """POST the word and, if a background job started, wait for it to finish."""
    payload = {"word_zh": word_zh}
    if day:
        payload["day"] = day
    if lang:
        payload["lang"] = lang
    with patch.object(ai, "_call_api", return_value=yaml_text):
        r = client.post("/api/add-word-ai", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        if "job_id" not in body:
            return body
        for _ in range(200):
            job = client.get(f"/api/add-word-ai/progress/{body['job_id']}").json()
            if job["status"] != "running":
                return {**body, "job": job}
            import time
            time.sleep(0.05)
        pytest.fail("add-word job never finished")


def _daily_leaf_decks(day=None):
    day = day or database.anki_today().isoformat()
    deck_id = database.get_or_create_deck_path(f"Daily::{day}")
    return database.get_or_create_category_decks(deck_id, day)


def _today_leaf_decks():
    return _daily_leaf_decks()


def test_new_word_lands_in_todays_deck_due_today(tmp_db):
    result = _run_add_word("生态")
    assert result["job"]["status"] == "done", result["job"]
    assert result["job"]["summary"]["imported"] == 1

    entry = database.get_word_by_zh("生态")
    assert entry is not None
    assert entry["pinyin"] == "shēngtài"
    assert entry["definition_de"] == "Ökologie / Ökosystem"

    today = database.anki_today().isoformat()
    leaf_ids = set(_today_leaf_decks().values())
    conn = database.get_db()
    cards = conn.execute(
        "SELECT category, deck_id, due, state FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry["id"],),
    ).fetchall()
    conn.close()

    assert {c["category"] for c in cards} == {"listening", "reading", "creating"}
    assert {c["deck_id"] for c in cards} <= leaf_ids
    # Suspended cards (reading, by importer default) carry no due date.
    assert all(c["due"] == today for c in cards if c["state"] != "suspended")


def test_full_entry_content_is_stored(tmp_db):
    """The point of the feature: the same richness as a hand-imported entry."""
    _run_add_word("生态")
    entry = database.get_word_by_zh("生态")
    detail = database.get_word_full(entry["id"])

    assert detail["examples"], "examples were not imported"
    assert detail["examples"][0]["example_de"].startswith("Die ökologische")
    assert any(r["related_zh"] == "环境" for r in detail["relations"])
    assert detail["notes"] and "Substantiv" in detail["notes"]


def test_known_word_is_reset_into_todays_deck_without_calling_the_ai(tmp_db):
    """Re-adding a studied word resets its cards to new and pulls them into
    today's deck (#675). cards has UNIQUE(word_id, category), so this moves the
    three cards it already owns — it never creates a fourth — and re-generating
    the entry would just burn an API call."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        r = client.post("/api/add-word-ai", json={"word_zh": "生态", "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset"
    assert any("Test" in name for name in body["previous_decks"])

    # The bug behind #643: the old /api/quick-add-word answered "✓ added" while
    # INSERT OR IGNORE silently dropped every card, so nothing reached the daily
    # deck. Prove the cards really moved — a success report is only worth
    # something if it matches reality.
    today = database.anki_today().isoformat()
    leaf_ids = set(_today_leaf_decks().values())
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (body["entry_id"],),
    ).fetchall()
    conn.close()
    assert rows and {r["deck_id"] for r in rows} == leaf_ids
    assert all(r["state"] == "new" and r["due"] == today for r in rows)
    assert body["cards_moved"] == len(rows)


def test_reset_clears_fsrs_progress_and_reports_what_it_discarded(tmp_db):
    """The reset is irreversible — stability/difficulty/interval/lapses are the
    word's whole memory model. The response must name the cost (#675)."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)
    entry_id = database.get_word_by_zh("生态")["id"]

    conn = database.get_db()
    conn.execute(
        """UPDATE cards SET state='review', repetitions=4, lapses=2, interval=30,
           stability=42.0, difficulty=6.5, last_review='2026-08-01', is_leech=1
           WHERE word_id=?""",
        (entry_id,),
    )
    conn.commit()
    n_cards = conn.execute(
        "SELECT COUNT(*) c FROM cards WHERE word_id=?", (entry_id,)
    ).fetchone()["c"]
    conn.close()

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        body = client.post("/api/add-word-ai", json={"word_zh": "生态", "confirm": True}).json()

    assert body["status"] == "reset"
    assert body["reviews_discarded"] == 4 * n_cards

    conn = database.get_db()
    rows = conn.execute(
        "SELECT * FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
    ).fetchall()
    conn.close()
    for row in rows:
        assert row["state"] == "new"
        assert row["stability"] is None and row["difficulty"] is None
        assert row["repetitions"] == 0 and row["lapses"] == 0 and row["interval"] == 0
        assert row["last_review"] is None and row["is_leech"] == 0


def test_saved_word_is_promoted_into_todays_deck(tmp_db):
    """A word only staged in the Saved deck has no scheduling progress to lose,
    so adding it means promoting it — still without an AI call."""
    r = client.post("/api/save-word", json={"word_zh": "生态", "pinyin": "shēngtài"})
    assert r.json()["status"] == "saved"
    entry_id = r.json()["entry_id"]

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        r = client.post("/api/add-word-ai", json={"word_zh": "生态", "confirm": True})
    assert r.json()["status"] == "promoted"

    today = database.anki_today().isoformat()
    leaf_ids = set(_today_leaf_decks().values())
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == leaf_ids
    assert all(r["state"] == "new" and r["due"] == today for r in rows)


def test_tomorrow_lands_in_tomorrows_deck_due_tomorrow(tmp_db):
    """day='tomorrow' (#636): both the deck and the cards' due date move a day
    forward — a future-dated daily deck stays locked until its date arrives, so
    a card left due today would be unreachable."""
    result = _run_add_word("生态", day="tomorrow")
    assert result["job"]["status"] == "done", result["job"]

    tomorrow = (database.anki_today() + timedelta(days=1)).isoformat()
    assert result["deck_path"] == f"Daily::{tomorrow}"

    entry = database.get_word_by_zh("生态")
    leaf_ids = set(_daily_leaf_decks(tomorrow).values())
    conn = database.get_db()
    cards = conn.execute(
        "SELECT deck_id, due, state FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry["id"],),
    ).fetchall()
    conn.close()

    assert {c["deck_id"] for c in cards} <= leaf_ids
    assert all(c["due"] == tomorrow for c in cards if c["state"] != "suspended")


def test_saved_word_promoted_to_tomorrow(tmp_db):
    r = client.post("/api/save-word", json={"word_zh": "生态", "pinyin": "shēngtài"})
    entry_id = r.json()["entry_id"]

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        r = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "tomorrow", "confirm": True})
    assert r.json()["status"] == "promoted"

    tomorrow = (database.anki_today() + timedelta(days=1)).isoformat()
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == set(_daily_leaf_decks(tomorrow).values())
    assert all(r["due"] == tomorrow for r in rows)


def test_invalid_day_is_rejected(tmp_db):
    r = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "next week"})
    assert r.status_code == 400


def test_non_chinese_input_is_rejected(tmp_db):
    r = client.post("/api/add-word-ai", json={"word_zh": "Ökologie"})
    assert r.status_code == 400


def test_empty_input_is_rejected(tmp_db):
    assert client.post("/api/add-word-ai", json={"word_zh": "   "}).status_code == 400


def test_ai_returning_prose_fails_the_job(tmp_db):
    """A model that answers in prose must surface as an error, not a silent
    no-op that leaves the user staring at an empty deck."""
    result = _run_add_word("生态", yaml_text="Sorry, I cannot help with that.")
    assert result["job"]["status"] == "error"
    assert database.get_word_by_zh("生态") is None


def test_offline_returns_explicit_error(tmp_db, monkeypatch):
    monkeypatch.setattr(routes.imports, "ai_disabled", lambda: True)
    r = client.post("/api/add-word-ai", json={"word_zh": "生态"})
    assert r.status_code == 503


def test_generate_word_entry_yaml_strips_markdown_fence():
    fenced = "Here you go:\n```yaml\n" + ENTRY_YAML + "```\n"
    with patch.object(ai, "_call_api", return_value=fenced):
        out = ai.generate_word_entry_yaml("生态")
    assert out.startswith("- type: word")
    assert "```" not in out


def test_generate_word_entry_yaml_raises_without_entry():
    with patch.object(ai, "_call_api", return_value="I don't know this word."):
        with pytest.raises(ValueError):
            ai.generate_word_entry_yaml("生态")


# ---------------------------------------------------------------------------
# Standalone /add page (#668)
# ---------------------------------------------------------------------------

def test_add_page_is_served_without_the_app_bundle():
    """The whole point of /add is opening instantly on the phone — pulling in
    the ~9000-line app.js would defeat it."""
    body = client.get("/add").text
    assert 'id="word"' in body
    assert "/static/shared.js" in body
    assert "/static/app.js" not in body  # a comment may mention it; a <script> must not


def test_add_word_pipeline_is_not_duplicated_in_app_js():
    """#643: adding a word must have exactly one client-side implementation.
    A second copy in app.js would drift from shared.js and every fix would
    silently have to be made twice."""
    import pathlib
    app_js = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    shared_js = pathlib.Path("static/shared.js").read_text(encoding="utf-8")
    assert "async function addWordViaAi(" in shared_js
    assert "async function addWordViaAi(" not in app_js
    assert "async function api(" in shared_js
    assert "async function api(" not in app_js


def test_background_deck_refresh_keeps_the_current_view():
    """#695: generation finishes ~30s later, often mid-review. loadDecks() ends
    in showView('decks'), so refreshing the due counts must pass keepView —
    otherwise finishing a word throws the user back to the home screen."""
    import pathlib
    shared_js = pathlib.Path("static/shared.js").read_text(encoding="utf-8")
    app_js = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    assert "loadDecks({ keepView: true })" in shared_js
    assert "if (!keepView) showView('decks');" in app_js


def test_add_page_uses_the_shared_endpoint():
    """Guards against the page growing its own add-word call."""
    import pathlib
    add_html = pathlib.Path("static/add.html").read_text(encoding="utf-8")
    assert "addWordViaAi(" in add_html
    assert "/api/add-word-ai" not in add_html


# ---------------------------------------------------------------------------
# ★ List: park a word in the Saved deck instead of a Daily deck (#677)
# ---------------------------------------------------------------------------

def _saved_deck_id():
    return database.get_or_create_saved_deck()


def test_list_generates_the_full_entry_but_parks_it_suspended(tmp_db):
    """day='list' still pays for a complete de-zh-bot entry — the word is just
    kept out of every review queue until promoted."""
    result = _run_add_word("生态", day="list")
    assert result["job"]["status"] == "done", result["job"]
    assert result["deck_path"] == "Saved"

    entry = database.get_word_by_zh("生态")
    conn = database.get_db()
    cards = conn.execute(
        "SELECT deck_id, state, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry["id"],),
    ).fetchall()
    examples = conn.execute(
        "SELECT COUNT(*) c FROM entry_examples WHERE word_id=?", (entry["id"],)
    ).fetchone()["c"]
    conn.close()

    assert cards, "no cards were created"
    assert {c["deck_id"] for c in cards} == {_saved_deck_id()}
    # `due` is NOT NULL in the schema — state='suspended' is what keeps a card
    # out of the queues, not its due date.
    assert all(c["state"] == "suspended" for c in cards)
    assert examples > 0, "list mode must still produce the full entry"


def test_listed_word_is_invisible_to_the_review_queue(tmp_db):
    """The whole point: a listed word must not turn up for review anywhere."""
    _run_add_word("生态", day="list")
    today_deck = database.get_or_create_deck_path(f"Daily::{database.anki_today().isoformat()}")
    for category in ("reading", "listening", "creating"):
        r = client.get(f"/api/today/{today_deck}/{category}")
        assert r.status_code == 200
        assert r.json().get("card") is None


def test_listing_a_studied_word_suspends_it_without_discarding_progress(tmp_db):
    """Parking is not a reset: suspending already keeps the card out of every
    queue, and promoting later resets it anyway (#677)."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)
    entry_id = database.get_word_by_zh("生态")["id"]
    conn = database.get_db()
    conn.execute(
        "UPDATE cards SET state='review', repetitions=3, stability=20.0 WHERE word_id=?",
        (entry_id,),
    )
    conn.commit()
    conn.close()

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        body = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "list", "confirm": True}).json()

    assert body["status"] == "listed"
    assert body["reviews_discarded"] == 0
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state, stability FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == {_saved_deck_id()}
    assert all(r["state"] == "suspended" for r in rows)
    assert all(r["stability"] == 20.0 for r in rows), "parking must not wipe FSRS state"


def test_listing_an_already_listed_word_is_reported_as_such(tmp_db):
    r = client.post("/api/save-word", json={"word_zh": "生态", "pinyin": "shēngtài"})
    assert r.json()["status"] == "saved"
    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        body = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "list"}).json()
    assert body["status"] == "already_listed"


# ---------------------------------------------------------------------------
# Confirm-before-mutating (#888): re-adding an existing word moves real cards
# and, for today/tomorrow, irreversibly wipes FSRS memory. The first call
# (no confirm) must report what WOULD happen without touching the database;
# only a follow-up call with confirm=true is allowed to write anything.
# ---------------------------------------------------------------------------

def test_reset_needs_confirmation_and_does_not_touch_the_database(tmp_db):
    """day='today' on a studied word previews a 'reset' — and leaves the cards
    and their FSRS state exactly as they were until confirmed."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)
    entry_id = database.get_word_by_zh("生态")["id"]

    conn = database.get_db()
    conn.execute(
        """UPDATE cards SET state='review', repetitions=4, lapses=2, interval=30,
           stability=42.0, difficulty=6.5, last_review='2026-08-01'
           WHERE word_id=?""",
        (entry_id,),
    )
    conn.commit()
    before = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchall()
    }
    conn.close()

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        r = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "today"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_confirmation"
    assert body["action"] == "reset"
    assert body["entry_id"] == entry_id
    assert any("Test" in name for name in body["previous_decks"])
    assert body["reviews_discarded"] == 4 * len(before)

    conn = database.get_db()
    after = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchall()
    }
    conn.close()
    assert after == before, "the preview call must not write anything"


def test_list_needs_confirmation_and_leaves_cards_unsuspended(tmp_db):
    """day='list' on a studied word previews a 'listed' move — the cards must
    still be reachable in review until the move is confirmed."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)
    entry_id = database.get_word_by_zh("生态")["id"]

    conn = database.get_db()
    before = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchall()
    }
    conn.close()

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        r = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "list"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_confirmation"
    assert body["action"] == "listed"
    assert any("Test" in name for name in body["previous_decks"])

    conn = database.get_db()
    after = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchall()
    }
    conn.close()
    assert after == before, "the preview call must not move or suspend the cards"


def test_already_listed_word_skips_confirmation(tmp_db):
    """A word only staged in Saved has nothing to move — re-listing it is a
    no-op, so it must not be gated behind a confirmation round trip."""
    r = client.post("/api/save-word", json={"word_zh": "生态", "pinyin": "shēngtài"})
    assert r.json()["status"] == "saved"
    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        body = client.post("/api/add-word-ai", json={"word_zh": "生态", "day": "list"}).json()
    assert body["status"] == "already_listed"


def test_confirm_true_behaves_like_the_pre_confirmation_flow(tmp_db):
    """The confirmed call must produce exactly the old (pre-#888) result and
    actually perform the move this time."""
    import importer

    other_deck = database.get_or_create_deck_path("Kouyu::Test")
    importer.import_yaml_content(ENTRY_YAML, other_deck)
    entry_id = database.get_word_by_zh("生态")["id"]

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        preview = client.post(
            "/api/add-word-ai", json={"word_zh": "生态", "day": "today"}
        ).json()
        assert preview["status"] == "needs_confirmation"

        r = client.post(
            "/api/add-word-ai", json={"word_zh": "生态", "day": "today", "confirm": True}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset"
    assert any("Test" in name for name in body["previous_decks"])

    today = database.anki_today().isoformat()
    leaf_ids = set(_today_leaf_decks().values())
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert rows and {r["deck_id"] for r in rows} == leaf_ids
    assert all(r["state"] == "new" and r["due"] == today for r in rows)


def test_needs_confirmation_flow_is_only_implemented_in_shared_js(tmp_db):
    """Same guard as test_add_word_pipeline_is_not_duplicated_in_app_js (#643):
    the confirmation modal must live in shared.js, not get re-implemented in
    app.js, or a fix would only land in one of the four pages that call it."""
    import pathlib
    app_js = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    shared_js = pathlib.Path("static/shared.js").read_text(encoding="utf-8")
    assert "needs_confirmation" in shared_js
    assert "function confirmExistingWord(" in shared_js
    assert "function confirmExistingWord(" not in app_js


def test_trashed_saved_deck_is_revived_instead_of_breaking_add_word(tmp_db):
    """Trashing the 'Saved' deck used to 500 every add-word request (#801).

    UNIQUE(name, parent_id) ignores `deleted_at`, so get_or_create_deck found no
    live deck, inserted, and hit an IntegrityError. Reviving is the only sane
    answer: the user asked for the word to be parked in ★ List, so ★ List has
    to exist.
    """
    saved_id = database.get_or_create_saved_deck("zh")
    database.delete_deck(saved_id)

    body = _run_add_word("生态", day="list")
    assert body["job"]["status"] == "done", body["job"]

    conn = database.get_db()
    row = conn.execute("SELECT deleted_at FROM decks WHERE id=?", (saved_id,)).fetchone()
    dupes = conn.execute(
        "SELECT COUNT(*) c FROM decks WHERE name='Saved' AND parent_id IS ?",
        (database.get_all_deck_id(),),
    ).fetchone()["c"]
    conn.close()
    assert row["deleted_at"] is None
    assert dupes == 1
    # The word really landed in the revived deck, not somewhere improvised.
    entry_id = database.get_word_by_zh("生态")["id"]
    conn = database.get_db()
    decks = {r["deck_id"] for r in conn.execute(
        "SELECT deck_id FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,))}
    conn.close()
    assert decks == {saved_id}


def test_reviving_a_deck_leaves_its_cards_alone(tmp_db):
    """Trashing a deck and emptying it are separate actions (#801): a deck that
    comes back must not drag deleted cards back with it."""
    deck_id = database.get_or_create_deck("Reviveme")
    database.delete_deck(deck_id)
    conn = database.get_db()
    conn.execute(
        "INSERT INTO entries (word_zh, pinyin) VALUES ('测试词', 'cèshìcí')")
    entry_id = conn.execute("SELECT id FROM entries WHERE word_zh='测试词'").fetchone()["id"]
    conn.execute(
        "INSERT INTO cards (word_id, deck_id, category, due, deleted_at)"
        " VALUES (?, ?, 'reading', date('now'), datetime('now'))",
        (entry_id, deck_id),
    )
    conn.commit()
    conn.close()

    assert database.get_or_create_deck("Reviveme") == deck_id
    conn = database.get_db()
    card = conn.execute("SELECT deleted_at FROM cards WHERE word_id=?", (entry_id,)).fetchone()
    conn.close()
    assert card["deleted_at"] is not None


def test_listed_word_can_be_promoted_into_a_daily_deck(tmp_db):
    """Browse's '→ Add to Daily' button must work on words added via ★ List —
    that round trip is the reason the feature exists."""
    _run_add_word("生态", day="list")
    entry_id = database.get_word_by_zh("生态")["id"]

    r = client.post(f"/api/saved/{entry_id}/promote")
    assert r.status_code == 200, r.text
    # Today, not tomorrow (#728): a future daily deck is locked, so the word
    # would be invisible for the rest of the day Daniel asked for it.
    today = database.anki_today().isoformat()
    assert r.json()["deck_path"] == f"Daily::{today}"

    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == set(_daily_leaf_decks(today).values())
    assert all(r["state"] == "new" and r["due"] == today for r in rows)


def test_listed_word_can_still_be_promoted_to_tomorrow(tmp_db):
    """day=tomorrow keeps the pre-#728 behaviour; deck and due move together."""
    _run_add_word("生态", day="list")
    entry_id = database.get_word_by_zh("生态")["id"]

    r = client.post(f"/api/saved/{entry_id}/promote?day=tomorrow")
    assert r.status_code == 200, r.text
    tomorrow = (database.anki_today() + timedelta(days=1)).isoformat()
    assert r.json()["deck_path"] == f"Daily::{tomorrow}"

    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, due FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry_id,),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == set(_daily_leaf_decks(tomorrow).values())
    assert all(r["due"] == tomorrow for r in rows)


def test_invalid_day_still_rejected_and_list_accepted(tmp_db):
    assert client.post("/api/add-word-ai",
                       json={"word_zh": "生态", "day": "nextweek"}).status_code == 400


def test_add_page_reads_word_and_day_from_the_url(tmp_db):
    """/add?word=生态&day=list (#686) — an iOS Shortcut can add a word from any
    app. Asserted on the page source because the behaviour lives in the page's
    own script; the browser round trip is covered manually."""
    import pathlib
    src = pathlib.Path("static/add.html").read_text(encoding="utf-8")
    assert "URLSearchParams" in src
    assert "params.get('word')" in src and "params.get('w')" in src
    assert "params.get('day')" in src
    # The word must be stripped from the URL after submitting, or a reload
    # (or iOS restoring tabs) silently spends another AI call on it.
    assert "history.replaceState" in src


# --- ★ List is the only destination the UI offers (#715) --------------------
# The API still accepts day=today|tomorrow (old iOS Shortcut links must keep
# working), so what these tests guard is the *interface*: no entry point may
# put a freshly added word straight into a review queue again.

def _static(name):
    import pathlib
    return pathlib.Path(f"static/{name}").read_text(encoding="utf-8")


def test_no_ui_entry_point_offers_today_or_tomorrow():
    """All three add-word entry points (top-bar ＋, the knowledge item's HSK
    table, and /add) stage the word instead of scheduling it."""
    for name in ("index.html", "add.html", "app.js"):
        assert "add-word-day-btn" not in _static(name), f"day selector left in {name}"


def test_add_word_calls_pass_the_list_destination():
    app_js = _static("app.js")
    assert "addWordViaAi(wordZh, 'list'" in app_js
    # The mutable day state behind the old selector must be gone, not merely
    # unused — a stale 'today' default is exactly how this would come back.
    assert "_addWordDay" not in app_js
    assert "_podcastAddDay" not in app_js


def test_add_page_defaults_to_the_list():
    src = _static("add.html")
    assert "let day = 'list'" in src
    # …but an explicit ?day= from an existing Shortcut is still honoured.
    assert "params.get('day')" in src


# ---------------------------------------------------------------------------
# French (issue #726) — the same pipeline with the French prompt and a parallel
# deck tree. The tree matters: every language filter in the app keys off
# decks.lang, so a French card in a zh deck is invisible under the fr tab and
# turns up in the Chinese review queue instead.
# ---------------------------------------------------------------------------

ENTRY_YAML_FR = """- type: word
  date: "08/13"
  word: séjour
  pos: nom (m)
  english: stay, sojourn
  german: Aufenthalt
  level: "B1"
  register: neutral
  note: |
    Bezeichnet den Aufenthalt an einem Ort.

    **Étymologie:** Vom altfranzösischen *sejorner*.
  examples:
    - fr: Bon séjour à Paris !
      english: Enjoy your stay in Paris!
      german: Schönen Aufenthalt in Paris!
  synonyms:
    - word: visite
      meaning: Besuch
"""

ENTRY_YAML_FR_VERB = """- type: word
  date: "08/13"
  word: parler
  pos: verbe
  english: to speak
  german: sprechen
  level: "A1"
  register: neutral
  note: |
    Regelmäßiges Verb auf -er.
  examples:
    - fr: Je parle français.
      english: I speak French.
      german: Ich spreche Französisch.
  conjugations:
    présent:
      je: parle
      tu: parles
      il/elle: parle
    participe passé: parlé (avoir)
"""


def _fr_leaf_decks(day=None):
    day = day or database.anki_today().isoformat()
    deck_id, _ = database.get_or_create_daily_deck(day, "fr")
    return database.get_or_create_category_decks(deck_id, day)


def test_french_word_lands_in_the_french_deck_tree(tmp_db):
    result = _run_add_word("séjour", yaml_text=ENTRY_YAML_FR, lang="fr")
    assert result["job"]["status"] == "done", result["job"]
    assert result["job"]["summary"]["imported"] == 1
    assert result["deck_path"] == f"Français::{database.anki_today().isoformat()}"

    entry = database.get_word_by_zh("séjour")
    assert entry["lang"] == "fr"
    assert entry["definition_de"] == "Aufenthalt"
    # level: "B1" → the shared 1-6 scale (#596)
    assert entry["hsk_level"] == 3

    conn = database.get_db()
    rows = conn.execute(
        """SELECT c.deck_id, d.lang FROM cards c JOIN decks d ON d.id = c.deck_id
           WHERE c.word_id=? AND c.deleted_at IS NULL""",
        (entry["id"],),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == set(_fr_leaf_decks().values())
    # The whole point: the decks themselves are French, not just the entry.
    assert all(r["lang"] == "fr" for r in rows)


def test_french_entry_keeps_its_conjugations(tmp_db):
    """The French format's one structural extra (#596) has to survive the trip
    — otherwise the in-app entry is poorer than a hand-imported one."""
    _run_add_word("parler", yaml_text=ENTRY_YAML_FR_VERB, lang="fr")
    entry = database.get_word_by_zh("parler")
    detail = database.get_word_full(entry["id"])
    forms = {(c["tense"], c["person"]): c["form"] for c in detail["conjugations"]}
    assert forms[("présent", "je")] == "parle"
    # Impersonal forms are stored with an empty person.
    assert forms[("participe passé", "")] == "parlé (avoir)"


def test_french_word_is_listed_in_the_french_saved_deck(tmp_db):
    """★ List must stage the word inside the French tree; the Chinese 'Saved'
    deck would hide it under the fr tab."""
    _run_add_word("séjour", yaml_text=ENTRY_YAML_FR, lang="fr", day="list")
    entry = database.get_word_by_zh("séjour")

    fr_saved = database.get_or_create_saved_deck("fr")
    assert fr_saved != _saved_deck_id(), "French words must not share the zh Saved deck"
    conn = database.get_db()
    rows = conn.execute(
        "SELECT deck_id, state FROM cards WHERE word_id=? AND deleted_at IS NULL",
        (entry["id"],),
    ).fetchall()
    conn.close()
    assert {r["deck_id"] for r in rows} == {fr_saved}
    assert all(r["state"] == "suspended" for r in rows)
    # Browse's saved view matches on the deck *name*, which the path's last
    # segment still is — that is why no Browse change was needed.
    assert database.get_deck(fr_saved)["name"] == "Saved"


def test_listed_french_word_is_promoted_inside_its_own_tree(tmp_db):
    _run_add_word("séjour", yaml_text=ENTRY_YAML_FR, lang="fr", day="list")
    entry_id = database.get_word_by_zh("séjour")["id"]

    r = client.post(f"/api/saved/{entry_id}/promote")
    assert r.status_code == 200, r.text
    today = database.anki_today().isoformat()
    assert r.json()["deck_path"] == f"Français::{today}"

    conn = database.get_db()
    decks = {row["deck_id"] for row in conn.execute(
        "SELECT deck_id FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,))}
    conn.close()
    assert decks == set(_fr_leaf_decks(today).values())


def test_existing_word_ignores_a_wrong_lang_and_follows_its_own(tmp_db):
    """word_zh is globally unique, so trusting the request's lang would scatter
    one word's cards over two language trees and hide it under both tabs."""
    _run_add_word("séjour", yaml_text=ENTRY_YAML_FR, lang="fr", day="list")
    entry_id = database.get_word_by_zh("séjour")["id"]

    with patch.object(ai, "_call_api", side_effect=AssertionError("AI was called")):
        body = client.post("/api/add-word-ai",
                           json={"word_zh": "séjour", "lang": "zh", "confirm": True}).json()

    assert body["deck_path"].startswith("Français::")
    conn = database.get_db()
    decks = {row["deck_id"] for row in conn.execute(
        "SELECT deck_id FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,))}
    conn.close()
    assert decks == set(_fr_leaf_decks().values())


def test_wrong_script_is_rejected_per_language(tmp_db):
    """The box takes no follow-up questions, so a word in the wrong script must
    fail loudly instead of being fed to the wrong prompt."""
    assert client.post("/api/add-word-ai",
                       json={"word_zh": "生态", "lang": "fr"}).status_code == 400
    assert client.post("/api/add-word-ai",
                       json={"word_zh": "séjour", "lang": "zh"}).status_code == 400


def test_unknown_lang_is_rejected(tmp_db):
    # 'es' used to be the stand-in "unsupported language" here, but #803 adds
    # it to languages.py as a real (foundation-only) language, so it's no
    # longer unknown — use a code that will never be a real entry here.
    assert client.post("/api/add-word-ai",
                       json={"word_zh": "séjour", "lang": "xx"}).status_code == 400


def test_french_prompt_is_used_for_french(tmp_db):
    """Sending the Chinese prompt would produce a Chinese-format entry that the
    French half of the importer can't read."""
    with patch.object(ai, "_call_api", return_value=ENTRY_YAML_FR) as call:
        ai.generate_word_entry_yaml("séjour", lang="fr")
    prompt = call.call_args[0][1][0]["content"]
    assert "French dictionary expert" in prompt
    assert "CEFR" in prompt and "conjugations" in prompt


def test_french_yaml_carries_its_lang_header(tmp_db):
    """The document states its language rather than relying on the target
    deck's — the entry format and the lang have to agree."""
    with patch.object(ai, "_call_api", return_value=ENTRY_YAML_FR):
        out = ai.generate_word_entry_yaml("séjour", lang="fr")
    import yaml as _yaml
    doc = _yaml.safe_load(out)
    assert doc["lang"] == "fr"
    assert doc["entries"][0]["word"] == "séjour"


def test_add_page_and_modal_pass_the_language():
    """One client-side pipeline (#643): both entry points hand lang to the same
    shared helper, neither talks to the endpoint directly."""
    assert "lang) {" in _static("shared.js") or "onUpdate, lang)" in _static("shared.js")
    assert "_addWordLang" in _static("app.js")
    assert "params.get('lang')" in _static("add.html")
    assert "/api/add-word-ai" not in _static("add.html")


def test_promote_uses_the_anki_day_not_the_calendar_day(tmp_db):
    """Between midnight and the day cutoff the calendar day is one ahead of the
    Anki day (#851). The deck name has to follow the Anki day, because the due
    dates importer._create_cards writes and the future-deck lock both do — a
    card due today inside a deck dated tomorrow is locked away for the day.

    Stubbing anki_today() to a different day is what pins that down: production
    code reading the calendar clock instead would build a deck for some other
    date than the one the cards are due on.
    """
    _run_add_word("生态", day="list")
    entry_id = database.get_word_by_zh("生态")["id"]

    stub_day = database.anki_today() - timedelta(days=1)
    with patch.object(database, "anki_today", return_value=stub_day):
        r = client.post(f"/api/saved/{entry_id}/promote")
    assert r.status_code == 200, r.text
    assert r.json()["deck_path"] == f"Daily::{stub_day.isoformat()}"

    conn = database.get_db()
    rows = conn.execute(
        "SELECT due FROM cards WHERE word_id=? AND deleted_at IS NULL", (entry_id,)
    ).fetchall()
    conn.close()
    assert all(row["due"] == stub_day.isoformat() for row in rows)

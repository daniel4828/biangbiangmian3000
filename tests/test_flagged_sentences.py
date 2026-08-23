"""Tests for flagging story sentences (issue #854, the mirror of starring #692).

Flagged sentences are the negative examples Daniel collects while reviewing —
sentences that read badly (grammar mistakes, awkward phrasing) — to feed back
into prompt tuning. Same requirement as starring: a flagged sentence still has
to carry which prompt made it (mode/model/episode_id from the story's
gen_params), because that context is the entire point.
"""

import sqlite3

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import database
import database.core
import main

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # database.core.DB_PATH, not database.DB_PATH — see conftest.py (#615).
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path / "test.db"


def _make_story(deck_id=None, *, mode="knowledge", lang="zh", category="reading"):
    """Create a one-sentence story and return (story_id, sentence_id)."""
    if deck_id is None:
        deck_id = database.get_or_create_deck("FlagDeck")
    story_id = database.create_story(
        "2026-08-22", category, deck_id,
        [{
            "position": 0,
            "sentence_zh": "他把这句话说得很别扭。",
            "sentence_en": "He phrased this sentence awkwardly.",
            "sentence_de": "Er formulierte diesen Satz umständlich.",
            "source_title": "某播客单集",
            "source_url": "https://example.com/ep1",
        }],
        gen_params={"mode": mode, "model": "deepseek-chat", "episode_id": 7},
        lang=lang,
    )
    sentences = database.get_story_sentences(story_id)
    return story_id, sentences[0]["id"]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migration_adds_columns_to_legacy_db(tmp_path, monkeypatch):
    """init_db() on a DB whose story_sentences predates #854 adds both columns,
    and running it a second time is a no-op (not an error)."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE story_sentences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        sentence_zh TEXT NOT NULL,
        sentence_en TEXT NOT NULL DEFAULT ''
    )""")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    database.init_db()  # idempotent

    conn = sqlite3.connect(db_file)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(story_sentences)")}
    conn.close()
    assert {"flagged", "flagged_at"} <= cols


def test_existing_sentences_default_to_unflagged(tmp_db):
    _make_story()
    assert database.get_flagged_sentences() == []


# ---------------------------------------------------------------------------
# database layer
# ---------------------------------------------------------------------------

def test_flag_and_unflag_round_trip(tmp_db):
    _, sentence_id = _make_story()

    result = database.set_sentence_flagged(sentence_id, True)
    assert result["flagged"] == 1
    assert result["flagged_at"]

    flagged = database.get_flagged_sentences()
    assert [s["id"] for s in flagged] == [sentence_id]

    result = database.set_sentence_flagged(sentence_id, False)
    assert result["flagged"] == 0
    assert result["flagged_at"] is None
    assert database.get_flagged_sentences() == []


def test_flag_unknown_sentence_returns_none(tmp_db):
    assert database.set_sentence_flagged(99999, True) is None


def test_flagged_list_carries_generation_context(tmp_db):
    """Without mode/source, a flagged sentence can't tell you which prompt to fix."""
    _, sentence_id = _make_story(mode="knowledge")
    database.set_sentence_flagged(sentence_id, True)

    s = database.get_flagged_sentences()[0]
    assert s["mode"] == "knowledge"
    assert s["model"] == "deepseek-chat"
    assert s["episode_id"] == 7
    assert s["story_date"] == "2026-08-22"
    assert s["deck_name"] == "FlagDeck"
    assert s["source_title"] == "某播客单集"
    assert s["sentence_de"] == "Er formulierte diesen Satz umständlich."


def test_flagged_list_newest_first(tmp_db):
    deck_id = database.get_or_create_deck("FlagDeck")
    _, first = _make_story(deck_id)
    _, second = _make_story(deck_id, category="listening")

    database.set_sentence_flagged(first, True)
    database.set_sentence_flagged(second, True)
    # Same-second timestamps are broken by id DESC, so the later flag still wins.
    assert [s["id"] for s in database.get_flagged_sentences()][0] == second


def test_flagged_list_filters_by_lang(tmp_db):
    deck_id = database.get_or_create_deck("FlagDeck")
    _, zh = _make_story(deck_id, lang="zh")
    _, fr = _make_story(deck_id, lang="fr", category="listening")
    database.set_sentence_flagged(zh, True)
    database.set_sentence_flagged(fr, True)

    assert [s["id"] for s in database.get_flagged_sentences(lang="fr")] == [fr]
    assert [s["id"] for s in database.get_flagged_sentences(lang="zh")] == [zh]
    assert len(database.get_flagged_sentences()) == 2


def test_star_and_flag_are_independent(tmp_db):
    """Daniel 2026-08-22 decided these are independent, not a three-way toggle:
    a sentence can be starred, flagged, both, or neither at the same time."""
    _, sentence_id = _make_story()
    database.set_sentence_starred(sentence_id, True)
    database.set_sentence_flagged(sentence_id, True)

    assert [s["id"] for s in database.get_starred_sentences()] == [sentence_id]
    assert [s["id"] for s in database.get_flagged_sentences()] == [sentence_id]

    database.set_sentence_starred(sentence_id, False)
    # Unstarring must not touch the flag.
    assert database.get_starred_sentences() == []
    assert [s["id"] for s in database.get_flagged_sentences()] == [sentence_id]


def test_again_regenerated_sentence_can_be_flagged(tmp_db):
    """Again-regen sentences live under the 'again' sentinel category but are
    ordinary story_sentences rows — flagging must work there too, since that's
    exactly where a freshly generated (possibly bad) sentence gets judged."""
    deck_id = database.get_or_create_deck("FlagDeck")
    conn = database.get_db()
    word_id = conn.execute(
        "INSERT INTO entries (word_zh, definition) VALUES ('别扭', 'awkward')"
    ).lastrowid
    conn.commit()
    conn.close()
    database.store_again_sentence(
        deck_id, word_id,
        {"sentence_zh": "说得很别扭。", "sentence_en": "Said awkwardly."},
        "2026-08-22",
    )
    again = database.get_again_sentence_for_word(word_id, "2026-08-22")

    assert database.set_sentence_flagged(again["id"], True)["flagged"] == 1
    assert [s["id"] for s in database.get_flagged_sentences()] == [again["id"]]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_flag_round_trip(tmp_db):
    _, sentence_id = _make_story()

    r = client.post(f"/api/story-sentence/{sentence_id}/flag", json={"flagged": True})
    assert r.status_code == 200
    assert r.json()["flagged"] == 1

    r = client.get("/api/flagged-sentences")
    assert r.status_code == 200
    body = r.json()["sentences"]
    assert [s["id"] for s in body] == [sentence_id]
    assert body[0]["mode"] == "knowledge"

    r = client.post(f"/api/story-sentence/{sentence_id}/flag", json={"flagged": False})
    assert r.json()["flagged"] == 0
    assert client.get("/api/flagged-sentences").json()["sentences"] == []


def test_api_flag_defaults_to_flagging(tmp_db):
    _, sentence_id = _make_story()
    r = client.post(f"/api/story-sentence/{sentence_id}/flag", json={})
    assert r.json()["flagged"] == 1


def test_api_flag_unknown_sentence_404(tmp_db):
    r = client.post("/api/story-sentence/99999/flag", json={"flagged": True})
    assert r.status_code == 404


def test_api_flagged_sentences_lang_filter(tmp_db):
    deck_id = database.get_or_create_deck("FlagDeck")
    _, zh = _make_story(deck_id, lang="zh")
    _, fr = _make_story(deck_id, lang="fr", category="listening")
    client.post(f"/api/story-sentence/{zh}/flag", json={"flagged": True})
    client.post(f"/api/story-sentence/{fr}/flag", json={"flagged": True})

    body = client.get("/api/flagged-sentences?lang=fr").json()["sentences"]
    assert [s["id"] for s in body] == [fr]


# ---------------------------------------------------------------------------
# Linking a flagged sentence back to the prompt that made it (#697, same rule)
# ---------------------------------------------------------------------------

def test_flagged_list_links_to_its_story_without_inlining_the_prompt(tmp_db):
    """A knowledge prompt embeds up to 15000 chars of transcript. Inlining it in a
    500-row list would make the response tens of MB — so the list carries the link
    (story_id) and a has_prompt flag, and the text is fetched on demand."""
    story_id, sentence_id = _make_story()
    conn = database.get_db()
    conn.execute("UPDATE stories SET prompt_text = ? WHERE id = ?",
                 ("完整的提示词正文……", story_id))
    conn.commit()
    conn.close()
    database.set_sentence_flagged(sentence_id, True)

    s = database.get_flagged_sentences()[0]
    assert s["story_id"] == story_id
    assert s["has_prompt"] == 1
    assert "prompt_text" not in s
    assert "完整的提示词正文" not in str(s)


def test_has_prompt_false_when_prompt_was_stripped(tmp_db):
    """The offline snapshot clears stories.prompt_text (offline_sync_server.py), and
    legacy stories predate the column — the UI has to be able to say so."""
    _, sentence_id = _make_story()
    database.set_sentence_flagged(sentence_id, True)
    assert database.get_flagged_sentences()[0]["has_prompt"] == 0

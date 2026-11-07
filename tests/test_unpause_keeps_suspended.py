"""Issue #1462: the ▶ (un-pause) toggles must only wake cards the ⏸ button paused.

★ List cards (decks named 'Saved') and leeches are suspended for other reasons.
They used to be reactivated by un-pausing (Saved cards became 'new').
"""
import pytest

import database


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()

    root = database.get_all_deck_id()
    daily = database.get_or_create_deck_path("Daily::2026-11-07 · Creating")
    saved = database.get_or_create_deck_path("Daily::Saved")

    def card(word_zh, deck_id):
        wid = database.insert_word({"word_zh": word_zh, "lang": "zh"})
        return database.insert_card(wid, "creating", deck_id, state="review")

    active = card("生态", daily)
    leech = card("环境", daily)
    saved_card = card("保护", saved)

    conn = database.get_db()
    conn.execute(
        "UPDATE cards SET is_leech=1, state='suspended', pre_suspend_state='review' "
        "WHERE id=?", (leech,))
    conn.execute(
        "UPDATE cards SET state='suspended', pre_suspend_state=NULL WHERE id=?",
        (saved_card,))
    conn.commit()
    conn.close()
    return {"root": root, "daily": daily, "saved": saved, "active": active,
            "leech": leech, "saved_card": saved_card}


def _state(card_id):
    conn = database.get_db()
    row = conn.execute("SELECT state FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return row["state"]


def _assert_only_active_restored(s):
    assert _state(s["active"]) == "review"
    assert _state(s["saved_card"]) == "suspended"
    assert _state(s["leech"]) == "suspended"


def test_creating_toggle_keeps_leech_suspended(setup):
    s = setup
    database.toggle_deck_creating_suspension(s["daily"])
    assert _state(s["active"]) == "suspended"
    database.toggle_deck_creating_suspension(s["daily"])
    assert _state(s["active"]) == "review"
    assert _state(s["leech"]) == "suspended"


def test_creating_toggle_keeps_saved_suspended(setup):
    s = setup
    # Everything in the Saved deck is already suspended -> this is the 'play' half.
    database.toggle_deck_creating_suspension(s["saved"])
    assert _state(s["saved_card"]) == "suspended"


def test_category_toggle_keeps_saved_and_leech_suspended(setup):
    s = setup
    database.toggle_category_suspension(s["root"], "creating")
    assert _state(s["active"]) == "suspended"
    database.toggle_category_suspension(s["root"], "creating")
    _assert_only_active_restored(s)


def test_all_toggle_keeps_saved_and_leech_suspended(setup):
    s = setup
    database.toggle_deck_all_suspension(s["root"])
    assert _state(s["active"]) == "suspended"
    database.toggle_deck_all_suspension(s["root"])
    _assert_only_active_restored(s)

"""Tests for issue #1291: card timeline uses real recorded intervals.

The old SM-2 replay inflated review intervals (FSRS is the real scheduler), so
the graph showed a fake drop at the final "scheduled" point. Review-state points
now take review_log.last_interval of the next row (cards.interval for the last
review); replay is only the fallback for legacy rows.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # database.core.DB_PATH, never database.DB_PATH (#615).
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _make_card(interval_before_list):
    """Review-state card with interval 10, due in the future, and one Good
    review per entry of interval_before_list (the review_log.last_interval)."""
    word_id = database.insert_word({
        "word_zh": "测试", "lang": "zh", "pinyin": "cèshì", "definition": "test",
        "pos": None, "hsk_level": None, "traditional": None, "definition_zh": None,
        "source": "test", "note_type": "vocabulary", "notes": None, "date_yaml": None,
        "source_sentence": None, "grammar_notes": None, "register": None,
        "definition_de": None, "definition_fr": None,
    })
    deck_id = database.get_or_create_deck("TimelineDeck")
    card_id = database.insert_card(word_id, "listening", deck_id)
    conn = database.get_db()
    conn.execute(
        "UPDATE cards SET state='review', interval=10, due='2099-01-01' WHERE id=?",
        (card_id,),
    )
    conn.commit()
    conn.close()
    for i, li in enumerate(interval_before_list):
        rid = database.insert_review(card_id, 3, state="review", last_interval=li)
        conn = database.get_db()
        conn.execute(
            "UPDATE review_log SET reviewed_at=? WHERE id=?",
            (f"2026-01-0{i + 1} 12:00:00", rid),
        )
        conn.commit()
        conn.close()
    return card_id


def _check_real_gaps(card):
    assert card["points"][0]["gap"] == 5.0
    assert card["points"][-1]["gap"] == 10.0
    assert card["scheduled"]["gap"] == 10.0


def test_review_points_use_real_intervals(tmp_db):
    card_id = _make_card([None, 5])
    card = database.get_card_timeline_data(card_id)["cards"][0]
    _check_real_gaps(card)


def test_session_timelines_use_real_intervals(tmp_db):
    card_id = _make_card([None, 5])
    card = database.get_session_timelines([card_id])["cards"][0]
    _check_real_gaps(card)


def test_legacy_null_last_interval_keeps_replayed_gap(tmp_db):
    card_id = _make_card([None, None])
    card = database.get_card_timeline_data(card_id)["cards"][0]
    first = card["points"][0]["gap"]
    # Not replaced by a real interval: replay from a new card gives the minimum interval.
    assert first is not None
    assert first < 5.0

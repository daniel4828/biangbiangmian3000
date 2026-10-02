"""
Tests for SM-2 scheduling logic in srs.py.

These tests cover pure dict-manipulation functions only — no DB is touched.
We call _handle_learning / _handle_review / _handle_relearn directly with
a minimal card dict so we can assert exact state transitions.
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import srs
import fsrs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_PRESET = {
    "learning_steps":      "1 10",
    "graduating_interval": 1,
    "easy_interval":       4,
    "relearning_steps":    "10",
    "minimum_interval":    1,
    "learned_interval":    3,
    "leech_threshold":     8,
    "learning_leech_threshold": 6,
    "leech_action":        "suspend",
    "enable_fsrs":         0,   # SM-2 paths are deterministic (modulo fuzz)
}

def make_card(state="new", step_index=0, interval=1, ease=2.5, lapses=0,
              repetitions=0, learning_again_count=0, probation=0):
    return {
        "id": 1,
        "state":        state,
        "step_index":   step_index,
        "interval":     interval,
        "ease":         ease,
        "lapses":       lapses,
        "repetitions":  repetitions,
        "learning_again_count": learning_again_count,
        "probation":    probation,
        "due":          "2026-01-01",
    }


# ---------------------------------------------------------------------------
# calc_review — pure math
# ---------------------------------------------------------------------------

class TestCalcReview:
    def test_again_reduces_ease_and_halves_interval(self):
        interval, ease, lapses = srs.calc_review(10, 2.5, 0, rating=1)
        assert interval == 5          # floor(10 * 0.5)
        assert ease == 2.3            # 2.5 - 0.20
        assert lapses == 1

    def test_again_ease_floor_at_1_3(self):
        _, ease, _ = srs.calc_review(10, 1.4, 0, rating=1)
        assert ease == pytest_approx(1.3)

    def test_hard_reduces_ease_multiplies_1_2(self):
        interval, ease, lapses = srs.calc_review(10, 2.5, 0, rating=2)
        assert interval == 12         # floor(10 * 1.2)
        assert ease == pytest_approx(2.35)  # 2.5 - 0.15
        assert lapses == 0

    def test_good_keeps_ease_multiplies_by_ease(self):
        interval, ease, lapses = srs.calc_review(10, 2.5, 0, rating=3)
        assert interval == 25         # floor(10 * 2.5)
        assert ease == 2.5
        assert lapses == 0

    def test_easy_increases_ease_multiplies_extra(self):
        interval, ease, lapses = srs.calc_review(10, 2.5, 0, rating=4)
        new_ease = 2.5 + 0.15         # = 2.65
        expected_interval = math.floor(10 * new_ease * 1.3)
        assert interval == expected_interval
        assert ease == pytest_approx(new_ease)
        assert lapses == 0

    def test_interval_minimum_1(self):
        # Very short interval should never go below 1
        interval, _, _ = srs.calc_review(1, 1.3, 0, rating=1)
        assert interval >= 1


# ---------------------------------------------------------------------------
# _handle_learning — new card through learning steps
# ---------------------------------------------------------------------------

class TestHandleLearning:
    def test_new_card_good_advances_to_step_1(self):
        card = make_card(state="new", step_index=0)
        result = srs._handle_learning(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "learning"
        assert result["step_index"] == 1

    def test_learning_step_1_good_enters_probation(self, monkeypatch):
        # step_index=1 is the last step in "1 10", so Good finishes the steps —
        # the Good compromise gets 2d, and the card stays 'learning' (probation)
        # until it survives an interval of >= learned_interval days.
        monkeypatch.setattr(srs, "_fuzz_interval", lambda days: days)
        card = make_card(state="learning", step_index=1)
        result = srs._handle_learning(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "learning"
        assert result["probation"] == 1
        assert result["interval"] == 2
        assert result["step_index"] == 0
        assert result["repetitions"] == 1

    def test_again_resets_to_step_0(self):
        card = make_card(state="learning", step_index=1)
        result = srs._handle_learning(card, DEFAULT_PRESET, rating=1)
        assert result["state"] == "learning"
        assert result["step_index"] == 0

    def test_easy_finishes_steps_into_probation(self):
        card = make_card(state="new", step_index=0)
        result = srs._handle_learning(card, DEFAULT_PRESET, rating=4)
        assert result["state"] == "learning"
        assert result["probation"] == 1
        # easy_interval=4 with ±1 day of fuzz
        assert 3 <= result["interval"] <= 5
        assert result["repetitions"] == 1

    def test_hard_at_step_0_uses_avg_delay(self):
        card = make_card(state="new", step_index=0)
        result = srs._handle_learning(card, DEFAULT_PRESET, rating=2)
        assert result["state"] == "learning"
        assert result["step_index"] == 0   # Hard doesn't advance the step


# ---------------------------------------------------------------------------
# _handle_review — review-phase card
# ---------------------------------------------------------------------------

class TestHandleReview:
    def test_good_increases_interval(self):
        card = make_card(state="review", interval=10, ease=2.5)
        result = srs._handle_review(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "review"
        # floor(10 * 2.5) = 25, with ±4 days of fuzz
        assert 21 <= result["interval"] <= 29

    def test_again_lapses_and_enters_relearn(self):
        card = make_card(state="review", interval=10, ease=2.5, lapses=0)
        result = srs._handle_review(card, DEFAULT_PRESET, rating=1)
        assert result["state"] == "relearn"
        assert result["lapses"] == 1
        assert result["step_index"] == 0

    def test_hard_does_not_lapse(self):
        card = make_card(state="review", interval=10, ease=2.5, lapses=0)
        result = srs._handle_review(card, DEFAULT_PRESET, rating=2)
        assert result["state"] == "review"
        assert result["lapses"] == 0

    def test_easy_increases_ease_and_interval(self):
        card = make_card(state="review", interval=10, ease=2.5)
        result = srs._handle_review(card, DEFAULT_PRESET, rating=4)
        assert result["ease"] == pytest_approx(2.65)
        assert result["interval"] > 10

    def test_minimum_interval_respected(self):
        preset = {**DEFAULT_PRESET, "minimum_interval": 3}
        card = make_card(state="review", interval=1, ease=1.3, lapses=0)
        result = srs._handle_review(card, preset, rating=1)
        assert result["interval"] >= 3


# ---------------------------------------------------------------------------
# _handle_relearn — lapsed card relearning
# ---------------------------------------------------------------------------

class TestHandleRelearn:
    def test_good_between_hard_and_easy_when_day_steps_collide(self, monkeypatch):
        monkeypatch.setattr(srs, "_fuzz_interval", lambda days: days)
        preset = {**FSRS_PRESET, "relearning_steps": "10m 1d",
                  "learning_hard_1d": 1}
        card = {**make_card(state="relearn", step_index=0), **preset,
                "stability": 5.5, "difficulty": 5.0}

        assert srs.preview_intervals(card) == {
            1: "10m", 2: "1d", 3: "7d", 4: "13d",
        }
        result = srs._handle_relearn(card, preset, rating=3)
        assert result["state"] == "review"
        assert result["interval"] == 7
        assert result["due"] == srs.next_review_due(7)

    def test_good_compromise_never_fuzzes_back_to_one_day(self, monkeypatch):
        monkeypatch.setattr(srs, "_fuzz_interval", lambda days: 1)
        preset = {**DEFAULT_PRESET, "relearning_steps": "10m 1d",
                  "learning_hard_1d": 1}
        card = {**make_card(state="relearn", step_index=0, interval=2), **preset}

        result = srs._handle_relearn(card, preset, rating=3)

        assert result["interval"] == 2

    def test_good_at_last_step_enters_probation(self):
        # "10" is a single-step relearn; step_index=0 is the last step.
        # Finishing the steps no longer returns straight to 'review' — the card
        # stays 'relearn' in probation until it survives >= learned_interval days.
        card = make_card(state="relearn", step_index=0, interval=5)
        result = srs._handle_relearn(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "relearn"
        assert result["probation"] == 1
        # interval 5 preserved, ±1 day of fuzz
        assert 4 <= result["interval"] <= 6
        assert result["repetitions"] == 1

    def test_again_stays_in_relearn_at_step_0(self):
        card = make_card(state="relearn", step_index=0, interval=5)
        result = srs._handle_relearn(card, DEFAULT_PRESET, rating=1)
        assert result["state"] == "relearn"
        assert result["step_index"] == 0

    def test_easy_finishes_steps_into_probation(self):
        card = make_card(state="relearn", step_index=0, interval=5)
        result = srs._handle_relearn(card, DEFAULT_PRESET, rating=4)
        assert result["state"] == "relearn"
        assert result["probation"] == 1
        assert result["interval"] > 5


# ---------------------------------------------------------------------------
# _handle_probation — steps finished, surviving the first day-intervals
# ---------------------------------------------------------------------------

class TestHandleProbation:
    def test_again_restarts_steps_without_lapse(self):
        card = make_card(state="learning", probation=1, interval=3, lapses=0)
        result = srs._handle_probation(card, DEFAULT_PRESET, rating=1)
        assert result["lapses"] == 0            # the whole point: no lapse
        assert result["state"] == "learning"
        assert result["probation"] == 0
        assert result["step_index"] == 0
        assert result["interval"] == 1          # floor(3 * 0.5)

    def test_relearn_again_restarts_steps_without_lapse(self):
        card = make_card(state="relearn", probation=1, interval=3, lapses=2)
        result = srs._handle_probation(card, DEFAULT_PRESET, rating=1)
        assert result["lapses"] == 2            # unchanged
        assert result["state"] == "relearn"
        assert result["probation"] == 0
        assert result["step_index"] == 0

    def test_good_after_surviving_threshold_graduates(self):
        # survived interval 3 >= learned_interval 3 → real 'review' card
        card = make_card(state="learning", probation=1, interval=3)
        result = srs._handle_probation(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "review"
        assert result["probation"] == 0
        # floor(3 * 2.5) = 7, ±2 days of fuzz
        assert 5 <= result["interval"] <= 9

    def test_good_below_threshold_stays_in_probation(self):
        # survived interval 1 < learned_interval 3 → interval grows, still probation
        card = make_card(state="learning", probation=1, interval=1)
        result = srs._handle_probation(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "learning"
        assert result["probation"] == 1
        # floor(1 * 2.5) = 2, ±1 day of fuzz
        assert 1 <= result["interval"] <= 3
        assert result["lapses"] == 0

    def test_relearn_good_after_threshold_returns_to_review(self):
        card = make_card(state="relearn", probation=1, interval=4, lapses=1)
        result = srs._handle_probation(card, DEFAULT_PRESET, rating=3)
        assert result["state"] == "review"
        assert result["probation"] == 0
        assert result["lapses"] == 1            # unchanged by passing


# ---------------------------------------------------------------------------
# Learning-phase FSRS short-term memory (#470)
# ---------------------------------------------------------------------------

FSRS_PRESET = {
    "learning_steps":           "10",
    "graduating_interval":      1,
    "easy_interval":            4,
    "relearning_steps":         "10",
    "minimum_interval":         1,
    "learned_interval":         3,
    "leech_threshold":          8,
    "learning_leech_threshold": 6,
    "leech_action":             "suspend",
    "enable_fsrs":              1,
    "enable_probation":         0,
    "desired_retention":        0.9,
    "maximum_interval":         36500,
    "fsrs_weights":             None,
    "learning_hard_1d":         0,
    "learning_hard_days":       1,
}

W = fsrs.DEFAULT_WEIGHTS


class TestLearningShortTermMemory:
    def test_learning_good_uses_compromise_when_day_steps_collide(self, monkeypatch):
        monkeypatch.setattr(srs, "_fuzz_interval", lambda days: days)
        preset = {**FSRS_PRESET, "learning_steps": "10m 1d",
                  "learning_hard_1d": 1}
        card = {**make_card(state="learning", step_index=0), **preset,
                "stability": 5.5, "difficulty": 5.0}

        assert srs.preview_intervals(card)[3] == "7d"
        result = srs._handle_learning(card, preset, rating=3)
        assert result["state"] == "review"
        assert result["interval"] == 7

    def test_learning_again_seeds_memory(self):
        card = make_card(state="new", step_index=0)
        card["stability"] = None
        card["difficulty"] = None
        result = srs._handle_learning(card, FSRS_PRESET, rating=1)
        assert result["stability"] == pytest.approx(W[0])  # w[0] = 0.40255
        assert result["difficulty"] == pytest.approx(fsrs.init_difficulty(W, 1))

    def test_learning_hard_shrinks_stability(self):
        card = make_card(state="learning", step_index=0)
        card["stability"] = 3.173
        card["difficulty"] = 5.0
        result = srs._handle_learning(card, FSRS_PRESET, rating=2)
        expected = 3.173 * math.exp(0.51655 * (-1 + 0.6621))
        assert result["stability"] == pytest.approx(expected)

    def test_again_then_good_graduates_at_1d(self):
        card = make_card(state="new", step_index=0)
        card["stability"] = None
        card["difficulty"] = None
        after_again = srs._handle_learning(card, FSRS_PRESET, rating=1)
        graduated = srs._handle_learning(after_again, FSRS_PRESET, rating=3)
        assert graduated["state"] == "review"
        assert graduated["interval"] == 1

    def test_pure_good_graduation_unchanged(self, monkeypatch):
        monkeypatch.setattr(srs, "_fuzz_interval", lambda x: x)
        card = make_card(state="new", step_index=0)
        card["stability"] = None
        card["difficulty"] = None
        result = srs._handle_learning(card, FSRS_PRESET, rating=3)
        assert result["state"] == "review"
        assert result["interval"] == 3

        preview_card = make_card(state="new", step_index=0)
        preview_card["stability"] = None
        preview_card["difficulty"] = None
        preview_card = {**preview_card, **FSRS_PRESET}
        assert srs.preview_intervals(preview_card)[3] == "3d"

    def test_fsrs_off_no_seeding(self):
        preset = {**FSRS_PRESET, "enable_fsrs": 0}
        card = make_card(state="new", step_index=0)
        card["stability"] = None
        card["difficulty"] = None
        result = srs._handle_learning(card, preset, rating=1)
        assert result.get("stability") is None

    def test_preview_adapts_after_seeding(self):
        card = make_card(state="learning", step_index=0)
        card["stability"] = 0.57
        card["difficulty"] = 5.0
        card = {**card, **FSRS_PRESET}
        assert srs.preview_intervals(card)[3] == "1d"


# ---------------------------------------------------------------------------
# _kouyu_hsk_to_int — importer helper
# ---------------------------------------------------------------------------

from importer import _kouyu_hsk_to_int

class TestKouyuHskToInt:
    def test_plain_number(self):
        assert _kouyu_hsk_to_int("4") == 4

    def test_slash_takes_higher(self):
        assert _kouyu_hsk_to_int("4/5") == 5

    def test_chaogong_returns_none(self):
        assert _kouyu_hsk_to_int("超纲") is None

    def test_empty_string_returns_none(self):
        assert _kouyu_hsk_to_int("") is None

    def test_none_input_returns_none(self):
        assert _kouyu_hsk_to_int(None) is None


# ---------------------------------------------------------------------------
# pytest_approx shorthand
# ---------------------------------------------------------------------------

def pytest_approx(val, rel=1e-6):
    """Thin wrapper so tests read naturally."""
    import pytest
    return pytest.approx(val, rel=rel)

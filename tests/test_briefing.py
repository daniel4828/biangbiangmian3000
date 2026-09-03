"""
Tests for the briefing mode rework (issue #444) and News flow v2 (issue #454):
  - validate_briefing_items: Python-only (no AI) validation of raw AI output,
    including article_idx monotonicity (#454)
  - _dedupe_consecutive_briefing_context: fallback repair for consecutive context runs
  - generate_briefing_sentences: single validation retry is triggered on violations,
    plus the AI fact-check retry path (#454)
  - resolve_briefing_model: env var + OpenAI models-API verification + fallback chain
  - database.get_story_position_map: word_id → sentence position for queue ordering (#454)
"""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import ai
import database
import database.core

CARDS = [
    {"word_id": 1, "word_zh": "担心", "pinyin": "dān xīn", "definition": "to worry"},
    {"word_id": 2, "word_zh": "努力", "pinyin": "nǔ lì", "definition": "to work hard"},
    {"word_id": 3, "word_zh": "进步", "pinyin": "jìn bù", "definition": "progress"},
]

ARTICLES = [{"url": "https://example.com/a", "title": "标题", "text": "正文内容"}]


# ---------------------------------------------------------------------------
# validate_briefing_items
# ---------------------------------------------------------------------------

class TestValidateBriefingItems:

    def test_valid_output_has_no_issues(self):
        items = [
            {"sentence_zh": "今天天气很好。", "target_word": None},
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        assert ai.validate_briefing_items(items, CARDS) == []

    def test_missing_word_detected(self):
        items = [
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("进步" in i for i in issues)

    def test_duplicated_word_detected(self):
        items = [
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他也很担心结果。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("重复" in i and "担心" in i for i in issues)

    def test_overlong_context_run_detected(self):
        """#1027: a run longer than BRIEFING_MAX_CONTEXT_RUN is still a
        violation — the budget was widened, not removed."""
        run = [{"sentence_de": f"Kontextsatz {i}.", "target_word": None}
               for i in range(ai.BRIEFING_MAX_CONTEXT_RUN + 1)]
        items = run + [
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("连续" in i for i in issues)

    def test_context_run_up_to_the_limit_is_fine(self):
        """#1027: several German context sentences in a row are the normal
        case now — that is how a topic without a matching target word gets
        told in full."""
        run = [{"sentence_de": f"Kontextsatz {i}.", "target_word": None}
               for i in range(ai.BRIEFING_MAX_CONTEXT_RUN)]
        items = run + [
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert not any("连续" in i for i in issues)

    def test_context_item_without_any_text_detected(self):
        """A context item carrying neither sentence_zh nor sentence_de is a
        hole in the summary, not something to skip silently (#1027)."""
        items = [
            {"target_word": None},
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("空句子" in i for i in issues)

    def test_overlong_german_context_sentence_detected(self):
        items = [
            {"sentence_de": "Wort " * (ai.BRIEFING_MAX_CONTEXT_CHARS // 2),
             "target_word": None},
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("德语上下文句子过长" in i for i in issues)

    def test_overlong_target_sentence_detected(self):
        long_sentence = "她" * 20 + "担心考试的结果会很不理想"
        items = [
            {"sentence_zh": long_sentence, "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("18" in i for i in issues)

    def test_too_many_sentences_detected(self):
        # 2 * 3 target words = 6 max; this has 7.
        items = [{"sentence_zh": f"上下文句{i}。", "target_word": None} for i in range(4)]
        items += [
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步"},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("超过" in i for i in issues)

    def test_monotonic_article_idx_is_fine(self):
        items = [
            {"sentence_zh": "今天天气很好。", "target_word": None, "article_idx": 0},
            {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 0},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 1},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 1},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert not any("回跳" in i for i in issues)

    def test_article_idx_backtrack_detected(self):
        items = [
            {"sentence_zh": "今天天气很好。", "target_word": None, "article_idx": 1},
            {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 1},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 1},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert any("回跳" in i for i in issues)

    def test_missing_article_idx_is_skipped_not_a_violation(self):
        items = [
            {"sentence_zh": "今天天气很好。", "target_word": None, "article_idx": 0},
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},  # no article_idx at all
            {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 1},
        ]
        issues = ai.validate_briefing_items(items, CARDS)
        assert not any("回跳" in i for i in issues)


# ---------------------------------------------------------------------------
# _dedupe_consecutive_briefing_context
# ---------------------------------------------------------------------------

class TestDedupeConsecutiveContext:

    def test_trims_run_back_to_the_limit(self):
        """#1027: the repair keeps the last BRIEFING_MAX_CONTEXT_RUN sentences
        of an over-long run instead of collapsing it to a single one — it drops
        content, and this mode now has to tell every topic in full."""
        n = ai.BRIEFING_MAX_CONTEXT_RUN
        items = [{"sentence_zh": f"上下文{i}。", "target_word": None}
                 for i in range(n + 2)]
        items.append({"sentence_zh": "她很担心考试。", "target_word": "担心"})
        fixed = ai._dedupe_consecutive_briefing_context(items, CARDS)
        context_sentences = [it["sentence_zh"] for it in fixed
                             if it["sentence_zh"] != "她很担心考试。"]
        assert context_sentences == [f"上下文{i}。" for i in range(2, n + 2)]
        assert len(fixed) == n + 1

    def test_no_op_when_already_valid(self):
        items = [
            {"sentence_zh": "上下文。", "target_word": None},
            {"sentence_zh": "她很担心考试。", "target_word": "担心"},
            {"sentence_zh": "他每天努力学习。", "target_word": "努力"},
        ]
        fixed = ai._dedupe_consecutive_briefing_context(items, CARDS)
        assert fixed == items

    def test_trailing_context_run_is_trimmed_to_the_limit(self):
        n = ai.BRIEFING_MAX_CONTEXT_RUN
        items = [{"sentence_zh": "她很担心考试。", "target_word": "担心"}]
        items += [{"sentence_zh": f"尾部上下文{i}。", "target_word": None}
                  for i in range(n + 1)]
        fixed = ai._dedupe_consecutive_briefing_context(items, CARDS)
        assert [it["sentence_zh"] for it in fixed] == (
            ["她很担心考试。"] + [f"尾部上下文{i}。" for i in range(1, n + 1)])


# ---------------------------------------------------------------------------
# generate_briefing_sentences — validation retry is triggered exactly once
# ---------------------------------------------------------------------------

VALID_ITEMS = [
    {"sentence_zh": "今天天气很好。", "target_word": None, "article_idx": 0},
    {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 0},
    {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
    {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 0},
]

# One more context sentence in a row than BRIEFING_MAX_CONTEXT_RUN allows (#1027).
INVALID_ITEMS_CONSECUTIVE_CONTEXT = [
    *({"sentence_de": f"Kontextsatz {i}.", "target_word": None, "article_idx": 0}
      for i in range(ai.BRIEFING_MAX_CONTEXT_RUN + 1)),
    {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 0},
    {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
    {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 0},
]


class TestGenerateBriefingSentencesRetry:

    def test_retries_once_on_validation_violation_then_accepts_fixed_result(self):
        """First AI response has consecutive context sentences; the retry
        response is valid. generate_briefing_sentences must call the AI
        exactly twice (initial + one validation retry) and return sentences
        built from the corrected (retry) response."""
        # fact_check_briefing makes its own _call_api call — mocked separately
        # (return []/"pass") since these tests only exercise the Python
        # validation retry path, not the fact-check path (issue #454).
        responses = [
            json.dumps(INVALID_ITEMS_CONSECUTIVE_CONTEXT),
            json.dumps(VALID_ITEMS),
        ]
        with patch("ai._call_api", side_effect=responses) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert mock_call.call_count == 2
        assert len(result) == len(CARDS)
        matched_words = {s["word_ids"][0] for s in result}
        assert matched_words == {c["word_id"] for c in CARDS}

    def test_no_retry_call_when_first_response_already_valid(self):
        """A valid first response must not trigger the extra validation-retry
        API call — only the (separate) missing-word retry loop may still run,
        and here there are no missing words so the loop exits after attempt 1."""
        with patch("ai._call_api", return_value=json.dumps(VALID_ITEMS)) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert mock_call.call_count == 1
        assert len(result) == len(CARDS)

    def test_persistent_violation_falls_back_to_dedupe_repair(self):
        """If the retry response STILL has consecutive context sentences, the
        dedupe fallback collapses them (dropping extras) instead of failing —
        every card must still end up with exactly one sentence."""
        responses = [
            json.dumps(INVALID_ITEMS_CONSECUTIVE_CONTEXT),
            json.dumps(INVALID_ITEMS_CONSECUTIVE_CONTEXT),
        ]
        with patch("ai._call_api", side_effect=responses) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert mock_call.call_count == 2
        assert len(result) == len(CARDS)
        matched_words = {s["word_ids"][0] for s in result}
        assert matched_words == {c["word_id"] for c in CARDS}

    def test_fact_check_issue_triggers_one_regeneration(self):
        """When fact_check_briefing reports issues on the first pass, the
        sentences are regenerated once more; a second fact-check pass (if any)
        is logged only and never triggers a further retry."""
        with patch("ai._call_api", return_value=json.dumps(VALID_ITEMS)) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", side_effect=[["句子1：数字不符"], []]) as mock_fc, \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        # 1 initial generation + 1 fact-check-triggered regeneration = 2 calls
        assert mock_call.call_count == 2
        assert mock_fc.call_count == 2
        assert len(result) == len(CARDS)


# ---------------------------------------------------------------------------
# resolve_briefing_model
# ---------------------------------------------------------------------------

class TestResolveBriefingModel:

    def setup_method(self):
        ai._briefing_model_cache = None

    def teardown_method(self):
        ai._briefing_model_cache = None

    def _mock_models_client(self, ids):
        mock_client = MagicMock()
        mock_client.models.list.return_value = MagicMock(
            data=[MagicMock(id=i) for i in ids]
        )
        return mock_client

    def test_uses_requested_model_when_available(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        with patch("openai.OpenAI", return_value=self._mock_models_client(
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5-mini"])):
            assert ai.resolve_briefing_model() == "gpt-5.6-luna"

    def test_falls_back_when_requested_model_missing(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        with patch("openai.OpenAI", return_value=self._mock_models_client(
            ["gpt-5.6-terra", "gpt-5-mini"])):
            assert ai.resolve_briefing_model() == "gpt-5.6-terra"

    def test_falls_back_to_mini_when_only_mini_available(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        with patch("openai.OpenAI", return_value=self._mock_models_client(["gpt-5-mini"])):
            assert ai.resolve_briefing_model() == "gpt-5-mini"

    def test_falls_back_to_last_resort_when_nothing_matches(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        with patch("openai.OpenAI", return_value=self._mock_models_client(["some-other-model"])):
            assert ai.resolve_briefing_model() == "gpt-5-mini"

    def test_falls_back_to_last_resort_on_api_error(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        mock_client = MagicMock()
        mock_client.models.list.side_effect = RuntimeError("network down")
        with patch("openai.OpenAI", return_value=mock_client):
            assert ai.resolve_briefing_model() == "gpt-5-mini"

    def test_result_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_MODEL", "gpt-5.6-luna")
        with patch("openai.OpenAI", return_value=self._mock_models_client(
            ["gpt-5.6-luna"])) as mock_cls:
            first = ai.resolve_briefing_model()
            second = ai.resolve_briefing_model()
        assert first == second == "gpt-5.6-luna"
        mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# database.get_story_position_map (issue #454 — review queue ordering)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated temp-DB fixture. NOTE: patches database.core.DB_PATH directly —
    database.DB_PATH is just a wildcard-import copy (see database/__init__.py's
    `from .core import *`) and does NOT affect what get_db() reads; patching
    only the package-level name silently leaves get_db() pointed at the real
    data/srs.db (test_api.py has this same pre-existing bug, out of scope here)."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


class TestGetStoryPositionMap:

    def _make_words(self):
        w1 = database.insert_word({"word_zh": "担心", "lang": "zh", "definition": "to worry"})
        w2 = database.insert_word({"word_zh": "努力", "lang": "zh", "definition": "to work hard"})
        w3 = database.insert_word({"word_zh": "进步", "lang": "zh", "definition": "progress"})
        return w1, w2, w3

    def test_returns_word_id_to_position_map_for_briefing_story(self, tmp_db):
        deck_id = database.get_or_create_deck("TestDeck")
        w1, w2, w3 = self._make_words()
        today = database.anki_today().isoformat()

        sentences = [
            {"position": 0, "sentence_zh": "她很担心考试。", "word_ids": [w1]},
            {"position": 1, "sentence_zh": "他每天努力学习。", "word_ids": [w2]},
            {"position": 2, "sentence_zh": "她看到了他的进步。", "word_ids": [w3]},
        ]
        database.create_story(today, "reading", deck_id, sentences,
                              gen_params={"mode": "briefing", "model": "gpt-5.1"}, lang="zh")

        pos_map = database.get_story_position_map(deck_id, "reading", today, lang="zh")
        assert pos_map == {w1: 0, w2: 1, w3: 2}

    def test_non_briefing_mode_returns_empty_map(self, tmp_db):
        deck_id = database.get_or_create_deck("TestDeck")
        w1, w2, w3 = self._make_words()
        today = database.anki_today().isoformat()

        sentences = [
            {"position": 0, "sentence_zh": "她很担心考试。", "word_ids": [w1]},
            {"position": 1, "sentence_zh": "他每天努力学习。", "word_ids": [w2]},
            {"position": 2, "sentence_zh": "她看到了他的进步。", "word_ids": [w3]},
        ]
        database.create_story(today, "reading", deck_id, sentences,
                              gen_params={"mode": "story", "model": "gpt-5.1"}, lang="zh")

        pos_map = database.get_story_position_map(deck_id, "reading", today, lang="zh")
        assert pos_map == {}

    def test_no_active_story_returns_empty_map(self, tmp_db):
        deck_id = database.get_or_create_deck("TestDeck")
        today = database.anki_today().isoformat()
        assert database.get_story_position_map(deck_id, "reading", today, lang="zh") == {}


# ---------------------------------------------------------------------------
# database.get_recent_story_keys (issue #458 — morning pregen reproduces the
# most recent real story keys instead of blindly generating for every leaf deck)
# ---------------------------------------------------------------------------

class TestGetRecentStoryKeys:

    def _make_word(self):
        return database.insert_word({"word_zh": "担心", "lang": "zh", "definition": "to worry"})

    def test_finds_yesterdays_briefing_key_with_parsed_gen_params(self, tmp_db):
        deck_id = database.get_or_create_deck("TestDeck")
        w1 = self._make_word()
        w2 = database.insert_word({"word_zh": "努力", "lang": "zh", "definition": "to work hard"})
        today = database.anki_today()
        yesterday = (today - timedelta(days=1)).isoformat()

        sentences = [
            {"position": 0, "sentence_zh": "她很担心考试。", "word_ids": [w1]},
            {"position": 1, "sentence_zh": "他每天努力学习。", "word_ids": [w2]},
        ]
        database.create_story(yesterday, "listening", deck_id, sentences,
                              gen_params={"mode": "briefing", "model": "gpt-5.1"}, lang="zh")

        keys = database.get_recent_story_keys(today.isoformat())
        assert len(keys) == 1
        assert keys[0]["deck_id"] == deck_id
        assert keys[0]["category"] == "listening"
        assert keys[0]["lang"] == "zh"
        assert keys[0]["gen_params"] == {"mode": "briefing", "model": "gpt-5.1"}

    def test_single_sentence_again_row_is_filtered_out(self, tmp_db):
        deck_id = database.get_or_create_deck("TestDeck")
        w1 = self._make_word()
        today = database.anki_today()
        yesterday = (today - timedelta(days=1)).isoformat()

        # Simulates database.store_again_sentence(): single-sentence row, real
        # category, real-looking gen_params — must still be excluded because it
        # only has 1 sentence (the actual again-sentinel uses AGAIN_CATEGORY too,
        # but the < 2 sentence filter is what get_recent_story_keys relies on).
        sentences = [{"position": 0, "sentence_zh": "她很担心考试。", "word_ids": [w1]}]
        database.create_story(yesterday, "listening", deck_id, sentences,
                              gen_params={"mode": "briefing", "model": "gpt-5.1"}, lang="zh")

        assert database.get_recent_story_keys(today.isoformat()) == []

    def test_no_history_returns_empty_list(self, tmp_db):
        today = database.anki_today().isoformat()
        assert database.get_recent_story_keys(today) == []


# ---------------------------------------------------------------------------
# routes.story._group_sentences_by_article (issue #456)
# ---------------------------------------------------------------------------

from routes.story import _group_sentences_by_article  # noqa: E402


class TestGroupSentencesByArticle:
    ARTS = [
        {"url": "https://ex.com/a", "title": "A"},
        {"url": "https://ex.com/b", "title": "B"},
        {"url": "https://ex.com/c", "title": "C"},
    ]

    def test_batch_cycling_regrouped_into_article_blocks(self):
        # Two concatenated batches, each cycling A→B→C (the #456 bug shape)
        sents = [
            {"sentence_zh": "a1", "source_url": "https://ex.com/a"},
            {"sentence_zh": "b1", "source_url": "https://ex.com/b"},
            {"sentence_zh": "c1", "source_url": "https://ex.com/c"},
            {"sentence_zh": "a2", "source_url": "https://ex.com/a"},
            {"sentence_zh": "b2", "source_url": "https://ex.com/b"},
            {"sentence_zh": "c2", "source_url": "https://ex.com/c"},
        ]
        out = [s["sentence_zh"] for s in _group_sentences_by_article(sents, self.ARTS)]
        # One contiguous block per article; batch order preserved inside a block
        assert out == ["a1", "a2", "b1", "b2", "c1", "c2"]

    def test_unknown_or_missing_url_kept_last_in_relative_order(self):
        sents = [
            {"sentence_zh": "x1", "source_url": None},
            {"sentence_zh": "b1", "source_url": "https://ex.com/b"},
            {"sentence_zh": "x2", "source_url": "https://other.com/z"},
            {"sentence_zh": "a1", "source_url": "https://ex.com/a"},
        ]
        out = [s["sentence_zh"] for s in _group_sentences_by_article(sents, self.ARTS)]
        assert out == ["a1", "b1", "x1", "x2"]

    def test_empty_inputs_are_noops(self):
        assert _group_sentences_by_article([], self.ARTS) == []
        sents = [{"sentence_zh": "a1", "source_url": "https://ex.com/a"}]
        assert _group_sentences_by_article(sents, []) == sents


# ---------------------------------------------------------------------------
# German context sentences (issue #1027)
# ---------------------------------------------------------------------------

GERMAN_CONTEXT_ITEMS = [
    {"sentence_de": "Die Koalition verhandelt seit drei Wochen.",
     "target_word": None, "article_idx": 0},
    {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 0},
    {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
    {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 0},
]


def _fake_translate(texts, target="en", source="zh-CN", **kw):
    return [f"[{source}->{target}] {t}" for t in texts]


class TestGermanContextSentences:
    """#1027: the model writes the context in German (it is the half Daniel
    reads as his morning briefing), so German is the original and the Chinese
    in reasoning_zh is the derived translation — the reverse of #444."""

    def test_german_context_is_stored_verbatim_and_chinese_is_derived(self):
        with patch("ai._call_api", return_value=json.dumps(GERMAN_CONTEXT_ITEMS)), \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None), \
             patch("translator.translate_batch", side_effect=_fake_translate):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        card = next(s for s in result if s["word_ids"] == [1])
        assert card["context_de"] == "Die Koalition verhandelt seit drei Wochen."
        assert card["reasoning_zh"] == (
            "[de->zh-CN] Die Koalition verhandelt seit drei Wochen.")

    def test_chinese_context_is_still_translated_the_old_way(self):
        """A reply that ignores the contract and writes Chinese context must
        not be filed as German — text in the wrong language slot is silent
        (#904), Google hands German→German back unchanged."""
        items = [dict(GERMAN_CONTEXT_ITEMS[0]) | {"sentence_de": None,
                                                  "sentence_zh": "联盟已经谈判了三周。"},
                 *GERMAN_CONTEXT_ITEMS[1:]]
        with patch("ai._call_api", return_value=json.dumps(items)), \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None), \
             patch("translator.translate_batch", side_effect=_fake_translate):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        card = next(s for s in result if s["word_ids"] == [1])
        assert card["reasoning_zh"] == "联盟已经谈判了三周。"
        assert card["context_de"] == "[zh-CN->de] 联盟已经谈判了三周。"

    def test_context_translation_failure_never_costs_the_story(self):
        with patch("ai._call_api", return_value=json.dumps(GERMAN_CONTEXT_ITEMS)), \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None), \
             patch("translator.translate_batch", side_effect=RuntimeError("boom")):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert len(result) == len(CARDS)
        card = next(s for s in result if s["word_ids"] == [1])
        assert card["context_de"] == "Die Koalition verhandelt seit drei Wochen."
        assert card["reasoning_zh"] == ""

    def test_prompt_asks_for_german_context_and_full_coverage(self):
        with patch("ai._call_api", return_value=json.dumps(GERMAN_CONTEXT_ITEMS)) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None), \
             patch("translator.translate_batch", side_effect=_fake_translate):
            ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        prompt = mock_call.call_args_list[0][0][1][0]["content"]
        assert "sentence_de" in prompt
        assert "覆盖面" in prompt


# ---------------------------------------------------------------------------
# #1036 — natural Chinese: off-topic escape hatch, per-round checks,
# naturalness pass
# ---------------------------------------------------------------------------

OFF_TOPIC_ITEMS = [
    {"sentence_de": "Die Koalition verhandelt seit drei Wochen.",
     "target_word": None, "article_idx": 0},
    {"sentence_zh": "她很担心考试。", "target_word": "担心", "article_idx": 0},
    {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
    {"sentence_zh": "她看到了他的进步。", "target_word": "进步",
     "article_idx": None, "off_topic": True},
]


class TestOffTopicSentences:
    """A due word with no place in the material gets an honest everyday
    sentence instead of being forced into the news (#1036)."""

    def test_prompt_offers_the_escape_hatch(self):
        with patch("ai._call_api", return_value=json.dumps(VALID_ITEMS)) as mock_call, \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        prompt = mock_call.call_args_list[0][0][1][0]["content"]
        assert "off_topic" in prompt
        assert "地道优先于一切" in prompt

    def test_off_topic_sentence_gets_no_source_and_no_context(self):
        with patch("ai._call_api", return_value=json.dumps(OFF_TOPIC_ITEMS)), \
             patch("ai.check_briefing_naturalness", return_value=[]), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai._fill_translations", lambda sentences, **kw: None), \
             patch("translator.translate_batch", side_effect=_fake_translate):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        off = next(s for s in result if s["word_ids"] == [3])
        assert off["source_url"] is None
        assert off["context_de"] is None
        # And the German context still reached the real target sentence.
        on = next(s for s in result if s["word_ids"] == [1])
        assert on["context_de"] == "Die Koalition verhandelt seit drei Wochen."

    def test_fact_check_does_not_see_the_off_topic_sentence(self):
        with patch("ai._call_api", return_value='{"ok": true}') as mock_call:
            ai.fact_check_briefing(ARTICLES, OFF_TOPIC_ITEMS, "gpt-5.6-luna")

        prompt = mock_call.call_args[0][1][0]["content"]
        assert "她看到了他的进步。" not in prompt
        assert "不用核对" in prompt


class TestBriefingNaturalness:

    def test_only_chinese_target_sentences_are_checked(self):
        with patch("ai._call_api", return_value='{"ok": true}') as mock_call:
            issues = ai.check_briefing_naturalness(OFF_TOPIC_ITEMS, CARDS, "gpt-5.6-luna")

        assert issues == []
        prompt = mock_call.call_args[0][1][0]["content"]
        assert "Die Koalition" not in prompt
        assert "她很担心考试。" in prompt

    def test_issues_are_returned_in_the_repairable_shape(self):
        raw = '{"ok": false, "issues": ["句子1：搭配不成立"]}'
        with patch("ai._call_api", return_value=raw):
            assert ai.check_briefing_naturalness(
                OFF_TOPIC_ITEMS, CARDS, "gpt-5.6-luna") == ["句子1：搭配不成立"]

    def test_failure_never_blocks_generation(self):
        with patch("ai._call_api", side_effect=RuntimeError("boom")):
            assert ai.check_briefing_naturalness(
                OFF_TOPIC_ITEMS, CARDS, "gpt-5.6-luna") == []

    def test_flagged_sentence_is_repaired_not_regenerated(self):
        """A naturalness issue goes through the targeted rewrite — the rest of
        the summary is fine and regenerating it would just roll the dice."""
        with patch("ai._call_api", return_value=json.dumps(VALID_ITEMS)), \
             patch("ai.fact_check_briefing", return_value=[]), \
             patch("ai.check_briefing_naturalness",
                   side_effect=[["句子1：搭配不成立"]]) as mock_nat, \
             patch("ai._repair_briefing_sentences",
                   return_value=VALID_ITEMS) as mock_repair, \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert mock_nat.call_count == 1
        assert mock_repair.call_count == 1
        assert mock_repair.call_args.kwargs["naturalness"] is True

    def test_repair_may_hand_back_an_off_topic_sentence(self):
        items = [{"sentence_zh": "工作人员给受损设备消毒。", "target_word": "担心",
                  "article_idx": 0}]
        raw = '[{"idx": 0, "sentence_zh": "她很担心考试。", "off_topic": true}]'
        with patch("ai._call_api", return_value=raw):
            fixed = ai._repair_briefing_sentences(
                ARTICLES, items, ["句子0：中文不这么说"], "gpt-5.6-luna",
                naturalness=True)

        assert fixed[0]["sentence_zh"] == "她很担心考试。"
        assert fixed[0]["off_topic"] is True
        assert fixed[0]["article_idx"] is None


class TestChecksRunEveryRound:
    """The 补漏 rounds used to reach the database unexamined (#1036)."""

    def test_second_round_output_is_validated_and_fact_checked(self):
        # Round 1 covers only 担心; round 2 is asked for the two missing words.
        round1 = [{"sentence_zh": "她很担心考试。", "target_word": "担心",
                   "article_idx": 0}]
        round2 = [
            {"sentence_zh": "他每天努力学习。", "target_word": "努力", "article_idx": 0},
            {"sentence_zh": "她看到了他的进步。", "target_word": "进步", "article_idx": 0},
        ]
        # Round 1 is short two words, so it also burns its own validation retry.
        with patch("ai._call_api",
                   side_effect=[json.dumps(round1), json.dumps(round1),
                                json.dumps(round2)]), \
             patch("ai.fact_check_briefing", return_value=[]) as mock_fc, \
             patch("ai.check_briefing_naturalness", return_value=[]) as mock_nat, \
             patch("ai._fill_translations", lambda sentences, **kw: None):
            result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

        assert len(result) == len(CARDS)
        assert mock_fc.call_count == 2
        assert mock_nat.call_count == 2
        # The second round was checked against the words it was asked for.
        assert [c["word_zh"] for c in mock_nat.call_args_list[1][0][1]] == ["努力", "进步"]

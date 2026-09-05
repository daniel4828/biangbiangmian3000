"""
Tests for the NotebookLM-specific podcast summary prompt (#1040).

Production binary-search found that NotebookLM's chat.ask silently returns an
empty stream once the prompt crosses roughly 4900 characters. The default
(API-path) prompt inlines the transcript and already exceeds that on its own
just from instructions + a long transcript, so
ai.build_podcast_summary_prompt(..., for_notebooklm=True) drops the inline
transcript (already uploaded as a NotebookLM source) and trims the
instructions, while keeping the same JSON contract.

Fast tests, no credentials/network/DB needed: pure string building.
"""

import ai
import podcast

LONG_TRANSCRIPT = "这是一段很长的播客文字记录测试内容。" * 800  # ~20000 chars
MARKER = "这是一段很长的播客文字记录测试内容"


def test_notebooklm_prompt_is_short():
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed",
                                             for_notebooklm=True)
    assert len(prompt) <= 4000


def test_notebooklm_prompt_does_not_inline_transcript():
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed",
                                             for_notebooklm=True)
    assert MARKER not in prompt


def test_notebooklm_prompt_keeps_json_contract():
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed",
                                             for_notebooklm=True)
    for key in ("title_suggestion", "summary_de", "summary_zh", "words"):
        assert key in prompt


def test_notebooklm_prompt_keeps_core_rules():
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed",
                                             for_notebooklm=True)
    assert "<b>" in prompt
    assert "<p>" in prompt
    assert "HSK" in prompt
    assert "GERMAN ONLY" in prompt


def test_default_prompt_still_inlines_transcript():
    """for_notebooklm defaults to False and must not change the existing
    API-path behavior at all — the transcript excerpt still appears inline."""
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed")
    assert MARKER in prompt


def test_notebooklm_max_prompt_chars_constant():
    """podcast._NOTEBOOKLM_MAX_PROMPT_CHARS exists and the actual
    NotebookLM prompt (even for a long transcript, since it isn't inlined)
    stays under it."""
    prompt = ai.build_podcast_summary_prompt(LONG_TRANSCRIPT, "测试标题", "detailed",
                                             for_notebooklm=True)
    assert len(prompt) <= podcast._NOTEBOOKLM_MAX_PROMPT_CHARS

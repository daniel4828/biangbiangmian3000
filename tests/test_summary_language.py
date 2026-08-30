"""Tests for issue #904: a summary_de that was written in Chinese.

The summary prompt asks for German first and a Chinese translation second
(#708). The model occasionally answers with Chinese in *both* JSON fields,
and nothing downstream noticed: the text is non-empty, so it passed the
success test, got pinyin-annotated, and was then handed to
knowledge/rendition.py as the "German" source every other study language is
translated from — producing the pinyin soup that filed the issue.

Two layers are covered here: rejection at generation time (ai.py and the
NotebookLM path) and the rendition guard that protects rows already stored.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ai
import database
import knowledge.rendition
import podcast
import zh_annotate


# A realistic pre-#979 German summary: German prose with a Chinese aside. The
# threshold has to keep tolerating those — the whole existing knowledge base
# still has them stored (they are stripped on read, not in the database).
GERMAN = ("<p><b>Der Fall der Berliner Mauer (bólínqiáng/柏林墙) war für viele "
          "Menschen ein einschneidendes Erlebnis.</b> Die Sendung ordnet die "
          "Ereignisse historisch ein und lässt Zeitzeugen zu Wort kommen.</p>")

# The failure mode: the model answered in Chinese under the German key.
CHINESE = ("<p><b>这段视频批判了所谓的“奥泽匹克经济”，一种利用人们绝望情绪来盈利的模式。</b>"
           "视频开头指出，人们对预测市场和加密货币的看法都是错误的。</p>")


def test_ratio_separates_german_from_chinese():
    assert zh_annotate.cjk_ratio(GERMAN) < zh_annotate.NON_CHINESE_TEXT_MAX_CJK
    assert zh_annotate.cjk_ratio(CHINESE) > zh_annotate.NON_CHINESE_TEXT_MAX_CJK
    assert ai.summary_de_is_german(GERMAN)
    assert not ai.summary_de_is_german(CHINESE)
    # Empty text is not a language problem — the "no summary yet" callers
    # already have their own check for it.
    assert ai.summary_de_is_german("")


def _summary_json(summary_de: str) -> str:
    import json
    return json.dumps({"summary_de": summary_de, "summary_zh": "", "words": []})


def test_chinese_summary_de_triggers_model_fallback(monkeypatch):
    """The first model answering in Chinese must not be accepted: the chain
    moves on, exactly as it does for an unparseable reply."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    calls = []

    def fake_call_api(model, messages, max_tokens, **kwargs):
        calls.append(model)
        return _summary_json(CHINESE if len(calls) == 1 else GERMAN)

    monkeypatch.setattr(ai, "_call_api", fake_call_api)
    result = ai.summarize_podcast_transcript("转录文本" * 50, "标题", "detailed")

    assert len(calls) == 2, "the Chinese answer should have been rejected"
    assert result["summary_de"] == GERMAN


def test_every_model_answering_in_chinese_is_a_failure(monkeypatch):
    """When no model produces German, an empty result is the honest answer —
    callers store status='error'. Storing the Chinese text would be worse."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(ai, "_call_api",
                        lambda *a, **k: _summary_json(CHINESE))

    result = ai.summarize_podcast_transcript("转录文本" * 50, "标题", "detailed")
    assert result["summary_de"] == ""


def test_notebooklm_chinese_summary_falls_back(monkeypatch):
    """The free NotebookLM path gets the same check; returning None hands the
    episode to the paid API chain rather than storing the wrong language."""
    monkeypatch.setattr(podcast, "_notebooklm_credentials_available", lambda: True)
    monkeypatch.setattr(podcast, "_run_notebooklm_summary", lambda *a, **k: None)
    monkeypatch.setattr(podcast.asyncio, "wait_for", lambda coro, timeout: coro)
    monkeypatch.setattr(podcast.asyncio, "run", lambda coro: _summary_json(CHINESE))

    assert podcast._summarize_via_notebooklm("转录", "标题", "detailed") is None


# ---------------------------------------------------------------------------
# The rendition guard, for rows already in the database
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # database.core.DB_PATH, never database.DB_PATH — see #615.
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def test_rendition_refuses_a_chinese_summary_de(tmp_db, monkeypatch):
    episode_id = database.create_pending_episode(
        video_id="vid-904", channel_id=None, title="Test", published_at=None,
        youtube_url="https://example.com/904",
    )
    database.update_episode(episode_id, status="summarized", summary_de=CHINESE)

    def fail(*a, **k):
        pytest.fail("the guard must fire before anything is translated")

    monkeypatch.setattr(knowledge.rendition.translator, "translate_strict", fail)

    with pytest.raises(knowledge.rendition.RenditionError) as excinfo:
        knowledge.rendition.get_or_create_rendition(episode_id, "fr")
    assert "not in German" in str(excinfo.value)
    assert database.get_knowledge_rendition(episode_id, "fr") is None

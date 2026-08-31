"""Issue #979: the German summary is German.

Until #979 the summary prompt asked the model to annotate every HSK5+ concept
with "pinyin/汉字" and to add a Chinese name after every company (#631), and
zh_annotate.annotate_de_summary() added pinyin to whatever the model left
bare. The German prose ended up unreadable:

    "AfD-Chef Björn Höcke (Björn Höcke, déguóxuǎnzédǎng.../德国选择党...)"

New summaries no longer carry any of it. The summaries already in the database
still do, so the cleanup lives on the READ path — database.podcast._hydrate(),
the one place get_episode() and list_episodes() both go through — which fixes
the whole existing knowledge base without re-summarizing anything.

#1001 extended the same treatment to summary_zh: its inline
"词（pīnyīn - Gloss）" annotations are gone too, because the reader can now
gloss any word on demand (#967/#996).

Isolation follows the house pattern: monkeypatch database.core.DB_PATH, never
database.DB_PATH (#615).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
import podcast
import zh_annotate

ANNOTATED_DE = ("<p><b>AfD-Chef Björn Höcke (Björn Höcke, déguóxuǎnzédǎng/德国选择党) "
                "will Ministerpräsident stürzen.</b> Die SPD (shèmíndǎng/社民党) "
                "muss sparen (ca. 12:30).</p>")
CLEAN_DE = ("<p><b>AfD-Chef Björn Höcke will Ministerpräsident stürzen.</b> "
            "Die SPD muss sparen (ca. 12:30).</p>")

ANNOTATED_ZH = "<p><b>社民党（shèmíndǎng - SPD）必须节省开支。</b></p>"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _episode_with_summary() -> int:
    ep = database.create_pending_episode(
        video_id="ep1", channel_id="https://feed.example/rss", title="Titel",
        published_at="2026-08-01", youtube_url="https://example.com/ep1",
    )
    database.update_episode(ep, summary_de=ANNOTATED_DE, summary_zh=ANNOTATED_ZH,
                            status="summarized")
    return ep


def test_stored_summary_is_cleaned_when_read(tmp_db):
    """The point of the read-path hook: no re-summarize needed."""
    ep = database.get_episode(_episode_with_summary())
    assert ep["summary_de"] == CLEAN_DE


def test_chinese_summary_is_cleaned_too(tmp_db):
    """#1001: the Chinese side lost its inline glosses as well — every word in
    the reader is tappable (#967) and Cmd / a left swipe glosses all of them
    (#996), so the parentheses only broke up the prose. Same read path, same
    reason: it fixes every summary already stored."""
    ep = database.get_episode(_episode_with_summary())
    assert ep["summary_zh"] == "<p><b>社民党必须节省开支。</b></p>"


def test_list_rows_are_cleaned_too(tmp_db):
    """The list carries summary_de, and the story loading screen shows it."""
    _episode_with_summary()
    rows = database.list_episodes()
    assert rows and rows[0]["summary_de"] == CLEAN_DE


def test_fresh_summaries_are_annotated_on_neither_side(monkeypatch):
    """#979 stopped annotating the German side, #1001 the Chinese one — what
    _annotate_summary still does is SCAN for the new words (the word table and
    the tappable words in the reader), never rewrite the text."""
    monkeypatch.setattr(zh_annotate, "extract_new_words",
                        lambda t: [{"word": "开支"}])
    out = podcast._annotate_summary({"summary_de": "Nur Deutsch.",
                                     "summary_zh": "只有中文。"})
    assert out["summary_de"] == "Nur Deutsch."
    assert out["summary_zh"] == "只有中文。"
    assert out["words"] == [{"word": "开支"}]


def test_summary_prompt_forbids_chinese_in_the_german_summary():
    """A prompt that keeps asking for annotations would refill the database."""
    import ai
    prompt = ai.build_podcast_summary_prompt("Transkript", "Titel", "detailed")
    assert "pinyin/汉字" not in prompt
    assert "GERMAN ONLY" in prompt

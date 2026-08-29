"""Full-text reading view (#972): the untruncated source text of a piece of
material, translated into the reading language and annotated by the same
pipeline as a summary and a book page.

The translator and annotator are stubbed throughout — what is under test is
the wiring around them (what gets sent, when it is generated at all, and
what is written on failure), not Google Translate.
"""
import pytest

import database
import database.core
import knowledge.rendition as rendition


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """database.core.DB_PATH, never database.DB_PATH — the latter is a
    wildcard-import copy and patching it silently writes to data/srs.db
    (#615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _episode(text, kind="newsletter", status="summarized"):
    episode_id = database.create_pending_episode(
        video_id=f"pasted:{abs(hash(text))}",
        channel_id="F.A.Z. Frühdenker",
        title="Titel",
        published_at=None,
        youtube_url="",
        kind=kind,
    )
    database.update_episode(episode_id, transcript_zh=text, status=status,
                            summary_de="<p>Zusammenfassung</p>")
    return episode_id


# ---------------------------------------------------------------------------
# plain text -> markup
# ---------------------------------------------------------------------------

def test_paragraphs_are_split_on_blank_lines_and_escaped():
    """render_html only sends text nodes to the translator, so the text has
    to sit inside tags — and an unescaped '<' in the source would start a
    tag, hiding everything after it from the translation."""
    html = rendition.text_to_paragraph_html("a < b\n\nzweiter Absatz")

    assert html == "<p>a &lt; b</p><p>zweiter Absatz</p>"


def test_single_newlines_become_line_breaks_not_paragraphs():
    """Newsletters hard-wrap their lines; treating each one as a paragraph
    would shred every sentence into fragments before translation."""
    html = rendition.text_to_paragraph_html("Zeile eins\nZeile zwei")

    assert html == "<p>Zeile eins<br>Zeile zwei</p>"


def test_empty_source_produces_no_markup():
    assert rendition.text_to_paragraph_html("   \n\n  ") == ""


# ---------------------------------------------------------------------------
# source language detection
# ---------------------------------------------------------------------------

def test_chinese_source_is_detected():
    """transcript_zh means "source material in any language" (#772). Asking
    Google for de->zh on text that is already Chinese returns it nearly
    unchanged, which would look like a successful translation."""
    assert rendition._source_lang_of("这是一段中文的转录文本，内容很长。") == "zh-CN"
    assert rendition._source_lang_of("Das ist ein deutscher Text.") == "de"


def test_chinese_source_into_chinese_is_annotated_not_translated(monkeypatch):
    monkeypatch.setattr(rendition, "_translate_html_strict",
                        lambda *a, **k: pytest.fail("must not translate zh->zh"))
    monkeypatch.setattr(rendition.annotate, "annotate_summary",
                        lambda html, lang: (html + "!", [{"word": "转录"}]))

    text, words = rendition.render_html("<p>这是中文</p>", "zh", source="zh-CN")

    assert text == "<p>这是中文</p>!"
    assert words == [{"word": "转录"}]


# ---------------------------------------------------------------------------
# generation policy
# ---------------------------------------------------------------------------

def test_get_without_generate_returns_none(monkeypatch):
    """Opening a detail page must not silently start translating an
    hour-long transcript."""
    monkeypatch.setattr(rendition, "render_html",
                        lambda *a, **k: pytest.fail("must not generate on read"))
    episode_id = _episode("Ein deutscher Text.")

    assert rendition.get_or_create_fulltext(episode_id, "zh") is None


def test_generate_stores_and_is_served_from_cache_afterwards(monkeypatch):
    calls = []
    monkeypatch.setattr(rendition, "render_html",
                        lambda html, lang, source="de": calls.append(source) or ("<p>中文</p>", [{"word": "中文"}]))
    episode_id = _episode("Ein deutscher Text.")

    first = rendition.get_or_create_fulltext(episode_id, "zh", generate=True)
    second = rendition.get_or_create_fulltext(episode_id, "zh")

    assert first["text"] == "<p>中文</p>"
    assert second["text"] == "<p>中文</p>"
    assert calls == ["de"]  # generated once, then cached


def test_failure_writes_nothing(monkeypatch):
    """#804's contract: never store source-language text under a target
    language's name — a failure has to leave the cache empty so the next
    attempt is a real one."""
    def boom(*a, **k):
        raise rendition.RenditionError("translation failed")
    monkeypatch.setattr(rendition, "render_html", boom)
    episode_id = _episode("Ein deutscher Text.")

    with pytest.raises(rendition.RenditionError):
        rendition.get_or_create_fulltext(episode_id, "zh", generate=True)

    assert database.get_knowledge_fulltext(episode_id, "zh") is None


def test_material_without_source_text_raises_with_a_readable_reason(monkeypatch):
    episode_id = _episode("")

    with pytest.raises(rendition.RenditionError) as e:
        rendition.get_or_create_fulltext(episode_id, "zh", generate=True)
    assert "原文" in str(e.value)


def test_chinese_is_allowed_unlike_the_summary_rendition():
    """get_or_create_rendition refuses zh because summary_zh is AI-native.
    A full text has no AI-native Chinese version, so it must not refuse."""
    with pytest.raises(rendition.RenditionError):
        rendition.get_or_create_rendition(1, "zh")

    # no raise: absent, not refused
    assert rendition.get_or_create_fulltext(_episode("Text."), "zh") is None


# ---------------------------------------------------------------------------
# newsletters are prepared ahead of time, everything else on request
# ---------------------------------------------------------------------------

def test_newsletters_are_prepared_but_other_kinds_are_not(monkeypatch):
    import podcast

    prepared = []
    monkeypatch.setattr(rendition, "get_or_create_fulltext",
                        lambda eid, lang, generate=False: prepared.append((eid, lang)))

    podcast._maybe_prepare_fulltext(1, "newsletter")
    podcast._maybe_prepare_fulltext(2, "podcast")
    podcast._maybe_prepare_fulltext(3, "article")

    assert prepared == [(1, "zh")]


def test_a_failed_full_text_never_fails_the_episode(monkeypatch):
    """Same as summary_zh (#708): the full text is an extra. An episode whose
    summary succeeded must not be marked failed over a translation hiccup."""
    import podcast

    def boom(*a, **k):
        raise RuntimeError("Google said no")
    monkeypatch.setattr(rendition, "get_or_create_fulltext", boom)

    podcast._maybe_prepare_fulltext(1, "newsletter")  # must not raise

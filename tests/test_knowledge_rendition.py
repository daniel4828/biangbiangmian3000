"""Tests for issue #804: per-language knowledge-base renditions.

The knowledge base (#650-#655) stores one AI summary (summary_de) per
episode; every other language's reading view is a lazily generated,
cached translate-then-annotate derivative of it (knowledge/rendition.py +
annotate/romance.py). Covers the four acceptance criteria from the issue:

  1. A rendition is generated once and reused on a second request (no
     re-translation).
  2. Already-known French words (including forms that only exist in
     entry_forms, e.g. a conjugated verb form) are not annotated; unknown
     words are.
  3. Regenerating an episode's summary clears its cached renditions.
  4. A translation failure writes nothing to the database.

Each test gets its own isolated temp DB by monkeypatching
database.core.DB_PATH (same pattern as tests/test_entry_forms.py) — never
database.DB_PATH, which is only a wildcard-import copy (#615).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import annotate
import database
import knowledge.rendition
import podcast
import translator


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


def _minimal_word(word_zh: str, lang: str = "zh") -> dict:
    return {
        "word_zh": word_zh,
        "lang": lang,
        "pinyin": None,
        "definition": None,
        "pos": None,
        "hsk_level": None,
        "traditional": None,
        "definition_zh": None,
        "source": "test",
        "note_type": "vocabulary",
        "notes": None,
        "date_yaml": None,
        "source_sentence": None,
        "grammar_notes": None,
        "register": None,
        "definition_de": None,
        "definition_fr": None,
    }


def _make_summarized_episode(summary_de: str = "<p>Bonjour le monde.</p>") -> int:
    episode_id = database.create_pending_episode(
        video_id="vid-1", channel_id=None, title="Test episode",
        published_at=None, youtube_url="https://example.com/1",
    )
    database.update_episode(episode_id, status="summarized", summary_de=summary_de,
                            transcript_zh="一些转录文本" * 5)
    return episode_id


# ---------------------------------------------------------------------------
# 1. Rendition generated once, reused on the second request
# ---------------------------------------------------------------------------

def test_rendition_generated_and_cached(tmp_db, monkeypatch):
    episode_id = _make_summarized_episode()

    calls = {"n": 0}

    def fake_translate_strict(text, target="en", source="zh-CN"):
        calls["n"] += 1
        return "Bonjour le monde."

    monkeypatch.setattr(translator, "translate_strict", fake_translate_strict)
    monkeypatch.setattr(translator, "translate_batch", lambda words, **kw: words)

    first = knowledge.rendition.get_or_create_rendition(episode_id, "fr")
    assert calls["n"] == 1
    assert "Bonjour" in first["summary"]

    second = knowledge.rendition.get_or_create_rendition(episode_id, "fr")
    assert calls["n"] == 1  # not re-translated
    assert second["summary"] == first["summary"]

    # And it's actually persisted, not just process-memoized.
    cached = database.get_knowledge_rendition(episode_id, "fr")
    assert cached is not None
    assert cached["summary"] == first["summary"]


# ---------------------------------------------------------------------------
# 2. Known words (incl. entry_forms-only conjugations) skipped; unknown flagged
# ---------------------------------------------------------------------------

def test_known_words_not_annotated_unknown_words_are(tmp_db):
    # "parler" is studied; "parlons" only exists as its entry_forms
    # conjugation, never as its own entries row (#803's whole point).
    fr_word_id = database.insert_word(_minimal_word("parler", lang="fr"))
    database.set_entry_forms(fr_word_id, [
        {"kind": "conjugation", "paradigm": "présent", "slot": "nous",
         "form": "parlons", "position": 0},
    ])
    # "bonjour" was never studied but Daniel marked it known (#710/#803).
    database.add_known_word("bonjour", "fr")

    # "amoindrir" is neither studied nor in the CEFR A1-A2 baseline (#922) —
    # a word past A2 is what "unknown" has to mean now that a floor exists.
    text = "Nous parlons de bonjour et d'amoindrir."
    annotated, new_words = annotate.annotate_summary(text, "fr")

    new_word_surfaces = {w["word"] for w in new_words}
    assert "parlons" not in new_word_surfaces
    assert "bonjour" not in new_word_surfaces
    assert "amoindrir" in new_word_surfaces

    assert "parlons (" not in annotated
    assert "bonjour (" not in annotated
    assert "amoindrir" in annotated


def test_zh_annotation_dispatch_untouched(tmp_db):
    """annotate.annotate_summary('zh') must produce the same result as
    calling zh_annotate directly — the dispatch wraps it, it doesn't change
    its behavior."""
    import zh_annotate
    text = "对就业的影响很大。"
    annotated, new_words = annotate.annotate_summary(text, "zh")
    assert annotated == zh_annotate.annotate_zh_summary(text)
    assert new_words == zh_annotate.extract_new_words(text)


# ---------------------------------------------------------------------------
# 3. regenerate-summary clears cached renditions
# ---------------------------------------------------------------------------

def test_regenerate_summary_clears_renditions(tmp_db, monkeypatch):
    episode_id = _make_summarized_episode()
    database.save_knowledge_rendition(episode_id, "fr", "Ancien résumé.", [])
    assert database.get_knowledge_rendition(episode_id, "fr") is not None

    def fake_summarize(*a, **kw):
        return {"summary_zh": "新总结。", "summary_de": "<p>Neuer Text.</p>", "words": []}

    monkeypatch.setattr(podcast, "summarize", fake_summarize)
    monkeypatch.setattr(podcast, "filter_new_words", lambda words: words)

    result = podcast.regenerate_summary(episode_id)
    assert result["regenerated"] is True
    assert database.get_knowledge_rendition(episode_id, "fr") is None


# ---------------------------------------------------------------------------
# 4. Translation failure writes nothing
# ---------------------------------------------------------------------------

def test_translation_failure_does_not_write(tmp_db, monkeypatch):
    episode_id = _make_summarized_episode()

    def fake_translate_strict(text, target="en", source="zh-CN"):
        raise RuntimeError("translator unavailable")

    monkeypatch.setattr(translator, "translate_strict", fake_translate_strict)

    with pytest.raises(knowledge.rendition.RenditionError):
        knowledge.rendition.get_or_create_rendition(episode_id, "fr")

    assert database.get_knowledge_rendition(episode_id, "fr") is None


def test_missing_summary_de_does_not_write(tmp_db):
    """An episode with no German summary yet (still pending/transcribing)
    raises rather than translating an empty string into a fake rendition."""
    episode_id = database.create_pending_episode(
        video_id="vid-2", channel_id=None, title="Not summarized yet",
        published_at=None, youtube_url="https://example.com/2",
    )
    with pytest.raises(knowledge.rendition.RenditionError):
        knowledge.rendition.get_or_create_rendition(episode_id, "fr")
    assert database.get_knowledge_rendition(episode_id, "fr") is None


def test_zh_has_no_rendition():
    with pytest.raises(knowledge.rendition.RenditionError):
        knowledge.rendition.get_or_create_rendition(1, "zh")


# ---------------------------------------------------------------------------
# 5. HTML survives the round trip, and the HTTP wiring actually works
# ---------------------------------------------------------------------------

def test_html_tags_are_preserved_and_never_annotated(tmp_db, monkeypatch):
    """summary_de is HTML (<p> paragraphs, <b> lead sentences).

    Two things must hold: the tags come back byte-identical (they are never
    sent to Google Translate — only the text nodes are, which also keeps
    every request under the endpoint's ~5000-character limit), and the
    annotator never treats a tag name as a word ("<strong>" must not turn
    into "<strong (stark)>", which would destroy the markup).
    """
    html = ("<p><b>Ein Satz.</b> Noch ein Satz.</p>"
            "<p><strong>Zweiter Absatz.</strong> Letzter Satz.</p>")
    episode_id = _make_summarized_episode(summary_de=html)

    sent = []

    def fake_translate_strict(text, target="en", source="zh-CN"):
        sent.append(text)
        return "\n".join("mot " + line for line in text.split("\n"))

    monkeypatch.setattr(translator, "translate_strict", fake_translate_strict)
    monkeypatch.setattr(translator, "translate_batch",
                        lambda words, **kw: ["gloss" for _ in words])

    out = knowledge.rendition.get_or_create_rendition(episode_id, "fr")["summary"]

    assert out.count("<p>") == 2 and out.count("</p>") == 2
    assert "<b>" in out and "<strong>" in out
    # No tag name ever got a gloss appended inside the tag itself.
    assert "<strong (" not in out and "<b (" not in out
    # Tags were never handed to the translator.
    assert all("<" not in chunk for chunk in sent)


def test_detail_endpoint_serves_rendition(tmp_db, monkeypatch):
    """End-to-end through FastAPI: ?lang=fr attaches a rendition, zh doesn't."""
    from fastapi.testclient import TestClient
    import main

    episode_id = _make_summarized_episode(summary_de="<p>Ein deutscher Satz.</p>")
    monkeypatch.setattr(translator, "translate_strict",
                        lambda text, target="en", source="zh-CN": "Une phrase française.")
    monkeypatch.setattr(translator, "translate_batch",
                        lambda words, **kw: ["gloss" for _ in words])

    client = TestClient(main.app)

    fr = client.get(f"/api/podcast/episodes/{episode_id}?lang=fr").json()
    assert fr["rendition"] is not None
    assert "phrase" in fr["rendition"]["summary"]
    assert fr["rendition_error"] is None

    zh = client.get(f"/api/podcast/episodes/{episode_id}").json()
    assert "rendition" not in zh  # zh path untouched (#804)
    assert zh["summary_de"] == "<p>Ein deutscher Satz.</p>"


def test_detail_endpoint_reports_translation_failure(tmp_db, monkeypatch):
    """A failed translation must not 500 the detail view, and must not store
    the German text as if it were French."""
    from fastapi.testclient import TestClient
    import main

    episode_id = _make_summarized_episode(summary_de="<p>Ein deutscher Satz.</p>")

    def boom(text, target="en", source="zh-CN"):
        raise RuntimeError("translator unavailable")

    monkeypatch.setattr(translator, "translate_strict", boom)

    client = TestClient(main.app)
    resp = client.get(f"/api/podcast/episodes/{episode_id}?lang=fr")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendition"] is None
    assert "translation failed" in body["rendition_error"]
    assert database.get_knowledge_rendition(episode_id, "fr") is None

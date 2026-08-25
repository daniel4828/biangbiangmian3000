"""
Tests for the static baseline vocabulary lists (#922).

These lists answer "Daniel already knew this word before the app existed":
French CEFR A1-A2 and Chinese HSK 3.0 1-4. They must make the annotators go
quiet on those words — and, just as importantly, must never turn into cards.
That second half is guaranteed structurally (nothing in annotate/ writes to the
database), so what is asserted here is the annotation behavior plus the
loader's failure posture.
"""

import pytest

import zh_annotate
from annotate import baseline, romance


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------

def test_shipped_lists_load():
    fr = baseline.baseline_words("fr")
    zh = baseline.baseline_words("zh")
    # Sanity floors, not exact counts: the lists get regenerated from upstream
    # resources and a pinned number would fail for the wrong reason.
    assert len(fr) > 10000
    assert len(zh) > 3000
    assert not any(w.startswith("#") for w in fr | zh)


def test_language_without_a_list_is_empty_not_an_error():
    """Spanish ships no baseline yet. That must be an empty set, not a crash —
    same posture as romance.stopwords() on an unreadable file."""
    assert baseline.baseline_words("es") == frozenset()


def test_unreadable_list_degrades_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(baseline, "_cache", {})
    monkeypatch.setattr(baseline.os.path, "dirname", lambda _p: str(tmp_path))
    assert baseline.baseline_words("fr") == frozenset()


# ---------------------------------------------------------------------------
# French — annotate/romance.py
# ---------------------------------------------------------------------------

@pytest.fixture
def fr_annotator(monkeypatch):
    """romance.annotate_summary with the database and Google Translate stubbed
    out: nothing is studied, nothing is marked known, every remaining new word
    gets a fake gloss. Whatever stays unannotated is therefore the baseline's
    doing and nothing else."""
    monkeypatch.setattr(romance.database, "forms_lookup", lambda words, lang: set())
    monkeypatch.setattr(romance.database, "known_words_exists", lambda words, lang: set())
    monkeypatch.setattr(romance, "_glosses", lambda words, lang: {w: f"DE:{w}" for w in words})
    return lambda text: romance.annotate_summary(text, "fr")


def test_a1_lemma_is_not_annotated(fr_annotator):
    annotated, new_words = fr_annotator("Il aime la maison.")
    assert annotated == "Il aime la maison."
    assert new_words == []


def test_inflected_form_of_an_a1_lemma_is_not_annotated(fr_annotator):
    """The whole reason the list ships fully inflected: the romance annotator
    matches surface forms exactly and does zero stemming (#803), so a
    lemma-only list would still flag "mangeons"."""
    assert "mangeons" in baseline.baseline_words("fr")
    annotated, new_words = fr_annotator("Nous mangeons ensemble.")
    assert annotated == "Nous mangeons ensemble."
    assert new_words == []


def test_word_above_a2_is_still_annotated(fr_annotator):
    """The baseline is a floor, not a mute button — anything past A2 must keep
    its gloss."""
    assert "amoindrir" not in baseline.baseline_words("fr")
    annotated, new_words = fr_annotator("Cela va amoindrir le risque.")
    assert "amoindrir (DE:amoindrir)" in annotated
    assert "amoindrir" in [w["word"] for w in new_words]


def test_elided_baseline_word_is_not_annotated(fr_annotator):
    """"l'argent" is looked up as "argent" (romance._strip_elision), so the
    baseline has to cover it too."""
    annotated, _ = fr_annotator("Il a perdu l'argent.")
    assert annotated == "Il a perdu l'argent."


# ---------------------------------------------------------------------------
# Chinese — zh_annotate.py
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_zh_database(monkeypatch):
    """No collection, no known_words rows — the baseline is the only source of
    "known" left in these Chinese tests."""
    import database
    monkeypatch.setattr(database, "word_zh_exists", lambda words: set())
    monkeypatch.setattr(database, "known_words_exists", lambda words, lang="zh": set())


def test_hsk30_word_missing_from_the_old_list_is_known():
    """下载 is HSK 5 in static/hsk_levels.json (the 2001 list) but HSK 3.0
    level 4 — exactly the ~2100-word gap this baseline closes."""
    assert zh_annotate._hsk_levels()["下载"] > zh_annotate.KNOWN_HSK_MAX
    assert "下载" in baseline.baseline_words("zh")
    assert zh_annotate._known_words(["下载"]) == {"下载"}
    assert zh_annotate.find_new_words("请下载这个文件。") == []


def test_word_outside_the_baseline_is_still_new():
    assert "垄断" not in baseline.baseline_words("zh")
    assert zh_annotate.find_new_words("这家公司垄断了市场。") == ["垄断"]

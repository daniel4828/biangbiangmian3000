"""Issue #1029 — the briefing chunker divides the source between its calls.

Since #1027 every call is told to cover every topic of the material it is
given. Handing all of it to each call therefore turned 76 due words into eight
summaries of the same newsletter instead of one briefing. The material is now
cut into as many contiguous slices as there are calls.

The escape hatch matters as much as the split: material that cannot be cut that
far keeps the old shape (every call gets everything), because merging chunks to
match would risk one call that overruns the 8192-token reply budget.
"""

from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

import ai
import routes.story as story_routes


def _cards(n):
    return [{"word_id": i, "word_zh": f"词{i}", "pinyin": "", "definition": ""}
            for i in range(n)]


PARAGRAPHS = [f"Absatz Nummer {i} mit etwas Text darin." for i in range(12)]
ARTICLE = {"url": "/#knowledge-1", "title": "Frühdenker",
           "text": "\n\n".join(PARAGRAPHS)}


# ---------------------------------------------------------------------------
# _split_text_into_parts
# ---------------------------------------------------------------------------

class TestSplitTextIntoParts:

    def test_parts_are_contiguous_and_lossless(self):
        parts = story_routes._split_text_into_parts(ARTICLE["text"], 4)
        assert len(parts) == 4
        assert "\n\n".join(parts) == ARTICLE["text"]

    def test_parts_are_roughly_balanced(self):
        parts = story_routes._split_text_into_parts(ARTICLE["text"], 3)
        lengths = [len(p) for p in parts]
        assert max(lengths) <= 2 * min(lengths)

    def test_falls_back_to_sentence_boundaries(self):
        text = "Erster Satz. Zweiter Satz. Dritter Satz. Vierter Satz."
        parts = story_routes._split_text_into_parts(text, 4)
        assert len(parts) == 4
        assert "".join(parts) == text

    def test_returns_fewer_parts_than_asked_when_uncuttable(self):
        """One unbroken paragraph cannot be cut — say so instead of returning
        a single part that the caller would mistake for a successful split."""
        assert story_routes._split_text_into_parts("Ein einziger Block", 5) == [
            "Ein einziger Block"]


# ---------------------------------------------------------------------------
# _split_material
# ---------------------------------------------------------------------------

class TestSplitMaterial:

    def test_single_call_keeps_the_article_untouched(self):
        assert story_routes._split_material([ARTICLE], 1) == [[ARTICLE]]

    def test_one_source_is_cut_into_one_slice_per_call(self):
        slices = story_routes._split_material([ARTICLE], 4)
        assert len(slices) == 4
        texts = [sl[0]["text"] for sl in slices]
        assert len(set(texts)) == 4
        assert "\n\n".join(texts) == ARTICLE["text"]
        # url/title survive — the card's source line and the per-article
        # grouping in _group_sentences_by_article both depend on them.
        assert all(sl[0]["url"] == ARTICLE["url"] for sl in slices)
        assert all("段" in sl[0]["section_label"] for sl in slices)

    def test_fewer_calls_than_sources_keeps_every_source_whole(self):
        articles = [dict(ARTICLE, url=f"/#knowledge-{i}", title=f"Quelle {i}")
                    for i in range(4)]
        slices = story_routes._split_material(articles, 2)
        assert len(slices) == 2
        assert sum(len(sl) for sl in slices) == 4
        assert [a["text"] for sl in slices for a in sl] == [a["text"] for a in articles]

    def test_every_source_gets_at_least_one_call(self):
        """A source that got zero calls would vanish from the story."""
        articles = [dict(ARTICLE, url="/#knowledge-1", text="\n\n".join(PARAGRAPHS)),
                    dict(ARTICLE, url="/#knowledge-2", text="Kurz.")]
        slices = story_routes._split_material(articles, 5)
        urls = {sl[0]["url"] for sl in slices}
        assert urls == {"/#knowledge-1", "/#knowledge-2"}


# ---------------------------------------------------------------------------
# the chunker wiring
# ---------------------------------------------------------------------------

class TestChunkerUsesSlices:

    def _run(self, cards, articles, chunk_size):
        seen = []

        def _fake(batch, arts, **kw):
            seen.append(arts)
            return [{"word_ids": [c["word_id"]], "sentence_zh": c["word_zh"],
                     "source_url": arts[0].get("url")} for c in batch]

        with patch("ai.generate_briefing_sentences", side_effect=_fake):
            story_routes._generate_briefing_story_sentences(
                cards, articles, model="gpt-5.6-luna", progress_key=None,
                batch_size=chunk_size)
        return seen

    def test_each_call_gets_its_own_slice(self):
        seen = self._run(_cards(12), [ARTICLE], chunk_size=3)
        assert len(seen) == 4
        texts = [s[0]["text"] for s in seen]
        assert len(set(texts)) == 4
        assert "\n\n".join(texts) == ARTICLE["text"]

    def test_single_call_is_byte_for_byte_the_old_behaviour(self):
        seen = self._run(_cards(3), [ARTICLE], chunk_size=10)
        assert seen == [[ARTICLE]]

    def test_uncuttable_material_falls_back_to_the_full_source(self):
        one_block = {"url": "/#knowledge-1", "title": "T", "text": "Ein Block ohne Absätze"}
        seen = self._run(_cards(9), [one_block], chunk_size=3)
        assert len(seen) == 3
        assert all(s == [one_block] for s in seen)


def test_prompt_names_the_section_when_the_source_was_split():
    """Without this the model writes an intro to material it cannot see."""
    prompts = []

    def _capture(model, messages, max_tokens, **kw):
        prompts.append(messages[0]["content"])
        return "[]"

    sliced = story_routes._split_material([ARTICLE], 3)
    with patch("ai._call_api", side_effect=_capture), \
         patch("ai.fact_check_briefing", return_value=[]), \
         patch("ai._fill_translations", lambda sentences, **kw: None), \
         patch("translator.translate_batch", side_effect=lambda t, **kw: t):
        ai.generate_briefing_sentences(_cards(2), sliced[1], model="gpt-5.6-luna")

    assert "这是整篇素材的一段" in prompts[0]
    assert "第2/3段" in prompts[0]


def test_prompt_says_nothing_about_sections_when_the_source_is_whole():
    prompts = []

    def _capture(model, messages, max_tokens, **kw):
        prompts.append(messages[0]["content"])
        return "[]"

    with patch("ai._call_api", side_effect=_capture), \
         patch("ai.fact_check_briefing", return_value=[]), \
         patch("ai._fill_translations", lambda sentences, **kw: None), \
         patch("translator.translate_batch", side_effect=lambda t, **kw: t):
        ai.generate_briefing_sentences(_cards(2), [ARTICLE], model="gpt-5.6-luna")

    assert "这是整篇素材的一段" not in prompts[0]

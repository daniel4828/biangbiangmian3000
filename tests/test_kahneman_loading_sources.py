"""#980: the story loading screen's "read the material while you wait" buttons
(#929, knowledge mode) also work for kahneman chapters.

The frontend can only seed those buttons from the checkboxes the user ticked —
an empty selection means "random 5", picked inside the generator. So the
generator has to report which chapters it actually used, and
/api/story-progress has to hand them to the loading screen.
"""
import ai
from routes import story as story_routes


def test_generator_reports_selected_chapters(monkeypatch):
    monkeypatch.setattr(ai, "generate_kahneman_sentences",
                        lambda cards, chapter, **kw: [])
    ai.reset_story_sources("k1")
    cards = [{"id": 1, "word_zh": "生态"}]

    story_routes._generate_kahneman_story_sentences(
        cards, [1, 2], model="deepseek-chat", progress_key="k1")

    sources = ai._story_sources["k1"]
    assert [s["id"] for s in sources] == [1, 2]
    assert all(s["kind"] == "kahneman" for s in sources)
    assert all(s["title"].startswith(f"第{s['id']}章") for s in sources)


def test_random_chapters_are_reported_too(monkeypatch):
    """No selection → the server picks 5; without this the loading screen would
    have nothing to offer in exactly the case the user could not know either."""
    monkeypatch.setattr(ai, "generate_kahneman_sentences",
                        lambda cards, chapter, **kw: [])
    ai.reset_story_sources("k2")

    story_routes._generate_kahneman_story_sentences(
        [{"id": 1, "word_zh": "生态"}], None,
        model="deepseek-chat", progress_key="k2")

    assert len(ai._story_sources["k2"]) == 5


def test_progress_endpoint_exposes_sources(monkeypatch):
    monkeypatch.setattr("database.get_deck_lang", lambda deck_id: "zh")
    ai.set_story_sources("7/reading/zh",
                         [{"id": 3, "kind": "kahneman", "title": "第3章 X"}])
    prog = story_routes.story_progress_endpoint(7, "reading")
    assert prog["sources"] == [{"id": 3, "kind": "kahneman", "title": "第3章 X"}]


def test_new_run_clears_the_previous_run_s_sources():
    """A stale chapter list next to a fresh progress bar is a lie about what is
    being generated."""
    ai.set_story_sources("k3", [{"id": 1, "kind": "kahneman", "title": "第1章"}])
    ai.reset_story_sources("k3")
    assert "k3" not in ai._story_sources

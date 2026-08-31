"""Knowledge mode's two kahneman-style buttons (issue #931).

Daniel asked for the same pair of clickable things kahneman cards have: 💡 for
the model's note on why it picked this passage, and 📄 for the source item's
own summary — instead of the reasoning being printed as plain text above the
sentence.

Two halves, tested accordingly:

  * the prompt half (`ai.py`) — reasoning_zh must ask for BOTH the fact and the
    why. The fact is not decoration: the prompt uses it as a self-check ("no
    two sentences may carry the same one"), so a future edit that drops it to
    "just explain why" would quietly bring duplicate sentences back.

  * the frontend half (`static/app.js`) — static checks, since there is no way
    to render a card in pytest. They guard the things that would rot silently:
    the episode id being read from source_url (not from gen_params, which only
    knows the story's first source), the summary popup being the shared one
    from #930 rather than a second copy, and the three OTHER modes still
    reaching their own branches.
"""
import pathlib

import ai

APP_JS = pathlib.Path("static/app.js").read_text(encoding="utf-8")


# --- prompt half ----------------------------------------------------------

def test_zh_knowledge_prompt_asks_for_fact_and_why():
    """The Chinese knowledge template keeps its reasoning in Chinese (Daniel
    reads these cards in Chinese), so the two labels are Chinese too."""
    tpl = ai.DEFAULT_PROMPT_TEMPLATES["knowledge"]
    assert "事实：" in tpl
    assert "为什么：" in tpl
    # The self-check that depends on the fact half must survive.
    assert "任何两句的「事实：」都不许相同" in tpl


def test_non_zh_knowledge_prompt_asks_for_fact_and_why():
    """The non-Chinese template's reasoning_zh has always been German (it is
    the one field not in the target language), so the labels are German."""
    tpl = ai._KNOWLEDGE_PROMPT_NON_ZH
    assert '"Fakt: "' in tpl
    assert '"Warum: "' in tpl
    assert "no\ntwo sentences may carry the same one" in tpl or \
           "no two sentences may carry the same one" in tpl.replace("\n", " ")


def test_both_knowledge_prompts_show_two_parts_in_the_json_example():
    """The JSON example is what the model actually copies — a rule in prose
    with a one-part example underneath gets the one-part answer."""
    assert "事实：… 为什么：…" in ai.DEFAULT_PROMPT_TEMPLATES["knowledge"]
    assert "Fakt: … Warum: …" in ai._KNOWLEDGE_PROMPT_NON_ZH


# --- frontend half --------------------------------------------------------

def test_episode_id_comes_from_source_url_not_gen_params():
    """Since #790 each knowledge sentence stores its own in-app detail link,
    so the id is per sentence. gen_params only records the story's FIRST
    source, which is the wrong one as soon as several were selected
    (#752/#776) — and needs no new column either."""
    assert "function _episodeIdFromSourceUrl(url)" in APP_JS
    assert r"/^\/#(?:podcast|knowledge)-(\d+)$/" in APP_JS


def test_source_button_reuses_the_shared_summary_popup():
    """#930 already built the popup (fetch + cache + shared kahneman modal).
    A second implementation here would drift from the detail page the first
    time the zh / rendition branches change."""
    assert APP_JS.count("function openKnowledgeSummaryPopup(") == 1
    assert "function _wireKnowledgeSourceLink(container, s)" in APP_JS
    # Wired on both faces of the card, not just the back.
    assert APP_JS.count("_wireKnowledgeSourceLink(") == 3  # 1 definition + 2 calls


def test_knowledge_reasoning_goes_into_the_light_bulb():
    assert "_currentReasoningIsKnowledge" in APP_JS
    # The light bulb's own popup is reused, not duplicated.
    assert APP_JS.count("function openReasoning()") == 1


def test_knowledge_does_not_set_the_popup_source_link():
    """The popup's 'open source' anchor is an <a href> for external pages;
    pointing it at /#knowledge-12 would navigate away mid-review. Knowledge
    opens its source through the 📄 button instead."""
    branch = APP_JS[APP_JS.index("_currentReasoningIsKnowledge = true;") - 900:
                    APP_JS.index("_currentReasoningIsKnowledge = true;")]
    assert "_currentSourceUrl = '';" in branch


def test_other_modes_keep_their_inline_context():
    """Kontextsummary's context line above the sentence (#452/#454/#464) and
    book mode's chapter link (#865) must not be collateral damage.

    The guard is _hidesInlineContext(), not the episode id alone: since #1011
    Kontextsummary draws its material from the knowledge base too, so its
    sentences carry the same in-app source_url — guarding on the id would strip
    exactly the context block that mode exists for."""
    assert "function _hidesInlineContext(s)" in APP_JS
    assert "_activeStoryMode() !== 'contextsummary'" in APP_JS
    assert "const ctxText = _hidesInlineContext(sentence) ? '' : _newsContextText(sentence);" in APP_JS
    assert "openBookChapterSummary(" in APP_JS
    assert "book-(\\d+)-chapter-(\\d+)" in APP_JS

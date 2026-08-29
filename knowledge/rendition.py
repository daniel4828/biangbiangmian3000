"""Per-language reading renditions of a knowledge-base episode's summary
(#804). The AI writes summary_de exactly once, no matter how many languages
Daniel is studying; every other language's detail-page view is a lazily
generated, cached translate-then-annotate derivative of that one German
text. Chinese is not a "rendition" — summary_zh is already AI-native and
annotated by zh_annotate.py, so routes/podcast.py's get_episode() never
calls into this module for lang == 'zh'.

See docs/multilang.md ("Knowledge base") for the full design and
schema.sql's knowledge_renditions table for storage.
"""
import logging
import re

import ai
import annotate
import database
import languages
import translator

logger = logging.getLogger(__name__)


# summary_de is HTML (<p> paragraphs, <b> lead sentences). Sending that whole
# blob to Google Translate is wrong twice over: the free endpoint rejects
# anything past ~5000 characters, and it reorders/eats tags. So we split the
# markup into tag and text nodes here, translate only the text, and put the
# tags back untouched — the rendition keeps exactly the markup the summary
# had, which is what podcast._summary_zh_html / app.js._summaryZhHtml expect.
_TAG_RE = re.compile(r"(<[^>]*>)")
# The free Google endpoint's limit is ~5000 chars; translator.py uses the same
# 4500 budget for its own batching.
_CHUNK_CHAR_BUDGET = 4500
_SEP = "\n"


def _translate_html_strict(html: str, target: str, source: str = "de") -> str:
    """Translate the text nodes of `html`, leaving every tag byte-identical.

    Strict on purpose (#804): any failure raises so the caller can report it
    instead of storing German text under a French label. Text nodes are sent
    in newline-joined chunks under the endpoint's size limit; if a chunk comes
    back with a different number of lines than it went out with (Google
    occasionally merges or splits lines) that chunk's nodes are retried one at
    a time, which is slower but keeps every node aligned with its position in
    the document.
    """
    parts = _TAG_RE.split(html)
    text_idx = [i for i, part in enumerate(parts)
                if not part.startswith("<") and part.strip()]
    if not text_idx:
        return html

    chunks: list[list[int]] = []
    budget = 0
    for i in text_idx:
        if chunks and budget + len(parts[i]) > _CHUNK_CHAR_BUDGET:
            chunks.append([])
            budget = 0
        elif not chunks:
            chunks.append([])
        chunks[-1].append(i)
        budget += len(parts[i]) + 1

    for chunk in chunks:
        originals = [parts[i].strip() for i in chunk]
        translated = translator.translate_strict(
            _SEP.join(originals), target=target, source=source)
        lines = (translated or "").split(_SEP)
        if len(lines) != len(originals):
            logger.info(
                "knowledge.rendition: line-count mismatch (%d vs %d), retrying node by node",
                len(lines), len(originals))
            lines = [translator.translate_strict(o, target=target, source=source)
                     for o in originals]
        for i, orig, line in zip(chunk, originals, lines):
            line = (line or "").strip()
            if not line:
                raise RuntimeError("translator returned an empty segment")
            # Keep the original node's surrounding whitespace so words don't
            # get glued to an adjacent tag.
            lead = parts[i][:len(parts[i]) - len(parts[i].lstrip())]
            trail = parts[i][len(parts[i].rstrip()):]
            parts[i] = f"{lead}{line}{trail}"
    return "".join(parts)


class RenditionError(Exception):
    """No rendition could be produced. Callers must report the reason
    instead of writing (or returning) anything — #804 is explicit that a
    half-translated or untranslated summary must never be stored or served
    as if it were a real rendition."""


def render_html(html: str, lang: str, source: str = "de") -> tuple[str, list[dict]]:
    """Translate `html` into `lang` and annotate its new words.

    The whole translate-then-annotate pipeline, with nothing episode-specific
    left in it, so the book reader (#836) renders a book page by exactly the
    rules that render an episode summary — one implementation, one set of
    annotation results, no second pipeline to keep in sync (#643's lesson,
    applied here).

    `html` must be markup whose text lives in text nodes (<p>/<b>/…): only
    those are sent to the translator, so the caller gets its markup back
    byte-identical. Raises RenditionError on any failure — a caller must
    never be handed source-language text wearing a target-language label.
    """
    if not languages.is_valid_lang(lang):
        raise RenditionError(f"unknown language: {lang!r}")
    html = (html or "").strip()
    if not html:
        raise RenditionError("nothing to render (empty text)")

    target = languages.get_lang_config(lang)["translator_source"]
    if target == source:
        # Already in the target language: annotate only, don't round-trip it
        # through the translator (which would paraphrase perfectly good text).
        translated = html
    else:
        try:
            translated = _translate_html_strict(html, target=target, source=source)
        except Exception as e:
            logger.warning("knowledge.rendition: translation failed (%s→%s) — %s",
                           source, target, e)
            raise RenditionError(f"translation failed: {e}") from e
        translated = (translated or "").strip()
        if not translated:
            raise RenditionError("translation returned empty text")

    return annotate.annotate_summary(translated, lang)


def get_or_create_rendition(episode_id: int, lang: str) -> dict:
    """{"lang", "summary", "new_words"} for episode_id in `lang`. Cached in
    knowledge_renditions after the first successful generation — repeat
    detail-page views (and repeat requests before a language switch) don't
    re-translate. Raises RenditionError on any failure; nothing is written
    to the database in that case."""
    if lang == languages.DEFAULT_LANG:
        raise RenditionError("zh has no rendition — read summary_zh directly")
    if not languages.is_valid_lang(lang):
        raise RenditionError(f"unknown language: {lang!r}")

    cached = database.get_knowledge_rendition(episode_id, lang)
    if cached:
        return {"lang": lang, "summary": cached["summary"], "new_words": cached["new_words"]}

    episode = database.get_episode(episode_id)
    if not episode:
        raise RenditionError(f"episode {episode_id} not found")
    summary_de = (episode.get("summary_de") or "").strip()
    if not summary_de:
        raise RenditionError("episode has no German summary yet")
    if not ai.summary_de_is_german(summary_de):
        # The summary itself is in Chinese (#904). Translating de->fr leaves
        # such text essentially untouched, so without this guard the page
        # would show pinyin-annotated Chinese under a French label — exactly
        # the "source-language text wearing a target-language label" this
        # module refuses to serve. ai.py now rejects these at generation
        # time; this guard covers the rows already in the database, which
        # Daniel fixes with the detail page's "Regenerate summary" button.
        raise RenditionError(
            "the German summary is not in German (the model answered in "
            "Chinese) — regenerate the summary first")

    try:
        annotated, new_words = render_html(summary_de, lang, source="de")
    except RenditionError:
        logger.warning("knowledge.rendition: failed for episode %s lang=%s",
                       episode_id, lang)
        raise
    database.save_knowledge_rendition(episode_id, lang, annotated, new_words)
    logger.info("knowledge.rendition: generated episode %s lang=%s (%d new word(s))",
               episode_id, lang, len(new_words))
    return {"lang": lang, "summary": annotated, "new_words": new_words}


# --- full text (#972) -------------------------------------------------------

def text_to_paragraph_html(text: str) -> str:
    """Turn plain source text into the markup render_html() expects.

    transcript_zh holds plain text with newlines; render_html only sends
    *text nodes* to the translator, so the text has to sit inside tags to
    survive the round trip with its structure intact. Escaping first is not
    optional: a '<' in the source would otherwise become a tag boundary and
    everything after it would be treated as markup — swallowed by the
    translator's tag handling instead of translated (the same reason
    books/paginate.py escapes before wrapping).
    """
    import html as html_mod

    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    if not blocks:
        return ""
    return "".join(
        # Single newlines inside a block are line breaks, not paragraph
        # breaks — newsletters wrap their lines.
        f"<p>{html_mod.escape(b).replace(chr(10), '<br>')}</p>" for b in blocks
    )


def _source_lang_of(text: str) -> str:
    """Which language the source text is in, as a translator source code.

    transcript_zh means "source material in any language" (#772): a
    newsletter is German, a Chinese podcast's transcript is Chinese. Getting
    this wrong is not cosmetic — asking Google to translate de→zh text that
    is already Chinese returns it essentially unchanged, which would look
    like success and store un-translated text under a target language.
    """
    import zh_annotate
    return "zh-CN" if zh_annotate.cjk_ratio(text) >= 0.2 else "de"


def get_or_create_fulltext(episode_id: int, lang: str, generate: bool = False) -> dict | None:
    """The full source text of an episode, in `lang` (#972).

    Returns None when nothing is cached and `generate` is False — reading a
    detail page must not silently kick off a translation of an hour-long
    transcript. Only an explicit request (POST) passes generate=True.

    Unlike get_or_create_rendition(), `lang` may be 'zh': a summary has an
    AI-native Chinese version to fall back on, a full text has none.
    """
    if not languages.is_valid_lang(lang):
        raise RenditionError(f"unknown language: {lang!r}")

    cached = database.get_knowledge_fulltext(episode_id, lang)
    if cached:
        return {"lang": lang, "text": cached["text"], "new_words": cached["new_words"]}
    if not generate:
        return None

    episode = database.get_episode(episode_id)
    if not episode:
        raise RenditionError(f"episode {episode_id} not found")
    source_text = (episode.get("transcript_zh") or "").strip()
    if not source_text:
        raise RenditionError("这条素材没有原文可读（还没转录/抽取正文）")

    html = text_to_paragraph_html(source_text)
    if not html:
        raise RenditionError("这条素材的原文是空的")

    annotated, new_words = render_html(html, lang, source=_source_lang_of(source_text))
    database.save_knowledge_fulltext(episode_id, lang, annotated, new_words)
    logger.info("knowledge.rendition: full text for episode %s lang=%s (%d new word(s))",
                episode_id, lang, len(new_words))
    return {"lang": lang, "text": annotated, "new_words": new_words}

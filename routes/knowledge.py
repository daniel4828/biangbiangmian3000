"""Knowledge base ingestion API (issue #651, extended #652, #668): turn a
pasted URL — or a pasted article body (#668, for paywalled articles the
server can't fetch) — into a podcast_episodes row (kind='video' for
YouTube, kind='article' for everything else / pasted text) that the
existing podcast pipeline can transcribe/summarize.

All the actual resolution logic lives in knowledge/ingest.py's
ingest_url()/ingest_text() (extracted issue #655, extended #668) so the
IMAP mailbox script (knowledge/mailbox.py) can call the exact same
pipeline instead of hitting this HTTP endpoint or reimplementing it — one
ingestion path per source type, see that module's docstring for why.
"""
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import ai
import annotate
import database
import knowledge.audio_upload
import knowledge.files
import knowledge.ingest
import knowledge.youtube
import routes.story
from routes.utils import ai_disabled

logger = logging.getLogger(__name__)
router = APIRouter()


class AddKnowledgeRequest(BaseModel):
    url: str
    # #731: ticked at paste time for material critical of China, which is the
    # one case DeepSeek can't summarize honestly. Defaults to False so the
    # iOS-shortcut / mailbox callers that don't send it keep the cheap
    # DeepSeek-first behavior.
    china_critical: bool = False
    # #1054: YouTube audiobooks have no caption track at all, so this routes
    # the URL to knowledge.ingest._ingest_audiobook (download audio + queue
    # local ASR) instead of the ordinary captions-only path. Only meaningful
    # for YouTube URLs — see add_knowledge below.
    as_audiobook: bool = False
    # Set by the frontend when re-submitting after a {"status":
    # "confirm_required"} response (a video whose length ingest_url couldn't
    # confirm is short) — see knowledge/ingest.py's _MAX_UNCONFIRMED_SECONDS.
    confirm_long: bool = False


class AddKnowledgeTextRequest(BaseModel):
    text: str
    # All three are optional (#833): whatever is left blank gets filled in
    # server-side by one cheap AI pass over the body (knowledge.ingest's
    # _fill_missing_metadata), with the body's first line as the last-resort
    # title. `title` used to be required — the frontend refused to submit
    # without one, which just meant Daniel typed the first line by hand.
    title: str | None = None
    author: str | None = None
    source_url: str | None = None
    china_critical: bool = False


@router.post("/api/knowledge/add")
def add_knowledge(body: AddKnowledgeRequest):
    if body.as_audiobook and not knowledge.youtube.parse_video_id(body.url):
        raise HTTPException(400, "as_audiobook is only supported for YouTube links")
    try:
        return knowledge.ingest.ingest_url(
            body.url, china_critical=body.china_critical,
            as_audiobook=body.as_audiobook, confirm_long=body.confirm_long,
        )
    except knowledge.ingest.IngestError as e:
        raise HTTPException(400, str(e))


@router.post("/api/knowledge/add-text")
def add_knowledge_text(body: AddKnowledgeTextRequest):
    """Paste-a-body counterpart to POST /api/knowledge/add (#668). Same
    response contract ({episode_id} or {status:"already_exists",
    episode_id}) — the frontend's add flow branches on URL vs. text but
    otherwise treats the result identically (poll .../process next)."""
    try:
        return knowledge.ingest.ingest_text(body.title, body.text, source_url=body.source_url,
                                            author=body.author,
                                            china_critical=body.china_critical)
    except knowledge.ingest.IngestError as e:
        raise HTTPException(400, str(e))


@router.post("/api/knowledge/add-file")
async def add_knowledge_file(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    author: str | None = Form(None),
    source_url: str | None = Form(None),
    china_critical: bool = Form(False),
):
    """Upload a .txt/.md/.pdf/.docx file (#835). This route only turns the
    file into text — storage goes through ingest_text(), the same function
    the paste box uses, so there is still exactly one path into
    podcast_episodes (see knowledge/ingest.py's docstring, #643).

    Same response contract as /api/knowledge/add-text ({episode_id} or
    {status:"already_exists", episode_id}), so the frontend handles an
    upload exactly like a paste.

    The filename (minus extension) is only the last-resort title: what
    Daniel typed wins, then whatever the AI reads out of the body (#833).
    """
    data = await file.read()
    try:
        text, title_guess = knowledge.files.extract_file_text(file.filename, data)
    except knowledge.files.FileExtractionError as e:
        raise HTTPException(400, str(e))

    try:
        return knowledge.ingest.ingest_text(
            (title or "").strip() or None, text,
            source_url=source_url, author=author,
            china_critical=china_critical,
            fallback_title=title_guess,
            platform="upload",
        )
    except knowledge.ingest.IngestError as e:
        raise HTTPException(400, str(e))


@router.post("/api/knowledge/add-audio")
async def add_knowledge_audio(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    author: str | None = Form(None),
    china_critical: bool = Form(False),
):
    """Upload an audio file directly (#1068) — the one ingestion path that
    depends on no external site: YouTube blocks the server's IP outright, and
    the #1067 cookie workaround eventually expires.

    Does NOT go through knowledge/files.py's extract_file_text() -> the
    ingest_text() family: an audio file has no text to extract, it needs
    transcription, which is exactly what this queues (same local-ASR job
    queue #1054's YouTube audiobook path uses, via
    knowledge.audio_upload.ingest_audio_upload ->
    knowledge.ingest._store_audiobook_episode — one row-building
    implementation, not a second one for this second source).

    `file.file` (not `await file.read()`) is passed straight through so the
    bytes are streamed to disk in fixed-size chunks — a multi-hundred-MB
    audiobook must never be pulled entirely into process memory.

    Same response contract as /api/knowledge/add ({episode_id, queued:True}
    or {status:"already_exists", episode_id}); unlike the URL/text/file-text
    paths there is no separate .../process call to kick off afterward — the
    file is already queued for transcription, not summarization, and
    summarization only makes sense once that transcript exists.
    """
    try:
        return knowledge.audio_upload.ingest_audio_upload(
            file.file, file.filename,
            title=title, author=author, china_critical=china_critical,
        )
    except knowledge.audio_upload.AudioUploadError as e:
        raise HTTPException(400, str(e))
    finally:
        file.file.close()


# ── Known words (#710) ──────────────────────────────────────────────────────
# Words Daniel knows without having studied them here. Marking one only
# widens zh_annotate's "already known" test (see database.known_words_exists)
# — no card is created and nothing is scheduled, which is the whole point:
# these are words he does NOT want to see again.


class KnownWordRequest(BaseModel):
    word: str
    # #804: known_words is keyed per language (see #803's known_words.lang) —
    # a known French word and a known Chinese word that happen to share a
    # surface form must not silently mark each other known. Defaults to 'zh'
    # so pre-#804 callers (no lang field sent) keep their existing behavior.
    lang: str = "zh"


@router.get("/api/known-words")
def get_known_words(lang: str | None = None):
    return {"words": database.list_known_words(lang)}


@router.post("/api/known-words")
def add_known_word(body: KnownWordRequest):
    word = (body.word or "").strip()
    if not word:
        raise HTTPException(400, "word required")
    database.add_known_word(word, body.lang)
    return {"status": "ok", "word": word, "lang": body.lang}


@router.delete("/api/known-words/{word}")
def delete_known_word(word: str, lang: str = "zh"):
    """Undo. Reports a miss instead of pretending — a 404 here means the
    frontend and the database disagree about what is on the list."""
    if not database.remove_known_word(word.strip(), lang):
        raise HTTPException(404, "word not on the known list")
    return {"status": "ok", "word": word, "lang": lang}


# ── New words of one text (#1006) ───────────────────────────────────────────


class NewWordsRequest(BaseModel):
    text: str
    lang: str = "zh"
    mode: str = "new"   # "new" | "all" (#1018)


@router.post("/api/new-words")
def new_words(body: NewWordsRequest):
    """Which words of `text` Daniel does not know yet, in order of first
    appearance — the same answer the knowledge reader and the book reader get,
    because it is the same call (annotate.annotate_summary, #804).

    The listening-hint slider (#1006) is the caller: its middle state blanks
    everything except these words. It used to decide that in the browser from
    /api/vocab-index + static/hsk_levels.json, which knows nothing about
    known_words (#710), the baseline lists (#922) or the transparent-compound
    rule (#638) — so the same word could count as new while reading and as
    known while reviewing. One judge, one answer.

    Never fails: annotate_summary swallows its own errors and returns [], and
    an empty text is not an error either (a card can have no sentence yet).

    mode="all" (#1018): every word of the text gets a gloss, not just the
    ones Daniel doesn't know yet — the "show every gloss" gesture (#996)
    extended past the new-word table. Same never-raises contract via
    annotate.all_words()."""
    text = (body.text or "").strip()
    if not text:
        return {"words": []}
    if body.mode == "all":
        return {"words": _with_entry_ids(annotate.all_words(text, body.lang), body.lang)}
    _, words = annotate.annotate_summary(text, body.lang)
    return {"words": words}


def _with_entry_ids(words: list[dict], lang: str) -> list[dict]:
    """Tag every word that already has an entry with its word_id (#1042).

    Only mode="all" needs this: a *new* word is by definition one without an
    entry. The reader uses it to make a word Daniel has already studied
    tappable — straight into its detail page — instead of leaving it as the
    display-only gloss #1018 gave it.

    Best-effort like everything else on this path: a lookup failure costs the
    links, not the glosses."""
    if not words:
        return words
    try:
        ids = database.entry_ids_for_forms([w.get("word", "") for w in words], lang)
    except Exception as e:
        logger.warning("new-words: entry id lookup failed for lang=%s — %s", lang, e)
        return words
    for w in words:
        wid = ids.get(w.get("word", ""))
        if wid:
            w["word_id"] = wid
    return words


# ── Chat about a knowledge item (#945) ──────────────────────────────────────
# Follow-up questions about the material Daniel just read, saved so they are
# still there next time he opens the item. The context is rebuilt from
# podcast_episodes on every turn (never copied into the chat tables), so a
# regenerated summary is immediately what the AI sees.


class KnowledgeChatRequest(BaseModel):
    message: str
    # Same whitelist the story model dropdown uses (routes/story.ALLOWED_MODELS);
    # an unknown value falls back to the default with a warning rather than
    # being sent to an API as if it were a model name (#721).
    model: str | None = None


@router.get("/api/knowledge/{episode_id}/chat")
def get_knowledge_chat(episode_id: int):
    """The saved conversation for one item. An item nobody has asked about
    yet is an empty list, not a 404 — the panel renders either way."""
    if database.get_episode(episode_id) is None:
        raise HTTPException(404, "knowledge item not found")
    chat = database.get_chat(episode_id)
    return {
        "episode_id": episode_id,
        "model": (chat or {}).get("model"),
        "messages": (chat or {}).get("messages", []),
    }


@router.post("/api/knowledge/{episode_id}/chat")
def post_knowledge_chat(episode_id: int, body: KnowledgeChatRequest):
    """Ask one question about the item and store the turn.

    Nothing is written until the answer is in hand: an AI failure must leave
    the history exactly as it was, so Daniel can just press send again (the
    frontend keeps his text in the box). A stored question with no answer
    would be a permanent hole in the conversation.
    """
    question = (body.message or "").strip()
    if not question:
        raise HTTPException(400, "message required")
    if ai_disabled():
        raise HTTPException(400, "AI is disabled")

    episode = database.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404, "knowledge item not found")

    # Same rule the knowledge story mode uses (#661): full transcript when
    # there is one, summary otherwise. Imported from routes.story rather than
    # copied so the two can't drift apart.
    material = routes.story._knowledge_material(episode)
    if not material:
        raise HTTPException(400, "this item has no transcript or summary yet — process it first")

    chat = database.get_chat(episode_id)
    history = (chat or {}).get("messages", [])
    model = routes.story._validated_model(body.model)

    try:
        answer, used_model = ai.chat_about_material(
            material, episode.get("title") or "", history, question, model=model)
    except Exception as e:
        logger.exception("knowledge chat failed for episode %s", episode_id)
        raise HTTPException(500, f"AI call failed: {e}")

    stored = database.add_turn(episode_id, question, answer, used_model)
    return {"episode_id": episode_id, "model": used_model, "messages": stored["messages"]}


@router.delete("/api/knowledge/{episode_id}/chat")
def delete_knowledge_chat(episode_id: int):
    """Clear the conversation. 404 when there was none — reporting a miss
    beats pretending success on an item the frontend thinks has a chat."""
    if not database.delete_chat(episode_id):
        raise HTTPException(404, "no chat for this item")
    return {"status": "ok", "episode_id": episode_id}

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

import database
import knowledge.files
import knowledge.ingest

logger = logging.getLogger(__name__)
router = APIRouter()


class AddKnowledgeRequest(BaseModel):
    url: str
    # #731: ticked at paste time for material critical of China, which is the
    # one case DeepSeek can't summarize honestly. Defaults to False so the
    # iOS-shortcut / mailbox callers that don't send it keep the cheap
    # DeepSeek-first behavior.
    china_critical: bool = False


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
    try:
        return knowledge.ingest.ingest_url(body.url, china_critical=body.china_critical)
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

"""In-app AI dictionary API (#746): a single-turn structured lookup that
replaces pasting words into a chat AI and copying the answer by hand. Every
result is stored (database/dictionary.py) so the /dict page can show a
searchable history. Adding a word from a result goes through the existing
POST /api/add-word-ai — this router never touches cards/entries directly
(#643's "one add pipeline" rule).
"""
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import ai
import database
from routes.utils import ai_disabled

logger = logging.getLogger(__name__)
router = APIRouter()


class DictLookupRequest(BaseModel):
    query: str
    lang: str = "zh"
    model: str | None = None
    # Repeat button (#777): ask the same question again and overwrite this
    # stored row instead of adding a second one. Daniel wants only the latest
    # answer in the history — a repeat means "that one was bad", not "keep both".
    replace_id: int | None = None


def _row_to_result(row: dict) -> dict:
    """Shape shared by POST /lookup and GET /history/{id}: the stored row
    plus the parsed JSON blob under "result"."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "query": row["query"],
        "lang": row["lang"],
        "result": json.loads(row["result_json"]),
    }


@router.post("/api/dict/lookup")
def lookup(body: DictLookupRequest):
    query = _validate_request(body)

    try:
        result, model_used = ai.dictionary_lookup(query, lang=body.lang, model=body.model)
    except ValueError as e:
        # Nothing is written on a parse failure — for a repeat that also means
        # the previous (good) answer stays untouched: a bad retry must never
        # destroy what it was meant to improve.
        logger.error("dict lookup failed for %r: %s", query, e)
        raise HTTPException(500, str(e))

    return _store(query, body.lang, body.replace_id, result, model_used)


def _validate_request(body: DictLookupRequest) -> str:
    """Shared up-front checks for both lookup endpoints. A bad language is the
    caller's mistake (400, not a 500 from the ValueError later); a missing
    replace_id is checked *before* spending a paid AI call on a repeat that
    could never be stored."""
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(400, "query required")
    if ai_disabled():
        raise HTTPException(400, "AI is disabled")
    if body.lang not in ("zh", "fr", "es"):
        raise HTTPException(400, f"language {body.lang!r} is not supported yet (zh/fr/es only)")
    if body.replace_id is not None and database.get_dict_query(body.replace_id) is None:
        raise HTTPException(404, "not found")
    return query


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/api/dict/lookup-stream")
def lookup_stream(body: DictLookupRequest):
    """Streaming lookup (#1381). Validation errors are plain HTTP errors raised
    before the stream starts; once streaming, failures arrive as an `error`
    event. The answer is parsed and stored only after the full text is in —
    on any failure nothing is written (a repeat keeps its old answer).
    text/event-stream so GZipMiddleware / the proxy don't buffer the chunks."""
    query = _validate_request(body)
    model_used = ai.dictionary_model(body.model)

    def gen():
        try:
            parts = []
            for delta in ai.dictionary_lookup_stream(query, lang=body.lang, model=body.model):
                parts.append(delta)
                yield _sse({"type": "delta", "text": delta})
            result = ai.parse_dictionary_response("".join(parts))
            record = _store(query, body.lang, body.replace_id, result, model_used)
        except Exception as e:  # noqa: BLE001 — every failure must reach the client as an error event
            logger.error("dict lookup (stream) failed for %r: %s", query, e)
            yield _sse({"type": "error", "detail": str(e)})
            return
        yield _sse({"type": "done", "record": record})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _store(query: str, lang: str, replace_id: int | None, result: dict, model_used: str) -> dict:
    """Save a new row or overwrite replace_id; return the record shape."""
    fields = dict(
        input_lang=result.get("input_lang"),
        kind=result.get("kind"),
        headline=result.get("headline") or None,
        result_json=json.dumps(result, ensure_ascii=False),
        model=model_used,
    )
    if replace_id is not None:
        if not database.update_dict_query(replace_id, **fields):
            raise HTTPException(404, "not found")
        row_id = replace_id
    else:
        row_id = database.save_dict_query(query=query, lang=lang, **fields)
    return _row_to_result(database.get_dict_query(row_id))


@router.get("/api/dict/history")
def history(q: str | None = None, limit: int = 50):
    return {"items": database.list_dict_queries(q=q, limit=limit)}


@router.get("/api/dict/history/{qid}")
def history_item(qid: int):
    row = database.get_dict_query(qid)
    if row is None:
        raise HTTPException(404, "not found")
    return _row_to_result(row)


@router.delete("/api/dict/history/{qid}")
def delete_history_item(qid: int):
    if not database.delete_dict_query(qid):
        raise HTTPException(404, "not found")
    return {"status": "ok"}

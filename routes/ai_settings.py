"""GPT-Backend-Einstellung (#1582): bezahlte API oder ChatGPT/Codex-Abo."""
import ai
import codex_sub
import database
from fastapi import APIRouter, HTTPException

router = APIRouter()


def _state() -> dict:
    return {
        "gpt_backend": ai.gpt_backend(),
        "options": list(ai.GPT_BACKENDS),
        "codex": codex_sub.login_status(),
    }


@router.get("/api/ai-backend")
def get_ai_backend():
    return _state()


@router.put("/api/ai-backend")
def put_ai_backend(body: dict):
    value = body.get("gpt_backend")
    if value not in ai.GPT_BACKENDS:
        raise HTTPException(400, f"gpt_backend must be one of {list(ai.GPT_BACKENDS)}")
    database.set_app_setting("gpt_backend", value)
    return _state()

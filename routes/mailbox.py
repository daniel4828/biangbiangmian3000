"""📬 Mailbox (#960): browse the Gmail inbox from inside the app and turn
the mails Daniel actually wants to read into knowledge-base entries.

The pipeline is the existing one, end to end. This module fetches a mail
and hands it to knowledge.mailbox.ingest_message() — the same routing and
the same ingest functions the cron uses — then delegates transcription and
summarising to routes.podcast.process_episode(), so the resulting entry is
indistinguishable from one that arrived by any other route, and the #821
task indicator picks it up for free.

Nothing here mirrors mail into the database: the list is fetched live from
IMAP on every request (envelopes only), and a body is downloaded only for
the one mail Daniel pressed "process" on.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
import knowledge.ingest
import knowledge.mailbox
from routes import podcast as podcast_routes

logger = logging.getLogger(__name__)
router = APIRouter()

# One page's worth. Capped so a stray ?limit=5000 can't turn one page view
# into 5000 IMAP round trips.
_MAX_LIMIT = 200


def _range(name: str) -> str:
    """Fall back to the default rather than 400 on an unknown range: the
    range is a view preference, and refusing to show the mailbox over a
    typo in a query string helps nobody."""
    return name if name in knowledge.mailbox._RANGES else knowledge.mailbox.DEFAULT_RANGE


@router.get("/api/mailbox")
def list_mailbox(offset: int = 0, limit: int = 50, q: str = "",
                 range: str = knowledge.mailbox.DEFAULT_RANGE):
    """One page of the inbox, newest first. Envelopes only — no mail body
    is downloaded here, and no flag is ever changed (the IMAP session is
    opened read-only)."""
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    try:
        return knowledge.mailbox.list_inbox(offset=offset, limit=limit,
                                            query=q.strip(), range_name=_range(range))
    except knowledge.mailbox.MailboxError as e:
        raise HTTPException(502, str(e))


@router.delete("/api/mailbox/{uid}")
def delete_mail(uid: str, uidvalidity: str = ""):
    """Move one mail to Gmail's trash — recoverable there for 30 days.
    Never an expunge (#968)."""
    try:
        return knowledge.mailbox.delete_message(uid, uidvalidity=uidvalidity)
    except knowledge.mailbox.MailboxError as e:
        raise HTTPException(502, str(e))


@router.post("/api/mailbox/{uid}/process")
def process_mail(uid: str, uidvalidity: str = ""):
    """Ingest one mail and start summarising it.

    Ingest runs synchronously — it is a fetch plus a database insert, and
    doing it here is what lets the response carry the episode_id the UI
    needs to link to. Only the slow part (transcription/summary/AI) goes to
    the background, through the very same endpoint the podcast and
    knowledge pages use, so its progress, its 409-on-double-submit guard
    and its task-indicator entry all come along unchanged.
    """
    try:
        msg = knowledge.mailbox.fetch_message(uid, uidvalidity=uidvalidity)
    except knowledge.mailbox.MailboxError as e:
        raise HTTPException(502, str(e))

    try:
        result = knowledge.mailbox.ingest_message(msg)
    except knowledge.ingest.IngestError as e:
        # A mail that can't become an article is a 400 with the reason, not
        # an empty knowledge entry Daniel discovers later (same contract as
        # /api/knowledge/add-text).
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("mailbox: 处理邮件 uid=%s 失败: %s", uid, e)
        raise HTTPException(500, f"处理失败: {e}")

    episode_id = result.get("episode_id")
    if not episode_id:
        raise HTTPException(500, "入库后没有拿到条目 id")

    if result.get("status") == "already_exists":
        # Nothing to summarise again, and nothing to charge for. The UI
        # links straight to the entry that already exists.
        return {"status": "already_exists", "episode_id": episode_id,
                "route": result.get("route")}

    try:
        podcast_routes.process_episode(episode_id)
    except HTTPException as e:
        # 409 (already running) and 400 (already summarised) are both fine
        # outcomes here — the entry exists either way, which is what the UI
        # cares about. Anything else is worth surfacing.
        if e.status_code not in (400, 409):
            raise
    return {"status": "processing", "episode_id": episode_id,
            "route": result.get("route"),
            "episode_ids": result.get("episode_ids")}


@router.get("/api/mailbox/senders")
def list_senders(refresh: bool = False,
                 range: str = knowledge.mailbox.DEFAULT_RANGE):
    """Who writes to this mailbox, with their subscription state (#965).

    The scan reads every header in the mailbox in one IMAP round trip, so
    it is cached for a few minutes; `refresh=1` (the ⟳ button) bypasses it.
    If the mailbox is unreachable the configured senders are still returned
    — unsubscribing must not depend on IMAP being up.
    """
    try:
        return knowledge.mailbox.list_senders(refresh=refresh, range_name=_range(range))
    except knowledge.mailbox.MailboxError as e:
        return {"senders": database.get_mail_senders(), "scanned": 0,
                "cached": False, "range": _range(range), "error": str(e)}


class SenderUpdate(BaseModel):
    address: str
    auto: bool
    name: str | None = None


class SenderBlock(BaseModel):
    address: str
    blocked: bool
    name: str | None = None


@router.put("/api/mailbox/senders")
def set_sender(payload: SenderUpdate):
    """Turn automatic processing for one sender on or off. This is the same
    gate the cron reads, so switching it on means "every future mail from
    this address gets summarised without asking" — including the paid AI
    call."""
    try:
        return database.set_mail_sender_auto(payload.address, payload.auto, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/api/mailbox/senders/block")
def block_sender(payload: SenderBlock):
    """Block or unblock a sender (#968): blocked mail is hidden from the
    list and never processed. Blocking clears the automatic switch — the
    two states contradict each other."""
    try:
        return database.set_mail_sender_blocked(payload.address, payload.blocked, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/mailbox/senders/{address}")
def delete_sender(address: str):
    """Forget a sender's settings — back to manual and unblocked. Deletes
    no mail: "delete sender" is about the setting, and the two must not be
    confusable. 404 when there was nothing stored, rather than a silent OK."""
    if not database.delete_mail_sender(address):
        raise HTTPException(404, "This sender has no stored settings")
    return {"status": "deleted", "address": address.strip().lower()}

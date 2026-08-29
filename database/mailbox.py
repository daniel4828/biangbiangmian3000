"""Mailbox view (#960): per-sender auto-process switches, and the
Message-ID bookkeeping that lets the mail list say "already processed"
without downloading a single message body.

Why Message-ID and not the existing dedup key: ingest_text()'s dedup key is
a hash of the *body* (stored in podcast_episodes.video_id). Computing it
means downloading the body — and the mail list deliberately fetches
envelopes only (a 1600-mail inbox, on every page view). The Message-ID sits
in the envelope we already have, so one cheap query turns the whole page
into "processed / not processed". The body hash stays as the second line of
defence: the same newsletter pasted in via Signal is still recognised as a
duplicate, Message-ID or not.
"""
from .core import get_db


def get_mail_senders() -> list:
    """Every sender Daniel has explicitly configured. Senders with no row
    here are manual — the default, and the safe direction."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT address, name, auto_process, created_at "
            "FROM mail_senders ORDER BY auto_process DESC, address"
        ).fetchall()
    return [dict(r) for r in rows]


def auto_mail_senders() -> set:
    """Addresses whose mail the cron processes unattended."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT address FROM mail_senders WHERE auto_process = 1"
        ).fetchall()
    return {r["address"] for r in rows}


def set_mail_sender_auto(address: str, auto: bool, name: str = None) -> dict:
    """Turn the automatic switch for one sender on or off.

    `name` is only ever written, never used to match: the display name a
    sender puts in its From header changes ("F.A.Z." vs "F.A.Z. Frühdenker")
    while the address does not, so the address alone is the identity.
    """
    address = (address or "").strip().lower()
    if not address:
        raise ValueError("address is required")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO mail_senders (address, name, auto_process) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "  auto_process = excluded.auto_process, "
            "  name = COALESCE(excluded.name, mail_senders.name)",
            (address, name, 1 if auto else 0),
        )
        conn.commit()
    return {"address": address, "auto_process": 1 if auto else 0, "name": name}


def processed_mail_message_ids(message_ids: list) -> dict:
    """Map Message-ID -> episode id for the ones already in the knowledge
    base. Called once per mail-list page with that page's ids, never per
    row: 50 separate lookups per page view is the kind of thing that only
    shows up as "the mailbox feels slow" months later."""
    ids = [m for m in (message_ids or []) if m]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, mail_message_id FROM podcast_episodes "
            f"WHERE mail_message_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {r["mail_message_id"]: r["id"] for r in rows}


def set_mail_message_id(episode_id: int, message_id: str) -> None:
    """Stamp an episode with the mail it came from. Never overwrites a
    non-empty value: an episode ingested from one mail keeps pointing at
    that mail even if the same body later arrives again from elsewhere."""
    if not message_id:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE podcast_episodes SET mail_message_id = ? "
            "WHERE id = ? AND (mail_message_id IS NULL OR mail_message_id = '')",
            (message_id, episode_id),
        )
        conn.commit()

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
            "SELECT address, name, auto_process, blocked, created_at "
            "FROM mail_senders ORDER BY auto_process DESC, address"
        ).fetchall()
    return [dict(r) for r in rows]


def auto_mail_senders() -> set:
    """Addresses whose mail the cron processes unattended.

    Blocked senders are excluded here as well as in the UI: the switch and
    the block live in the same row, and a check that exists in only one of
    the two places is a blocked sender who still costs money every morning.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT address FROM mail_senders WHERE auto_process = 1 AND blocked = 0"
        ).fetchall()
    return {r["address"] for r in rows}


def auto_mail_sender_windows() -> dict:
    """address -> the moment its automatic switch was turned on (#997), or
    None for rows that predate the column.

    The cron uses it as the lower bound of its IMAP SINCE: a fresh
    subscription must not reach back and summarise (at one paid AI call
    each) everything that sender already sent. The UI says as much.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT address, auto_since FROM mail_senders "
            "WHERE auto_process = 1 AND blocked = 0"
        ).fetchall()
    return {r["address"]: r["auto_since"] for r in rows}


_ABANDONED_KEY = "mail_abandoned_message_ids"
_ABANDONED_MAX = 200


def abandoned_mail_message_ids() -> set:
    """Message-IDs the cron gave up on permanently (#997).

    Bookkeeping used to be the \\Seen flag; since the cron no longer looks
    at read state (that is what made a mail Daniel had opened in Gmail
    invisible to it forever), a mail that can never succeed — a body too
    short to be an article — needs somewhere else to be remembered. Without
    it the same doomed mail is re-downloaded and re-attempted every five
    minutes for as long as it stays inside the window.
    """
    import json
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_ABANDONED_KEY,)
        ).fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (ValueError, TypeError):
        return set()


def abandon_mail_message_id(message_id: str) -> None:
    """Remember one permanently-failed mail. Keeps only the most recent
    _ABANDONED_MAX: this is a "don't retry" hint, not an archive, and an
    unbounded JSON blob in app_settings would be read on every cron run."""
    import json
    message_id = (message_id or "").strip()
    if not message_id:
        return
    ids = [m for m in abandoned_mail_message_ids() if m != message_id]
    ids.append(message_id)
    ids = ids[-_ABANDONED_MAX:]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_ABANDONED_KEY, json.dumps(ids)),
        )
        conn.commit()


def blocked_mail_senders() -> set:
    """Addresses hidden from the mail list entirely (#968)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT address FROM mail_senders WHERE blocked = 1"
        ).fetchall()
    return {r["address"] for r in rows}


def set_mail_sender_blocked(address: str, blocked: bool, name: str = None) -> dict:
    """Block or unblock a sender. Blocking clears auto_process — see the
    column comment in schema.sql for why the contradictory state is not
    allowed to exist rather than resolved later."""
    address = (address or "").strip().lower()
    if not address:
        raise ValueError("address is required")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO mail_senders (address, name, blocked, auto_process) "
            "VALUES (?, ?, ?, 0) "
            "ON CONFLICT(address) DO UPDATE SET "
            "  blocked = excluded.blocked, "
            "  auto_process = CASE WHEN excluded.blocked = 1 THEN 0 ELSE mail_senders.auto_process END, "
            "  name = COALESCE(excluded.name, mail_senders.name)",
            (address, name, 1 if blocked else 0),
        )
        conn.commit()
    return {"address": address, "blocked": 1 if blocked else 0}


def delete_mail_sender(address: str) -> bool:
    """Forget a sender's settings — back to the default (manual, not
    blocked). Deletes no mail: the mail list is a live IMAP view, nothing
    about it is stored here. Returns False if there was no row, so the API
    can 404 instead of pretending."""
    address = (address or "").strip().lower()
    with get_db() as conn:
        cur = conn.execute("DELETE FROM mail_senders WHERE address = ?", (address,))
        conn.commit()
    return cur.rowcount > 0


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
            "INSERT INTO mail_senders (address, name, auto_process, auto_since) "
            "VALUES (?, ?, ?, CASE WHEN ? = 1 THEN datetime('now') END) "
            "ON CONFLICT(address) DO UPDATE SET "
            "  auto_process = excluded.auto_process, "
            # Subscribing to a blocked sender unblocks them — that is what
            # the click means, and the two states must not coexist.
            "  blocked = CASE WHEN excluded.auto_process = 1 THEN 0 ELSE mail_senders.blocked END, "
            # Stamped only on the off -> on transition (#997): re-clicking a
            # switch that is already on must not move the window forward and
            # skip mail that arrived in between.
            "  auto_since = CASE WHEN excluded.auto_process = 1 AND mail_senders.auto_process = 0 "
            "                    THEN datetime('now') ELSE mail_senders.auto_since END, "
            "  name = COALESCE(excluded.name, mail_senders.name)",
            (address, name, 1 if auto else 0, 1 if auto else 0),
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

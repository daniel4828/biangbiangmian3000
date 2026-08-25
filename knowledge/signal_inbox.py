"""Knowledge base Signal intake (issue #749): poll signal-cli's linked-device
session for messages Daniel shared to his own "Note to Self" conversation,
and ingest any URL found in them via the one shared pipeline,
`knowledge.ingest.ingest_url()` — the exact same pipeline the paste-a-URL box
in the UI uses (POST /api/knowledge/add) and knowledge/mailbox.py's IMAP
intake use. No second/parallel "URL -> episode row" implementation here
either — see knowledge/ingest.py's docstring for why that matters in this
repo (#643 is the cautionary tale: two entry points, a bug fixed in one
came back in the other).

A message whose first line is just the keyword `text` (#834) is handled the
other way round: the rest of the message IS the article, ingested via
`knowledge.ingest.ingest_text()` — the same function the paste-a-body box in
the UI uses. That covers paywalled pieces the server can never fetch but
Daniel can read and copy on his phone.

Unlike IMAP (mailbox.py can leave a message UNSEEN and retry it next poll),
`signal-cli receive` permanently drains the queued messages off the Signal
server the moment it's called — there is no "leave it, try again next run"
option at the protocol level. So a URL whose ingest_url() call fails here is
stashed in a small JSON retry queue (app_settings['signal_retry_queue'],
via database.get_app_setting/set_app_setting) and re-attempted on the next
poll, up to 3 attempts before it's dropped (and Daniel is told so in the
receipt).

Security: the server holds a *linked device* of Daniel's own Signal account
(SIGNAL_ACCOUNT, same env var podcast.send_signal() sends notifications
from). signal-cli syncs down EVERY message Daniel's primary device (his
phone) sends, not just Note-to-Self ones — an ordinary message he sends to
a friend shows up here too, as a `syncMessage.sentMessage` with a different
destination. Only messages both FROM the account and addressed back TO the
account itself (Note to Self) are ever treated as ingest input; anything
else is ignored. This is the same role KNOWLEDGE_MAIL_ALLOWED_SENDERS plays
for the mailbox intake — the one gate stopping this channel from turning
into "anyone who can message Daniel triggers a paid AI call".

Privacy (measured in production, #755): `receive` doesn't hand over just
Note-to-Self messages, it drains EVERYTHING Signal has queued for this
linked device — message bodies from other people, attachment metadata,
read receipts, typing indicators. The gate above keeps all of it out of the
database, but it does pass through this process's memory. So: never log a
raw envelope, and never put envelope contents in an error message or a
Signal receipt. Log the URL and the outcome, nothing else.
"""
import json
import logging
import os
import subprocess

import database
import knowledge.ingest
import podcast
from knowledge.mailbox import extract_urls

logger = logging.getLogger(__name__)

_RETRY_QUEUE_KEY = "signal_retry_queue"
_MAX_ATTEMPTS = 3

# Keyword that turns a Note-to-Self message into "this whole message IS the
# article" instead of "find the links in it" (#834). Paywalled pieces
# (Spiegel+, FAZ) can't be fetched server-side, but Daniel can select the
# text on his phone and share it here.
#
# The keyword must occupy the first line ALONE: a normal message that merely
# begins with the word "Text" ("Text von gestern, siehe Link") must keep
# going down the URL path. Matching is case-insensitive because phone
# keyboards capitalise the first word of a message on their own — Daniel
# types "text", the phone may send "Text", and that must not silently fail.
_PASTE_KEYWORDS = {"text", "文本"}

# `signal-cli receive` without -t does NOT "drain the queue and exit" — it
# keeps listening for new messages until killed, which is exactly what we
# don't want from a cron one-shot. -t tells it to return once the queue has
# been quiet for this many seconds (#755: the first production run hit the
# subprocess timeout instead of ever returning).
_RECEIVE_IDLE_SECONDS = 10

# Wall-clock ceiling for the whole receive call. Generous on purpose: the
# FIRST run after an account has only ever *sent* (this one had been sending
# podcast notifications since #521 without ever receiving) has to chew
# through everything Signal queued up server-side — sync copies of every
# message from every linked device, delivery receipts, typing indicators.
# That measured at over 2 minutes in production, which is what blew the
# original 120s ceiling. Later runs, five minutes apart, see a handful of
# envelopes and return in seconds.
_RECEIVE_TIMEOUT = 300


def _default_runner(args: list) -> str:
    """Default `runner`: shell out to signal-cli for real. Tests always
    inject a fake runner instead — see module tests, and knowledge/mailbox.py
    which does the equivalent with `imap_factory` for the same reason
    (CLAUDE.md: test suites must never reach real network services)."""
    result = subprocess.run(args, capture_output=True, timeout=_RECEIVE_TIMEOUT)
    stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        raise RuntimeError(f"signal-cli exited {result.returncode}: {stderr.strip()}")
    return stdout


def _load_retry_queue() -> list:
    raw = database.get_app_setting(_RETRY_QUEUE_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _save_retry_queue(queue: list) -> None:
    database.set_app_setting(_RETRY_QUEUE_KEY, json.dumps(queue))


def _extract_note_to_self_text(envelope: dict, account: str) -> str | None:
    """Return the message body if `envelope` is a Note-to-Self message from
    Daniel's own account (`account`), else None.

    Two JSON shapes both count, matching signal-cli's `-o json receive`
    output:
      - envelope.syncMessage.sentMessage — the normal case for a linked
        device: a sync copy of a message the phone sent. Only qualifies as
        Note to Self if BOTH the envelope's source AND the sentMessage's
        destination are the account itself (a message sent to a friend has
        the same source but a different destination, and must be ignored —
        see module docstring's Security section).
      - envelope.dataMessage — a message addressed directly to this device
        with source == account. Not how Note-to-Self normally arrives for a
        linked device, but handled defensively rather than assumed away.

    Every other shape (an ordinary incoming message from someone else, a
    receipt, a typing indicator, ...) returns None.

    This check is deliberately fail-closed: anything that isn't PROVEN to be
    Note-to-Self is dropped, never guessed at as "probably fine". Two traps
    that make this necessary:
      - A message Daniel sends to a GROUP has no destinationNumber/Uuid at
        all — only `groupInfo` — so a naive "dest is empty -> allow" check
        (the bug this replaced) waves through every group message he sends
        from his phone. He'd have no idea the server was fetching and
        summarizing links from his group chats.
      - Any other missing/ambiguous destination is treated the same way:
        better to silently miss a legitimate Note-to-Self message (Daniel
        just re-shares it, mildly annoying) than to silently spend money
        transcribing/summarizing something he never meant to send here.
    """
    source = (envelope.get("sourceNumber") or envelope.get("sourceUuid") or
              envelope.get("source") or "").strip()
    if not source or source != account:
        return None

    sync = envelope.get("syncMessage") or {}
    sent = sync.get("sentMessage")
    if sent is not None:
        if sent.get("groupInfo"):
            # Sent to a group, not to himself — never Note to Self.
            return None
        dest = (sent.get("destinationNumber") or sent.get("destinationUuid") or
                sent.get("destination") or "")
        if not dest or dest != account:
            # No provable recipient, or a recipient that isn't the account
            # itself -> fail closed, do not guess.
            return None
        return sent.get("message")

    data = envelope.get("dataMessage")
    if data is not None:
        if data.get("groupInfo"):
            return None
        return data.get("message")

    return None


def parse_pasted_text(body: str) -> str | None:
    """Return the article body of a "paste the text" message (#834), or None
    if this message isn't one.

    Format: the first line is the keyword alone (`text`, optionally with a
    trailing colon, or `文本`); everything after it is the article. See
    _PASTE_KEYWORDS for why the keyword must stand alone on that line.
    """
    if not body:
        return None
    first, _, rest = body.partition("\n")
    keyword = first.strip().rstrip(":：").strip().lower()
    if keyword not in _PASTE_KEYWORDS:
        return None
    rest = rest.strip()
    return rest or None


def send_receipt(lines: list) -> bool:
    """Send one Signal "Note to Self" receipt summarizing a
    check_signal_inbox() run's results. No message is sent if `lines` is
    empty — a run that found nothing to do must not show up on Daniel's
    phone at all (module docstring / issue #749: "don't spam him")."""
    if not lines:
        return False
    return podcast.send_signal_text("\n".join(lines), context="signal-inbox-receipt")


def _process_new_episode(episode_id: int, result_lines: list) -> None:
    """Synchronously run transcription+summary for a freshly-ingested
    episode (the equivalent of POST /api/podcast/episodes/{id}/process, but
    called in-process rather than over HTTP since this runs inside a
    one-shot cron script — there is no long-lived server process here to
    hand a background thread off to, it would just get killed when the
    script exits). Appends one short status line to `result_lines`;
    podcast.retry_episode -> podcast._process_episode already sends the
    full summary via podcast.send_signal() on success, so this must NOT
    repeat it (see issue #749's explicit "don't double-send the summary").

    Never raises — podcast.retry_episode's underlying _process_episode is
    documented as never raising (any failure is stored as status='error'),
    but this is wrapped anyway so a genuinely unexpected exception here
    still shows up in the receipt instead of vanishing.
    """
    try:
        outcome = podcast.retry_episode(episode_id)
    except Exception as e:
        logger.error("knowledge.signal_inbox: 处理 episode %s 时异常: %s", episode_id, e)
        result_lines.append(f"⚠️ 处理时出错（episode {episode_id}）— {e}")
        return

    status = outcome.get("status")
    episode = database.get_episode(episode_id) or {}
    title = episode.get("title") or f"episode {episode_id}"
    if status == "summarized":
        result_lines.append(f"🧠 已处理完成：{title}")
    elif status == "no_transcript":
        result_lines.append(f"⚠️ 未能获取内容：{title}")
    else:
        error = outcome.get("error") or "unknown error"
        result_lines.append(f"⚠️ 处理失败：{title} — {error}")


def _ingest_pasted_body(body: str, summary: dict, receipt_lines: list) -> None:
    """Store one "paste the text" message (#834) as an article and kick off
    its summary, appending one receipt line.

    Deliberately NOT retried through the retry queue the URL path uses:
      - that queue lives in app_settings as JSON, sized for URLs; whole
        article bodies do not belong in a settings row
      - the way a pasted body fails is "too short", and re-running it next
        poll produces the identical failure. Retrying a failed download is
        worth it; retrying arithmetic is not.
    Daniel just re-sends the message, and the receipt says why.

    Title/author/source URL are left to the server (#833): whatever the AI
    can read out of the body itself, with the first URL in the message —
    typically the article's own link, pasted along with it — as source_url.

    Never logs or reports the body itself (module docstring, Privacy) —
    only its length and the outcome.
    """
    urls = extract_urls(body)
    try:
        result = knowledge.ingest.ingest_text(
            None, body, source_url=urls[0] if urls else None, platform="signal")
    except Exception as e:
        logger.warning("knowledge.signal_inbox: 粘贴正文入库失败（%d 字）: %s", len(body), e)
        summary["failed"] += 1
        summary["errors"].append(f"pasted text ({len(body)} chars): {e}")
        summary["results"].append({"pasted_chars": len(body), "ok": False, "error": str(e)})
        receipt_lines.append(f"❌ 粘贴的正文入库失败（{len(body)} 字）— {e}")
        return

    summary["ingested"] += 1
    summary["processed"] += 1
    episode_id = result.get("episode_id")
    episode = database.get_episode(episode_id) if episode_id else None
    title = (episode or {}).get("title") or f"episode {episode_id}"
    summary["results"].append({
        "pasted_chars": len(body), "ok": True, "episode_id": episode_id, "title": title,
    })

    if result.get("status") == "already_exists":
        receipt_lines.append(f"↺ 已在库中：{title}")
        return
    receipt_lines.append(f"✅ {title}")
    if episode_id:
        _process_new_episode(episode_id, receipt_lines)


def check_signal_inbox(runner=None) -> dict:
    """Poll signal-cli for new Note-to-Self messages (plus any previously
    failed URLs sitting in the retry queue), ingest every URL found, and
    kick off transcription+summary for newly-created episodes. Sends a
    Signal receipt with the results (unless nothing happened this run).

    `runner` is injectable for tests: a callable `(args: list[str]) -> str`
    returning signal-cli's stdout. Defaults to a real subprocess call.
    Never used for real signal-cli/network I/O in tests — see
    knowledge/mailbox.py's `imap_factory` for the same pattern.
    """
    summary = {
        "checked": 0, "processed": 0, "skipped": 0, "failed": 0,
        "ingested": 0, "errors": [], "results": [],
    }

    account = os.environ.get("SIGNAL_ACCOUNT")
    if not account:
        logger.info("knowledge.signal_inbox: SIGNAL_ACCOUNT 未配置，跳过")
        summary["reason"] = "no_account"
        return summary

    if runner is None:
        runner = _default_runner
    cli_path = os.environ.get("SIGNAL_CLI_PATH", "signal-cli")

    # 先处理上一轮失败留下的重试队列（收到的顺序在前，即"更旧的失败优先"）。
    retry_items = []
    for item in _load_retry_queue():
        url = (item or {}).get("url")
        attempts = (item or {}).get("attempts", 0)
        if url:
            retry_items.append((url, attempts))

    try:
        stdout = runner([cli_path, "-a", account, "-o", "json", "receive",
                         "-t", str(_RECEIVE_IDLE_SECONDS)])
    except Exception as e:
        # 超时不是"坏了"，通常是首轮积压还没消化完（见 _RECEIVE_TIMEOUT
        # 的注释）。日志要说人话，否则看到一行 timeout 只会以为功能是坏的。
        logger.warning(
            "knowledge.signal_inbox: signal-cli receive 失败: %s"
            "（若是超时：可能是首轮积压未消化完，下一轮 cron 会继续）", e)
        summary["reason"] = "receive_failed"
        summary["errors"].append(str(e))
        return summary

    new_urls = []
    pasted_bodies = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            logger.debug("knowledge.signal_inbox: 忽略非 JSON 行: %s", line[:120])
            continue

        # `signal-cli -o json receive` wraps each event as {"envelope": {...},
        # "account": "..."}. Fall back to the parsed object itself so a
        # differently-shaped line (or a test fixture that hands over the
        # envelope directly) still works rather than being silently dropped.
        envelope = parsed.get("envelope") if isinstance(parsed, dict) else None
        if envelope is None:
            envelope = parsed if isinstance(parsed, dict) else {}

        summary["checked"] += 1
        text = _extract_note_to_self_text(envelope, account)
        if text is None:
            # 非本人发给自己的消息 —— 见模块顶部 Security 说明，一律忽略。
            summary["skipped"] += 1
            continue

        # "paste the text" messages (#834) are checked BEFORE the URL scan:
        # the body usually contains the article's own link too, and that link
        # belongs in source_url, not in a separate fetch-this-URL job.
        pasted = parse_pasted_text(text)
        if pasted is not None:
            pasted_bodies.append(pasted)
            continue

        found = extract_urls(text)
        if not found:
            summary["skipped"] += 1
            continue
        new_urls.extend(found)

    # 合并重试队列 + 新链接，同一 URL 只处理一次（本轮内去重；跨轮去重靠
    # ingest_url() 自身的 already_exists 幂等）。
    seen = set()
    work_items = []
    for url, attempts in retry_items:
        if url not in seen:
            seen.add(url)
            work_items.append((url, attempts))
    for url in new_urls:
        if url not in seen:
            seen.add(url)
            work_items.append((url, 0))

    if work_items or pasted_bodies:
        parts = []
        if work_items:
            parts.append(f"{len(work_items)} 个链接")
        if pasted_bodies:
            parts.append(f"{len(pasted_bodies)} 段正文")
        podcast.send_signal_text(
            f"📥 已收到 {' + '.join(parts)}，开始处理…",
            context="signal-inbox-start",
        )

    next_retry_queue = []
    receipt_lines = []

    for url, attempts in work_items:
        try:
            result = knowledge.ingest.ingest_url(url)
        except Exception as e:
            attempts += 1
            logger.warning("knowledge.signal_inbox: 入库失败（第 %d 次）%s: %s", attempts, url, e)
            summary["errors"].append(f"{url}: {e}")
            summary["results"].append({"url": url, "ok": False, "error": str(e)})
            if attempts >= _MAX_ATTEMPTS:
                summary["failed"] += 1
                receipt_lines.append(f"❌ 已放弃：{url} — {e}")
            else:
                next_retry_queue.append({"url": url, "attempts": attempts})
                # Every URL that triggers the "📥 已收到…开始处理" notice above
                # must get SOME follow-up line, or Daniel sees "started" and
                # then silence with no way to tell "still running" from
                # "quietly retrying" from "the whole thing died" (this was a
                # reported bug: attempts 1-2 wrote nothing here at all).
                receipt_lines.append(f"⏳ 稍后重试（第 {attempts}/{_MAX_ATTEMPTS} 次）：{url} — {e}")
            continue

        summary["ingested"] += 1
        summary["processed"] += 1
        episode_id = result.get("episode_id")
        already_exists = result.get("status") == "already_exists"
        episode = database.get_episode(episode_id) if episode_id else None
        title = episode.get("title") if episode else None

        summary["results"].append({
            "url": url, "ok": True, "episode_id": episode_id, "title": title,
        })
        if already_exists:
            receipt_lines.append(f"↺ 已在库中：{title or url}")
        else:
            receipt_lines.append(f"✅ {title or url}")
            if episode_id:
                _process_new_episode(episode_id, receipt_lines)

    for body in pasted_bodies:
        _ingest_pasted_body(body, summary, receipt_lines)

    _save_retry_queue(next_retry_queue)
    send_receipt(receipt_lines)

    return summary

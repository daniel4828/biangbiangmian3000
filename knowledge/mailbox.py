"""Knowledge base mailbox intake (issue #655, extended #668, #925): poll a
dedicated mailbox via IMAP, and for each UNSEEN mail:

  0. the sender is a known newsletter (knowledge.newsletter.source_name(),
     #925 — e.g. F.A.Z. Frühdenker, forwarded here by a Gmail rule) -> route
     to knowledge.newsletter.ingest_newsletter() and process it immediately
     (podcast.retry_episode()), BEFORE the URL branch below even runs. This
     check comes first on purpose: a newsletter body is stuffed with dozens
     of paywalled links back to the source site, and the URL branch would
     otherwise try (and fail) to fetch every one of them instead of using
     the content already sitting right there in the mail body;
  1. it contains a URL (phone "share -> mail" is the easiest way to get a
     link onto the server) -> ingest every URL via
     knowledge.ingest.ingest_url() — the exact same pipeline the
     paste-a-URL box in the UI uses (POST /api/knowledge/add);
  2. no URL, but the body is >= 200 chars -> treat the body itself as a
     pasted article (#668, for paywalled articles Daniel can read in his
     browser but the server can't fetch) via knowledge.ingest.ingest_text(),
     subject as title;
  3. neither -> skip, leave UNSEEN (unchanged from #655).

No second/parallel "URL/text -> episode row" implementation here, see
knowledge/ingest.py's docstring for why that matters in this repo.

Since #960 this module also serves the 📬 Mailbox UI: list_inbox() lists
envelopes (never bodies) and process_uid() ingests one mail on demand.

Security: this is the one intake channel that lets *anyone who knows the
mailbox address* trigger a server-side fetch + paid AI call on Daniel's
account. The gate is auto_process_senders() — the per-sender switches
Daniel sets by hand (#960, replacing KNOWLEDGE_MAIL_ALLOWED_SENDERS). If it
comes back empty the whole poll is skipped (nothing read, nothing marked
seen), never "process everything because the gate came back empty".
Everyone else's mail is only ever ingested when Daniel presses a button.

Only stdlib (imaplib/email/html.parser) is used — no new dependency for
this.
"""
import contextlib
import email
import imaplib
import logging
import os
import re
from email.header import decode_header
from email.utils import parseaddr
from html.parser import HTMLParser

import knowledge.ingest
import knowledge.newsletter

logger = logging.getLogger(__name__)

# Matches http(s) URLs; trailing punctuation commonly glued on by mail
# clients/copy-paste (.,;:!?) and closing brackets/quotes are stripped
# after the match rather than excluded from the character class, so URLs
# that legitimately end mid-path still match in full.
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_TRAILING_PUNCT = '.,;:!?)]}\'"'

# Same "too short to be a real article" threshold ingest_text() enforces —
# checked here too so a mail with only a two-line body doesn't even get to
# the ingest call (and doesn't get logged as a "failed" retry candidate for
# something that will never succeed).
#
# Derived, never re-typed: if this were a literal 200 and the shared
# threshold later moved up, this gate would wave a mail through that
# ingest_text() then rejects — and a rejected mail is deliberately NOT
# marked read, so it would be retried forever, every single poll.
_MIN_BODY_CHARS = knowledge.ingest._MIN_TEXT_CHARS


def auto_process_senders() -> set:
    """Addresses the cron may process unattended — Daniel's per-sender
    switches in the 📬 Mailbox UI (mail_senders.auto_process, #960).

    This replaces KNOWLEDGE_MAIL_ALLOWED_SENDERS (#655). That variable
    existed to stop anyone who knows the address from triggering a paid AI
    call; now that every other sender is processed only when Daniel presses
    a button, the switch table IS that gate. Its most important property is
    inherited unchanged: an empty result means process NOBODY, never
    "process everybody because the gate came back empty".

    Falls back to an empty set if the database is unreachable — the failure
    direction has to stay "do nothing", not "do everything".
    """
    try:
        import database
        return {addr.strip().lower() for addr in database.auto_mail_senders() if addr.strip()}
    except Exception as e:
        logger.error(
            "knowledge.mailbox: 读取自动处理发件人失败，本轮不处理任何邮件: %s", e)
        return set()


def _decode_header_value(value) -> str:
    """RFC 2047 header decoding ('=?UTF-8?B?...?=' etc.) — Subject lines
    from phone mail clients are frequently encoded this way."""
    if not value:
        return ""
    chunks = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                chunks.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                chunks.append(text.decode("utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks)


def _decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _body_text(msg: email.message.Message) -> str:
    """Concatenate every text/plain and text/html part. Share-to-mail apps
    are inconsistent about which MIME type they use, so both are scanned
    rather than picking one."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            if part.get_content_type() in ("text/plain", "text/html"):
                parts.append(_decode_payload(part))
    elif msg.get_content_type() in ("text/plain", "text/html"):
        parts.append(_decode_payload(msg))
    return "\n".join(parts)


class _HTMLTextExtractor(HTMLParser):
    """Bare-bones tag stripper for turning an HTML mail body into plain
    text before it's used as a pasted article body (#668). `_body_text()`
    above leaves tags in on purpose — it only feeds the URL regex, where
    stray markup is harmless — but text handed to the AI summarizer must
    not contain `<div>`/`<a>` soup, so this path strips it. <script>/<style>
    contents are dropped entirely rather than emitted as text.

    Block-level tag boundaries MUST become newlines (#925 review fix):
    marketing/newsletter HTML is frequently minified onto a single physical
    line, and downstream processing is line-based — knowledge.newsletter's
    clean_body() decides what to keep or drop line by line. Without an
    inserted newline at every block boundary, a minified mail collapses to
    one giant line: if that line happens to contain a boilerplate phrase
    like "Abbestellen", clean_body() drops the ENTIRE body (real content and
    all), which then makes ingest_text() raise on "too short" for a mail
    that in fact had plenty of real content — every 5 minutes, forever,
    since a raised IngestError normally leaves the mail unread for retry.
    Inserting a newline back at every block tag is what makes a line-based
    filter actually operate per-paragraph instead of per-mail."""

    _SKIP_TAGS = ("script", "style")
    # Elements whose boundary is a paragraph/line break in rendered
    # markup — approximately CSS's default "display: block" set, the tags
    # this newsletter (and virtually any HTML mail) is built from.
    _BLOCK_TAGS = (
        "p", "div", "br", "tr", "td", "th", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "blockquote", "section", "article",
    )

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _strip_html(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.warning("knowledge.mailbox: HTML 解析失败，退回原始文本（可能残留标签）")
        return html
    return parser.text()


def plain_text_body(msg: email.message.Message) -> str:
    """Body text suitable for use as a pasted article (#668, no-URL
    fallback): text/plain parts used as-is, text/html parts have tags
    stripped via `_strip_html()`. Unlike `_body_text()` (URL scanning
    only), this is what gets handed to `knowledge.ingest.ingest_text()`,
    so markup must actually be gone, not just tolerated."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                parts.append(_decode_payload(part))
            elif ctype == "text/html":
                parts.append(_strip_html(_decode_payload(part)))
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            parts.append(_decode_payload(msg))
        elif ctype == "text/html":
            parts.append(_strip_html(_decode_payload(msg)))
    return "\n".join(p.strip() for p in parts if p.strip()).strip()


def extract_urls(text: str) -> list:
    """Pull URLs out of one string, de-duplicated, order preserved."""
    if not text:
        return []
    seen = set()
    urls = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(_TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_urls_from_message(msg: email.message.Message) -> list:
    """Subject AND body both get scanned (#655): phone share sheets put
    the link in one or the other depending on app/OS, and HTML mail wraps
    it in an <a href> that text/plain extraction would miss."""
    subject = _decode_header_value(msg.get("Subject"))
    body = _body_text(msg)
    seen = set()
    urls = []
    for url in extract_urls(subject) + extract_urls(body):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _sender_address(msg: email.message.Message) -> str:
    """Handles both 'addr@x.de' and 'Name <addr@x.de>' From headers,
    case-insensitive, comparing only the address part."""
    _, addr = parseaddr(msg.get("From") or "")
    return addr.strip().lower()


def _candidate_newsletter_addresses(msg: email.message.Message) -> list:
    """Every header that might carry a forwarded newsletter's original
    sender address (#925). Gmail's auto-forward keeps `From` pointing at the
    original sender, so `_sender_address()` alone is normally enough — but
    `Sender`/`Return-Path` are checked too for forwarding setups that
    rewrite `From` (e.g. some mailing-list or "forward as attachment"
    configurations), so a newsletter still gets recognized rather than
    silently falling through to the URL branch and failing on every
    paywalled link in its body."""
    addrs = []
    for header in ("From", "Sender", "Return-Path"):
        _, addr = parseaddr(msg.get(header) or "")
        addr = addr.strip().lower()
        if addr:
            addrs.append(addr)
    return addrs


def _newsletter_source_for(msg: email.message.Message) -> str | None:
    """First known-newsletter match among the candidate sender headers, or
    None. Returns the sender address that matched (not the display name),
    for use as `sender` in newsletter.ingest_newsletter()."""
    for addr in _candidate_newsletter_addresses(msg):
        if knowledge.newsletter.source_name(addr):
            return addr
    return None


def _search_allowed(conn, allowed, summary):
    """UNSEEN mail from whitelisted senders only — the union of one
    `UNSEEN FROM <addr>` search per address, never a bare `UNSEEN`.

    The mailbox being polled is Daniel's personal Gmail inbox, not the
    dedicated empty mailbox #655 assumed. A bare UNSEEN search would pull
    every unread private mail's full body through this process; the
    whitelist check downstream keeps them out of the database, but by then
    they have already passed through here (the #755 lesson: blocking
    ingestion is not the same as never reading it). Asking the IMAP server
    to filter means they are never fetched at all.

    IMAP FROM is a substring match, so this is a narrowing filter, not a
    guarantee — `sender not in allowed` downstream stays as the exact check.

    Returns the message ids, or None if a search failed (caller returns the
    summary as-is: acting on a partial id list would mark a subset processed
    and leave the rest silently unexamined).
    """
    msg_ids = []
    for addr in sorted(allowed):
        status, data = conn.search(None, "UNSEEN", "FROM", addr)
        if status != "OK":
            logger.warning(
                "knowledge.mailbox: IMAP SEARCH 失败（发件人 %s）: %s", addr, status
            )
            summary["reason"] = "search_failed"
            return None
        for msg_id in (data[0].split() if data and data[0] else []):
            if msg_id not in msg_ids:
                msg_ids.append(msg_id)
    return msg_ids


class MailboxError(Exception):
    """IMAP-side failure the UI should show verbatim (no credentials, login
    refused, the UID vanished). Kept distinct from IngestError so the route
    layer can tell "your mailbox is unreachable" from "this mail can't be
    turned into an article"."""


def _imap_config():
    host = os.environ.get("KNOWLEDGE_IMAP_HOST")
    user = os.environ.get("KNOWLEDGE_IMAP_USER")
    password = os.environ.get("KNOWLEDGE_IMAP_PASSWORD")
    try:
        port = int(os.environ.get("KNOWLEDGE_IMAP_PORT", "993"))
    except ValueError:
        port = 993
    return host, port, user, password


@contextlib.contextmanager
def _connection(imap_factory=None, readonly=True):
    """Log in, SELECT INBOX, and always log out again.

    readonly=True issues SELECT in IMAP's read-only mode, which makes it
    impossible for this connection to change a flag even by accident — the
    mailbox view's central promise is that browsing Gmail from the app
    changes nothing in Gmail.
    """
    host, port, user, password = _imap_config()
    if not host or not user or not password:
        raise MailboxError(
            "IMAP 凭据未配置（KNOWLEDGE_IMAP_HOST/KNOWLEDGE_IMAP_USER/KNOWLEDGE_IMAP_PASSWORD）")
    if imap_factory is None:
        def imap_factory():
            return imaplib.IMAP4_SSL(host, port)
    conn = imap_factory()
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=readonly)
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


def _uidvalidity(conn) -> str:
    """UIDVALIDITY of the selected mailbox. A change means the server
    renumbered everything and every UID the browser is holding now points
    somewhere else — the list must be refetched rather than acted upon."""
    try:
        status, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    except Exception:
        pass
    return ""


# IMAP header set for the list. Deliberately NOT the body: the inbox this
# points at is Daniel's personal Gmail (1600 mails and counting), and the
# list is re-fetched on every page view. Envelope-sized fetches keep that
# cheap, and — the actual point — mean his private mail is never downloaded
# to the server at all unless he presses "process" on it.
_ENVELOPE_PARTS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"


def list_inbox(offset: int = 0, limit: int = 50, query: str = "",
               imap_factory=None) -> dict:
    """One page of the inbox, newest first, envelopes only (#960).

    `query` is matched by the IMAP server against From and Subject (OR'd),
    not by us — filtering client-side would mean downloading every header
    in a 1600-mail mailbox to throw almost all of them away.
    """
    with _connection(imap_factory=imap_factory, readonly=True) as conn:
        if query:
            criteria = ("OR", "FROM", query, "SUBJECT", query)
        else:
            criteria = ("ALL",)
        status, data = conn.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise MailboxError(f"IMAP SEARCH 失败: {status}")
        uidvalidity = _uidvalidity(conn)

        # SEARCH returns ascending UIDs; the newest mail is what Daniel
        # wants to see first, so the page is cut from the reversed list.
        uids = list(reversed(data[0].split() if data and data[0] else []))
        total = len(uids)
        page = uids[offset:offset + limit]

        messages = []
        for uid in page:
            status, msg_data = conn.uid("FETCH", uid, _ENVELOPE_PARTS)
            if status != "OK" or not msg_data or not msg_data[0]:
                # One unreadable mail must not blank the whole page — the
                # other 49 still tell Daniel what arrived.
                logger.warning("knowledge.mailbox: 无法读取邮件信封 uid=%s", uid)
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            name, addr = parseaddr(msg.get("From") or "")
            messages.append({
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                "from": (addr or "").lower(),
                "from_name": _decode_header_value(name) or (addr or ""),
                "subject": _decode_header_value(msg.get("Subject")) or "(无主题)",
                "date": _decode_header_value(msg.get("Date")),
                "message_id": (msg.get("Message-ID") or "").strip(),
            })

    import database
    processed = database.processed_mail_message_ids([m["message_id"] for m in messages])
    auto = database.auto_mail_senders()
    for m in messages:
        episode_id = processed.get(m["message_id"])
        m["processed"] = episode_id is not None
        m["episode_id"] = episode_id
        m["auto_process"] = m["from"] in auto

    return {
        "messages": messages,
        "total": total,
        "offset": offset,
        "limit": limit,
        "uidvalidity": uidvalidity,
    }


def fetch_message(uid: str, uidvalidity: str = "", imap_factory=None):
    """Fetch one full message by UID, without touching its \\Seen flag.

    `uidvalidity`, when the caller has one, is verified first: UIDs are only
    meaningful within one UIDVALIDITY generation, so acting on a stale UID
    after the server renumbered would process a different mail than the one
    Daniel clicked. Mismatch is an error, never a best guess.
    """
    with _connection(imap_factory=imap_factory, readonly=True) as conn:
        current = _uidvalidity(conn)
        if uidvalidity and current and uidvalidity != current:
            raise MailboxError(
                "邮箱已重新编号（UIDVALIDITY 变化），列表已过期，请刷新后重试")
        status, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise MailboxError(f"读取邮件失败（uid={uid}），它可能已被删除或移动")
        return email.message_from_bytes(msg_data[0][1])


def ingest_message(msg) -> dict:
    """Ingest one already-fetched mail through whichever pipeline
    route_message() picked, and remember which mail it came from.

    Returns {"route", "episode_id", "episode_ids", "status"}. Raises
    knowledge.ingest.IngestError for "this mail can't become an article"
    (too short, extraction failed) — the route layer turns that into a 400
    with the reason shown to Daniel, rather than an empty knowledge entry.
    """
    import database

    route, payload = route_message(msg)
    message_id = (msg.get("Message-ID") or "").strip()
    subject = _decode_header_value(msg.get("Subject")) or "(无主题)"

    if route == "skip":
        raise knowledge.ingest.IngestError(f"这封邮件无法处理：{payload}")

    if route == "newsletter":
        result = knowledge.newsletter.ingest_newsletter(
            payload, subject, plain_text_body(msg))
        episode_ids = [result.get("episode_id")]
    elif route == "urls":
        episode_ids = []
        for url in payload:
            result = knowledge.ingest.ingest_url(url)
            episode_ids.append(result.get("episode_id"))
    else:
        result = knowledge.ingest.ingest_text(subject, payload, platform="email")
        episode_ids = [result.get("episode_id")]

    episode_ids = [e for e in episode_ids if e]
    for episode_id in episode_ids:
        database.set_mail_message_id(episode_id, message_id)

    return {
        "route": route,
        "episode_id": episode_ids[0] if episode_ids else None,
        "episode_ids": episode_ids,
        "status": result.get("status") if route != "urls" else None,
    }


def route_message(msg):
    """Decide what a mail *is*, once, for both callers (#960).

    Returns (route, payload):
      ("newsletter", sender_address) — a registered newsletter (#925). Must
          be tested first: a newsletter body carries dozens of paywalled
          links back to the source site, so the URL route below would fire
          off a guaranteed-failing fetch for every one of them instead of
          using the content already sitting in the body.
      ("urls", [url, ...])           — shared-from-phone link mail (#655).
      ("text", body_text)            — no URL but a long enough body (#668).
      ("skip", reason)               — nothing usable in it.

    The cron (check_mailbox) and the mailbox UI's per-mail "process" button
    both call this. The ingest functions were already shared; the *order*
    of the branches was not, and a second copy of it would quietly drift —
    the same reason this repo keeps one add-word pipeline (#643) and one
    ingest pipeline (knowledge/ingest.py).

    Flag handling is deliberately NOT part of this: marking mail \\Seen is
    the cron's business (that is how it remembers what it did). The manual
    path never touches a flag — the mailbox view is read-only against
    Gmail, and "already processed" is answered from mail_message_id.
    """
    newsletter_addr = _newsletter_source_for(msg)
    if newsletter_addr:
        return "newsletter", newsletter_addr

    urls = extract_urls_from_message(msg)
    if urls:
        return "urls", urls

    body_text = plain_text_body(msg)
    if len(body_text) >= _MIN_BODY_CHARS:
        return "text", body_text

    return "skip", f"正文过短（{len(body_text)} 字）且未提取到 URL"


def check_mailbox(imap_factory=None) -> dict:
    """Poll KNOWLEDGE_IMAP_HOST's INBOX for UNSEEN mail from whitelisted
    senders, ingest every URL found in each one, and mark the message
    \\Seen only if every URL in it ingested without error (failures are
    left UNSEEN so the next run retries them; ingest_url() is idempotent
    for already-ingested URLs via its existing_exists dedup, so retrying a
    partially-succeeded message is safe).

    `imap_factory` is injectable for tests: a zero-arg callable returning
    an object implementing the imaplib.IMAP4_SSL interface (login/select/
    search/fetch/store/close/logout). Never used for real network I/O in
    tests.
    """
    summary = {
        "checked": 0, "processed": 0, "skipped": 0, "failed": 0,
        "ingested": 0, "errors": [],
    }

    allowed = auto_process_senders()
    if not allowed:
        logger.info(
            "knowledge.mailbox: 没有发件人开着自动处理开关，本轮不处理任何邮件"
            "（不读取、不标已读）"
        )
        summary["reason"] = "no_auto_senders"
        return summary

    host = os.environ.get("KNOWLEDGE_IMAP_HOST")
    user = os.environ.get("KNOWLEDGE_IMAP_USER")
    password = os.environ.get("KNOWLEDGE_IMAP_PASSWORD")
    port_raw = os.environ.get("KNOWLEDGE_IMAP_PORT", "993")
    try:
        port = int(port_raw)
    except ValueError:
        port = 993

    if not host or not user or not password:
        logger.warning(
            "knowledge.mailbox: IMAP 凭据未完整配置"
            "（KNOWLEDGE_IMAP_HOST/KNOWLEDGE_IMAP_USER/KNOWLEDGE_IMAP_PASSWORD），跳过"
        )
        summary["reason"] = "no_credentials"
        return summary

    if imap_factory is None:
        def imap_factory():
            return imaplib.IMAP4_SSL(host, port)

    conn = imap_factory()
    try:
        conn.login(user, password)
        conn.select("INBOX")

        msg_ids = _search_allowed(conn, allowed, summary)
        if msg_ids is None:
            return summary
        summary["checked"] = len(msg_ids)

        for msg_id in msg_ids:
            # BODY.PEEK[] instead of RFC822: fetching RFC822 implicitly sets
            # \Seen, which would mark a mail read before the whitelist check
            # below ever runs. PEEK leaves the flag alone, so the only thing
            # that ever marks a mail read is the explicit store(+FLAGS) after
            # a successful ingest.
            status, msg_data = conn.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                logger.warning("knowledge.mailbox: 无法读取邮件 %s，本轮跳过（不标已读）", msg_id)
                summary["failed"] += 1
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            sender = _sender_address(msg)
            if sender not in allowed:
                logger.info("knowledge.mailbox: 发件人 %s 不在白名单，跳过（不标已读）", sender)
                summary["skipped"] += 1
                continue

            # 通讯分支（#925），必须排在 URL 分支之前：F.A.Z. Frühdenker 这类
            # 通讯正文里有几十个付费墙链接，走 URL 分支会逐个抓取、全部失败
            # 还浪费时间。转发邮件的 From 通常仍是原发件人（上面的白名单检
            # 查已经用它判过了），这里额外查 Sender/Return-Path 只是为了兜
            # 底改写 From 的转发配置。
            # 分支顺序统一由 route_message() 决定（#960）——手动「处理」走
            # 的是同一个函数。这里各分支只保留自己的 \Seen 处理：那是 cron
            # 特有的记账方式，手动路径根本不碰标志位。
            route, payload = route_message(msg)

            if route == "newsletter":
                newsletter_addr = payload
                subject = _decode_header_value(msg.get("Subject")) or "(无主题)"
                body = plain_text_body(msg)
                try:
                    result = knowledge.newsletter.ingest_newsletter(newsletter_addr, subject, body)
                    summary["ingested"] += 1
                    logger.info("knowledge.mailbox: 通讯已入库 %s -> %s", subject, result)
                except knowledge.ingest.IngestError as e:
                    # 永久失败（同 knowledge/signal_inbox.py 对粘贴正文失败
                    # 的处理）：正文太短这种失败，重试一百次结果完全一样，
                    # cron 每 5 分钟跑一次——留着不读只会让每一轮都白跑一次
                    # 注定失败的活儿。标已读，放弃这封。
                    logger.error(
                        "knowledge.mailbox: 通讯 %s 永久失败（放弃，标已读）: %s",
                        subject, e,
                    )
                    summary["errors"].append(f"(newsletter {msg_id}, abandoned): {e}")
                    summary["failed"] += 1
                    conn.store(msg_id, "+FLAGS", "\\Seen")
                    continue
                except Exception as e:
                    # 网络/数据库/AI 抖动等暂时性故障——不标已读，下轮重试。
                    logger.warning("knowledge.mailbox: 通讯入库失败 %s: %s", subject, e)
                    summary["errors"].append(f"(newsletter {msg_id}): {e}")
                    summary["failed"] += 1
                    continue

                episode_id = result.get("episode_id")
                already = result.get("status") == "already_exists"
                if not already and episode_id:
                    # "早上就要读"（同 knowledge/signal_inbox.py 的道理）：
                    # 入库后立即同步转录+摘要+通知，不等前端另外调 .../process。
                    # 失败仅记日志，不标已读——ingest_text 按正文哈希去重，
                    # 下一轮重试是安全的（不会造第二行，只会重新处理）。
                    try:
                        import podcast
                        podcast.retry_episode(episode_id)
                    except Exception as e:
                        logger.warning(
                            "knowledge.mailbox: 通讯 episode %s 处理失败: %s", episode_id, e)
                        summary["errors"].append(f"(newsletter process {episode_id}): {e}")
                        summary["failed"] += 1
                        continue

                conn.store(msg_id, "+FLAGS", "\\Seen")
                summary["processed"] += 1
                continue

            # 主路径不变（#655）：有 URL 就走 ingest_url()，一个字节都不能变。
            if route == "urls":
                urls = payload
                all_ok = True
                for url in urls:
                    try:
                        result = knowledge.ingest.ingest_url(url)
                        summary["ingested"] += 1
                        logger.info("knowledge.mailbox: 已处理 %s -> %s", url, result)
                    except Exception as e:
                        all_ok = False
                        logger.warning("knowledge.mailbox: 处理 URL 失败 %s: %s", url, e)
                        summary["errors"].append(f"{url}: {e}")

                if all_ok:
                    conn.store(msg_id, "+FLAGS", "\\Seen")
                    summary["processed"] += 1
                else:
                    # 邮件里至少一个 URL 处理失败——整封不标已读，下轮重试。
                    # ingest_url() 对已入库的 URL 返回 already_exists，重试
                    # 部分成功的邮件是安全的，不会重复造行。
                    summary["failed"] += 1
                continue

            # 无 URL 时的正文投递路径（#668）：正文（HTML 已去标签）够长就
            # 当作粘贴文章处理，标题取邮件主题。
            if route != "text":
                logger.info(
                    "knowledge.mailbox: 邮件 %s（来自 %s）无法处理，跳过（不标已读）：%s",
                    msg_id, sender, payload,
                )
                summary["skipped"] += 1
                continue
            body_text = payload

            subject = _decode_header_value(msg.get("Subject")) or "(无主题)"
            try:
                result = knowledge.ingest.ingest_text(subject, body_text, platform="email")
                summary["ingested"] += 1
                summary["processed"] += 1
                conn.store(msg_id, "+FLAGS", "\\Seen")
                logger.info("knowledge.mailbox: 已按正文投递处理邮件 %s -> %s", msg_id, result)
            except Exception as e:
                logger.warning("knowledge.mailbox: 正文投递处理失败 %s: %s", msg_id, e)
                summary["errors"].append(f"(mail {msg_id}): {e}")
                summary["failed"] += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass

    return summary

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

Security: this is the one intake channel that lets *anyone who knows the
mailbox address* trigger a server-side fetch + paid AI call on Daniel's
account. KNOWLEDGE_MAIL_ALLOWED_SENDERS is mandatory — if it's unset/empty
the whole mailbox is skipped (nothing is read, nothing is marked seen),
never "process everything because the allowlist check couldn't run".
Non-whitelisted senders are skipped individually (their mail is left
UNSEEN, harmless, and simply ignored every run).

Only stdlib (imaplib/email/html.parser) is used — no new dependency for
this.
"""
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


def _env_allowed_senders() -> set:
    raw = os.environ.get("KNOWLEDGE_MAIL_ALLOWED_SENDERS", "")
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


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

    allowed = _env_allowed_senders()
    if not allowed:
        logger.warning(
            "knowledge.mailbox: KNOWLEDGE_MAIL_ALLOWED_SENDERS 未配置，"
            "拒绝处理任何邮件（不读取、不标已读）"
        )
        summary["reason"] = "no_allowed_senders"
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

        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("knowledge.mailbox: IMAP SEARCH 失败: %s", status)
            summary["reason"] = "search_failed"
            return summary

        msg_ids = data[0].split() if data and data[0] else []
        summary["checked"] = len(msg_ids)

        for msg_id in msg_ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
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
            newsletter_addr = _newsletter_source_for(msg)
            if newsletter_addr:
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
            urls = extract_urls_from_message(msg)
            if urls:
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
            body_text = plain_text_body(msg)
            if len(body_text) < _MIN_BODY_CHARS:
                logger.info(
                    "knowledge.mailbox: 邮件 %s（来自 %s）未提取到 URL 且正文过短（%d 字），跳过（不标已读）",
                    msg_id, sender, len(body_text),
                )
                summary["skipped"] += 1
                continue

            subject = _decode_header_value(msg.get("Subject")) or "(无主题)"
            try:
                result = knowledge.ingest.ingest_text(subject, body_text)
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

#!/usr/bin/env python3
"""知识库邮件收件定时脚本（issue #655）。

轮询一个专用邮箱（`imaplib`），把 UNSEEN 邮件主题/正文里的 URL 逐个送进
`knowledge.ingest.ingest_url()`——和界面「粘贴 URL」用的是同一条管线
（见 `knowledge/ingest.py`、`knowledge/mailbox.py` 的模块说明）。

和 `scripts/podcast_check.py` 不同：podcast 检查是对**运行中的服务器**
发一次 HTTP 请求，由服务器进程做实际工作；这个脚本没有对应的 HTTP 接口
（IMAP 轮询+入库不需要经过网络往返），而是直接 `import database` +
`database.init_db()` 后在本进程里完成，风格照抄 `scripts/fsrs_optimize.py`
这类直接触库的脚本。SQLite 允许多进程并发读写（WAL），和同时运行的服务器
进程不冲突。

并发锁：用一个简单的 PID 锁文件（`data/.knowledge_mail_check.lock`）避免
cron 每分钟触发时上一轮还没跑完就叠跑——处理邮件里的 URL 可能触发文章抓取
/YouTube 字幕下载/AI 翻译标题，慢的话能到几十秒。

用法：
    python scripts/knowledge_mail_check.py

环境变量（见 CLAUDE.md 环境变量表）：
    KNOWLEDGE_IMAP_HOST              IMAP 服务器
    KNOWLEDGE_IMAP_PORT              默认 993（SSL）
    KNOWLEDGE_IMAP_USER / _PASSWORD  凭据
    （#960 起自动处理哪些发件人由库里的 mail_senders 开关决定，
     不再是 KNOWLEDGE_MAIL_ALLOWED_SENDERS；一个都没开就整轮跳过）
    DB_PATH                          数据库路径，默认 data/srs.db
"""
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".knowledge_mail_check.lock")


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main() -> int:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("上一轮知识库邮件检查仍在进行，本轮跳过。")
        return 0

    try:
        lock_file.write(str(os.getpid()))
        lock_file.flush()

        import database
        database.init_db()
        import knowledge.mailbox

        _log("知识库邮件检查开始")
        summary = knowledge.mailbox.check_mailbox()

        reason = summary.get("reason")
        if reason == "no_auto_senders":
            _log("没有发件人开着自动处理开关，跳过（未处理任何邮件）。")
            return 0
        if reason == "no_credentials":
            _log("IMAP 凭据未完整配置，跳过。")
            return 0
        if reason == "search_failed":
            _log("IMAP 搜索失败。")
            return 1

        _log(
            f"未读邮件: {summary['checked']}  已处理: {summary['processed']}  "
            f"跳过: {summary['skipped']}  失败: {summary['failed']}  "
            f"入库 URL 数: {summary['ingested']}"
        )
        for err in summary.get("errors", []):
            _log(f"错误: {err}")

        return 0 if not summary["failed"] else 1
    except Exception as e:
        _log(f"知识库邮件检查异常: {e}")
        return 1
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())

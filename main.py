import argparse
import logging
import os
import pathlib
import sys


def _load_env(path: str = ".env") -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, val = line.partition("=")
                val = val.strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)
    except FileNotFoundError:
        pass

_load_env()

import database
import importer

# ---------------------------------------------------------------------------
# Logging — set LOG_LEVEL=DEBUG in .env for verbose output
# ---------------------------------------------------------------------------

def _make_formatter() -> logging.Formatter:
    if not sys.stderr.isatty():
        return logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    R = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"

    LEVEL_COLOR = {
        "DEBUG":    "\033[96m",    # bright cyan
        "INFO":     "\033[92m",    # bright green
        "WARNING":  "\033[93m",    # yellow
        "ERROR":    "\033[91m",    # bright red
        "CRITICAL": "\033[1;91m",  # bold red
    }
    LOGGER_COLOR = {
        "main":           "\033[34m",   # blue
        "routes.review":  "\033[1;96m", # bold cyan
        "routes.story":   "\033[35m",   # magenta
        "ai":             "\033[33m",   # orange/yellow
        "tts":            "\033[36m",   # cyan
        "importer":       "\033[37m",   # white
        "ui":             "\033[95m",   # bright magenta
    }
    METHOD_COLOR = {
        "POST":   "\033[34m",    # blue
        "GET":    "\033[32m",    # green
        "PUT":    "\033[33m",    # yellow
        "DELETE": "\033[31m",    # red
        "PATCH":  "\033[35m",    # magenta
    }

    class _ColorFmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            ts = f"{DIM}{self.formatTime(record, '%H:%M:%S')}{R}"
            msg = record.getMessage()
            parts = msg.split()

            # HTTP request lines — compact and dim
            if record.name == "main" and parts and parts[0] in METHOD_COLOR:
                mc = METHOD_COLOR[parts[0]]
                method = f"{mc}{BOLD}{parts[0]}{R}"
                rest = f"{DIM}{' '.join(parts[1:])}{R}"
                return f"{ts}  {method} {rest}"

            lc = LEVEL_COLOR.get(record.levelname, "")
            level = f"{lc}[{record.levelname:<5}]{R}"
            nc = LOGGER_COLOR.get(record.name, DIM)
            short = record.name.split(".")[-1]
            name = f"{nc}{short}{R}"
            return f"{ts} {level} {name}: {msg}"

    return _ColorFmt(datefmt="%H:%M:%S")


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_make_formatter())
logging.root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logging.root.addHandler(_handler)
logger = logging.getLogger("main")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# In-memory ring buffer of recent log lines, exposed via GET /api/logs
# (issue #454) — lets Daniel check server logs from the settings page
# without SSH access.
# ---------------------------------------------------------------------------

import collections


class _RingBufferHandler(logging.Handler):
    def __init__(self, maxlen: int = 4000):
        super().__init__()
        self.buffer: "collections.deque[str]" = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass  # never let logging itself raise


_log_buffer_handler = _RingBufferHandler(maxlen=4000)
# Always use the plain (non-colored) formatter, regardless of whether stderr
# is a tty, so the buffer never contains ANSI escape codes.
_log_buffer_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
logging.root.addHandler(_log_buffer_handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_import(args):
    print("Importing from imports/...")
    result = importer.import_all("imports")
    invalid = result.get("skipped_invalid", 0)
    invalid_str = f", {invalid} skipped as invalid" if invalid else ""
    print(f"Done — imported {result['imported']} words "
          f"({result['skipped_duplicate']} skipped as duplicates{invalid_str})")


def cmd_status(args):
    decks = database.get_all_decks()
    if args.deck:
        decks = [d for d in decks if d["name"].lower() == args.deck.lower()]
        if not decks:
            print(f"No deck named '{args.deck}'")
            return

    categories = ["reading", "listening", "creating"]
    header = f"{'Deck':<20} {'Category':<12} {'New':>5} {'Learning':>9} {'Review':>7}"
    print(header)
    print("-" * len(header))

    for deck in decks:
        for cat in categories:
            counts = database.count_due(deck["id"], cat)
            print(f"{deck['name']:<20} {cat:<12} "
                  f"{counts['new']:>5} {counts['learning']:>9} {counts['review']:>7}")


def main():
    database.init_db()

    parser = argparse.ArgumentParser(description="biangbiangmian3000")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("import", help="Import vocabulary from imports/")
    status_p = sub.add_parser("status", help="Show due counts per deck/category")
    status_p.add_argument("--deck", help="Filter to a specific deck name")

    args = parser.parse_args()
    if args.command == "import":
        cmd_import(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

try:
    import base64
    import binascii
    import hashlib
    import hmac
    import secrets
    import time
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Form, Request
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import (
        FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
    )
    from starlette.middleware.gzip import GZipMiddleware
    import uvicorn

    from offline import LOCAL_MODE, OFFLINE_MODE
    from routes import decks, review, story, browse, imports, podcast as podcast_routes, knowledge as knowledge_routes, dictionary as dictionary_routes, tasks as tasks_routes, books as books_routes

    @asynccontextmanager
    async def lifespan(app):
        # Recover podcast episodes orphaned by a mid-transcription restart/crash
        # (#598): _process_episode marks an episode 'processing' while it works,
        # so any 'processing' row at startup is a leftover (nothing runs yet) —
        # flip it to 'error' so run_check's auto-retry reprocesses it, reusing
        # any stored transcript. Best-effort: never block or crash startup.
        try:
            recovered = database.recover_orphaned_podcast_episodes()
            if recovered:
                logger.info("podcast: recovered %d episode(s) orphaned by a restart", recovered)
        except Exception as e:
            logger.warning("podcast: orphan recovery skipped at startup: %s", e)
        yield  # startup done; below runs at shutdown
        if os.environ.get("DEV_CLEAR_DB"):
            import shutil
            import tts as _tts
            try:
                os.unlink(database.DB_PATH)
            except FileNotFoundError:
                pass
            try:
                shutil.rmtree(_tts.TTS_CACHE_DIR)
            except FileNotFoundError:
                pass
            logger.info("[dev] DB and TTS cache cleared on exit.")

    app = FastAPI(title="biangbiangmian3000", lifespan=lifespan)

    # gzip JSON/JS/CSS responses over ~500 bytes (issue #513: app.js is ~9000
    # lines uncompressed, and /api/decks etc. can also be sizeable JSON).
    app.add_middleware(GZipMiddleware, minimum_size=500)

    ui_logger = logging.getLogger("ui")

    # Optional single-user auth — enabled only when both AUTH_USERNAME and
    # AUTH_PASSWORD are set (issue #419). If either is missing, this middleware
    # is a no-op — local dev behavior unchanged.
    #
    # Primary flow is a plain HTML login form + a long-lived signed cookie
    # (#666): iOS Keychain only ever saves *form* logins, so the original
    # HTTP-Basic-only version meant retyping the password on every visit from
    # Daniel's iPhone. Basic Auth stays supported as a fallback for curl and
    # scripts.
    _AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
    _AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
    _AUTH_ENABLED = bool(_AUTH_USERNAME and _AUTH_PASSWORD)
    _SESSION_COOKIE = "anki_session"
    _SESSION_MAX_AGE = 365 * 24 * 3600  # a year — this is a single-user app
    # Signing key derived from the credentials, so changing the password
    # invalidates every outstanding session for free (no key to store).
    _SESSION_KEY = hashlib.sha256(
        f"{_AUTH_USERNAME}:{_AUTH_PASSWORD}".encode("utf-8")
    ).digest()

    def _make_session_cookie() -> str:
        expires = str(int(time.time()) + _SESSION_MAX_AGE)
        sig = hmac.new(_SESSION_KEY, expires.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{expires}.{sig}"

    def _session_cookie_valid(value: str) -> bool:
        expires, _, sig = value.partition(".")
        if not sig:
            return False
        expected = hmac.new(_SESSION_KEY, expires.encode("utf-8"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return False
        try:
            return int(expires) > time.time()
        except ValueError:
            return False

    def _credentials_ok(username: str, password: str) -> bool:
        # Both comparisons always run — no short-circuit that would leak which
        # half was wrong through timing.
        user_ok = secrets.compare_digest(username.encode("utf-8"), _AUTH_USERNAME.encode("utf-8"))
        pass_ok = secrets.compare_digest(password.encode("utf-8"), _AUTH_PASSWORD.encode("utf-8"))
        return user_ok and pass_ok

    def _basic_auth_ok(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        username, _, password = decoded.partition(":")
        return _credentials_ok(username, password)

    def _unauthorized(request: Request):
        # API callers must get a real 401 — handing fetch() an HTML login page
        # with status 200 would make every frontend request "succeed" with
        # garbage. Browsers navigating to a page get sent to the form instead.
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": 'Basic realm="biangbiangmian3000"'},
            )
        return RedirectResponse("/login", status_code=303)

    def _login_page(error: bool) -> HTMLResponse:
        html = pathlib.Path("static/login.html").read_text(encoding="utf-8")
        banner = '<p class="error">Wrong username or password.</p>' if error else ""
        return HTMLResponse(
            html.replace("<!--ERROR-->", banner),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/login")
    def login_form(request: Request, error: int = 0):
        if not _AUTH_ENABLED:
            return RedirectResponse("/", status_code=303)
        return _login_page(bool(error))

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
        if not _AUTH_ENABLED:
            return RedirectResponse("/", status_code=303)
        if not _credentials_ok(username, password):
            logger.warning("login failed for user %r", username[:40])
            return RedirectResponse("/login?error=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            _make_session_cookie(),
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            # Behind Caddy the app itself speaks plain HTTP, so trust the
            # proxy's header; without it a local http:// run could never
            # keep a session.
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if not _AUTH_ENABLED or request.url.path == "/login":
            return await call_next(request)
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        if cookie and _session_cookie_valid(cookie):
            return await call_next(request)
        if _basic_auth_ok(request):
            return await call_next(request)
        return _unauthorized(request)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        ms = round((time.time() - start) * 1000)
        if request.url.path == "/api/log":
            return response
        if request.method != "GET" or response.status_code >= 400:
            params = dict(request.query_params)
            readable = {k: (v[:30] + "…" if len(v) > 30 else v) for k, v in params.items()}
            param_str = f"  {readable}" if readable else ""
            logger.info("%s %s%s → %d  (%dms)",
                        request.method, request.url.path, param_str,
                        response.status_code, ms)
        return response

    # Request-timing middleware (issue #458 measurement) — logs how long every
    # /api/ request actually takes, so slow endpoints show up in /api/logs
    # without needing SSH access. High-frequency polling endpoints are
    # excluded from routine logging (only surfaced if they somehow go slow),
    # since logging every poll would flood the ring buffer.
    _TIMING_QUIET_PREFIXES = (
        "/api/logs",
        "/api/story-progress",
        "/api/tts-progress",
        "/api/speak-status",
    )

    @app.middleware("http")
    async def request_timing(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        start = time.time()
        response = await call_next(request)
        ms = round((time.time() - start) * 1000)

        quiet = any(path.startswith(p) for p in _TIMING_QUIET_PREFIXES)
        if quiet:
            if ms > 500:
                logger.warning("SLOW %s %s: %d ms", request.method, path, ms)
            return response

        if ms > 500:
            logger.warning("SLOW %s %s: %d ms", request.method, path, ms)
        elif ms >= 100:
            logger.info("%s %s: %d ms", request.method, path, ms)
        else:
            logger.debug("%s %s: %d ms", request.method, path, ms)
        return response

    @app.middleware("http")
    async def static_cache_headers(request: Request, call_next):
        """Short max-age + must-revalidate for /static/* (issue #513): browsers
        skip the request entirely for 60s instead of always round-tripping, but
        deploys (~2min after a PR merges) are still picked up quickly since the
        cache is short and StaticFiles' ETag/Last-Modified handle revalidation.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
        return response

    if os.path.exists("static"):
        app.mount("/static", StaticFiles(directory="static"), name="static")

    @app.get("/")
    def root():
        return FileResponse("static/index.html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

    # Standalone quick-add page (#668): a bookmarkable URL that opens straight
    # into "type a word, AI generates the full entry" without loading the whole
    # app — meant for the iPhone home screen. Same pipeline as the toolbar ＋
    # button (POST /api/add-word-ai), just a much smaller page around it.
    @app.get("/add")
    def add_word_page():
        return FileResponse("static/add.html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

    # Standalone knowledge-base page (#681): the /add counterpart for material —
    # paste a link or an article body from the phone without loading the app.
    @app.get("/save")
    def save_material_page():
        return FileResponse("static/save.html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

    # Standalone AI dictionary page (#746): the /add counterpart for lookups —
    # type a word/phrase/sentence and get a structured, option-by-option
    # translation with a one-tap add into ★ List. Same "no app.js" reasoning
    # as /add and /save: bookmarkable/home-screen, opens fast.
    @app.get("/dict")
    def dictionary_page():
        return FileResponse("static/dict.html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

    # Bookmarkable knowledge-base tab links (#704): /knowledge/videos is the
    # browsing counterpart to /add and /save — a clean URL for the iPhone home
    # screen that lands on one sub-tab. Unlike those two this is *not* a
    # standalone page: browsing needs the full app (detail view, new-word
    # table, process buttons), so it just redirects into the app's hash route.
    # Plural and singular both work, and an unrecognized kind falls back to
    # podcasts rather than 404ing — same reasoning as the `day` parameter in
    # #686: getting to the knowledge base is the point, don't fail over a typo.
    _KNOWLEDGE_TAB_KINDS = {
        "podcast": "podcast", "podcasts": "podcast",
        "video": "video", "videos": "video",
        # Reels are a frontend-only split of kind='video' (#764), but the
        # bookmarkable URL has to exist all the same — /knowledge/reels is
        # exactly the link Daniel wants on his home screen.
        "reel": "reel", "reels": "reel", "instagram": "reel",
        "article": "article", "articles": "article",
        "newsletter": "newsletter", "newsletters": "newsletter",
    }

    @app.get("/knowledge/{kind}")
    def knowledge_tab_page(kind: str):
        tab = _KNOWLEDGE_TAB_KINDS.get(kind.lower(), "podcast")
        return RedirectResponse(f"/#knowledge-{tab}", status_code=303)

    # Running-version info (issue #450): read the current commit once at
    # startup — deploy.sh restarts the process on every deploy, so process
    # start time ≈ deploy time. Never fails: without git (or outside a
    # checkout) the badge just shows "unknown".
    def _read_version() -> dict:
        import subprocess
        try:
            log_out = subprocess.run(
                ["git", "log", "-1", "--format=%h%n%s%n%cI"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip().split("\n")
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            return {"commit": log_out[0], "message": log_out[1],
                    "commit_date": log_out[2], "branch": branch}
        except Exception as e:
            logger.warning("version info unavailable (%s)", e)
            return {"commit": "unknown", "message": "", "commit_date": "", "branch": "unknown"}

    from datetime import datetime as _dt
    # astimezone() attaches the server's real UTC offset (#706). A naive
    # isoformat() loses it, and the browser then parses the string as *its own*
    # local time — the production server runs on Asia/Shanghai, so the badge
    # showed Shanghai's digits no matter where it was read. Without the offset
    # no amount of client-side formatting can recover the actual instant.
    _version_info = {**_read_version(),
                     "deployed_at": _dt.now().astimezone().isoformat(timespec="seconds")}

    @app.get("/api/version")
    def get_version():
        return _version_info

    app.include_router(decks.router)
    app.include_router(review.router)
    app.include_router(story.router)
    app.include_router(browse.router)
    app.include_router(imports.router)
    app.include_router(podcast_routes.router)
    app.include_router(knowledge_routes.router)
    app.include_router(dictionary_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(books_routes.router)
    # Sync only makes sense on a laptop copy — on the server these routes must
    # not exist at all, or a stray call would overwrite production (#625).
    if LOCAL_MODE or OFFLINE_MODE:
        from routes import sync as sync_routes
        app.include_router(sync_routes.router)

    import threading
    import time

    from pydantic import BaseModel

    class LogBody(BaseModel):
        action: str

    @app.post("/api/log")
    def log_ui_action(body: LogBody):
        ui_logger.info("点击 → %s", body.action)
        return {"ok": True}

    @app.get("/api/logs")
    def get_logs(lines: int = 500):
        n = max(1, min(lines, 4000))
        recent = list(_log_buffer_handler.buffer)[-n:]
        return PlainTextResponse("\n".join(recent))

    @app.post("/api/restart")
    def restart_server():
        def _do_restart():
            time.sleep(0.3)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=False).start()
        return {"ok": True}

except ImportError as e:
    import sys
    print(f"Import error (app disabled): {e}", file=sys.stderr)
    app = None  # FastAPI not installed


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main()
    elif app:
        import uvicorn
        database.init_db()
        database.purge_old_trash()
        # PORT lets the offline instance (run.offline.sh) run alongside
        # anything already on 8000 — issue #612.
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                    access_log=False)
    else:
        print("Install fastapi and uvicorn to run the web server.")
        print("Usage: python main.py import | status [--deck NAME]")

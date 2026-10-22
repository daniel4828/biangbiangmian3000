"""
Tests for the form login + signed session cookie (#666).

The auth globals are read at call time inside main's middleware, so enabling
auth for a test is just monkeypatching them — no reimport of main needed.
"""

import base64
import hashlib
import hmac
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import main

USER = "daniel"
PASSWORD = "geheim123"


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setattr(main, "_AUTH_USERNAME", USER)
    monkeypatch.setattr(main, "_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(
        main, "_SESSION_KEY",
        hashlib.sha256(f"{USER}:{PASSWORD}".encode("utf-8")).digest(),
    )
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _sign(expires, key=None):
    key = key or hashlib.sha256(f"{USER}:{PASSWORD}".encode("utf-8")).digest()
    sig = hmac.new(key, str(expires).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


# --- login form ------------------------------------------------------------

def test_login_page_has_autofill_fields(auth_client):
    """The autocomplete tokens are the whole point: without them iOS Keychain
    never offers to save the password (which is why Basic Auth was replaced)."""
    body = auth_client.get("/login").text
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body
    assert 'action="/login"' in body


def test_login_page_shows_error_only_when_requested(auth_client):
    assert "Wrong username or password" not in auth_client.get("/login").text
    assert "Wrong username or password" in auth_client.get("/login?error=1").text


def test_login_success_sets_session_cookie(auth_client):
    r = auth_client.post("/login", data={"username": USER, "password": PASSWORD})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.cookies.get("anki_session")
    assert cookie and main._session_cookie_valid(cookie)


def test_login_failure_redirects_back_without_cookie(auth_client):
    r = auth_client.post("/login", data={"username": USER, "password": "wrong"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=1"
    assert "anki_session" not in r.cookies


# --- middleware ------------------------------------------------------------

def test_unauthenticated_api_gets_401_not_a_redirect(auth_client):
    """fetch() must see a real failure — an HTML login page with status 200
    would look like a successful response carrying garbage."""
    r = auth_client.get("/api/version")
    assert r.status_code == 401


def test_unauthenticated_page_redirects_to_login(auth_client):
    r = auth_client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_valid_cookie_grants_access(auth_client):
    auth_client.cookies.set("anki_session", _sign(int(time.time()) + 600))
    assert auth_client.get("/api/version").status_code == 200


@pytest.mark.parametrize("cookie", [
    "nonsense",
    "9999999999.deadbeef",                                  # forged signature
    _sign(int(time.time()) - 10),                           # expired
    _sign("later", hashlib.sha256(b"other:creds").digest()),  # wrong key
])
def test_bad_cookies_rejected(auth_client, cookie):
    auth_client.cookies.set("anki_session", cookie)
    assert auth_client.get("/api/version").status_code == 401


def test_basic_auth_still_works(auth_client):
    """Kept as a fallback for curl and scripts."""
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    r = auth_client.get("/api/version", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200
    bad = base64.b64encode(f"{USER}:nope".encode()).decode()
    assert auth_client.get("/api/version", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_password_change_invalidates_old_sessions(auth_client, monkeypatch):
    old = _sign(int(time.time()) + 600)
    monkeypatch.setattr(main, "_SESSION_KEY", hashlib.sha256(b"daniel:neues").digest())
    auth_client.cookies.set("anki_session", old)
    assert auth_client.get("/api/version").status_code == 401


def test_auth_disabled_is_a_noop():
    """AUTH_* unset (CI, local dev) must behave exactly as before."""
    with TestClient(main.app, follow_redirects=False) as c:
        assert c.get("/api/version").status_code == 200
        assert c.get("/login").status_code == 303


@pytest.mark.parametrize("path", [
    "/static/manifest.webmanifest",
    "/static/icons/app-icon-192.png",
    "/static/icons/app-icon-512.png",
    "/static/icons/apple-touch-icon.png",
])
def test_web_app_manifest_is_public(auth_client, path):
    # #1277: browsers fetch the manifest and its icons without the session
    # cookie when installing the app — a redirect to /login breaks install.
    r = auth_client.get(path)
    assert r.status_code == 200


def test_manifest_exemption_does_not_open_other_static_files(auth_client):
    r = auth_client.get("/static/app.js")
    assert r.status_code == 303

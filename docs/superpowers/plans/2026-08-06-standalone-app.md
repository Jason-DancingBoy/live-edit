# Standalone App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn live-edit into a single-tenant standalone deployable app — own server entry, login with multi-user roles, role-gated merge approval, and Docker packaging — without touching the core engine/router.

**Architecture:** A thin shell (`live_edit/server.py`) builds its own FastAPI app, mounts the existing `setup_live_edit()` router unchanged at `/live-edit`, and layers a session+role auth boundary (`live_edit/auth.py`) on top. The shell's HTTP middleware requires a valid session for `/live-edit/*` (bypassing `/static`, `/health`, `/metrics`), enforces `admin` for `/live-edit/admin/*`, and injects the configured `admin_key` header for admin sessions so the router's existing admin-key check passes untouched. The shell also serves standalone host pages (`/login`, `/editor`, `/admin`) and a small user-management API.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (stdlib `sqlite3`), `hashlib.scrypt`, `uvicorn`, Docker. Frontend is vanilla JS (existing `live-edit.js`).

## Global Constraints

- **Do NOT modify** `live_edit/engine.py` or `live_edit/router.py`. All core logic stays byte-for-byte identical.
- Only these existing files may be touched: `live_edit/cli.py` (add subcommands), `live_edit/static/live-edit.js` (auto-open tweak), `pyproject.toml` (add `uvicorn` runtime dep).
- Config-driven target repo: the business project is whatever git repo `project_root` points at; its `.live-edit.toml` drives provider/LLM/modes. No project code is hardcoded.
- Roles are exactly `admin` and `business_user`. No self-registration; admin bootstraps the first admin via env and adds users via CLI/API.
- `admin_key` header remains the API-level admin credential; the middleware exempts requests already carrying a valid one.
- Passwords hashed with `hashlib.scrypt`, never stored plaintext. Never hardcode secrets — env vars or parameters only.
- Python >= 3.10, line length 100, ruff rules `E,F,I,UP,B,C4,SIM`, coverage `fail_under = 60`.
- Each task ends with `pytest <new test file>` green and a commit.

---

### Task 1: auth.py — users, password hashing, sessions

**Files:**
- Create: `live_edit/auth.py`
- Test: `tests/test_auth.py`

**Interfaces (Produced — later tasks consume these exact names):**
- `@dataclass User(id: int, username: str, role: str, created_at: str)`
- `class UserExistsError(Exception)`
- `hash_password(password: str) -> str` — format `scrypt$n$r$p$salt_hex$dk_hex`
- `verify_password(password: str, stored: str) -> bool`
- `class UserStore(db_path: str)`:
  - `create_user(username: str, password: str, role: str) -> User` — raises `UserExistsError` on duplicate
  - `get_user(username: str) -> User | None`
  - `list_users() -> list[User]`
  - `authenticate(username: str, password: str) -> User | None`
  - `count() -> int`
- `class SessionManager(db_path: str, ttl_seconds: int = 1800)`:
  - `create_session(username: str) -> str` (returns opaque token)
  - `get_user(token: str, user_store: UserStore) -> User | None` (expired sessions auto-deleted → `None`)
  - `delete_session(token: str) -> None`
- `ensure_admin(user_store: UserStore, username: str, password: str) -> None` — creates the admin only when the store is empty

- [ ] **Step 1: Write the failing tests**

`tests/test_auth.py`:

```python
"""Tests for live_edit.auth — users, password hashing, sessions."""

import pytest


class TestPasswordHashing:
    def test_hash_and_verify(self):
        from live_edit.auth import hash_password, verify_password

        h = hash_password("secret")
        assert h.startswith("scrypt$")
        assert verify_password("secret", h)
        assert not verify_password("wrong", h)

    def test_hash_is_salted(self):
        from live_edit.auth import hash_password

        assert hash_password("pw") != hash_password("pw")


class TestUserStore:
    def test_create_and_get(self, tmp_path):
        from live_edit.auth import UserStore

        store = UserStore(str(tmp_path / "auth.db"))
        user = store.create_user("alice", "pw", "business_user")
        assert user.username == "alice"
        assert user.role == "business_user"
        got = store.get_user("alice")
        assert got is not None and got.role == "business_user"
        assert store.get_user("nobody") is None

    def test_duplicate_raises(self, tmp_path):
        from live_edit.auth import UserExistsError, UserStore

        store = UserStore(str(tmp_path / "auth.db"))
        store.create_user("alice", "pw", "business_user")
        with pytest.raises(UserExistsError):
            store.create_user("alice", "other", "admin")

    def test_authenticate(self, tmp_path):
        from live_edit.auth import UserStore

        store = UserStore(str(tmp_path / "auth.db"))
        store.create_user("bob", "pw123", "admin")
        assert store.authenticate("bob", "pw123") is not None
        assert store.authenticate("bob", "bad") is None
        assert store.authenticate("ghost", "pw123") is None

    def test_list_users(self, tmp_path):
        from live_edit.auth import UserStore

        store = UserStore(str(tmp_path / "auth.db"))
        store.create_user("a", "x", "business_user")
        store.create_user("b", "x", "admin")
        assert [u.username for u in store.list_users()] == ["a", "b"]


class TestSessionManager:
    def test_lifecycle(self, tmp_path):
        from live_edit.auth import SessionManager, UserStore

        db = str(tmp_path / "auth.db")
        store = UserStore(db)
        store.create_user("carol", "pw", "admin")
        sess = SessionManager(db, ttl_seconds=60)
        token = sess.create_session("carol")
        assert token
        assert sess.get_user(token, store) is not None
        assert sess.get_user("bogus", store) is None
        sess.delete_session(token)
        assert sess.get_user(token, store) is None

    def test_expired_session_returns_none(self, tmp_path):
        from live_edit.auth import SessionManager, UserStore

        db = str(tmp_path / "auth.db")
        store = UserStore(db)
        store.create_user("carol", "pw", "admin")
        sess = SessionManager(db, ttl_seconds=-1)
        token = sess.create_session("carol")
        assert sess.get_user(token, store) is None


class TestEnsureAdmin:
    def test_only_bootstraps_when_empty(self, tmp_path):
        from live_edit.auth import UserStore, ensure_admin

        store = UserStore(str(tmp_path / "auth.db"))
        ensure_admin(store, "admin", "pw")
        assert store.count() == 1
        assert store.get_user("admin").role == "admin"
        ensure_admin(store, "admin2", "pw")  # store not empty → no-op
        assert store.count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_edit.auth'`

- [ ] **Step 3: Write the implementation**

`live_edit/auth.py`:

```python
"""Authentication primitives for the standalone shell: users, sessions, roles."""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


@dataclass
class User:
    id: int
    username: str
    role: str
    created_at: str


class UserExistsError(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


class _DbMixin:
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn


class UserStore(_DbMixin):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS app_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def create_user(self, username: str, password: str, role: str) -> User:
        if self.get_user(username) is not None:
            raise UserExistsError(f"用户已存在: {username}")
        created_at = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO app_users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, created_at),
            )
            uid = cur.lastrowid
        return User(id=uid, username=username, role=role, created_at=created_at)

    def get_user(self, username: str) -> User | None:
        row = self._conn().execute(
            "SELECT id, username, role, created_at FROM app_users WHERE username = ?", (username,)
        ).fetchone()
        return User(**dict(row)) if row else None

    def list_users(self) -> list[User]:
        rows = self._conn().execute(
            "SELECT id, username, role, created_at FROM app_users ORDER BY id"
        ).fetchall()
        return [User(**dict(r)) for r in rows]

    def authenticate(self, username: str, password: str) -> User | None:
        row = self._conn().execute(
            "SELECT id, username, role, created_at, password_hash FROM app_users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            return None
        return User(
            id=row["id"], username=row["username"], role=row["role"], created_at=row["created_at"]
        )

    def count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM app_users").fetchone()[0]


class SessionManager(_DbMixin):
    def __init__(self, db_path: str, ttl_seconds: int = 1800):
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds
        self._local = threading.local()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS app_sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_sessions (token, username, created_at) VALUES (?, ?, ?)",
                (token, username, time.time()),
            )
        return token

    def get_user(self, token: str, user_store: UserStore) -> User | None:
        row = self._conn().execute(
            "SELECT username, created_at FROM app_sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["created_at"] > self.ttl_seconds:
            self.delete_session(token)
            return None
        return user_store.get_user(row["username"])

    def delete_session(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM app_sessions WHERE token = ?", (token,))


def ensure_admin(user_store: UserStore, username: str, password: str) -> None:
    if user_store.count() == 0:
        try:
            user_store.create_user(username, password, "admin")
        except UserExistsError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/auth.py tests/test_auth.py
git commit -m "feat(auth): user store, scrypt hashing, session manager for standalone shell"
```

---

### Task 2: server.py — create_app, login/logout, session gating

**Files:**
- Create: `live_edit/server.py`
- Test: `tests/test_server_auth.py`

**Consumes:** Task 1 `UserStore`, `SessionManager`, `ensure_admin`, `User`; router's `setup_live_edit(project_root, config_path, admin_key, provider, storage, vcs, **extra)`.

**Produces:**
- `create_app(project_root=".", config_path=".live-edit.toml", auth_db_path=None, session_ttl_seconds=1800, admin_user=None, admin_password=None, admin_key=None, provider=None, storage=None, vcs=None, **extra) -> FastAPI`
  - `auth_db_path` default: `os.path.join(project_root, "live_edit_app.db")`
  - `admin_user`/`admin_password` default from env `LIVE_EDIT_ADMIN_USER`/`LIVE_EDIT_ADMIN_PASSWORD`; `admin_key` from env `LIVE_EDIT_ADMIN_KEY`
- `COOKIE_NAME = "live_edit_session"` (module constant)
- Routes: `POST /login` (JSON body `{"username","password"}`; 200 `{"ok": true}` + sets cookie, or 401), `POST /logout` (303 → `/login`), `GET /` (302 → `/editor` or `/login`), `GET /login` (login.html), `GET /editor` (editor.html or 302 → `/login`)

- [ ] **Step 1: Write the failing tests**

`tests/test_server_auth.py`:

```python
"""Tests for the standalone shell: login/logout and session gating."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from test_router import FakeProvider, _write_router_config


@pytest.fixture
def server_app(tmp_path):
    from live_edit.server import create_app

    config_path = _write_router_config(tmp_path)
    mock_vcs = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get_sessions.return_value = []
    app = create_app(
        project_root=str(tmp_path),
        config_path=str(config_path),
        auth_db_path=str(tmp_path / "live_edit_app.db"),
        admin_user="admin",
        admin_password="adminpass",
        admin_key="admin-secret",
        provider=FakeProvider(),
        storage=mock_storage,
        vcs=mock_vcs,
    )
    app.state.vcs = mock_vcs
    app.state.storage = mock_storage
    return app


class TestLoginLogout:
    def test_login_success_sets_cookie(self, server_app):
        client = TestClient(server_app)
        resp = client.post("/login", json={"username": "admin", "password": "adminpass"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert "live_edit_session" in resp.cookies

    def test_login_wrong_password(self, server_app):
        resp = TestClient(server_app).post(
            "/login", json={"username": "admin", "password": "nope"}
        )
        assert resp.status_code == 401

    def test_login_unknown_user(self, server_app):
        resp = TestClient(server_app).post(
            "/login", json={"username": "ghost", "password": "x"}
        )
        assert resp.status_code == 401

    def test_logout_clears_session(self, server_app):
        client = TestClient(server_app)
        client.post("/login", json={"username": "admin", "password": "adminpass"})
        client.post("/logout")
        resp = client.get("/editor")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"


class TestSessionGating:
    def test_gated_endpoint_requires_login(self, server_app):
        resp = TestClient(server_app).get("/live-edit/timeline")
        assert resp.status_code == 401

    def test_health_is_bypassed(self, server_app):
        resp = TestClient(server_app).get("/live-edit/health")
        assert resp.status_code == 200

    def test_static_is_bypassed(self, server_app):
        resp = TestClient(server_app).get("/live-edit/static/live-edit.js")
        assert resp.status_code == 200

    def test_logged_in_user_can_hit_gated_endpoint(self, server_app):
        client = TestClient(server_app)
        client.post("/login", json={"username": "admin", "password": "adminpass"})
        resp = client.get("/live-edit/timeline")
        assert resp.status_code == 200


class TestHostRedirects:
    def test_index_redirects_anon_to_login(self, server_app):
        resp = TestClient(server_app).get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_editor_requires_login(self, server_app):
        resp = TestClient(server_app).get("/editor")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
```

> Note: `_write_router_config` and `FakeProvider` already exist in `tests/test_router.py` (they build a valid `.live-edit.toml` and a stub LLM provider). Reuse them — do not copy.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_edit.server'`

- [ ] **Step 3: Write the implementation**

`live_edit/server.py`:

```python
"""Standalone shell: a thin FastAPI app wrapping the live-edit router with auth."""

import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import SessionManager, UserStore, ensure_admin

COOKIE_NAME = "live_edit_session"
AUTH_BYPASS_PREFIXES = ("/live-edit/static/", "/live-edit/health", "/live-edit/metrics")
HOST_DIR = os.path.join(os.path.dirname(__file__), "static", "host")


def _current_user(request: Request, sessions: SessionManager, user_store: UserStore):
    token = request.cookies.get(COOKIE_NAME)
    return sessions.get_user(token, user_store) if token else None


def create_app(
    project_root: str = ".",
    config_path: str = ".live-edit.toml",
    auth_db_path: str | None = None,
    session_ttl_seconds: int = 1800,
    admin_user: str | None = None,
    admin_password: str | None = None,
    admin_key: str | None = None,
    provider=None,
    storage=None,
    vcs=None,
    **extra,
) -> FastAPI:
    from .router import setup_live_edit

    admin_user = admin_user or os.environ.get("LIVE_EDIT_ADMIN_USER") or ""
    admin_password = admin_password or os.environ.get("LIVE_EDIT_ADMIN_PASSWORD") or ""
    admin_key = admin_key if admin_key is not None else os.environ.get("LIVE_EDIT_ADMIN_KEY", "")

    db_path = auth_db_path or os.path.join(project_root, "live_edit_app.db")
    user_store = UserStore(db_path)
    sessions = SessionManager(db_path, ttl_seconds=session_ttl_seconds)
    if admin_user and admin_password:
        ensure_admin(user_store, admin_user, admin_password)

    router_kwargs = {
        "project_root": project_root,
        "config_path": config_path,
        "admin_key": admin_key,
        **extra,
    }
    if provider is not None:
        router_kwargs["provider"] = provider
    if storage is not None:
        router_kwargs["storage"] = storage
    if vcs is not None:
        router_kwargs["vcs"] = vcs
    router = setup_live_edit(**router_kwargs)

    app = FastAPI(title="live-edit standalone")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/live-edit/"):
            return await call_next(request)
        if any(path.startswith(p) for p in AUTH_BYPASS_PREFIXES):
            return await call_next(request)
        user = _current_user(request, sessions, user_store)
        if user is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return await call_next(request)

    # ── Auth endpoints ──

    @app.post("/login")
    async def login(request: Request):
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = user_store.authenticate(username, password)
        if user is None:
            return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
        token = sessions.create_session(user.username)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME, token, max_age=session_ttl_seconds, httponly=True, samesite="lax", path="/"
        )
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            sessions.delete_session(token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    # ── Host pages ──

    @app.get("/login")
    async def login_page():
        return FileResponse(os.path.join(HOST_DIR, "login.html"))

    @app.get("/")
    async def index(request: Request):
        user = _current_user(request, sessions, user_store)
        return RedirectResponse("/editor" if user else "/login", status_code=302)

    @app.get("/editor")
    async def editor_page(request: Request):
        user = _current_user(request, sessions, user_store)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(os.path.join(HOST_DIR, "editor.html"))

    app.include_router(router)
    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server_auth.py -v`
Expected: PASS (the `login.html`/`editor.html` files don't need to exist for these tests; `FileResponse` is only rendered on a 200 response — `test_editor_requires_login` and `test_index_redirects_anon_to_login` never render it. If a test unexpectedly returns 500 instead of 302, create placeholder host pages first, see Task 4.)

- [ ] **Step 5: Commit**

```bash
git add live_edit/server.py tests/test_server_auth.py
git commit -m "feat(server): standalone shell with login, logout, session gating"
```

---

### Task 3: admin role gating + user-management API

**Files:**
- Modify: `live_edit/server.py` (middleware admin branch + `/api/users` routes)
- Test: `tests/test_server_admin.py`

**Consumes:** Task 1 `UserStore`, Task 2 `create_app`, `COOKIE_NAME`, `_current_user`; router's `/live-edit/admin/*` endpoints (require `X-Admin-Key`).

**Produces:**
- Middleware behavior: `/live-edit/admin/*` requires an `admin` user; admin sessions get `x-admin-key` injected so the router's own check passes; requests already carrying a valid `X-Admin-Key` header bypass session checks (API credential preserved).
- Routes: `GET /api/users`, `POST /api/users` (JSON `{"username","password","role"}`) — both admin-only.

- [ ] **Step 1: Write the failing tests**

`tests/test_server_admin.py`:

```python
"""Tests for standalone-shell admin role gating and user-management API."""

import subprocess
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from test_router import FakeProvider, _write_router_config


def _init_git_repo(tmp_path):
    """Create a real temp git repo with a live-edit/s1 branch so the router's
    subprocess git calls (merge/list) resolve."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "init.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "branch", "live-edit/s1"], cwd=str(tmp_path), capture_output=True)


@pytest.fixture
def server_app(tmp_path):
    from live_edit.server import create_app

    config_path = _write_router_config(tmp_path)
    _init_git_repo(tmp_path)
    mock_vcs = MagicMock()
    mock_vcs.repo_path = str(tmp_path)
    mock_vcs.list_unmerged_branches.return_value = [
        {
            "session_id": "s1",
            "branch": "live-edit/s1",
            "commit_hash": "abc1234",
            "commit_time": "2026-06-19 12:00:00 +0800",
            "subject": "live-edit: fix button",
        }
    ]
    mock_vcs.merge_commit.return_value = "deadbeef"
    mock_storage = MagicMock()
    mock_storage.get_sessions.return_value = []
    mock_storage.get_session_detail.return_value = {
        "session_id": "s1",
        "request": "Fix the button color",
        "committed": 1,
        "commit_hash": "abc1234",
        "files": '["a.py"]',
        "mode": "quick",
        "messages": "[]",
    }
    app = create_app(
        project_root=str(tmp_path),
        config_path=str(config_path),
        auth_db_path=str(tmp_path / "live_edit_app.db"),
        admin_user="admin",
        admin_password="adminpass",
        admin_key="admin-secret",
        provider=FakeProvider(),
        storage=mock_storage,
        vcs=mock_vcs,
    )
    app.state.vcs = mock_vcs
    app.state.storage = mock_storage
    return app


def _login(client, username, password):
    return client.post("/login", json={"username": username, "password": password})


def _add_business_user(tmp_path):
    from live_edit.auth import UserStore

    UserStore(str(tmp_path / "live_edit_app.db")).create_user("ops", "opspass", "business_user")


class TestAdminRoleGating:
    def test_unauth_admin_api_is_401(self, server_app):
        resp = TestClient(server_app).get("/live-edit/admin/branches")
        assert resp.status_code == 401

    def test_business_user_denied(self, server_app, tmp_path):
        _add_business_user(tmp_path)
        client = TestClient(server_app)
        _login(client, "ops", "opspass")
        resp = client.get("/live-edit/admin/branches")
        assert resp.status_code == 403

    def test_admin_injected_key_passes(self, server_app):
        client = TestClient(server_app)
        _login(client, "admin", "adminpass")
        resp = client.get("/live-edit/admin/branches")
        assert resp.status_code == 200
        assert resp.json()["branches"][0]["session_id"] == "s1"

    def test_admin_key_header_bypasses_session(self, server_app):
        client = TestClient(server_app)
        resp = client.get("/live-edit/admin/branches", headers={"X-Admin-Key": "admin-secret"})
        assert resp.status_code == 200

    def test_admin_can_merge(self, server_app):
        client = TestClient(server_app)
        _login(client, "admin", "adminpass")
        resp = client.post("/live-edit/admin/branches/s1/merge")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_business_cannot_merge(self, server_app, tmp_path):
        _add_business_user(tmp_path)
        client = TestClient(server_app)
        _login(client, "ops", "opspass")
        resp = client.post("/live-edit/admin/branches/s1/merge")
        assert resp.status_code == 403


class TestUserManagementAPI:
    def test_admin_creates_user(self, server_app):
        client = TestClient(server_app)
        _login(client, "admin", "adminpass")
        resp = client.post(
            "/api/users", json={"username": "ops", "password": "pw", "role": "business_user"}
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "ops"
        assert resp.json()["role"] == "business_user"

    def test_duplicate_user_409(self, server_app):
        client = TestClient(server_app)
        _login(client, "admin", "adminpass")
        payload = {"username": "dup", "password": "pw", "role": "business_user"}
        assert client.post("/api/users", json=payload).status_code == 200
        assert client.post("/api/users", json=payload).status_code == 409

    def test_business_cannot_create_user(self, server_app, tmp_path):
        _add_business_user(tmp_path)
        client = TestClient(server_app)
        _login(client, "ops", "opspass")
        resp = client.post(
            "/api/users", json={"username": "x", "password": "pw", "role": "admin"}
        )
        assert resp.status_code == 403

    def test_unauthenticated_denied(self, server_app):
        resp = TestClient(server_app).get("/api/users")
        assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server_admin.py -v`
Expected: FAIL — `test_admin_injected_key_passes` and `test_admin_can_merge` return 403 (middleware has no admin branch yet); `test_*_users` return 404 (routes don't exist).

- [ ] **Step 3: Write the implementation**

In `live_edit/server.py`, replace the middleware's inner block with the admin branch:

```python
        # Existing API credential governs on its own; the router validates the key.
        if path.startswith("/live-edit/admin/") and request.headers.get("x-admin-key"):
            return await call_next(request)
        user = _current_user(request, sessions, user_store)
        if user is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        if path.startswith("/live-edit/admin/"):
            if user.role != "admin":
                return JSONResponse({"detail": "无权限"}, status_code=403)
            headers = list(request.scope["headers"])
            headers.append((b"x-admin-key", admin_key.encode()))
            request.scope["headers"] = headers
        return await call_next(request)
```

> Order matters: the `x-admin-key` header exemption must run BEFORE the session check, otherwise API clients carrying the key (but no session) would be rejected with 401. `test_admin_key_header_bypasses_session` guards this.

And add these routes just before `app.include_router(router)`:

```python
    # ── User-management API (admin only) ──

    @app.get("/api/users")
    async def list_users_api(request: Request):
        user = _current_user(request, sessions, user_store)
        if user is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        if user.role != "admin":
            return JSONResponse({"detail": "无权限"}, status_code=403)
        return {
            "users": [
                {"username": u.username, "role": u.role, "created_at": u.created_at}
                for u in user_store.list_users()
            ]
        }

    @app.post("/api/users")
    async def create_user_api(request: Request):
        user = _current_user(request, sessions, user_store)
        if user is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        if user.role != "admin":
            return JSONResponse({"detail": "无权限"}, status_code=403)
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role = body.get("role") or "business_user"
        if not username or not password:
            return JSONResponse({"detail": "用户名和密码必填"}, status_code=400)
        if role not in ("admin", "business_user"):
            return JSONResponse({"detail": "角色无效"}, status_code=400)
        try:
            created = user_store.create_user(username, password, role)
        except UserExistsError:
            return JSONResponse({"detail": "用户已存在"}, status_code=409)
        return {"username": created.username, "role": created.role}
```

And update the `from .auth import ...` import at the top of `server.py` to include `UserExistsError`:

```python
from .auth import SessionManager, UserExistsError, UserStore, ensure_admin
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server_admin.py -v`
Expected: PASS (all 10 tests). If `test_admin_can_merge` fails on the router's `preview_manager.stop`, the preview is disabled by default in the shared test config, so this should pass; if not, check `git rev-parse` subprocess calls resolve against `tmp_path` (they do — the repo is real).

- [ ] **Step 5: Commit**

```bash
git add live_edit/server.py tests/test_server_admin.py
git commit -m "feat(server): admin role gating on /live-edit/admin + user-management API"
```

---

### Task 4: host pages (login, editor) + frontend auto-open

**Files:**
- Create: `live_edit/static/host/login.html`, `live_edit/static/host/editor.html`
- Modify: `live_edit/static/live-edit.js` (auto-open tweak)
- Modify: `live_edit/server.py` (add `GET /api/me`)
- Test: `tests/test_server_pages.py`

**Consumes:** Task 2 `create_app`, `_current_user`, `COOKIE_NAME`; Task 3 `User`.

**Produces:**
- `GET /api/me` → 401 unauthenticated, else `{"username": str, "role": str}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_server_pages.py`:

```python
"""Tests for standalone host pages and the /api/me endpoint."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from test_router import FakeProvider, _write_router_config


@pytest.fixture
def server_app(tmp_path):
    from live_edit.server import create_app

    config_path = _write_router_config(tmp_path)
    app = create_app(
        project_root=str(tmp_path),
        config_path=str(config_path),
        auth_db_path=str(tmp_path / "live_edit_app.db"),
        admin_user="admin",
        admin_password="adminpass",
        admin_key="admin-secret",
        provider=FakeProvider(),
        storage=MagicMock(),
        vcs=MagicMock(),
    )
    return app


class TestLoginPage:
    def test_login_page_served(self, server_app):
        resp = TestClient(server_app).get("/login")
        assert resp.status_code == 200
        assert "登录" in resp.text
        assert "/login" in resp.text  # the form posts to /login via fetch


class TestEditorPage:
    def test_editor_requires_login(self, server_app):
        resp = TestClient(server_app).get("/editor")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_editor_has_panel_hook_when_logged_in(self, server_app):
        client = TestClient(server_app)
        client.post("/login", json={"username": "admin", "password": "adminpass"})
        resp = client.get("/editor")
        assert resp.status_code == 200
        assert "data-live-edit-auto-open" in resp.text
        assert "/live-edit/static/live-edit.js" in resp.text


class TestApiMe:
    def test_me_requires_login(self, server_app):
        resp = TestClient(server_app).get("/api/me")
        assert resp.status_code == 401

    def test_me_returns_user(self, server_app):
        client = TestClient(server_app)
        client.post("/login", json={"username": "admin", "password": "adminpass"})
        resp = client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json() == {"username": "admin", "role": "admin"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server_pages.py -v`
Expected: FAIL — `test_login_page_served` 500 (file missing), `test_editor_has_panel_hook_when_logged_in` 500 (file missing), `test_me_*` 404 (route missing).

- [ ] **Step 3: Write the implementation**

`live_edit/static/host/login.html`:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>登录 — live-edit</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #f6f7f9; display: flex; justify-content: center; }
    .le-login-card { margin-top: 96px; background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.1); padding: 32px; width: 320px; }
    .le-login-card h2 { margin-top: 0; }
    .le-login-card input { width: 100%; box-sizing: border-box; margin: 8px 0; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
    .le-login-card button { width: 100%; margin-top: 12px; padding: 10px; background: #2f6fdb; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
    .le-error { color: #c0392b; display: none; font-size: 13px; }
  </style>
</head>
<body>
  <div class="le-login-card">
    <h2>登录 live-edit</h2>
    <form id="login-form">
      <input id="username" name="username" placeholder="用户名" autocomplete="username">
      <input id="password" name="password" type="password" placeholder="密码" autocomplete="current-password">
      <p class="le-error" id="error">用户名或密码错误</p>
      <button type="submit">登录</button>
    </form>
  </div>
  <script>
    document.getElementById("login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const resp = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value,
          password: document.getElementById("password").value,
        }),
      });
      if (resp.ok) {
        window.location.href = "/editor";
      } else {
        document.getElementById("error").style.display = "block";
      }
    });
  </script>
</body>
</html>
```

`live_edit/static/host/editor.html`:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>即时编辑 — live-edit</title>
  <link rel="stylesheet" href="/live-edit/static/live-edit.css">
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; }
    .le-topbar { display: flex; justify-content: space-between; align-items: center; padding: 10px 16px; border-bottom: 1px solid #eee; background: #fff; }
    .le-topbar a { margin-left: 12px; color: #2f6fdb; text-decoration: none; }
  </style>
</head>
<body data-live-edit-auto-open>
  <header class="le-topbar">
    <strong>live-edit 即时编辑</strong>
    <span id="user-info"></span>
    <nav>
      <a id="admin-link" href="/admin" style="display:none">管理后台</a>
      <a href="/logout">登出</a>
    </nav>
  </header>
  <div id="le-host-body"></div>
  <script src="/live-edit/static/live-edit.js"></script>
  <script>
    fetch("/api/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((u) => {
        if (u && u.username) {
          document.getElementById("user-info").textContent =
            u.username + (u.role === "admin" ? "（管理员）" : "");
          if (u.role === "admin") {
            document.getElementById("admin-link").style.display = "";
          }
        }
      });
  </script>
</body>
</html>
```

Modify `live_edit/static/live-edit.js` — replace the bottom init block (the section `// ── Init ──`):

```js
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", createPanel);
  } else {
    createPanel();
  }
```

with:

```js
  function maybeAutoOpen() {
    if (document.body && document.body.hasAttribute("data-live-edit-auto-open")) {
      expandPanel();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      createPanel();
      maybeAutoOpen();
    });
  } else {
    createPanel();
    maybeAutoOpen();
  }
```

In `live_edit/server.py`, add `GET /api/me` just before `app.include_router(router)`:

```python
    @app.get("/api/me")
    async def me(request: Request):
        user = _current_user(request, sessions, user_store)
        if user is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return {"username": user.username, "role": user.role}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server_pages.py -v`
Expected: PASS. Also run the earlier suites to confirm no regression: `pytest tests/test_server_auth.py tests/test_server_admin.py -v`.

- [ ] **Step 5: Commit**

```bash
git add live_edit/static/host/ live_edit/static/live-edit.js live_edit/server.py tests/test_server_pages.py
git commit -m "feat(ui): standalone login/editor host pages + panel auto-open"
```

---

### Task 5: admin.html + admin page route

**Files:**
- Create: `live_edit/static/host/admin.html`
- Modify: `live_edit/server.py` (add `GET /admin`)
- Test: `tests/test_server_admin_pages.py`

**Consumes:** Task 2/3 routes `/live-edit/admin/*`, `/api/users`, `COOKIE_NAME`; middleware admin injection.

**Produces:** `GET /admin` — 302 → `/login` unauthenticated, 403 for non-admin, 200 HTML for admin.

- [ ] **Step 1: Write the failing tests**

`tests/test_server_admin_pages.py`:

```python
"""Tests for the standalone admin page route."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from test_router import FakeProvider, _write_router_config


@pytest.fixture
def server_app(tmp_path):
    from live_edit.server import create_app

    config_path = _write_router_config(tmp_path)
    app = create_app(
        project_root=str(tmp_path),
        config_path=str(config_path),
        auth_db_path=str(tmp_path / "live_edit_app.db"),
        admin_user="admin",
        admin_password="adminpass",
        admin_key="admin-secret",
        provider=FakeProvider(),
        storage=MagicMock(),
        vcs=MagicMock(),
    )
    return app


class TestAdminPage:
    def test_admin_page_requires_login(self, server_app):
        resp = TestClient(server_app).get("/admin")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_admin_page_ok_for_admin(self, server_app):
        client = TestClient(server_app)
        client.post("/login", json={"username": "admin", "password": "adminpass"})
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "管理后台" in resp.text

    def test_business_user_forbidden(self, server_app, tmp_path):
        from live_edit.auth import UserStore

        UserStore(str(tmp_path / "live_edit_app.db")).create_user("ops", "opspass", "business_user")
        client = TestClient(server_app)
        client.post("/login", json={"username": "ops", "password": "opspass"})
        resp = client.get("/admin")
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server_admin_pages.py -v`
Expected: FAIL — `test_admin_page_ok_for_admin` returns 404 (no `/admin` route).

- [ ] **Step 3: Write the implementation**

`live_edit/static/host/admin.html`:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>管理后台 — live-edit</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #f6f7f9; }
    .le-topbar { display: flex; justify-content: space-between; align-items: center; padding: 10px 16px; border-bottom: 1px solid #eee; background: #fff; }
    .le-topbar a { margin-left: 12px; color: #2f6fdb; text-decoration: none; }
    main { max-width: 860px; margin: 24px auto; padding: 0 16px; }
    section { background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.08); padding: 20px; margin-bottom: 20px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; }
    button { padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; }
    .le-merge { background: #2f6fdb; color: #fff; }
    .le-delete { background: #c0392b; color: #fff; }
  </style>
</head>
<body>
  <header class="le-topbar">
    <strong>live-edit 管理后台</strong>
    <nav><a href="/editor">返回编辑器</a><a href="/logout">登出</a></nav>
  </header>
  <main>
    <section>
      <h2>未合并分支</h2>
      <table id="branches-table">
        <thead><tr><th>会话</th><th>请求</th><th>提交</th><th>操作</th></tr></thead>
        <tbody id="branches-body"></tbody>
      </table>
    </section>
    <section>
      <h2>用户管理</h2>
      <form id="user-form">
        <input id="new-username" placeholder="用户名" required>
        <input id="new-password" type="password" placeholder="密码" required>
        <select id="new-role">
          <option value="business_user">业务用户</option>
          <option value="admin">管理员</option>
        </select>
        <button type="submit" class="le-merge">创建用户</button>
      </form>
      <table>
        <thead><tr><th>用户名</th><th>角色</th><th>创建时间</th></tr></thead>
        <tbody id="users-body"></tbody>
      </table>
    </section>
  </main>
  <script>
    async function load() {
      const b = await fetch("/live-edit/admin/branches").then((r) => r.json());
      const body = document.getElementById("branches-body");
      body.innerHTML = "";
      (b.branches || []).forEach((br) => {
        const tr = document.createElement("tr");
        tr.innerHTML = "<td></td><td></td><td></td><td></td>";
        const tds = tr.querySelectorAll("td");
        tds[0].textContent = br.session_id;
        tds[1].textContent = br.summary || br.subject || "";
        tds[2].textContent = (br.commit_hash || "").slice(0, 8);
        const mergeBtn = document.createElement("button");
        mergeBtn.className = "le-merge";
        mergeBtn.textContent = "合并";
        mergeBtn.onclick = () => mergeBranch(br.session_id);
        const delBtn = document.createElement("button");
        delBtn.className = "le-delete";
        delBtn.textContent = "删除";
        delBtn.onclick = () => deleteBranch(br.session_id);
        tds[3].append(mergeBtn, delBtn);
        body.appendChild(tr);
      });
      const u = await fetch("/api/users").then((r) => r.json());
      const ub = document.getElementById("users-body");
      ub.innerHTML = "";
      (u.users || []).forEach((usr) => {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.textContent = usr.username;
        const td2 = document.createElement("td");
        td2.textContent = usr.role;
        const td3 = document.createElement("td");
        td3.textContent = usr.created_at;
        tr.append(td, td2, td3);
        ub.appendChild(tr);
      });
    }

    async function mergeBranch(sid) {
      const r = await fetch(`/live-edit/admin/branches/${sid}/merge`, { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (r.ok) { alert("已合并: " + (data.commit_hash || "")); load(); }
      else { alert(data.detail || "合并失败"); load(); }
    }

    async function deleteBranch(sid) {
      if (!window.confirm("删除分支 " + sid + "？")) return;
      const r = await fetch(`/live-edit/admin/branches/${sid}/delete`, { method: "POST" });
      if (r.ok) { load(); } else { alert((await r.json().catch(() => ({}))).detail || "删除失败"); }
    }

    document.getElementById("user-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const r = await fetch("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("new-username").value,
          password: document.getElementById("new-password").value,
          role: document.getElementById("new-role").value,
        }),
      });
      if (r.ok) { load(); } else { alert((await r.json().catch(() => ({}))).detail || "创建失败"); }
      e.target.reset();
    });

    load();
  </script>
</body>
</html>
```

In `live_edit/server.py`, add `GET /admin` just before `app.include_router(router)`:

```python
    @app.get("/admin")
    async def admin_page(request: Request):
        user = _current_user(request, sessions, user_store)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        if user.role != "admin":
            return JSONResponse({"detail": "无权限"}, status_code=403)
        return FileResponse(os.path.join(HOST_DIR, "admin.html"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server_admin_pages.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add live_edit/static/host/admin.html live_edit/server.py tests/test_server_admin_pages.py
git commit -m "feat(ui): standalone admin page with branch merge/delete + user management"
```

---

### Task 6: CLI serve + create-user subcommands

**Files:**
- Modify: `live_edit/cli.py` (add `cmd_serve`, `cmd_create_user`, dispatch)
- Modify: `pyproject.toml` (add `uvicorn` to runtime deps)
- Test: `tests/test_cli_standalone.py`

**Consumes:** Task 2 `create_app`; Task 1 `UserStore`, `UserExistsError`.

**Produces:**
- `cmd_create_user(root=".", username=None, password=None, role="business_user", db=None) -> bool`
- `cmd_serve(root=".", host="0.0.0.0", port=8000, config_path=".live-edit.toml") -> None`
- CLI subcommands: `live-edit serve [--project-root DIR] [--host H] [--port P]`, `live-edit create-user [--project-root DIR] [--username U] [--password P] [--role R] [--db PATH]`

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_standalone.py`:

```python
"""Tests for standalone CLI subcommands: serve and create-user."""


class TestCreateUser:
    def test_create_user_writes_db(self, tmp_path):
        from live_edit.auth import UserStore
        from live_edit.cli import cmd_create_user

        (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
        ok = cmd_create_user(
            root=str(tmp_path), username="u", password="pw", role="business_user"
        )
        assert ok is True
        store = UserStore(str(tmp_path / "live_edit_app.db"))
        assert store.get_user("u") is not None
        assert store.authenticate("u", "pw") is not None

    def test_create_user_duplicate_returns_false(self, tmp_path):
        from live_edit.cli import cmd_create_user

        (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
        assert cmd_create_user(root=str(tmp_path), username="u", password="pw") is True
        assert cmd_create_user(root=str(tmp_path), username="u", password="pw") is False

    def test_create_user_respects_custom_db(self, tmp_path):
        from live_edit.auth import UserStore
        from live_edit.cli import cmd_create_user

        db = str(tmp_path / "custom.db")
        cmd_create_user(root=str(tmp_path), username="u", password="pw", db=db)
        assert UserStore(db).get_user("u") is not None


class TestServe:
    def test_serve_launches_uvicorn(self, tmp_path, monkeypatch):
        import uvicorn

        from live_edit import server as server_mod
        from live_edit.cli import cmd_serve

        captured = {}

        def fake_create_app(**kwargs):
            captured["kw"] = kwargs
            return object()

        def fake_run(app, host, port):
            captured["run"] = (host, port)

        monkeypatch.setattr(server_mod, "create_app", fake_create_app)
        monkeypatch.setattr(uvicorn, "run", fake_run)

        cmd_serve(root=str(tmp_path), host="127.0.0.1", port=8123)
        assert captured["run"] == ("127.0.0.1", 8123)
        assert captured["kw"]["project_root"] == str(tmp_path)
        assert captured["kw"]["config_path"] == ".live-edit.toml"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_standalone.py -v`
Expected: FAIL — `AttributeError: module 'live_edit.cli' has no attribute 'cmd_serve'/'cmd_create_user'`.

- [ ] **Step 3: Write the implementation**

In `live_edit/cli.py`, add two functions before `_print_help`:

```python
def cmd_create_user(root=".", username=None, password=None, role="business_user", db=None):
    """Create a user in the standalone app's auth database."""
    from .auth import UserExistsError, UserStore

    db_path = db or os.path.join(os.path.abspath(root), "live_edit_app.db")
    store = UserStore(db_path)
    try:
        user = store.create_user(username, password, role)
    except UserExistsError:
        print(f"用户已存在: {username}")
        return False
    print(f"已创建用户: {user.username} (role={user.role})")
    return True


def cmd_serve(root=".", host="0.0.0.0", port=8000, config_path=".live-edit.toml"):
    """Run the standalone server. Returns True after the server stops."""
    import uvicorn

    from .server import create_app

    app = create_app(project_root=root, config_path=config_path)
    print(f"live-edit standalone 已启动: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
    return True
```

Update `_print_help` to list the new commands:

```python
    print("  live-edit init          [目录]  生成 .live-edit.toml 配置文件")
    print("  live-edit check         [路径]  验证配置文件")
    print("  live-edit serve         [选项]  启动独立部署服务")
    print("  live-edit create-user   [选项]  创建用户 (admin/business_user)")
```

Add a shared flag parser helper and dispatch in `main()`:

```python
def _parse_flags(args):
    """Parse --key value / --flag pairs into a dict."""
    flags = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[key] = args[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            i += 1
    return flags
```

And in `main()`, replace the `elif cmd == "check":` block's tail to add the new branches:

```python
    elif cmd == "serve":
        flags = _parse_flags(args)
        ok = cmd_serve(
            root=flags.get("project_root", "."),
            host=flags.get("host", "0.0.0.0"),
            port=int(flags.get("port", 8000)),
            config_path=flags.get("config", ".live-edit.toml"),
        )
        sys.exit(0 if ok else 1)

    elif cmd == "create-user":
        flags = _parse_flags(args)
        ok = cmd_create_user(
            root=flags.get("project_root", "."),
            username=flags.get("username"),
            password=flags.get("password"),
            role=flags.get("role", "business_user"),
            db=flags.get("db"),
        )
        sys.exit(0 if ok else 1)
```

In `pyproject.toml`, add `uvicorn` to runtime dependencies:

```toml
dependencies = [
    "httpx>=0.27.0",
    "fastapi>=0.100.0",
    "tomli>=2.0.0",
    "uvicorn>=0.23.0",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_standalone.py tests/test_cli.py -v`
Expected: PASS. `cmd_serve` returns `True` after `uvicorn.run` stops, so the dispatch's `sys.exit(0 if ok else 1)` exits 0.

- [ ] **Step 5: Commit**

```bash
git add live_edit/cli.py pyproject.toml tests/test_cli_standalone.py
git commit -m "feat(cli): serve and create-user subcommands for standalone deployment"
```

---

### Task 7: Docker packaging + entrypoint

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `scripts/entrypoint.sh`
- Test: `tests/test_entrypoint.py`

**Consumes:** Task 6 CLI (`live-edit init`, `live-edit serve`).

**Produces:** `scripts/entrypoint.sh` supporting `--prepare-only` (repo init + config gen, no serve) so it's testable.

- [ ] **Step 1: Write the failing tests**

`tests/test_entrypoint.py`:

```python
"""Smoke tests for the Docker entrypoint script."""

import os
import subprocess

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestEntrypointScript:
    def test_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "scripts" / "entrypoint.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_prepare_only_creates_repo_and_config(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_cli = bin_dir / "live-edit"
        fake_cli.write_text(
            "#!/usr/bin/env bash\n"
            '[ "$1" = "init" ] && { echo "name = \\"x\\"" > "$2/.live-edit.toml"; }\n'
        )
        fake_cli.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["LIVE_EDIT_PROJECT_ROOT"] = str(repo)

        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "entrypoint.sh"), "--prepare-only"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (repo / ".git").exists()
        assert (repo / ".live-edit.toml").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_entrypoint.py -v`
Expected: FAIL — `FileNotFoundError: scripts/entrypoint.sh` (file missing).

- [ ] **Step 3: Write the implementation**

`scripts/entrypoint.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="${LIVE_EDIT_PROJECT_ROOT:-/workspace}"

prepare_project() {
  if [ ! -d "$ROOT/.git" ]; then
    git init -q "$ROOT"
    git -C "$ROOT" config user.email "live-edit@local"
    git -C "$ROOT" config user.name "live-edit"
    echo "# $(basename "$ROOT")" > "$ROOT/README.md"
    git -C "$ROOT" add -A
    git -C "$ROOT" commit -q -m "init: seed empty repository for live-edit"
  fi
  if [ ! -f "$ROOT/.live-edit.toml" ]; then
    live-edit init "$ROOT"
  fi
}

if [ "${1:-}" = "--prepare-only" ]; then
  prepare_project
  exit 0
fi

prepare_project
exec live-edit serve --project-root "$ROOT" --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
```

`Dockerfile`:

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/live-edit
COPY pyproject.toml README.md ./
COPY live_edit/ live_edit/
RUN pip install --no-cache-dir .

COPY scripts/entrypoint.sh /usr/local/bin/live-edit-entrypoint
RUN chmod +x /usr/local/bin/live-edit-entrypoint

EXPOSE 8000
ENV LIVE_EDIT_PROJECT_ROOT=/workspace
ENTRYPOINT ["live-edit-entrypoint"]
```

`docker-compose.yml`:

```yaml
services:
  live-edit:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./business-repo:/workspace
    environment:
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - LIVE_EDIT_ADMIN_USER=${LIVE_EDIT_ADMIN_USER:-admin}
      - LIVE_EDIT_ADMIN_PASSWORD=${LIVE_EDIT_ADMIN_PASSWORD:?set LIVE_EDIT_ADMIN_PASSWORD in .env}
      - LIVE_EDIT_ADMIN_KEY=${LIVE_EDIT_ADMIN_KEY:-}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_entrypoint.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml scripts/entrypoint.sh tests/test_entrypoint.py
git commit -m "feat(deploy): Dockerfile, compose, and entrypoint for standalone deployment"
```

---

## Self-Review Notes

- **Spec coverage:** §1 shell (`server.py` + mount router) → Task 2; §2 auth (users/roles/sessions/`admin_key` exemption) → Tasks 1–3; §3 host pages + frontend auto-open → Tasks 4–5; §4 data flow (login→edit→admin merge) → Tasks 2–5; §5 error handling (401/403/redirects) → Tasks 2–5; §6 tests → every task; §7 Docker → Task 7; §8 out-of-scope items are intentionally absent.
- **Placeholders:** none — every task has real test code and real implementation code.
- **Type consistency:** `User`, `UserStore.*`, `SessionManager.get_user(token, user_store)`, `create_app(...)` signature, `COOKIE_NAME`, `_current_user`, `cmd_serve(root, host, port, config_path)`, `cmd_create_user(root, username, password, role, db)` are the same names/signatures everywhere they appear.

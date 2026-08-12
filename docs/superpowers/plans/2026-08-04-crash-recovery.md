# Crash Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a live-edit session survive a hard crash — persist the conversation each round, keep freshly-crashed worktrees for a TTL, and let `/continue` resume a session from its persisted record.

**Architecture:** Three cooperating changes. (1) `run_edit_session` persists `session.messages` to SQLite at the end of every agent round (today it only persists in the `finally` block, so a crash loses everything) and refreshes the worktree dir mtime as the "last activity" signal. (2) `GitVCS.cleanup_stale_worktrees` becomes TTL-based: on startup it deletes only worktrees idle longer than `stale_worktree_ttl` (default 24h), keeping fresh crash leftovers for recovery. (3) `/continue/{session_id}` falls back to rebuilding an `EditSession` from the persisted record when the in-memory session is gone, so a post-crash restart can resume the conversation and the surviving worktree's partial edits.

**Tech Stack:** Python 3.12, FastAPI, asyncio, raw sqlite3 (no ORM), git worktrees, pytest.

## Global Constraints

- No new dependencies. Use only stdlib and the git CLI, matching existing code.
- Worktree root is the module constant `_WORKTREE_ROOT = "/tmp/live-edit"` in `live_edit/vcs.py:12`.
- Default `stale_worktree_ttl = 86400` (24 hours).
- Keep backward compatibility: new constructor/config parameters must have defaults; existing callers keep working.
- Storage methods are called with keyword arguments (see `_persist_session`, `engine.py:1075`).
- Every task ends green: `pytest tests/<file> -x -q` for the touched file, and the full suite before finishing the task.
- Comments in Chinese or English matching the surrounding file's style; no new comments unless they explain a non-obvious invariant (the mtime-touch and TTL rules qualify).
- Do not change graceful-session behavior: `_do_commit` still removes the worktree dir after a successful commit, and the `finally` block still discards the worktree on a graceful end. These changes only affect crash recovery.

---
---

### Task 1: Add `stale_worktree_ttl` config option

**Files:**
- Modify: `live_edit/config.py:67-73` (TimeoutsConfig), `live_edit/config.py:253-260` (`_parse_timeouts`)
- Test: `tests/test_config.py` (add to `TestParseConfig`)
- Docs: `USER_MANUAL.md` (timeouts config reference)

**Interfaces:**
- Consumes: nothing.
- Produces: `Config.timeouts.stale_worktree_ttl: int` (default `86400`), populated by `parse_config` from `[timeouts] stale_worktree_ttl`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` inside `class TestParseConfig`:

```python
    def test_timeouts_stale_worktree_ttl_default(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are helpful."
""")
        config = parse_config(str(toml_path))
        assert config.timeouts.stale_worktree_ttl == 86400

    def test_timeouts_stale_worktree_ttl_custom(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[timeouts]
stale_worktree_ttl = 3600

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are helpful."
""")
        config = parse_config(str(toml_path))
        assert config.timeouts.stale_worktree_ttl == 3600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -x -q`
Expected: FAIL — `TimeoutsConfig` has no attribute `stale_worktree_ttl`.

- [ ] **Step 3: Implement the config field**

In `live_edit/config.py` `TimeoutsConfig` (after `max_rounds: int = 15`):

```python
    stale_worktree_ttl: int = 86400
```

In `_parse_timeouts` (after `max_rounds=data.get("max_rounds", 15),`):

```python
        stale_worktree_ttl=data.get("stale_worktree_ttl", 86400),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -x -q`
Expected: PASS.

- [ ] **Step 5: Document the option**

In `USER_MANUAL.md`, in the `[timeouts]` config reference table, add:

```
stale_worktree_ttl = 86400   # 崩溃后保留未完成 worktree 供恢复的秒数（默认 24h）
```

- [ ] **Step 6: Commit**

```bash
git add live_edit/config.py tests/test_config.py USER_MANUAL.md
git commit -m "feat(config): add stale_worktree_ttl for crash-recovery retention"
```

---
---

### Task 2: TTL-based worktree cleanup + `session_worktree_path` helper

**Files:**
- Modify: `live_edit/vcs.py:12` (helper after `_WORKTREE_ROOT`), `live_edit/vcs.py:147-150` (`GitVCS.__init__`), `live_edit/vcs.py:173-214` (`cleanup_stale_worktrees`), `live_edit/vcs.py:216-218` (`create_worktree`)
- Test: `tests/test_vcs.py` (new class `TestCleanupStaleWorktrees`)

**Interfaces:**
- Consumes: nothing from prior tasks.
- Produces:
  - `session_worktree_path(session_id: str) -> str` — deterministic worktree path for a session (used by Task 4's `rehydrate_session`).
  - `GitVCS(repo_path, worktree_ttl: int = 86400)` — new optional param.
  - `GitVCS.cleanup_stale_worktrees(self, ttl_seconds: int | None = None)` — keeps worktrees idle shorter than the TTL; deletes older ones (registered → worktree + branch; orphan dir → `rmtree`).

- [ ] **Step 1: Write the failing test**

Add `import time` to `tests/test_vcs.py` imports (currently `os`, `subprocess`, `Path`, `pytest`). Add a new test class at the end of the file:

```python
class TestCleanupStaleWorktrees:
    def test_keeps_fresh_worktree_removes_stale(self, git_repo):
        vcs = GitVCS(str(git_repo), worktree_ttl=86400)
        fresh = vcs.create_worktree("sess-fresh")
        stale = vcs.create_worktree("sess-stale")
        # Backdate the stale worktree past the TTL.
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))

        vcs.cleanup_stale_worktrees(ttl_seconds=86400)

        assert os.path.isdir(fresh)
        assert not os.path.isdir(stale)
        branches = subprocess.run(
            ["git", "-C", str(git_repo), "branch", "--list", "live-edit/sess-stale"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branches == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_vcs.py::TestCleanupStaleWorktrees -x -q`
Expected: FAIL — `GitVCS` takes no `worktree_ttl` argument.

- [ ] **Step 3: Add the path helper and refactor `create_worktree`**

In `live_edit/vcs.py`, add `import time` to the imports. After the `_WORKTREE_ROOT` constant (line 12):

```python
def session_worktree_path(session_id: str) -> str:
    """Deterministic worktree path for a session (create + recovery)."""
    return os.path.join(_WORKTREE_ROOT, session_id)
```

In `create_worktree`, replace the first line of the body:

```python
        worktree_path = os.path.join(_WORKTREE_ROOT, session_id)
```

with:

```python
        worktree_path = session_worktree_path(session_id)
```

- [ ] **Step 4: Make cleanup TTL-aware**

Change `GitVCS.__init__` signature and its cleanup call:

```python
    def __init__(self, repo_path, worktree_ttl: int = 86400):
        self.repo_path = str(repo_path)
        self._main_branch: str | None = None
        self._worktree_ttl = worktree_ttl
        self.cleanup_stale_worktrees(self._worktree_ttl)
```

Replace the body of `cleanup_stale_worktrees` so fresh worktrees are kept:

```python
    def cleanup_stale_worktrees(self, ttl_seconds: int | None = None):
        """Remove crashed-session leftovers idle longer than ttl_seconds.

        A freshly-crashed worktree (dir mtime inside the TTL) is kept so the
        session can be recovered via /continue. The engine refreshes the dir
        mtime every round; see engine.run_edit_session.
        """
        ttl = self._worktree_ttl if ttl_seconds is None else ttl_seconds
        if not os.path.isdir(_WORKTREE_ROOT):
            return
        # Resolve repo_path so we can detect when running inside a worktree
        my_path = os.path.abspath(self.repo_path)
        now = time.time()
        # Get list of registered worktrees
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            registered = set()
            for line in result.stdout.split("\n"):
                if line.startswith("worktree "):
                    registered.add(line.split("worktree ", 1)[1].strip())
        except Exception:
            registered = set()

        for name in os.listdir(_WORKTREE_ROOT):
            path = os.path.join(_WORKTREE_ROOT, name)
            if not os.path.isdir(path) or os.path.islink(path):
                continue  # skip symlinks (e.g. live-edit -> package source)
            # Skip the worktree this process is running from (preview server)
            if os.path.abspath(path) == my_path:
                continue
            if now - os.path.getmtime(path) < ttl:
                continue  # fresh crash — keep for recovery
            if path in registered:
                try:
                    self.discard_session_branch(name, worktree_path=path)
                    logger.info("Cleaned up stale worktree: %s", path)
                except Exception as e:
                    logger.warning("Failed to remove registered worktree %s: %s", path, e)
            else:
                # Not registered — just delete the directory
                try:
                    shutil.rmtree(path)
                    logger.info("Cleaned up orphan worktree dir: %s", path)
                except Exception as e:
                    logger.warning("Failed to remove orphan dir %s: %s", path, e)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_vcs.py -x -q`
Expected: PASS (including pre-existing worktree tests — the new `worktree_ttl` param defaults to 86400 and cleanup still tolerates missing branches).

- [ ] **Step 6: Commit**

```bash
git add live_edit/vcs.py tests/test_vcs.py
git commit -m "feat(vcs): TTL-based stale worktree cleanup for crash recovery"
```

---
---

### Task 3: Persist conversation each round + refresh worktree mtime

**Files:**
- Modify: `live_edit/engine.py:854-855` (insert after the round's assistant + tool_results append)
- Test: `tests/test_engine.py` (add to `TestRunEditSession`)

**Interfaces:**
- Consumes: `_persist_session(session, storage, messages)` (`engine.py:1075`) — already defined in the module.
- Produces: a mid-loop persist + a worktree mtime refresh each round. Later tasks rely on the persisted `messages_json` being present for a crashed session.

- [ ] **Step 1: Write the failing test**

Add this method to `TestRunEditSession` in `tests/test_engine.py` (imports `os` already present):

```python
    @pytest.mark.asyncio
    async def test_incremental_persist_and_worktree_freshness(self):
        """Mid-run rounds persist the conversation; worktree mtime is refreshed."""
        import tempfile
        import time as _time

        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("old")

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {
                            "path": "edit_me.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_storage = MagicMock()
        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        # Backdate the worktree so we can prove the loop refreshed it.
        old = _time.time() - 1000
        os.utime(tmp, (old, old))

        session = EditSession("s1", "edit file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
        )

        calls = mock_storage.save_session.call_args_list
        assert len(calls) >= 2, "expected a mid-loop persist plus the final persist"
        assert "edit_me.py" in calls[-1].kwargs["messages_json"]
        assert os.path.getmtime(tmp) > old
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_engine.py::TestRunEditSession::test_incremental_persist_and_worktree_freshness -x -q`
Expected: FAIL — `save_session` is called only once (in the `finally` block), so `len(calls) >= 2` fails; `getmtime(tmp)` is `old`.

- [ ] **Step 3: Implement the mid-loop persist + mtime touch**

In `run_edit_session`, immediately after the two `messages.append(...)` lines (the assistant append and the tool_results append, currently `engine.py:854-855`), insert:

```python
            # Persist each round so a hard crash mid-session doesn't lose the
            # conversation; refresh the worktree mtime so the stale-worktree
            # TTL counts from last activity, not last commit.
            _persist_session(session, storage, messages)
            if session._worktree_path:
                os.utime(session._worktree_path, None)
```

(`os` is already imported at the top of `engine.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_engine.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat(engine): persist conversation each round for crash recovery"
```

---
---

### Task 4: `rehydrate_session` helper

**Files:**
- Modify: `live_edit/engine.py` (import line 20 `from .vcs import VCS`, and a new function after `_persist_session` ends at line 1099)
- Test: `tests/test_engine.py` (new class `TestRehydrateSession`; add `import json` to the file's imports)

**Interfaces:**
- Consumes: `_repair_messages(messages)` (`engine.py:60`), `session_worktree_path(session_id)` (Task 2).
- Produces: `rehydrate_session(session_id: str, detail: dict) -> EditSession | None` — rebuilds a session from a persisted record; returns `None` when the record has no messages. Sets `EditSession.messages/_mode/_modified_files/_committed/_commit_hash/_worktree_path/_merged`. Used by Task 5.

- [ ] **Step 1: Write the failing tests**

`get_session_detail` already parses the JSON columns into lists, so the tests pass lists (matching the storage contract). Add a new test class:

```python
class TestRehydrateSession:
    def test_restores_fields_and_strips_orphan_tool_use(self):
        from live_edit.engine import rehydrate_session

        detail = {
            "request": "polish doc",
            "mode": "deep",
            "committed": 0,
            "commit_hash": "",
            "files": ["doc.md"],
            "messages": [
                {"role": "user", "content": "polish"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "orphan",
                            "name": "edit_file",
                            "input": {"path": "doc.md"},
                        }
                    ],
                },
            ],
        }
        session = rehydrate_session("s-rehydrate", detail)
        assert session is not None
        assert session.id == "s-rehydrate"
        assert session.request == "polish doc"
        assert session._mode == "deep"
        assert session._modified_files == ["doc.md"]
        # The unpaired tool_use from a crash window must be stripped.
        leftovers = [
            b
            for m in session.messages
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "tool_use"
        ]
        assert leftovers == []

    def test_reuses_surviving_worktree(self, tmp_path, monkeypatch):
        import live_edit.vcs as vcs_mod
        from live_edit.engine import rehydrate_session

        sid = "s-wt"
        wt = tmp_path / sid
        wt.mkdir()
        monkeypatch.setattr(vcs_mod, "_WORKTREE_ROOT", str(tmp_path))
        detail = {
            "request": "r",
            "mode": "quick",
            "committed": 0,
            "commit_hash": "",
            "files": [],
            "messages": [{"role": "user", "content": "hi"}],
        }
        session = rehydrate_session(sid, detail)
        assert session._worktree_path == str(wt)
        assert session._merged is False

    def test_returns_none_without_messages(self):
        from live_edit.engine import rehydrate_session

        assert rehydrate_session("s-empty", {"request": "x", "messages": []}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_engine.py::TestRehydrateSession -x -q`
Expected: FAIL — `ImportError`/`AttributeError`: `rehydrate_session` does not exist.

- [ ] **Step 3: Implement the helper**

In `live_edit/engine.py`, update the vcs import:

```python
from .vcs import VCS, session_worktree_path
```

Add after the `_persist_session` function (after its `except` block, around line 1099):

```python
def rehydrate_session(session_id: str, detail: dict) -> EditSession | None:
    """Rebuild an EditSession from a persisted record (crash recovery).

    Returns None when the record has no messages to continue from.
    """
    messages = list(detail.get("messages") or [])
    if not messages:
        return None
    _repair_messages(messages)  # strip any unpaired tool_use from a crash window
    session = EditSession(session_id, detail.get("request", ""))
    session.messages = messages
    session._mode = detail.get("mode", "quick")
    session._modified_files = list(detail.get("files") or [])
    session._committed = bool(detail.get("committed", 0))
    session._commit_hash = detail.get("commit_hash", "") or ""
    worktree = session_worktree_path(session_id)
    if os.path.isdir(worktree):
        session._worktree_path = worktree
        session._merged = False
    else:
        session._worktree_path = ""
        session._merged = False
    return session
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_engine.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat(engine): add rehydrate_session to rebuild sessions from storage"
```

---
---

### Task 5: `/continue` storage fallback

**Files:**
- Modify: `live_edit/router.py:17-23` (engine import), `live_edit/router.py:192-197` (`continue_stream`)
- Test: `tests/test_router.py` (extract config writer, add `make_recovery_app` fixture + `TestContinueRecovery`)
- Docs: `USER_MANUAL.md` (recovery note near the continue/API section)

**Interfaces:**
- Consumes: `rehydrate_session` (Task 4), `storage.get_session_detail(session_id)` (`storage.py:208`), `session_store.add` / `session_store.get` (`engine.py:205`).
- Produces: `/continue/{session_id}` resumes from the persisted record when the in-memory session is gone. Also wires `stale_worktree_ttl` (Task 1) into `GitVCS` construction.

- [ ] **Step 1: Write the failing tests**

In `tests/test_router.py`:
1. Add `import os` to the imports.
2. Extract the config-file writing currently inline in `app_with_router` (the `config_path.write_text("""...""")` block, `test_router.py:30-83`) into a module-level helper, and have `app_with_router` call it:

```python
def _write_router_config(tmp_path):
    """Write the shared .live-edit.toml used by router fixtures."""
    config_path = tmp_path / ".live-edit.toml"
    config_path.write_text(
        """[project]
name = "TestApp"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"

[safety]
allowed_dirs = ["."]

[timeouts]
api_request = 180
shell_command = 30

[sessions]
max_active = 10

[hooks]

[ui]
default_mode = "quick"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]

[modes.quick.prompt]
base = "You are a helpful AI."
user_persona = "Non-technical user."
communication_rules = "Use Chinese."

[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"

[modes.deep.prompt]
base = "You are a dev assistant."
user_persona = "Developer."
communication_rules = "Use technical terms."

[errors.quick]
"old_string 在文件中未找到" = "文件内容已变化"
[errors.deep]
"""
    )
    return config_path
```

Replace the config-writing block in `app_with_router` with `config_path = _write_router_config(tmp_path)`.

3. Add the recovery fixtures and tests:

```python
def _recoverable_session_detail():
    # mirrors the parsed output of storage.get_session_detail (JSON columns
    # already decoded into lists)
    return {
        "session_id": "s-recover",
        "request": "polish",
        "committed": 0,
        "commit_hash": "",
        "files": ["doc.md"],
        "mode": "deep",
        "messages": [{"role": "user", "content": "polish the doc"}],
    }


@pytest.fixture
def make_recovery_app(tmp_path):
    def _make(session_detail):
        from live_edit.router import setup_live_edit

        config_path = _write_router_config(tmp_path)
        mock_provider = FakeProvider()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = str(tmp_path / "wt")
        os.makedirs(mock_vcs.create_worktree.return_value, exist_ok=True)
        mock_vcs.log_live_edit_commits.return_value = []
        mock_storage = MagicMock()
        mock_storage.get_sessions.return_value = []
        mock_storage.get_session_detail.return_value = session_detail
        router = setup_live_edit(
            project_root=str(tmp_path),
            config_path=str(config_path),
            provider=mock_provider,
            storage=mock_storage,
            vcs=mock_vcs,
        )
        app = FastAPI()
        app.include_router(router)
        return app

    return _make


class TestContinueRecovery:
    def test_recovers_session_from_storage(self, make_recovery_app):
        app = make_recovery_app(_recoverable_session_detail())
        client = TestClient(app)

        resp = client.post(
            "/live-edit/continue/s-recover", json={"request": "keep going"}
        )

        assert resp.status_code == 200
        assert '"done"' in resp.text

    def test_404_when_storage_has_no_record(self, make_recovery_app):
        app = make_recovery_app(None)
        client = TestClient(app)

        resp = client.post("/live-edit/continue/s-missing", json={"request": "x"})

        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_router.py -x -q`
Expected: FAIL — `test_recovers_session_from_storage` returns 404 (the router has no storage fallback yet). Existing tests must still pass after the `_write_router_config` refactor.

- [ ] **Step 3: Implement the router fallback + TTL wiring**

In `live_edit/router.py`, add `rehydrate_session` to the engine import:

```python
from .engine import (
    EditSession,
    SessionStore,
    build_timeline,
    continue_edit_session,
    rehydrate_session,
    run_edit_session,
)
```

Replace the top of `continue_stream`:

```python
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
```

with:

```python
        session = session_store.get(session_id)
        if session is None:
            # Crash recovery: rebuild the session from its persisted record.
            detail = storage.get_session_detail(session_id)
            session = rehydrate_session(session_id, detail) if detail else None
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            if not session_store.add(session):
                raise HTTPException(status_code=503, detail="会话数已达上限，请稍后再试")
```

Wire the new TTL into the `GitVCS` construction (currently `live_edit/router.py:98`):

```python
        vcs = GitVCS(project_root, worktree_ttl=config.timeouts.stale_worktree_ttl)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_router.py -x -q`
Expected: PASS (new recovery tests + all pre-existing router tests).

- [ ] **Step 5: Document recovery behavior**

In `USER_MANUAL.md`, near the continue/API section, add a short note:

```
会话崩溃恢复：每次编辑轮次都会把对话历史持久化到数据库。
若进程在会话中途崩溃，worktree 会保留 stale_worktree_ttl 秒（默认 24h）。
重启后 POST /live-edit/continue/{session_id} 会从持久化记录恢复会话并继续未完成的修改。
```

- [ ] **Step 6: Run the full suite and commit**

Run: `pytest tests/ -x -q`
Expected: PASS.

```bash
git add live_edit/router.py tests/test_router.py USER_MANUAL.md
git commit -m "feat(router): resume crashed sessions via /continue storage fallback"
```

---
---

## Post-Plan Verification Checklist

- [ ] Full suite green: `pytest tests/ -x -q`
- [ ] Manual smoke: start a real session, kill the process mid-run, restart the server, `POST /continue/<session_id>` resumes the conversation and the partial edits are still in the worktree.
- [ ] `cleanup_stale_worktrees` keeps a freshly-crashed worktree and removes one older than the TTL.

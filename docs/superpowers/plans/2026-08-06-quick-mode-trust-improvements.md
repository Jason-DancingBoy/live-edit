# Quick-Mode Trust Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make quick-mode approvals trustworthy for non-technical users by (1) showing a preview diff on every write-approval card, (2) adding a one-click "撤销" revert button to the timeline, and (3) reducing approval friction with a "全部批准" batch button.

**Architecture:** Three backend capabilities feed three thin frontend hooks. (1) A pure in-memory edit application (`apply_edit`) is extracted from `edit_file.py`; a new `diff.py` computes a preview unified diff for write tools; `engine.py` includes it in the `tool_plan` event. (2) Revert endpoints already exist (`/revert/{hash}/preview`, `/revert/{hash}/execute`) — only a frontend button is added. (3) `EditSession` gains an `auto_approve` flag consulted by `wait_for_approval`, toggled by a new `/approve/{session_id}/batch` endpoint. The `setup_live_edit` dependency-injection pattern is extended with an optional `session_store` parameter so the batch endpoint is unit-testable.

**Tech Stack:** Python 3.10+, stdlib `difflib`, FastAPI, pydantic, vanilla JS/CSS. **No new dependencies.**

## Global Constraints

- Python `>=3.10`; no new runtime dependencies; `ruff` (line-length 100, quote-style double) and `mypy` must stay clean.
- All existing tests keep passing: `pytest` (with `asyncio_mode = "auto"`), coverage `fail_under = 60`. Current baseline: 352 passed, 1 skipped, 71.26% coverage.
- Follow the existing dependency-injection pattern: new `setup_live_edit` parameters are **optional** and go at the **end** of the signature so existing positional callers keep working.
- `EditSession.set_auto_approve` must NOT bypass `safety.py` checks — dangerous shell/file ops stay blocked regardless of approval mode.
- The `live_edit/session_memory.py` deprecation migration is in progress; **do not touch `memory.py` or `session_memory.py`**.
- The frontend (`live_edit/static/live-edit.js`) has **no JS test infra** in this repo. Frontend tasks end with a manual-verification checklist instead of pytest steps, and must not break the existing pytest suite.
- Config docs (USER_MANUAL.md) mention approval behavior; update the quick-mode approval description in USER_MANUAL.md as a final task if behavior text changes.

---

### Task 1: Extract pure `apply_edit` from `edit_file.py`

**Files:**
- Modify: `live_edit/builtin_tools/edit_file.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `apply_edit(content: str, old: str, new: str) -> dict` — pure, no I/O. Returns `{"ok": True, "content": <new content>, "matched_via": "exact"|"whitespace_normalized"}` or `{"ok": False, "error": <message>}`. Consumed by Task 2's `compute_write_diff`.
- The existing `execute()` keeps its exact return shape (`{"ok", "path", "modified"[, "matched_via"]}`) so all current `test_edit_file*` tests pass unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tools.py` (after the existing `test_edit_file` test):

```python
    def test_apply_edit_exact(self):
        from live_edit.builtin_tools.edit_file import apply_edit

        result = apply_edit("old\nline\n", "old", "new")
        assert result["ok"] is True
        assert result["content"] == "new\nline\n"
        assert result["matched_via"] == "exact"

    def test_apply_edit_not_found(self):
        from live_edit.builtin_tools.edit_file import apply_edit

        result = apply_edit("hello\n", "missing", "new")
        assert result["ok"] is False
        assert "未找到" in result["error"]

    def test_apply_edit_multiple_matches(self):
        from live_edit.builtin_tools.edit_file import apply_edit

        result = apply_edit("a a a\n", "a", "b")
        assert result["ok"] is False
        assert "匹配了 3 处" in result["error"]

    def test_apply_edit_whitespace_normalized_single_match(self):
        from live_edit.builtin_tools.edit_file import apply_edit

        result = apply_edit("hello   world\nnext\n", "hello world", "hi")
        assert result["ok"] is True
        assert result["matched_via"] == "whitespace_normalized"
        assert result["content"] == "hi\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tools.py -k apply_edit -v`
Expected: FAIL — `ImportError: cannot import name 'apply_edit'`.

- [ ] **Step 3: Refactor `edit_file.py`**

Move the matching logic from `execute()` into a module-level pure function, then have `execute()` call it. Replace the body of `execute()` (lines 9–87) with:

```python
async def execute(args: dict, project_root: str, config=None) -> dict:
    path = safe_path(args["path"], project_root)
    old = args["old_string"]
    new = args["new_string"]
    with open(path, encoding="utf-8") as f:
        content = f.read()

    result = apply_edit(content, old, new)
    if not result.get("ok"):
        return {"ok": False, "error": result["error"]}
    with open(path, "w", encoding="utf-8") as f:
        f.write(result["content"])
    return {
        "ok": True,
        "path": args["path"],
        "modified": True,
        "matched_via": result.get("matched_via", "exact"),
    }


def apply_edit(content: str, old: str, new: str) -> dict:
    """Apply an old→new replacement in memory. Pure function, no I/O.

    Returns {"ok": True, "content": <new content>, "matched_via": ...} on success,
    or {"ok": False, "error": <user-facing message>} on failure.
    """
    count = content.count(old)
    if count == 1:
        return {"ok": True, "content": content.replace(old, new, 1), "matched_via": "exact"}

    if count == 0:
        norm_old = re.sub(r"\s+", " ", old).strip()
        norm_content = re.sub(r"\s+", " ", content)
        norm_positions = []
        pos = 0
        while True:
            idx = norm_content.find(norm_old, pos)
            if idx == -1:
                break
            norm_positions.append(idx)
            pos = idx + 1

        if len(norm_positions) == 0:
            head_lines = content.strip().split("\n")[:3]
            head_preview = "\n".join(head_lines)[:200]
            return {
                "ok": False,
                "error": f"old_string 在文件中未找到。文件开头预览:\n{head_preview}",
            }

        if len(norm_positions) == 1:
            norm_line_start = norm_content.rfind("\n", 0, norm_positions[0]) + 1
            norm_line_end = norm_content.find("\n", norm_positions[0] + len(norm_old))
            line_end = norm_line_end if norm_line_end != -1 else len(content)
            orig_match = content[norm_line_start:line_end]
            return {
                "ok": True,
                "content": content.replace(orig_match, new, 1),
                "matched_via": "whitespace_normalized",
            }

        line_info = []
        for pos in norm_positions[:5]:
            lineno = norm_content[:pos].count("\n") + 1
            snippet = norm_content[pos : pos + len(norm_old) + 40] + "..."
            line_info.append(f"  L{lineno}: ...{snippet}")
        return {
            "ok": False,
            "error": (
                f"old_string 模糊匹配了 {len(norm_positions)} 处（仅空白差异），"
                "请提供更多上下文:\n" + "\n".join(line_info)
            ),
        }

    if count > 1:
        line_info = []
        for m in re.finditer(re.escape(old), content):
            if len(line_info) >= 5:
                break
            lineno = content[: m.start()].count("\n") + 1
            ctx_start = max(0, m.start() - 20)
            ctx_end = min(len(content), m.end() + 40)
            snippet = content[ctx_start:ctx_end].replace("\n", "\\n") + "..."
            line_info.append(f"  L{lineno}: ...{snippet}")
        return {
            "ok": False,
            "error": (
                f"old_string 匹配了 {count} 处，请提供更多上下文使其唯一:\n"
                + "\n".join(line_info)
            ),
        }

    return {"ok": False, "error": "unreachable"}  # all paths return above
```

Note: `execute()` now returns `"matched_via"` for exact matches too (previously absent). This is an additive key; no existing test asserts its absence.

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/python -m pytest tests/test_tools.py -v`
Expected: PASS — all `test_edit_file*`, `test_write_file*`, and the 4 new `test_apply_edit*` tests.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, coverage ≥ 60%.

```bash
git add live_edit/builtin_tools/edit_file.py tests/test_tools.py
git commit -m "refactor(tools): extract pure apply_edit from edit_file"
```

---

### Task 2: Add `live_edit/diff.py` — preview diff computation

**Files:**
- Create: `live_edit/diff.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `apply_edit` from Task 1.
- Produces:
  - `diff_text(old: str, new: str, filename: str = "") -> str`
  - `compute_write_diff(tool_name: str, args: dict, project_root: str) -> str` — returns a unified diff for `edit_file`/`write_file`, `""` for everything else (including tools whose edit would fail, e.g. `old_string` not found). Consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_diff.py`:

```python
"""Tests for live_edit.diff — preview diff computation."""


def test_diff_text_shows_removed_and_added():
    from live_edit.diff import diff_text

    diff = diff_text("a\nb\n", "a\nB\n")
    assert "-b" in diff
    assert "+B" in diff


def test_diff_text_empty_when_identical():
    from live_edit.diff import diff_text

    assert diff_text("same\n", "same\n") == ""


def test_compute_write_diff_edit_file(tmp_path):
    from live_edit.diff import compute_write_diff

    p = tmp_path / "app.py"
    p.write_text("old\n", encoding="utf-8")
    diff = compute_write_diff(
        "edit_file",
        {"path": "app.py", "old_string": "old", "new_string": "new"},
        str(tmp_path),
    )
    assert "-old" in diff
    assert "+new" in diff


def test_compute_write_diff_write_file_new(tmp_path):
    from live_edit.diff import compute_write_diff

    diff = compute_write_diff(
        "write_file", {"path": "new.py", "content": "print(1)\n"}, str(tmp_path)
    )
    assert "+print(1)" in diff


def test_compute_write_diff_write_file_overwrites_existing(tmp_path):
    from live_edit.diff import compute_write_diff

    p = tmp_path / "app.py"
    p.write_text("old\n", encoding="utf-8")
    diff = compute_write_diff("write_file", {"path": "app.py", "content": "new\n"}, str(tmp_path))
    assert "-old" in diff
    assert "+new" in diff


def test_compute_write_diff_non_write_tool_returns_empty(tmp_path):
    from live_edit.diff import compute_write_diff

    assert compute_write_diff("run_shell", {"cmd": "echo hi"}, str(tmp_path)) == ""


def test_compute_write_diff_edit_failure_returns_empty(tmp_path):
    from live_edit.diff import compute_write_diff

    assert (
        compute_write_diff(
            "edit_file",
            {"path": "nope.py", "old_string": "x", "new_string": "y"},
            str(tmp_path),
        )
        == ""
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_diff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_edit.diff'`.

- [ ] **Step 3: Implement `live_edit/diff.py`**

```python
"""Unified-diff helpers for previewing write operations before approval."""

import difflib
import os

from .builtin_tools.edit_file import apply_edit


def diff_text(old: str, new: str, filename: str = "") -> str:
    """Return a unified diff between old and new content (empty when identical)."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=filename,
            tofile=filename,
        )
    )


def _read_or_empty(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def compute_write_diff(tool_name: str, args: dict, project_root: str) -> str:
    """Compute a preview unified diff for a write tool without applying it.

    Returns "" for non-write tools, missing paths, or edits that would fail
    (e.g. edit_file's old_string not found).
    """
    path = (args.get("path") or "").strip()
    if not path:
        return ""
    abs_path = os.path.join(project_root, path)
    current = _read_or_empty(abs_path)

    if tool_name == "edit_file":
        result = apply_edit(current, args.get("old_string", ""), args.get("new_string", ""))
        if not result.get("ok"):
            return ""
        return diff_text(current, result["content"], path)

    if tool_name == "write_file":
        return diff_text(current, args.get("content", ""), path)

    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_diff.py -v`
Expected: PASS — all 7 tests.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, coverage ≥ 60%.

```bash
git add live_edit/diff.py tests/test_diff.py
git commit -m "feat(diff): preview unified diff for write tools"
```

---

### Task 3: Include `preview_diff` in quick-mode approval events

**Files:**
- Modify: `live_edit/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `compute_write_diff` from Task 2.
- Produces: quick-mode `tool_plan` events (the dict passed to `EditSession.wait_for_approval`) now include a `preview_diff: str` key for `edit_file`/`write_file` (empty for other tools). Consumed by Task 4's frontend.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engine.py` inside `class TestRunEditSession` (modeled on `test_approval_timeout_records_audit_and_metric`, but with a real `wait_for_approval` monkeypatched to capture its payload):

```python
    async def test_quick_mode_tool_plan_includes_preview_diff(self):
        """quick-mode write approval receives a preview_diff for edit_file."""
        import os
        import tempfile
        from types import SimpleNamespace

        edit_root = tempfile.mkdtemp()
        fpath = os.path.join(edit_root, "edit_me.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("old\n")

        write_def = SimpleNamespace(is_write=True, require_approval=False)

        class FakeToolRegistry:
            def get_tools(self, mode):
                return ["edit_file"]

            def get_tool(self, name):
                return write_def

            async def execute(self, name, args, root, config):
                return {"ok": True}

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
                [{"type": "text", "text": "done"}],
            ]
        )

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = edit_root
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = edit_root

        session = EditSession("s1", "Edit")
        captured = {}

        async def fake_wait(tool_id, tool_data, timeout=300.0):
            captured["data"] = tool_data
            return {"approved": True}

        session.wait_for_approval = fake_wait  # type: ignore[method-assign]

        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            tool_registry=FakeToolRegistry(),
        )

        assert "preview_diff" in captured["data"]
        assert "-old" in captured["data"]["preview_diff"]
        assert "+new" in captured["data"]["preview_diff"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine.py -k preview_diff -v`
Expected: FAIL — `KeyError: 'preview_diff'` (or `assert 'preview_diff' in captured["data"]` fails).

- [ ] **Step 3: Implement**

3a. Add the import to `live_edit/engine.py`:

```python
from .diff import compute_write_diff
```

3b. In `run_edit_session`, inside the `if needs_approval:` branch (currently `engine.py:854-865`), compute and pass `preview_diff`:

```python
                if needs_approval:
                    reason = tool_input.get("reason", "")
                    summary = _tool_summary(tool_name, tool_input)
                    preview_diff = compute_write_diff(tool_name, tool_input, _root)
                    result = await session.wait_for_approval(
                        tool_id,
                        {
                            "tool": tool_name,
                            "args": tool_input,
                            "reason": reason,
                            "summary": summary,
                            "preview_diff": preview_diff,
                        },
                    )
```

(`_root` is already defined at `engine.py:570` as `session._worktree_path`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_engine.py -k preview_diff -v`
Expected: PASS.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, coverage ≥ 60%.

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat(engine): include preview_diff in quick-mode approval events"
```

---

### Task 4: Render preview diff in the approval card

**Files:**
- Modify: `live_edit/static/live-edit.js`
- Modify: `live_edit/static/live-edit.css`

**Interfaces:**
- Consumes: the `preview_diff` key on `tool_plan` events from Task 3.
- Note: there is no JS test infra; verification is manual (see Step 3). Backend behavior is already covered by Task 3's pytest.

- [ ] **Step 1: Modify `addApprovalCard`**

In `live_edit/static/live-edit.js`, replace `addApprovalCard` (currently `live-edit.js:466-494`) with a version that renders a collapsible diff block when `event.preview_diff` is present. Reuses the existing `renderDiff` helper (`live-edit.js:538-547`) and `.le-diff-content` class:

```javascript
  function addApprovalCard(event) {
    const tl = document.getElementById("le-timeline");
    const card = document.createElement("div");
    card.className = "le-tool-card";
    card.dataset.toolId = event.id;
    const diffHtml = event.preview_diff
      ? `<details class="le-tool-diff"><summary>查看改动</summary><div class="le-diff-content">${renderDiff(event.preview_diff)}</div></details>`
      : "";
    card.innerHTML = `
      <div class="le-tool-summary">${escapeHtml(event.summary || event.tool)}</div>
      ${event.reason ? '<div class="le-tool-detail">原因: ' + escapeHtml(event.reason) + "</div>" : ""}
      ${diffHtml}
      <div class="le-tool-actions">
        <button class="le-btn le-btn-danger le-reject-btn">拒绝</button>
        <button class="le-btn le-btn-primary le-approve-btn">批准</button>
      </div>
    `;

    card.querySelector(".le-approve-btn").addEventListener("click", () => {
      approveTool(event.id, true);
      card.querySelector(".le-tool-actions").innerHTML =
        '<span style="color:var(--le-success)">已批准 &#10003;</span>';
    });

    card.querySelector(".le-reject-btn").addEventListener("click", () => {
      approveTool(event.id, false);
      card.querySelector(".le-tool-actions").innerHTML =
        '<span style="color:var(--le-error)">已拒绝 &#10007;</span>';
    });

    tl.appendChild(card);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }
```

- [ ] **Step 2: Add CSS for the collapsible diff**

Append to `live_edit/static/live-edit.css`:

```css
/* Quick-mode approval card: collapsible preview diff */
.le-tool-card details.le-tool-diff {
  margin: 6px 0;
  font-size: 12px;
}
.le-tool-card details.le-tool-diff summary {
  cursor: pointer;
  color: var(--le-text-muted);
  margin-bottom: 4px;
}
.le-tool-card details.le-tool-diff .le-diff-content {
  max-height: 200px;
  overflow: auto;
}
```

- [ ] **Step 3: Manual verification**

Run a minimal app that mounts the router and a project with a real file:

```bash
# temp project
mkdir -p /tmp/live-edit-manual && cd /tmp/live-edit-manual
git init && printf 'old\n' > app.txt
cat > server.py <<'EOF'
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from live_edit import setup_live_edit

app = FastAPI()
app.include_router(setup_live_edit(project_root="."))
app.get("/")(lambda: HTMLResponse('<script src="/live-edit/static/live-edit.js"></script>'))
EOF
cd /home/jason/agent/live-edit && .venv/bin/python -m uvicorn server:app --port 8000 --app-dir /tmp/live-edit-manual
```

Open `http://127.0.0.1:8000/`, press `Ctrl+Shift+D`, set mode to 快速修改, and request "把 app.txt 里的 old 改成 new". Expected: an approval card appears with a collapsible "查看改动" section showing a `-old` / `+new` diff. Reject and approve both work.

- [ ] **Step 4: Run full pytest suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (frontend changes must not break the Python suite).

```bash
git add live_edit/static/live-edit.js live_edit/static/live-edit.css
git commit -m "feat(ui): show preview diff in quick-mode approval card"
```

---

### Task 5: `EditSession` auto-approve flag

**Files:**
- Modify: `live_edit/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces:
  - `EditSession.set_auto_approve(active: bool) -> None`
  - `EditSession.wait_for_approval` behavior: when `_auto_approve` is set, it emits `tool_plan` with `auto=True` and returns `{"approved": True, "auto": True}` immediately instead of waiting. Consumed by Task 6 (router) and Task 7 (frontend).
- When `_auto_approve` is False (default), behavior is byte-for-byte identical to today.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py` inside `class TestEditSession` (next to `test_wait_for_approval_approved`, `test_wait_for_approval_timeout`):

```python
    def test_auto_approve_off_by_default(self):
        session = EditSession("s1", "Test")
        assert session._auto_approve is False

    async def test_wait_for_approval_auto_approve_returns_immediately(self):
        session = EditSession("s1", "Auto")
        session.set_auto_approve(True)
        result = await session.wait_for_approval("t1", {"tool": "edit_file", "summary": "s"})
        assert result == {"approved": True, "auto": True}
        # The plan event is still emitted so the UI shows what auto-ran.
        event = session.queue.get_nowait()
        assert event["type"] == "tool_plan"
        assert event["id"] == "t1"
        assert event["auto"] is True

    async def test_auto_approve_does_not_emit_approval_wait(self):
        session = EditSession("s1", "Auto")
        session.set_auto_approve(True)
        # Must not block: if it waited, this await would hang the test until timeout.
        result = await session.wait_for_approval("t1", {"tool": "edit_file"}, timeout=0.1)
        assert result["approved"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine.py -k auto_approve -v`
Expected: FAIL — `AttributeError: 'EditSession' object has no attribute 'set_auto_approve'`.

- [ ] **Step 3: Implement**

3a. In `EditSession.__init__` (after `self._cancelled = asyncio.Event()`), add:

```python
        self._auto_approve: bool = False
```

3b. Add the method (after `cancel()`):

```python
    def set_auto_approve(self, active: bool) -> None:
        """When True, all subsequent write tools in this session auto-approve."""
        self._auto_approve = active
```

3c. Replace `wait_for_approval` (currently `engine.py:176-187`):

```python
    async def wait_for_approval(
        self, tool_id: str, tool_data: dict, timeout: float = 300.0
    ) -> dict:
        """Send tool_plan event and wait for frontend to call approve endpoint."""
        self._approve_event.clear()
        self._approve_result = None
        if self._auto_approve:
            self.queue.put_nowait(
                {"type": "tool_plan", "id": tool_id, "auto": True, **tool_data}
            )
            return {"approved": True, "auto": True}
        self.queue.put_nowait({"type": "tool_plan", "id": tool_id, **tool_data})
        try:
            await asyncio.wait_for(self._approve_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"approved": False, "reason": "用户超时未响应"}
        return self._approve_result or {"approved": False}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/python -m pytest tests/test_engine.py -v`
Expected: PASS — new auto-approve tests plus all existing `TestEditSession` and `TestRunEditSession` tests.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, coverage ≥ 60%.

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat(engine): EditSession auto-approve flag for batch approval"
```

---

### Task 6: Router batch-approve endpoint + `session_store` injection

**Files:**
- Modify: `live_edit/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `EditSession.set_auto_approve` from Task 5.
- Produces:
  - `BatchApproveRequest(BaseModel)` with `enabled: bool = True`.
  - `POST /live-edit/approve/{session_id}/batch` — sets auto-approve, records `approve_batch` audit event, returns `{"ok": True, "enabled": <bool>}`; 404 for missing session.
  - `setup_live_edit(..., session_store: SessionStore | None = None)` — optional param at the **end** of the signature; enables deterministic router tests. Existing callers unaffected.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_router.py`:

```python
class TestBatchApprove:
    def test_batch_approve_missing_session(self, client):
        """POST batch approve on a nonexistent session returns 404."""
        response = client.post(
            "/live-edit/approve/nonexistent/batch", json={"enabled": True}
        )
        assert response.status_code == 404

    def test_batch_approve_enables_auto_approve(self, tmp_path):
        """Batch approve toggles auto-approve on the target session."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from live_edit.engine import EditSession, SessionStore
        from live_edit.router import setup_live_edit

        config_path = _write_router_config(tmp_path)
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "Edit")
        store.add(session)

        router = setup_live_edit(
            project_root=str(tmp_path),
            config_path=str(config_path),
            provider=FakeProvider(),
            storage=MagicMock(),
            vcs=MagicMock(),
            session_store=store,
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.post("/live-edit/approve/s1/batch", json={"enabled": True})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "enabled": True}
        assert session._auto_approve is True

        response = client.post("/live-edit/approve/s1/batch", json={"enabled": False})
        assert response.json() == {"ok": True, "enabled": False}
        assert session._auto_approve is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_router.py -k batch_approve -v`
Expected: FAIL — batch endpoint returns 404 (route not found) and/or `setup_live_edit() got an unexpected keyword argument 'session_store'`.

- [ ] **Step 3: Implement**

3a. Add the request model next to `ApproveRequest` (currently `router.py:47`):

```python
class BatchApproveRequest(BaseModel):
    enabled: bool = True
```

3b. Add the optional param to `setup_live_edit` (signature ends at `metrics: Metrics | None = None,`):

```python
    session_store: SessionStore | None = None,
```

And update the store creation block (currently `router.py:140`) to:

```python
    ttl = getattr(config.timeouts, "session_ttl", 1800) if hasattr(config, "timeouts") else 1800
    max_active = getattr(config.sessions, "max_active", 10) if hasattr(config, "sessions") else 10
    if session_store is None:
        session_store = SessionStore(max_active=max_active, ttl_seconds=ttl, audit_log=audit_log)
```

3c. Add the endpoint right after `approve_tool` (after `router.py:361`):

```python
    # ── POST /live-edit/approve/{session_id}/batch ──

    @router.post("/approve/{session_id}/batch")
    async def batch_approve(session_id: str, req: BatchApproveRequest):
        """Enable/disable auto-approval for all subsequent write tools in a session."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        session.set_auto_approve(req.enabled)
        audit_log.record(
            "approve_batch",
            target=session_id,
            session_id=session_id,
            result="enabled" if req.enabled else "disabled",
        )
        return {"ok": True, "enabled": req.enabled}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_router.py -k batch_approve -v`
Expected: PASS — both tests.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, coverage ≥ 60%.

```bash
git add live_edit/router.py tests/test_router.py
git commit -m "feat(router): batch approve endpoint toggles session auto-approval"
```

---

### Task 7: Frontend batch-approve button

**Files:**
- Modify: `live_edit/static/live-edit.js`
- Modify: `live_edit/static/live-edit.css`

**Interfaces:**
- Consumes: `POST /live-edit/approve/{session_id}/batch` from Task 6, and the `auto=True` `tool_plan` events from Task 5 (the existing `tool_plan` handler at `live-edit.js:261-267` already routes `auto` events to the "执行中" card — no change needed there).
- No JS test infra; manual verification.

- [ ] **Step 1: Add `batchApprove` helper and wire the button**

In `live_edit/static/live-edit.js`:

1a. Add the API helper next to `approveTool` (after `live-edit.js:562`):

```javascript
  async function batchApprove(enabled) {
    if (!currentSessionId) return;
    try {
      await fetch(API_PREFIX + "/approve/" + currentSessionId + "/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch (e) {
      console.error("live-edit: batch approve error", e);
    }
  }
```

1b. In `addApprovalCard`, add a third button and its handler. Insert the button into the `.le-tool-actions` div and the listener after the reject-button listener:

```javascript
      <div class="le-tool-actions">
        <button class="le-btn le-btn-primary le-batch-approve">全部批准</button>
        <button class="le-btn le-btn-danger le-reject-btn">拒绝</button>
        <button class="le-btn le-btn-primary le-approve-btn">批准</button>
      </div>
```

```javascript
    card.querySelector(".le-batch-approve").addEventListener("click", () => {
      batchApprove(true);
      approveTool(event.id, true);
      card.querySelector(".le-tool-actions").innerHTML =
        '<span style="color:var(--le-success)">已批准，后续操作将自动执行 &#10003;</span>';
    });
```

- [ ] **Step 2: Add CSS**

Append to `live_edit/static/live-edit.css`:

```css
.le-btn.le-batch-approve {
  margin-right: 4px;
  opacity: 0.9;
}
```

- [ ] **Step 3: Manual verification**

Repeat the Task 4 manual setup. Request a change that touches **multiple files** (e.g. "把 app.txt 和 notes.txt 里的 old 改成 new"). Expected: the first approval card shows "全部批准". Clicking it approves the current op and every subsequent write automatically — subsequent ops appear as "执行中" cards, no further approval dialogs.

- [ ] **Step 4: Run full pytest suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add live_edit/static/live-edit.js live_edit/static/live-edit.css
git commit -m "feat(ui): batch approve button for quick mode"
```

---

### Task 8: One-click revert button on the timeline

**Files:**
- Modify: `live_edit/static/live-edit.js`
- Modify: `live_edit/static/live-edit.css`

**Interfaces:**
- Consumes: existing backend endpoints `POST /live-edit/revert/{commit_hash}/preview` and `POST /live-edit/revert/{commit_hash}/execute` (already tested in `tests/test_router.py::TestRevert`). Timeline entries already expose `commit_hash` and `is_live_edit` (`build_timeline` at `engine.py:269-312`, root entry has `is_live_edit: False`).
- No JS test infra; manual verification.

- [ ] **Step 1: Add the revert button to timeline entries**

In `live_edit/static/live-edit.js`, modify `showTimeline` (currently `live-edit.js:576-607`). After the `el.innerHTML` assignment and before `tl.appendChild(el)`, add a revert button for live-edit commits:

```javascript
      if (entry.commit_hash && entry.is_live_edit) {
        const btn = document.createElement("button");
        btn.className = "le-btn le-btn-danger le-revert-btn";
        btn.textContent = "撤销";
        btn.addEventListener("click", () => confirmRevert(entry.commit_hash, entry.message));
        el.appendChild(btn);
      }
```

- [ ] **Step 2: Add `confirmRevert` helper**

Add this function after `showTimeline`:

```javascript
  async function confirmRevert(commitHash, message) {
    if (!window.confirm("撤销这次修改？\n\n" + (message || "") + "\n\n这会回滚到该提交之前的 live-edit 改动。")) {
      return;
    }
    try {
      const previewResp = await fetch(API_PREFIX + "/revert/" + commitHash + "/preview", {
        method: "POST",
      });
      const preview = await previewResp.json();
      if (!preview.ok) {
        window.alert("撤销检查失败: " + (preview.error || "未知错误"));
        return;
      }
      if (!preview.can_revert) {
        window.alert("无法自动撤销：存在冲突。\n" + (preview.error || ""));
        return;
      }
      const detail = preview.diff_summary ? "\n\n影响文件:\n" + preview.diff_summary : "";
      if (!window.confirm("确认撤销？" + detail)) return;
      const execResp = await fetch(API_PREFIX + "/revert/" + commitHash + "/execute", {
        method: "POST",
      });
      const result = await execResp.json();
      if (result.ok) {
        window.alert("已成功撤销。");
        showTimeline();
      } else {
        window.alert("撤销失败: " + (result.error || "未知错误"));
      }
    } catch (e) {
      window.alert("撤销请求失败: " + e.message);
    }
  }
```

- [ ] **Step 3: Add CSS**

Append to `live_edit/static/live-edit.css`:

```css
.le-revert-btn {
  margin-top: 6px;
  font-size: 11px;
  padding: 2px 8px;
}
```

- [ ] **Step 4: Manual verification**

Repeat the Task 4 manual setup. Make two live-edit changes (each committed), then open the timeline (press `Ctrl+Shift+D` and view history). Expected: each live-edit entry shows a red "撤销" button; clicking it asks for confirmation, shows affected files, and after confirming, reloads the timeline with the revert commit on top. The initial commit entry has no button.

- [ ] **Step 5: Run full pytest suite + commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add live_edit/static/live-edit.js live_edit/static/live-edit.css
git commit -m "feat(ui): one-click revert button on timeline"
```

---

## Self-Review

**Spec coverage (three improvements):**
1. Approval card shows diff → Task 1 (`apply_edit`), Task 2 (`diff.py`), Task 3 (engine wiring), Task 4 (frontend card). ✔
2. One-click revert → Task 8 (frontend button over existing revert endpoints). ✔
3. Approval friction → Task 5 (`auto_approve` flag), Task 6 (batch endpoint), Task 7 (frontend button). ✔

**Placeholder scan:** No TBD/TODO; every code step has full code. Manual-verification tasks (4, 7, 8) include explicit setup commands and expected outcomes because the repo has no JS test infra.

**Type consistency:**
- `apply_edit(content, old, new) -> dict` defined in Task 1, consumed identically in Task 2.
- `compute_write_diff(tool_name, args, project_root) -> str` defined in Task 2, consumed in Task 3 with the `preview_diff` key read by Task 4's frontend.
- `EditSession.set_auto_approve(active: bool)` defined in Task 5, called by Task 6's endpoint and Task 7's frontend via HTTP.
- `preview_diff` key name consistent across Task 3 (backend emit) and Task 4 (frontend read).
- `session_store` param added at the end of `setup_live_edit` in Task 6; positional callers in existing tests unaffected.

**Frontend/backend contract check:** The `tool_plan` `auto=True` flag consumed by the existing frontend handler (`live-edit.js:261-267`) predates Task 5; Task 7 relies on that existing path and adds no new handler.

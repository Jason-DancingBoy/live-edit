# Critic Agent & delete_file Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the `introspect` eval stage into a fresh-context, read-only critic agent that returns structured findings, and add a safety-gated `delete_file` tool.

**Architecture:** A new `live_edit/critic.py` module runs a small isolated agent loop (own messages, read-only qa tools minus write tools, ≤2 explore rounds) that returns a JSON verdict. `evaluation.py`'s `_run_stage_introspect` calls it; critical/high findings fail the stage and reuse the existing fix loop untouched. A new `delete_file` tool applies a 3-tier policy (session-created files deletable, pre-existing source protected). Only three small edits touch the engine.

**Tech Stack:** Python 3.10+, asyncio, existing Provider/ToolRegistry/VCS abstractions, pytest + pytest-asyncio.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-critic-agent-design.md` (committed `8471f2f`).
- Stage id stays `introspect` — do NOT rename; config `stages` defaults `["lint", "test", "introspect"]` and `.toml` files must keep working.
- Critic must never receive write tools — the tool list is always qa-visible tools **minus** `get_write_tool_names("qa")`.
- System prompts go in `{"role": "user"}` messages (project convention, see engine.py:684) — there is no system param in the provider.
- Fail-open philosophy: provider error / invalid JSON after retry / timeout → return an empty `CriticVerdict(goal_achieved=True)`, never raise.
- Chinese user-facing text; English docstrings/comments (match `evaluation.py` / `engine.py` style).
- No new dependencies; `subprocess` for git checks, `asyncio.wait_for` not needed inside tools (registry wraps timeout).

---

### Task 1: `live_edit/critic.py` — critic agent core

**Files:**
- Create: `live_edit/critic.py`
- Test: `tests/test_critic.py`

**Interfaces:**
- Produces:
  - `@dataclass CriticFinding: severity: str; file: str; line: int | None = None; description: str = ""`
  - `@dataclass CriticVerdict: goal_achieved: bool; findings: list[CriticFinding] = field(default_factory=list); summary: str = ""` with property `blocking: bool` (True if any finding severity in `("critical", "high")`)
  - `async def run_critic_agent(*, provider, tool_registry, worktree_path: str, user_request: str, diff: str, max_rounds: int = 2, is_cancelled: Callable[[], bool] | None = None) -> CriticVerdict`
  - `def _build_critic_tools(tool_registry) -> list[dict]` (read-only schemas)
- Consumes: `provider.call_with_tools(messages, tools, on_thinking, on_text) -> list[dict]` where each dict has `type` in `{"text","thinking","tool_use"}` (see provider.py:111-164); `tool_registry.get_tools("qa")`, `tool_registry.get_write_tool_names("qa")`, `tool_registry.execute(name, args, worktree_path, None)`.
- Later tasks rely on: `run_critic_agent` and `CriticVerdict.blocking` (Task 5).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_critic.py`:

```python
"""Tests for live_edit/critic.py"""

import json

import pytest

from live_edit.critic import (
    CriticFinding,
    CriticVerdict,
    _build_critic_tools,
    _parse_verdict_text,
    run_critic_agent,
)


class FakeProvider:
    """Returns scripted content_blocks. A block is a dict or list of dicts."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.tools_seen = []

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools))
        if not self.script:
            return [{"type": "text", "text": VERDICT_JSON}]
        item = self.script.pop(0)
        return item if isinstance(item, list) else [item]


class FakeToolRegistry:
    def __init__(self, tools, write_names):
        self._tools = tools
        self._write = set(write_names)
        self.executed = []

    def get_tools(self, mode):
        return [dict(t) for t in self._tools]

    def get_write_tool_names(self, mode):
        return set(self._write)

    async def execute(self, name, args, root, config=None):
        self.executed.append((name, args))
        return {"ok": True, "name": name}


VERDICT_JSON = json.dumps(
    {"goal_achieved": True, "summary": "ok", "findings": []}, ensure_ascii=False
)
BLOCKING_JSON = json.dumps(
    {
        "goal_achieved": True,
        "summary": "caller mismatch",
        "findings": [
            {"severity": "high", "file": "src/a.py", "line": 5, "description": "broken caller"}
        ],
    },
    ensure_ascii=False,
)


def read_tool():
    return {"name": "read_file", "input": {"path": "a.py"}}


class TestCriticVerdict:
    def test_blocking_true_for_critical_and_high(self):
        v = CriticVerdict(goal_achieved=True, findings=[
            CriticFinding(severity="high", file="a.py", description="x"),
        ])
        assert v.blocking is True

    def test_blocking_false_for_low_and_clean(self):
        v = CriticVerdict(goal_achieved=True, findings=[
            CriticFinding(severity="low", file="a.py", description="nit"),
        ])
        assert v.blocking is False
        assert CriticVerdict(goal_achieved=True).blocking is False


class TestBuildCriticTools:
    def test_excludes_write_tools(self):
        reg = FakeToolRegistry(
            tools=[
                {"name": "read_file", "x": 1},
                {"name": "delete_file", "y": 2},
                {"name": "write_file", "z": 3},
            ],
            write_names=["delete_file", "write_file"],
        )
        names = [t["name"] for t in _build_critic_tools(reg)]
        assert names == ["read_file"]

    def test_none_registry_returns_empty(self):
        assert _build_critic_tools(None) == []


class TestParseVerdictText:
    def test_plain_json(self):
        v = _parse_verdict_text(VERDICT_JSON)
        assert v.goal_achieved is True
        assert v.findings == []

    def test_markdown_fence_stripped(self):
        v = _parse_verdict_text(f"```json\n{BLOCKING_JSON}\n```")
        assert v.blocking is True
        assert v.findings[0].file == "src/a.py"
        assert v.findings[0].line == 5

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_verdict_text("not json at all")


@pytest.mark.asyncio
class TestRunCriticAgent:
    async def test_single_verdict_round(self):
        provider = FakeProvider([[{"type": "text", "text": VERDICT_JSON}]])
        verdict = await run_critic_agent(
            provider=provider,
            tool_registry=FakeToolRegistry([read_tool()], []),
            worktree_path="/tmp/wt",
            user_request="fix it",
            diff="--- a\n+++ b",
        )
        assert verdict.goal_achieved is True
        assert len(provider.calls) == 1

    async def test_explore_then_verdict(self):
        provider = FakeProvider(
            [
                [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}],
                [{"type": "text", "text": BLOCKING_JSON}],
            ]
        )
        reg = FakeToolRegistry([read_tool()], [])
        verdict = await run_critic_agent(
            provider=provider, tool_registry=reg, worktree_path="/tmp/wt",
            user_request="fix", diff="diff",
        )
        assert verdict.blocking is True
        assert reg.executed == [("read_file", {"path": "a.py"})]
        # round 1 must pass the read-only tools; no write tools ever sent
        assert all(t["name"] not in ("write_file", "delete_file") for t in provider.tools_seen[0])

    async def test_invalid_json_retries_once(self):
        provider = FakeProvider(
            [
                [{"type": "text", "text": "not json"}],
                [{"type": "text", "text": VERDICT_JSON}],
            ]
        )
        verdict = await run_critic_agent(
            provider=provider, tool_registry=None, worktree_path="/tmp/wt",
            user_request="fix", diff="d",
        )
        assert verdict.goal_achieved is True
        assert len(provider.calls) == 2

    async def test_fail_open_on_provider_error(self):
        class Boom:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                raise RuntimeError("api down")

        verdict = await run_critic_agent(
            provider=Boom(), tool_registry=None, worktree_path="/tmp/wt",
            user_request="fix", diff="d",
        )
        assert verdict.goal_achieved is True
        assert verdict.findings == []

    async def test_rounds_exhausted_forces_verdict(self):
        # model keeps asking for tools beyond max_rounds -> forced tools=[] final call
        provider = FakeProvider(
            [
                [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}}],
                [{"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b"}}],
            ]
        )
        reg = FakeToolRegistry([read_tool()], [])
        verdict = await run_critic_agent(
            provider=provider, tool_registry=reg, worktree_path="/tmp/wt",
            user_request="fix", diff="d", max_rounds=1,
        )
        assert verdict.goal_achieved is True
        # last call used tools=[] to force a text verdict
        assert provider.tools_seen[-1] == []

    async def test_cancelled_returns_empty_verdict(self):
        provider = FakeProvider([[{"type": "text", "text": BLOCKING_JSON}]])
        verdict = await run_critic_agent(
            provider=provider, tool_registry=None, worktree_path="/tmp/wt",
            user_request="fix", diff="d", is_cancelled=lambda: True,
        )
        assert verdict.goal_achieved is True
        assert verdict.findings == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_critic.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.critic'`

- [ ] **Step 3: Write the implementation**

Create `live_edit/critic.py`:

```python
"""Fresh-context, read-only critic agent for the introspect eval stage."""

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("live-edit.critic")

_BLOCKING_SEVERITIES = ("critical", "high")
_DIFF_LIMIT = 4000


@dataclass
class CriticFinding:
    severity: str
    file: str
    line: int | None = None
    description: str = ""


@dataclass
class CriticVerdict:
    goal_achieved: bool
    findings: list[CriticFinding] = field(default_factory=list)
    summary: str = ""

    @property
    def blocking(self) -> bool:
        return any(f.severity in _BLOCKING_SEVERITIES for f in self.findings)


def _build_critic_tools(tool_registry) -> list[dict]:
    """Read-only tool schemas: all qa-visible tools minus any write tool.

    qa-visible tools alone are NOT sufficient once a write tool declares
    modes=None (all modes). Explicitly exclude write tools so the critic can
    never mutate the codebase.
    """
    if tool_registry is None:
        return []
    write_names = tool_registry.get_write_tool_names("qa")
    return [t for t in tool_registry.get_tools("qa") if t["name"] not in write_names]


def _build_critic_messages(user_request: str, diff: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                "你是独立的代码审查 agent。你只有只读工具（读文件/搜索/glob），"
                "可以核验代码库，但不能修改任何文件。\n\n"
                "用户需求：\n"
                f"{user_request}\n\n"
                "改动 diff：\n"
                f"```diff\n{(diff or '')[:_DIFF_LIMIT]}\n```\n\n"
                "你的任务：判断改动是否达成用户目标，并找出会直接破坏功能的致命 bug。\n"
                "严重度标准：\n"
                "  critical/high —— 未达成用户目标；未定义引用；明显逻辑错误；会导致崩溃或功能破坏。\n"
                "  medium/low —— 仅当明确有价值时才写（命名、小瑕疵），不要为挑刺而挑刺。\n"
                "先用只读工具核验（读改动文件、查调用方）。"
                "最后一轮只输出一个 JSON 对象，不要输出其他文字：\n"
                '{"goal_achieved": true, "summary": "一句话结论", '
                '"findings": [{"severity": "high", "file": "src/api.py", "line": 45, '
                '"description": "问题描述"}]}'
            ),
        }
    ]


def _parse_verdict_text(text: str) -> CriticVerdict:
    """Parse the model's text as JSON (stripping a markdown fence). Raises ValueError."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    data = json.loads(stripped)  # raises ValueError/JSONDecodeError on malformed
    findings = []
    for item in data.get("findings", []):
        findings.append(
            CriticFinding(
                severity=str(item.get("severity", "low")),
                file=str(item.get("file", "")),
                line=item.get("line"),
                description=str(item.get("description", "")),
            )
        )
    return CriticVerdict(
        goal_achieved=bool(data.get("goal_achieved", True)),
        findings=findings,
        summary=str(data.get("summary", "")),
    )


def _empty_verdict(reason: str = "") -> CriticVerdict:
    return CriticVerdict(goal_achieved=True, summary=reason)


async def run_critic_agent(
    *,
    provider,
    tool_registry,
    worktree_path: str,
    user_request: str,
    diff: str,
    max_rounds: int = 2,
    is_cancelled: Callable[[], bool] | None = None,
) -> CriticVerdict:
    """Run a fresh-context, read-only review and return a structured verdict.

    Fail-open: any infra/format failure yields an empty passing verdict, never
    an exception. Blocking is decided by the caller from verdict.blocking.
    """
    if not diff:
        return _empty_verdict("no diff")

    tools = _build_critic_tools(tool_registry)
    messages = _build_critic_messages(user_request, diff)

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    for _ in range(max_rounds):
        if cancelled():
            return _empty_verdict("critic cancelled")
        try:
            content_blocks = await provider.call_with_tools(
                messages=messages, tools=tools, on_thinking=None, on_text=None
            )
        except Exception as e:
            logger.warning("Critic provider error (fail-open): %s", e)
            return _empty_verdict(f"critic error: {e}")

        if not content_blocks:
            return _empty_verdict("critic: no LLM response")

        tool_uses = []
        assistant_content = []
        text_parts = []
        for block in content_blocks:
            if block is None:
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                assistant_content.append({"type": "text", "text": block.get("text", "")})
            elif btype == "thinking":
                assistant_content.append(
                    {"type": "thinking", "thinking": block.get("thinking", "")}
                )
            elif btype == "tool_use":
                tool_uses.append(block)
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                )

        if not tool_uses:
            try:
                return _parse_verdict_text("".join(text_parts))
            except (ValueError, json.JSONDecodeError):
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append(
                    {
                        "role": "user",
                        "content": "你刚才的输出不是合法 JSON。请重新输出，只输出一个 JSON 对象，不要其他文字。",
                    }
                )
                continue  # one correction round

        # Execute read-only tools (tool_registry is non-None here: with tools=[],
        # the model cannot emit tool_use).
        tool_results = []
        for tool in tool_uses:
            exec_result = await tool_registry.execute(
                tool["name"], tool.get("input", {}), worktree_path, None
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool["id"],
                    "content": [
                        {"type": "text", "text": json.dumps(exec_result, ensure_ascii=False)}
                    ],
                }
            )
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

    # Rounds exhausted while still exploring: force a text verdict with no tools.
    try:
        content_blocks = await provider.call_with_tools(
            messages=messages, tools=[], on_thinking=None, on_text=None
        )
        text = "".join(
            b.get("text", "") for b in (content_blocks or []) if b and b.get("type") == "text"
        )
        return _parse_verdict_text(text)
    except Exception as e:
        logger.warning("Critic final-verdict error (fail-open): %s", e)
        return _empty_verdict(f"critic error: {e}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_critic.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/jason/agent/live-edit
git add live_edit/critic.py tests/test_critic.py
git commit -m "feat(critic): fresh-context read-only critic agent

Upgrade the introspect review path: run_critic_agent() spins up an
isolated read-only loop that verifies against the codebase and returns a
structured CriticVerdict. Fail-open on provider/parse errors.
"
```

---

### Task 2: `delete_file` tool

**Files:**
- Create: `live_edit/builtin_tools/delete_file.py`
- Modify: `live_edit/builtin_tools/__init__.py:3,5` (import + ALL_MODULES)
- Modify: `live_edit/engine.py:43-56` (`_DEFAULT_ERROR_MAP["quick"]` — add delete-specific mapping)
- Test: `tests/test_delete_file.py`

**Interfaces:**
- Produces: builtin tool `delete_file` (name, `is_write=True`), registered via `create() -> ToolDef`. `execute(args, project_root, config) -> dict` returns `{"ok": True, "path", "deleted": True, "size"}` or `{"ok": False, "error": "..."}`.
- Consumes: `safe_path(rel_path, project_root)` (raises `ValueError` on escape), `check_write_allowed(path, project_root, allow_overwrite, overwrite_dirs)`, `config.safety.overwrite_allowed_dirs`, `config.safety.allow_overwrite_existing`.
- Later tasks rely on: `delete_file` being visible to `tool_registry.get_write_tool_names("qa")` (Task 1's filter excludes it from the critic).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_delete_file.py`:

```python
"""Tests for live_edit/builtin_tools/delete_file.py"""

import subprocess

import pytest

from live_edit.builtin_tools import delete_file as df


class FakeSafety:
    def __init__(self, allow_overwrite=False):
        self.overwrite_allowed_dirs = ["static", "public", "assets"]
        self.allow_overwrite_existing = allow_overwrite


class FakeConfig:
    def __init__(self, allow_overwrite=False):
        self.safety = FakeSafety(allow_overwrite)


def _init_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
    )


def _tracked_file(tmp_path, rel, content="x\n"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", f"add {rel}"],
        cwd=str(tmp_path),
        check=True,
    )
    return p


@pytest.mark.asyncio
class TestDeleteFile:
    async def test_delete_new_file_ok(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "new.txt").write_text("hi")
        result = await df.execute({"path": "new.txt"}, str(tmp_path), FakeConfig())
        assert result["ok"] is True
        assert not (tmp_path / "new.txt").exists()

    async def test_delete_tracked_source_file_blocked(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "src/utils.py")
        result = await df.execute({"path": "src/utils.py"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "受保护" in result["error"] or "覆写" in result["error"]
        assert (tmp_path / "src/utils.py").exists()

    async def test_delete_tracked_overwrite_dir_ok(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "static/app.css")
        result = await df.execute({"path": "static/app.css"}, str(tmp_path), FakeConfig())
        assert result["ok"] is True
        assert not (tmp_path / "static/app.css").exists()

    async def test_delete_tracked_with_allow_overwrite_ok(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "src/utils.py")
        result = await df.execute({"path": "src/utils.py"}, str(tmp_path), FakeConfig(allow_overwrite=True))
        assert result["ok"] is True

    async def test_delete_missing_file_errors(self, tmp_path):
        _init_git(tmp_path)
        result = await df.execute({"path": "nope.py"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "不存在" in result["error"]

    async def test_delete_directory_refused(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "adir").mkdir()
        result = await df.execute({"path": "adir"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "目录" in result["error"]

    async def test_delete_escaped_path_blocked(self, tmp_path):
        _init_git(tmp_path)
        result = await df.execute({"path": "../outside.txt"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False

    async def test_tool_def_is_write(self):
        td = df.create()
        assert td.name == "delete_file"
        assert td.is_write is True
        assert "path" in td.input_schema["required"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_delete_file.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.builtin_tools.delete_file'`

- [ ] **Step 3: Write the implementation**

Create `live_edit/builtin_tools/delete_file.py`:

```python
"""Delete a file tool with a conservative 3-tier write policy."""

import os
import subprocess

from ..safety import check_write_allowed, safe_path
from ..tool_registry import ToolDef


def _exists_in_head(project_root: str, rel_path: str) -> bool:
    """True if rel_path exists in the worktree's HEAD commit tree.

    Files NOT in HEAD were created this session (or are untracked) and are
    deletable; committed files are treated as protected source.
    """
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "ls-tree", "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True  # conservative: on git error, treat as pre-existing


async def execute(args: dict, project_root: str, config=None) -> dict:
    rel_path = args["path"]
    abs_path = safe_path(rel_path, project_root)  # raises ValueError on escape
    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"文件不存在: {rel_path}"}
    if os.path.isdir(abs_path):
        return {"ok": False, "error": f"不能删除目录: {rel_path}"}

    # 3-tier policy (spec §2): session-created/untracked → deletable;
    # otherwise mirror write_file's protection.
    if _exists_in_head(project_root, rel_path):
        overwrite_dirs = None
        allow_overwrite = False
        if config and hasattr(config, "safety"):
            overwrite_dirs = getattr(config.safety, "overwrite_allowed_dirs", None)
            allow_overwrite = getattr(config.safety, "allow_overwrite_existing", False)
        err = check_write_allowed(rel_path, project_root, allow_overwrite, overwrite_dirs)
        if err:
            return {"ok": False, "error": f"删除受保护文件被拒绝：{err}"}

    size = os.path.getsize(abs_path)
    os.remove(abs_path)
    return {"ok": True, "path": rel_path, "deleted": True, "size": size}


def create() -> ToolDef:
    return ToolDef(
        name="delete_file",
        description="删除一个文件（不支持目录）。新建/未跟踪文件可删；"
        "受保护的既有文件需配置 allow_overwrite_existing=true 或在 overwrite_allowed_dirs 内。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "reason": {"type": "string", "description": "删除原因（向用户解释）"},
            },
            "required": ["path"],
        },
        execute=execute,
        is_write=True,
    )
```

Modify `live_edit/builtin_tools/__init__.py` — line 3 import and line 5 ALL_MODULES:

```python
from . import delete_file, edit_file, glob, list_dir, read_file, run_shell, search_code, write_file

ALL_MODULES = [read_file, search_code, glob, list_dir, edit_file, write_file, delete_file, run_shell]
```

Modify `live_edit/engine.py` `_DEFAULT_ERROR_MAP` (line ~44-53) — add after the `"write_file 只能覆写"` line:

```python
        "删除受保护文件被拒绝": "该文件受保护，不能删除。新建的文件可以删，既有源码需要在配置里放开",
```

Also modify `.live-edit.toml` `[errors.quick]` — add the same key after the `"write_file 只能覆写"` mapping (line ~51). `translate_error` REPLACES the default map when a custom `[errors.quick]` exists (engine.py:71), so both are required:

```toml
"删除受保护文件被拒绝" = "该文件受保护，不能删除。新建的文件可以删，既有源码需要在配置里放开"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_delete_file.py -q`
Expected: PASS (8 tests)

Also confirm the registry still loads (the delete_file import shouldn't break the conftest registry):
Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_tool_registry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/jason/agent/live-edit
git add live_edit/builtin_tools/delete_file.py live_edit/builtin_tools/__init__.py live_edit/engine.py tests/test_delete_file.py
git commit -m "feat(tools): add safety-gated delete_file tool

3-tier policy: session-created/untracked files deletable, pre-existing
source protected via existing write rules. is_write=True so quick mode
gates it per-tool.
"
```

---

### Task 3: delete preview + summary support

**Files:**
- Modify: `live_edit/diff.py:52-53` (`compute_write_diff` — add delete branch)
- Modify: `live_edit/tools.py:36-52` (`_tool_summary` — add delete branch)
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `compute_write_diff(tool_name, args, project_root)` (diff.py:31), `diff_text(old, new, filename)` (diff.py:9), `_tool_summary(name, args)` (tools.py:24).
- Produces: `compute_write_diff` handles `"delete_file"` → returns the file's content as an all-removed diff; `_tool_summary("delete_file", args)` → `"删除 <path>"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diff.py`:

```python
class TestDeleteFileDiff:
    def test_delete_file_returns_full_removal_diff(self, tmp_path):
        from live_edit.diff import compute_write_diff

        p = tmp_path / "gone.py"
        p.write_text("print('bye')\n")
        d = compute_write_diff("delete_file", {"path": "gone.py"}, str(tmp_path))
        assert "-print('bye')" in d
        assert "+print('bye')" not in d
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_diff.py::TestDeleteFileDiff -q`
Expected: FAIL (diff is empty for delete_file today — the function returns `""` for unknown tools)

- [ ] **Step 3: Write the implementation**

Modify `live_edit/diff.py` `compute_write_diff` — after the `write_file` branch (line ~52-53), add:

```python
    if tool_name == "delete_file":
        return diff_text(current, "", path)
```

Modify `live_edit/tools.py` `_tool_summary` — after the `edit_file` branch, add:

```python
    elif name in ("delete_file", "Delete"):
        return f"删除 {path}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_diff.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/jason/agent/live-edit
git add live_edit/diff.py live_edit/tools.py tests/test_diff.py
git commit -m "feat(ui): delete_file approval preview and summary
"
```

---

### Task 4: `critic_max_rounds` config

**Files:**
- Modify: `live_edit/config.py:105` (add field after `preview_pages`) and `live_edit/config.py:369` (parse)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `EvaluationConfig.critic_max_rounds: int = 2`; parsed from `[evaluation] critic_max_rounds` with default `2`.
- Consumes: existing `_parse_config`/dataclass pattern in config.py.
- Later tasks rely on: `config.evaluation.critic_max_rounds` (Task 5).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
class TestCriticMaxRounds:
    def test_default_is_2(self):
        from live_edit.config import EvaluationConfig

        assert EvaluationConfig().critic_max_rounds == 2

    def test_parses_from_toml(self):
        from live_edit.config import load_config

        cfg = load_config(
            path="",
            raw={
                "evaluation": {"critic_max_rounds": 4},
            },
        )
        assert cfg.evaluation.critic_max_rounds == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_config.py::TestCriticMaxRounds -q`
Expected: FAIL (`EvaluationConfig` has no attribute `critic_max_rounds`)

- [ ] **Step 3: Write the implementation**

Modify `live_edit/config.py` — add to `EvaluationConfig` dataclass (after `preview_pages`, line ~106):

```python
    critic_max_rounds: int = 2
```

Modify the `EvaluationConfig(...)` constructor in the parse function (after `preview_pages=...`, line ~369):

```python
        critic_max_rounds=eval_data.get("critic_max_rounds", 2),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_config.py::TestCriticMaxRounds tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/jason/agent/live-edit
git add live_edit/config.py tests/test_config.py
git commit -m "feat(config): critic_max_rounds knob
"
```

---

### Task 5: wire the critic into the evaluation pipeline + engine

**Files:**
- Modify: `live_edit/evaluation.py:244` (`run_evaluation_pipeline` signature + introspect runner lambda ~254), `live_edit/evaluation.py:169-203` (replace `_run_stage_introspect` body)
- Modify: `live_edit/engine.py:1064` (pass `tool_registry`)
- Test: `tests/test_evaluation.py` (update two introspect fakes + add threading/severity tests)

**Interfaces:**
- Consumes: `run_critic_agent` and `CriticVerdict` from Task 1; `config.evaluation.critic_max_rounds` from Task 4.
- Produces: `run_evaluation_pipeline(session, provider, config, preview_manager=None, tool_registry=None)`; `_run_stage_introspect(provider, user_request, diff, *, worktree_path="", tool_registry=None, critic_max_rounds=2, is_cancelled=None) -> dict` returning `{"ok": bool, "output": str}`.
- Later: engine passes `tool_registry` so the real (non-test) path runs the full critic.

- [ ] **Step 1: Update the existing introspect/pipeline fakes**

In `tests/test_evaluation.py`, `test_skipped_stage_continues` (line ~122), change the fake signature to swallow new kwargs:

```python
        async def _introspect(provider, req, diff, **kwargs):
            return {"ok": True, "output": ""}
```

Also add `tool_registry=None` to the two `run_evaluation_pipeline(...)` calls in `TestPipelineThreeState` (lines ~134, ~171) so the new optional param is exercised:

```python
        result = await run_evaluation_pipeline(sess, None, cfg, tool_registry=None)
```

In `tests/test_engine.py`, the engine's call site will now pass `tool_registry=` (a new keyword). Update the **three** `_fake_pipeline` definitions (lines ~1712, ~1754, ~1795) to accept it — change each signature from:

```python
        async def _fake_pipeline(session, provider, config, preview_manager=None):
```
to:
```python
        async def _fake_pipeline(session, provider, config, preview_manager=None, tool_registry=None):
```

- [ ] **Step 2: Write the new failing tests**

Append to `tests/test_evaluation.py`:

```python
class TestIntrospectStage:
    async def test_blocking_findings_fail_stage(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": (
                            '{"goal_achieved": true, "summary": "x", '
                            '"findings": [{"severity": "high", "file": "a.py", '
                            '"line": 5, "description": "broken caller"}]}'
                        ),
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "user wants batch delete", "diff",
            worktree_path="/tmp/wt", tool_registry=None, critic_max_rounds=2,
        )
        assert result["ok"] is False
        assert "[high]" in result["output"]

    async def test_goal_not_achieved_fails_stage(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": (
                            '{"goal_achieved": false, "summary": "batch delete missing", "findings": []}'
                        ),
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "add batch delete", "diff",
            worktree_path="/tmp/wt", tool_registry=None,
        )
        assert result["ok"] is False

    async def test_clean_verdict_passes(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": '{"goal_achieved": true, "summary": "ok", "findings": []}',
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "fix it", "diff",
            worktree_path="/tmp/wt", tool_registry=None,
        )
        assert result["ok"] is True

    async def test_pipeline_threads_tool_registry(self, monkeypatch):
        import live_edit.evaluation as ev

        class FakeSession:
            def __init__(self):
                self._worktree_path = "/tmp/fake"
                self._preview_url = ""
                self.request = "fix it"
                self._cached_diff = "diff"
                self.events = []

            def emit(self, event_type, **data):
                self.events.append({"type": event_type, **data})

        seen = {}

        async def _introspect(provider, req, diff, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ev, "_run_stage_introspect", _introspect)

        from live_edit.config import Config, EvaluationConfig, PreviewConfig

        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["introspect"]),
            preview=PreviewConfig(enabled=False),
        )
        sess = FakeSession()
        await ev.run_evaluation_pipeline(sess, None, cfg, tool_registry="REG")
        assert seen.get("tool_registry") == "REG"
        assert seen.get("worktree_path") == "/tmp/fake"
        assert seen.get("critic_max_rounds") == 2
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_evaluation.py -q`
Expected: FAIL — new tests error (`_run_stage_introspect` doesn't accept `worktree_path`/`tool_registry`, and the old fake receives unexpected kwargs).

- [ ] **Step 4: Write the implementation**

Modify `live_edit/evaluation.py` — replace `_run_stage_introspect` (lines 169-203):

```python
async def _run_stage_introspect(
    provider,
    user_request: str,
    diff: str,
    *,
    worktree_path: str = "",
    tool_registry=None,
    critic_max_rounds: int = 2,
    is_cancelled=None,
) -> dict:
    """Ask a fresh-context critic agent whether the changes achieved the goal."""
    if not diff:
        return {"ok": True, "output": "No diff to introspect"}
    try:
        verdict = await run_critic_agent(
            provider=provider,
            tool_registry=tool_registry,
            worktree_path=worktree_path,
            user_request=user_request,
            diff=diff,
            max_rounds=critic_max_rounds,
            is_cancelled=is_cancelled,
        )
    except Exception as e:
        return {"ok": True, "output": f"Critic error (treated as pass): {e}"}

    if verdict.goal_achieved and not verdict.blocking:
        note = f"审查通过：{verdict.summary}" if verdict.summary else "审查通过"
        return {"ok": True, "output": note}

    reason = "改动未达成用户目标" if not verdict.goal_achieved else "存在致命问题"
    lines = [f"审查未通过：{reason}"]
    for f in verdict.findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"- [{f.severity}] {loc} — {f.description}")
    return {"ok": False, "output": "\n".join(lines)}
```

Modify `run_evaluation_pipeline` signature (line 244):

```python
async def run_evaluation_pipeline(session, provider, config, preview_manager=None, tool_registry=None) -> EvalResult:
```

Modify the introspect runner lambda (lines 254-256):

```python
        "introspect": lambda: _run_stage_introspect(
            provider,
            session.request,
            getattr(session, "_cached_diff", ""),
            worktree_path=session._worktree_path,
            tool_registry=tool_registry,
            critic_max_rounds=(
                config.evaluation.critic_max_rounds if hasattr(config, "evaluation") else 2
            ),
            is_cancelled=(
                (lambda: session._cancelled.is_set())
                if getattr(session, "_cancelled", None) is not None
                else None
            ),
        ),
```

Add the import at the top of `evaluation.py` (next to the other imports, after `from dataclasses import ...`):

```python
from .critic import run_critic_agent
```

Modify `live_edit/engine.py` — the `run_evaluation_pipeline` call (line 1064):

```python
                eval_result = await run_evaluation_pipeline(
                    session=session,
                    provider=provider,
                    config=config,
                    preview_manager=preview_manager,
                    tool_registry=tool_registry,
                )
```

- [ ] **Step 5: Run the full evaluation test module to verify all pass**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest tests/test_evaluation.py tests/test_critic.py -q`
Expected: PASS (old + new tests)

- [ ] **Step 6: Run the whole suite**

Run: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest -q`
Expected: PASS (full suite)

- [ ] **Step 7: Commit**

```bash
cd /home/jason/agent/live-edit
git add live_edit/evaluation.py live_edit/engine.py tests/test_evaluation.py
git commit -m "feat(eval): run critic agent in introspect stage

Thread tool_registry into the eval pipeline; introspect now calls
run_critic_agent and fails only on critical/high findings or unmet goals.
"
```

---

## Post-implementation verification

- [ ] Full suite green: `cd /home/jason/agent/live-edit && .venv/bin/python -m pytest -q`
- [ ] Manual smoke: `live_edit init`-style project — request "把新建的 README.md 删掉" in quick mode; delete_file triggers approval with a full-removal preview; after approval the file is gone and the staged diff shows the deletion.
- [ ] Manual smoke: request "给 src/api.py 加批量删除接口，调用方全部同步" with a deliberately wrong call site; critic flags it as a high finding; fix loop repairs; second eval passes.

# Tool Registry + Evaluation Pipeline Design

## Overview

Two new subsystems for live-edit, implemented together because evaluation uses tools from the registry.

1. **Plugin-based Tool Registry** — replace hardcoded `TOOLS` list with a dynamic registry supporting three tool sources (built-in, TOML config, Python plugins).
2. **Evaluation Pipeline** — post-edit verification with staged checks (lint → test → preview → introspection → HTML diff) and auto-retry on failure.

---

## Part 1: Tool Registry

### Core abstraction

New protocol `ToolRegistry` in `live_edit/tool_registry.py`, following the existing pattern of `Provider`/`Storage`/`VCS`:

```
ToolRegistry
  register(tool: ToolDef) -> None
  get_tools(mode: str) -> list[dict]       # Anthropic-format schemas
  execute(name, args, project_root, config) -> dict
  list_tools(mode: str) -> list[str]
```

### Tool sources (priority low → high)

| Priority | Source | Location | Format | Loaded |
|----------|--------|----------|--------|--------|
| 1 (low) | Built-in | `live_edit/builtin_tools/` | Python module per tool | Startup |
| 2 (mid) | TOML config | `.live-edit.toml` `[[tools]]` | Shell command declaration | Config parse |
| 3 (high) | Python plugins | `live_edit_tools/` (project dir) | `@tool` decorator | Startup auto-discover |

Higher priority overrides lower when tool names collide.

### Built-in tools migration

Current `tools.py` monolithic file split into `live_edit/builtin_tools/`:

```
live_edit/builtin_tools/
  __init__.py
  read_file.py
  search_code.py
  glob.py
  list_dir.py
  edit_file.py
  write_file.py
  run_shell.py
```

Each module exports a `ToolDef` (name, description, input_schema, execute function, modes). The registry imports them all at startup.

Safety functions (`_safe_path`, `_check_shell_cmd`, `_check_write_allowed`) move to `live_edit/safety.py` so both built-in tools and the registry can use them.

### TOML config tools

```toml
[[tools]]
name = "run_migration"
description = "运行数据库迁移"
command = "python manage.py migrate"
modes = ["deep"]
require_approval = true
timeout = 60
```

- `command` is executed via `subprocess.run(shell=True)` with safety checks applied
- `modes` controls which agent modes can see the tool (omit = all modes)
- `require_approval` controls whether the tool needs user approval in quick mode
- `timeout` defaults to 30s

### Python plugin tools

A project creates `live_edit_tools/` directory at project root. Files inside are auto-discovered:

```python
# live_edit_tools/db_tools.py
from live_edit.tool_registry import tool


@tool(name="db_schema", description="查看数据库表结构", modes=["deep", "qa"])
async def db_schema(args, project_root, config):
    import subprocess

    result = subprocess.run(["python", "manage.py", "inspectdb"], ...)
    return {"ok": True, "tables": result.stdout}
```

- `@tool` decorator registers the function into the global registry at import time
- Function signature: `async def fn(args: dict, project_root: str, config) -> dict`
- Return format `{"ok": True/False, ...}` matches existing `execute_tool()` convention
- `modes` in decorator controls visibility per mode

### Mode filtering

- Built-in tools keep existing mode logic (qa = read-only subset)
- TOML tools respect the `modes` field (empty = all modes)
- Python plugin tools respect the `modes` decorator parameter
- `ToolRegistry.get_tools(mode)` returns the union of all tools visible in that mode

### Dependency injection

`setup_live_edit()` in `router.py` accepts an optional `tool_registry` parameter. When not provided, creates a default `ToolRegistry` with built-in tools loaded.

---

## Part 2: Evaluation Pipeline

### Pipeline stages

After agent completes all edits, before showing diff to user:

```
Agent edits complete
  |
  Stage 1: Lint / compile check
  |-- pass --> Stage 2
  |-- fail --> inject error into agent loop, retry (max 3)
  |
  Stage 2: Test execution
  |-- pass --> Stage 3
  |-- fail --> inject failure log, retry
  |
  Stage 3: Preview health check
  |-- pass --> Stage 4
  |-- fail --> inject error, retry
  |
  Stage 4: LLM introspection
  |-- pass (LLM says "achieved") --> Stage 5
  |-- fail (LLM says "issues") --> inject introspection, retry
  |
  Stage 5: HTML structure diff
  |-- pass --> show diff to user, wait for approval, commit
  |-- fail --> inject diff anomalies, retry

After 3 failed retries: show current state + eval report to user, let them decide
```

### Stage implementations

| Stage | How | Command detection |
|-------|-----|-------------------|
| Lint | `subprocess.run` in worktree | Auto-detect: Python→`python -m py_compile`, Node→`npm run lint --if-present`, Go→`go vet ./...` |
| Test | `subprocess.run` in worktree | Auto-detect: Python→`pytest`, Node→`npm test`, Go→`go test ./...` |
| Preview health | `httpx.get(preview_url + "/live-edit/health")` | Uses existing PreviewManager |
| Introspection | Provider call (no tools, text-only) | Prompt: "Given the user's request X and the diff Y, did the changes achieve the goal? Are there omissions?" |
| HTML diff | `httpx.get(preview_url + page)` before/after edits | Compare DOM structure: tag count diff, significant text changes |

### Screenshot (optional, opt-in)

When `evaluation.screenshot = true` in config:
- Lazily import playwright only when this stage runs
- Take screenshot of configured pages before and after edits
- Send both screenshots to the LLM for visual comparison
- If playwright is not installed, log warning and skip

### Configuration

New `[evaluation]` section in `.live-edit.toml`:

```toml
[evaluation]
enabled = true
max_retries = 3
stages = ["lint", "test", "preview", "introspect", "html_diff"]
test_command = ""       # override auto-detect
lint_command = ""       # override auto-detect
screenshot = false      # enable playwright screenshot
preview_pages = ["/"]   # pages to fetch for HTML diff
```

### SSE events

New event types emitted during evaluation:

```json
{"type": "eval_started", "stages": ["lint", "test", "preview", "introspect", "html_diff"]}
{"type": "eval_stage", "stage": "lint", "status": "running"}
{"type": "eval_stage", "stage": "lint", "status": "passed"}
{"type": "eval_stage", "stage": "test", "status": "failed", "error": "3 tests failed: ..."}
{"type": "eval_retry", "round": 1, "reason": "测试失败，正在自动修复..."}
{"type": "eval_complete", "passed": true, "report": "所有检查通过"}
```

### Integration with agent loop

In `engine.py`, after the agent loop completes edits and before the diff/approval phase:

```python
if config.evaluation and config.evaluation.enabled:
    eval_result = await run_evaluation_pipeline(
        session=session,
        provider=provider,
        config=config,
        preview_manager=preview_manager,
        max_retries=config.evaluation.max_retries,
    )
    # eval_result.passed, eval_result.failed_stage, eval_result.report
```

The retry loop: on stage failure, construct a user message describing what failed and append it to `session.messages`, then re-enter the agent loop (the agent gets the failure context and can call tools to fix the issue).

### Error translation for quick mode

Evaluation failures in quick mode get user-friendly messages (same pattern as tool error translation):
- Lint failure → "代码有语法问题，AI 正在自动修复"
- Test failure → "部分测试未通过，AI 正在调整"
- Preview failure → "预览服务启动失败，AI 正在排查"

---

## File changes summary

| File | Change |
|------|--------|
| `live_edit/tool_registry.py` | **New** — ToolRegistry protocol, ToolDef dataclass, default implementation |
| `live_edit/builtin_tools/` | **New** — 7 tool modules migrated from tools.py |
| `live_edit/safety.py` | **New** — safety functions extracted from tools.py |
| `live_edit/evaluation.py` | **New** — EvaluationPipeline, stage implementations |
| `live_edit/tools.py` | **Modified** — thin re-export from registry for backwards compat, then deprecated |
| `live_edit/engine.py` | **Modified** — accept ToolRegistry, call eval pipeline after agent loop |
| `live_edit/config.py` | **Modified** — add EvaluationConfig dataclass, parse `[evaluation]` section |
| `live_edit/router.py` | **Modified** — wire ToolRegistry into setup_live_edit(), pass to engine |
| `live_edit/static/live-edit.js` | **Modified** — handle new SSE event types (eval_started, eval_stage, etc.) |

## Non-goals

- No multi-agent orchestration
- No cross-session memory / RAG
- No visual regression testing framework (screenshot is simple before/after comparison only)
- No plugin marketplace or remote plugin loading

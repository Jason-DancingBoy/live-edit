# Critic Agent & delete_file Tool Design

**Date**: 2026-08-10
**Status**: draft
**Scope**: upgrade `introspect` eval stage into a tool-using critic agent + add a safety-gated `delete_file` tool
**Route**: self-contained (Approach A) — independent read-only mini agent loop, no changes to the main edit loop

## Overview

live-edit already has a semantic review stage: `introspect` (evaluation.py:169). It is a single one-shot prompt — it hands a diff truncated to 4000 chars to the provider and asks "did the changes achieve the user's goal?" (passed/failed). It has **no tool access** and cannot verify against the actual codebase, so it misses the two things a real reviewer catches: goal-attainment gaps visible only by reading surrounding code, and fatal bugs (undefined references, broken call sites, obvious logic errors).

This spec **upgrades `introspect` in place** into a fresh-context, read-only **critic agent** that can actually read files, search code, and glob before judging. It returns **structured findings** (severity + file:line), and only critical/high findings fail the stage and trigger the existing fix loop.

During design we found a real gap that blocks the critic's fix loop: **live-edit has no safe way to delete a file.** `run_shell` blocks `rm`/`unlink`/`git rm` (safety.py:8-10) and there is no delete tool. If a user request requires deleting a file, the agent physically cannot do it, the critic flags "goal not achieved", and the fix loop can never succeed. This spec adds a `delete_file` tool with a conservative 3-tier write policy.

### Key decisions (from brainstorm)

| # | Decision | Choice |
|---|----------|--------|
| 1 | introspect relationship | Upgrade in place (stage id stays `introspect`) |
| 2 | Critic scope | Goal attainment + fatal bugs (not full code review) |
| 3 | Output form | Structured findings: severity + file:line + description |
| 4 | Blocking semantics | Only critical/high fail the stage; medium/low are reported only |
| 5 | Internal shape | Approach A — independent mini agent loop, isolated messages |
| 6 | delete_file policy | Conservative 3-tier (session-created deletable; pre-existing protected) |

## Architecture

```
live_edit/
├── critic.py           (new)     run_critic_agent(), CriticFinding, CriticVerdict
├── builtin_tools/delete_file.py  (new)    3-tier-policy delete tool
├── evaluation.py                replace _run_stage_introspect body with run_critic_agent
├── engine.py                    pass tool_registry into run_evaluation_pipeline (1 line)
├── config.py                    + EvaluationConfig.critic_max_rounds = 2
├── diff.py                      + delete_file branch in compute_write_diff (approval preview)
└── tools.py                     + delete_file branch in _tool_summary (approval reason line)
```

The main agent loop is **untouched**. The critic runs as the `introspect` stage of the existing post-edit eval pipeline; its failure reuses the existing fix loop (engine.py:1063-1106) unchanged.

```
main agent loop ──► eval pipeline (existing)
                     ├─ lint (unchanged)
                     ├─ test (unchanged)
                     └─ introspect ──► run_critic_agent (new)
                                          │  isolated messages, read-only tools, ≤2 explore rounds
                                          ▼
                                   JSON verdict → findings
                                          │
                            any critical/high? ── yes ──► existing fix loop (unchanged)
                                          └── no ──► eval passes, proceed to commit
```

## 1. Critic agent (`live_edit/critic.py`)

### Data model

```python
@dataclass
class CriticFinding:
    severity: str          # "critical" | "high" | "medium" | "low"
    file: str
    line: int | None
    description: str

@dataclass
class CriticVerdict:
    goal_achieved: bool
    findings: list[CriticFinding]
    summary: str

    @property
    def blocking(self) -> bool:
        return any(f.severity in ("critical", "high") for f in self.findings)
```

### Interface

```python
async def run_critic_agent(
    *,
    provider,
    tool_registry,
    worktree_path: str,
    user_request: str,
    diff: str,
    max_rounds: int = 2,
    is_cancelled: Callable[[], bool] | None = None,
) -> CriticVerdict
```

Kept decoupled from `EditSession`: takes `worktree_path` and an `is_cancelled` callable. The evaluation stage wires `session._cancelled.is_set` in.

### Read-only tool set

The critic must **never** be able to write. Tool visibility currently comes only from `ToolDef.modes` (tool_registry.py:36), and write tools default to `modes=None` (all modes) — so `get_tools("qa")` alone is NOT sufficient once `delete_file` exists. The critic builds its tool list by explicitly excluding write tools:

```python
write_names = tool_registry.get_write_tool_names("qa")
critic_tools = [t for t in tool_registry.get_tools("qa") if t["name"] not in write_names]
```

This is robust regardless of how future write tools declare `modes`.

### Loop behavior

```
messages = [system(critic persona + criteria + JSON schema),
            user(user_request + diff, truncated)]

round 1..max_rounds:
    content_blocks = await provider.call_with_tools(messages, critic_tools, ...)
    if has tool_use: execute each (read-only), append tool_results to messages; continue
    else:            text is the verdict; parse and return

rounds exhausted while still exploring:
    final call with tools=[] and a nudge
    ("直接输出审查结论 JSON，不要再调用工具") → guaranteed verdict
```

- Default `max_rounds = 2` (1 explore round + 1 verdict round); a `critic_max_rounds` config knob caps cost.
- Respect `is_cancelled` between rounds → abort with empty verdict.

### System prompt & JSON schema

System prompt (Chinese, matching project convention): critic persona (independent reviewer, read-only), the user request, the diff, severity criteria, and a requirement to verify with read-only tools before judging.

Severity criteria:
- **critical/high** — goal not achieved; undefined reference; obvious logic error; would crash or break functionality.
- **medium/low** — only when clearly valuable (naming, small defects); do not nitpick.

Final-round JSON schema:

```json
{
  "goal_achieved": true,
  "summary": "一句话结论",
  "findings": [
    { "severity": "high", "file": "src/api.py", "line": 45, "description": "函数签名改了但调用方未同步" }
  ]
}
```

### Parsing, retry, fallback

- Strip markdown ```json fences before `json.loads`.
- Parse failure → append a correction message ("输出必须是合法 JSON，请重试") → one more call.
- Second failure / provider error / timeout → **fail-open**: return empty verdict (`goal_achieved=True`, no findings) and record the reason in the eval report. Rationale: lint+test already passed; a format/infra fault in one semantic check should not block a commit. Matches current introspect behavior (evaluation.py:203 treats errors as pass).
- Tool execution errors → appended as `tool_result`; the critic decides how to react (same as the main loop).
- Cancellation → abort immediately, empty verdict.

### Stage mapping (`evaluation.py`)

`_run_stage_introspect` gains `worktree_path`, `tool_registry`, and `is_cancelled` params (passed by `run_evaluation_pipeline`, which already has `session`), and returns the existing `{"ok", "output"}` shape:

```python
verdict = await run_critic_agent(...)
ok = verdict.goal_achieved and not verdict.blocking
output = serialize(verdict)   # human-readable summary + findings list
```

Stage id stays **`introspect`** — config `stages` lists and existing `.toml` files keep working; frontend `eval_stage` rendering is untouched. The critic runs in all modes (quick/deep/qa) wherever the eval pipeline is enabled, exactly as `introspect` does today.

### Fix-loop feedback

On failure, `output` is a list with anchors:

```
审查未通过：改动未达成用户目标。
- [critical] 用户要求批量删除，实现只支持单个删除
- [high] src/api.py:45 — update_user 签名已改，调用方未同步
```

The existing engine fix loop (engine.py:1081) dumps `failed_output[:1500]` into the fix prompt verbatim — the fix signal improves from "a wall of text" to "a checklist with file:line anchors", with **no engine change**.

## 2. `delete_file` tool (`live_edit/builtin_tools/delete_file.py`)

### Behavior

- Name `delete_file`, `is_write=True` → auto-gated in quick mode (engine.py:878), part of final batch in deep mode.
- Input: `path` (required), `reason` (optional).
- Returns `{"ok": true, "path": ..., "deleted": true, "size": n}` or `{"ok": false, "error": "..."}`.

### 3-tier write policy (decision #6)

Deletable if and only if:

1. `safe_path(path, project_root)` stays inside the project (safety.py:99).
2. Target exists and is a regular file (refuse directories and non-existent paths).
3. **Any** of:
   a. File is **not in the main branch commit tree** — i.e. created this session and not yet merged into main → deletable. Implemented as `git -C <worktree> ls-tree main -- <path>` (falling back to `master`; non-empty = pre-existing). This is the session branch (`live-edit/<session_id>`) because the session branch diverges from main — a file committed only to the session branch is NOT in main, so it stays deletable until the session is merged. Files merged into main become protected.
   b. File is inside `overwrite_allowed_dirs` (default `static`, `public`, `assets`) — existing `check_write_allowed` path.
   c. `allow_overwrite_existing=true` — user explicitly opted into deleting existing source.

Semantics: files merged into main are "pre-existing" and protected; anything the session created that has not reached main (untracked, or committed only on the session branch) is session-owned and deletable until merge. Quick-mode approval still shows a full-delete diff preview (`is_write=True`), so a human confirms every deletion.

**Known edge case**: untracked files that pre-existed the session (never committed) also match (a) and are deletable. Accepted — they are not in any commit, and quick-mode approval shows a full-delete diff preview.

**Conservative fallback**: if neither `main` nor `master` can be resolved (repo with no commits, or a non-git directory), the file is treated as protected — never collapse to allow-all.

### Supporting changes

- `diff.py:31` `compute_write_diff` — add a `delete_file` branch returning `diff_text(current, "", path)` so the approval dialog previews the removed content.
- `tools.py:24` `_tool_summary` — add `删除 {path}` branch.
- `.live-edit.toml [errors.quick]` — add mappings for `文件不存在` / `不能删除目录` / delete-specific rejection messages.
- Deletions flow into the staged diff via the engine's existing `git add -A` (engine.py:1124), so the critic and the final diff both see them.

## 3. Engine wiring

One change in `run_edit_session`:

```python
eval_result = await run_evaluation_pipeline(
    session=session, provider=provider, config=config,
    preview_manager=preview_manager, tool_registry=tool_registry,   # + this
)
```

`run_evaluation_pipeline` (evaluation.py:244) and `_run_stage_introspect` gain a `tool_registry` param, threaded through to `run_critic_agent`. Nothing else in the engine changes — the fix loop, severity→retry flow, and commit behavior are reused as-is.

## 4. Configuration

`config.py` `EvaluationConfig` adds:

```python
critic_max_rounds: int = 2   # critic explore-round cap (cost gate)
```

Parsed from the existing `[evaluation]` section (`eval_data.get("critic_max_rounds", 2)`), default `2`. No changes to `stages` defaults (`["lint", "test", "introspect"]`).

## 5. Error handling

| Failure | Behavior |
|---|---|
| provider error / timeout | fail-open: empty verdict, reason recorded in eval report, not blocking |
| JSON parse failure | one correction retry → still failing → fail-open + report note |
| tool execution error | appended as tool_result; critic reacts |
| user cancellation | abort between rounds, empty verdict |
| delete_file policy rejection | clear Chinese error (quick mode via translation table); agent adjusts |

Fail-open philosophy: once lint+test pass, a format/infra fault in one semantic check must not hold up a commit — but the reason is surfaced, not silent. Eval failure does **not** veto commits: the engine preserves and (in deep mode) commits the work, with an explicit failure note (`_eval_failure_note`, engine.py:32) telling the user checks failed, auto-fix was attempted, and changes are preserved.

## 6. Testing

`tests/test_critic.py` (fake provider + fake registry, existing test style):
- Loop: round 1 issues read-only tool calls → executed → round 2 returns verdict.
- Critic tool list never contains a write tool (delete_file registered → still absent).
- JSON parsing: markdown fence stripping; invalid JSON triggers correction retry; second failure → fail-open.
- Severity gate: critical/high → ok=False; only low → ok=True; `goal_achieved=false` → ok=False.
- Fail-open: provider exception → empty verdict, no raise.
- Cancellation between rounds → empty verdict.

`tests/test_delete_file.py`:
- Path escape blocked; directory refused; non-existent refused.
- Policy: pre-existing file outside overwrite dirs blocked; session-created (not in HEAD) deletable; overwrite-dir file deletable; `allow_overwrite_existing=true` unlocks source deletion.
- `compute_write_diff("delete_file", ...)` returns full-removal diff.
- quick mode: `needs_approval` true for delete_file.

## 7. Out of scope (explicit)

- Renaming the `introspect` stage id (config/`stages` + frontend churn not worth it).
- Directory deletion.
- Per-mode critic strictness (one critical/high gate for all modes).
- Interleaving the critic into the main loop (stays a post-edit gate).
- Critic memory / multi-round self-healing — one-shot fresh-eyes review.

## 8. Success criteria

1. A request that the one-shot introspect would miss (e.g. call site left inconsistent) now fails the stage via a tool-verified finding, and the fix loop repairs it.
2. Deleting a file created in-session works end-to-end and appears in the staged diff and commit.
3. Deleting a pre-existing source file is rejected by default policy; config flip enables it.
4. All eval behaviors preserved when the critic errors (fail-open) — a commit is never blocked by a critic infra failure.
5. Full test suite passes.

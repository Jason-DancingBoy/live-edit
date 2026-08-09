# Bug: preview 运行中的会话，空闲 worktree 清理会连 session 分支一起删

> **状态：已修复（2026-08-09）。** 复现、根因、修复、验证见下文。另在调查中发现一个相关潜在 bug，见文末「附带发现」。

## 一句话

**一个正常结束、改动已提交到 `live-edit/<session_id>` 分支、但挂着 preview 的会话，其 worktree 目录会一直保留；空闲超过 `stale_worktree_ttl`（默认 24h）后，应用重启时的自动清理会调用 `discard_session_branch`——连目录带分支一起删，导致两天后管理员无法把该分支合入 main。**

## 冲突的机制（为什么两个"各自合理"的设计撞出这个 bug）

**机制 1 — preview 运行时要保留 worktree 目录**（`_do_commit`，engine.py:499-502）：

```python
if session._preview_url:
    # Preview is running — keep worktree dir so preview can serve files.
    # Admin merge/delete/cleanup endpoints handle cleanup later.
    pass
else:
    vcs.remove_worktree_dir(session._worktree_path, session.id)
```

同时 `_do_commit` 的意图是"**保留分支，供管理页手动合入 main**"（engine.py:458）。

**机制 2 — 自动清理按 mtime 判定 stale，stale 就删分支**（`cleanup_stale_worktrees`，vcs.py:180-234）：

```python
if now - mtime >= ttl:
    if path in registered:
        self.discard_session_branch(name, worktree_path=path)  # ← 连分支删
```

`discard_session_branch`（vcs.py:263-284）会 `git branch -D live-edit/<session_id>`（line 283）。

**mtime 只在会话每轮跑时刷新**（engine.py:992-995）。会话结束后没有轮次了，目录 mtime 冻结。于是：

- 会话结束，preview 还挂着 → 目录保留（机制 1），mtime 不再刷新；
- 空闲 >24h → 下次启动 `GitVCS.__init__` 触发清理（vcs.py:157）→ 判定 stale → 已注册 → `discard_session_branch` → **分支被删**。

## 复现路径

1. 跑一个会话，改动提交到 `live-edit/<sid>` 分支，`_preview_url` 被设置（preview 运行中）。
2. 会话空闲超过 `stale_worktree_ttl`（默认 86400s / 24h），期间无轮次 → 目录 mtime 不再刷新。
3. 应用重启 → 清理把 worktree 目录 + 分支一起删掉。
4. 管理员 `POST /live-edit/admin/branches/<sid>/merge` → 404 "分支不存在"（router.py:988）。

## 影响

- **已提交的改动变 unreachable**：分支 ref 被删，commit 仍留在 object DB（reflog 可找回，直到 gc），但正常管理路径无法合并。
- **未提交的改动直接丢失**：随 worktree 目录一起删。
- **违反设计承诺**：`_do_commit` 明确写着"改动保留在分支上供管理页手动合入"（engine.py:458-459），`_committed = True` 的会话本应长期可合。

## 安全对照（不受影响的路径）

- 会话正常走完且**无 preview** → `remove_worktree_dir` 只删目录留分支，目录没了，清理逻辑扫不到它 → 分支长期存活，两天后能合 ✅。
- 会话**未提交、崩溃** → 本就要删目录（未提交改动丢失），是否连删分支属于崩溃恢复语义，另当别论。
- 受影响的是 **preview 保留目录 + 已提交** 这个组合。

## 修复方向（择一或组合）

- **A. preview 期间刷新 mtime / 清理时跳过活跃 preview**：preview 存在期间保持目录"新鲜"，或清理前查 preview 是否仍活跃，活跃则跳过。
- **B. preview 停止时收敛目录**：preview manager 停止会话时，若会话已提交，执行"删目录、留分支"（复刻无 preview 路径），从根上消除残留目录。
- **C. 清理只删目录、不自动删分支**：registered 且 stale 的 worktree 只 `git worktree remove`，分支删除留给显式的 `admin_cleanup`。需要重新定义崩溃残留的分支回收策略（避免无限泄漏）。
- **D. 落库一个"可丢弃"标志**：storage 记录会话已正常提交，清理时只有该标志为真才删分支；崩溃残留保留。

## 建议验收标准

- preview 运行中的会话 worktree 空闲超 TTL 后，分支不被自动删除。
- preview 停止后，目录最终被清，分支保留，`/admin/branches/<sid>/merge` 数天后仍可用。
- 崩溃恢复不回退：刚崩溃的会话 worktree 仍保留供 `/continue`。
- 全量测试 `pytest tests/ -q` 绿。

## 修复（2026-08-09，采用方向 C）

**根因：** `cleanup_stale_worktrees` 对已注册的 stale worktree 调用 `discard_session_branch`（vcs.py:224），而它**连分支一起删**（`git branch -D`，vcs.py:283）。但"已提交 + preview 保留"的 worktree 是功能性状态——分支是管理员要合入 main 的交付物，自动磁盘回收不应该销毁它。分支删除应只走显式路径（会话正常结束的 finally，或 `admin_cleanup` 端点）。

**改动：** `live_edit/vcs.py` `cleanup_stale_worktrees` 中，对已注册 stale worktree 改用 `remove_worktree_dir(path, name)`（只删目录、保留 `live-edit/<session_id>` 分支），不再调 `discard_session_branch`。

**验证：**
- 改 `tests/test_vcs.py::TestCleanupStaleWorktrees::test_keeps_fresh_worktree_removes_stale`：断言从 `branches == ""` 改为 `branches != ""`（分支保留）。
- 新增 `tests/test_vcs.py::TestCleanupStaleWorktrees::test_cleanup_keeps_committed_branch_for_admin_merge`：在分支上真实 commit 后清理，断言目录被回收、分支仍在、且仍在 `list_unmerged_branches`（可合）。
- 修复前两个测试红（复现），修复后绿；全量 `pytest tests/ -q` 382 通过。

**副作用评估：** 崩溃会话若留下分支，未提交的会指向 main 头（`merge-base --is-ancestor` 判为已合并，不出现于 unmerged 列表），无害；部分提交的分支会出现在 unmerged 列表成为可合交付物——比之前静默删除更好。分支 ref 极小，泄漏成本可忽略，且有 `admin_cleanup` 端点做显式清理。

## 附带发现（已修复 2026-08-09）：`/continue` 恢复的已提交会话，正常结束时可能丢弃自己的分支

调查中发现的**相关潜在 bug**，详见 `2026-08-09-continue-discards-committed-branch.md`：

`rehydrate_session` 无条件置 `_merged=False`（engine.py:1291），且 `run_edit_session` 的 `finally` 用 `if not session._merged and session._worktree_path` 判断是否 `discard_session_branch`（engine.py:1237-1242）。一个已提交的会话崩溃后经 `/continue` 恢复，若未再次 commit 就正常结束，finally 会连分支一起丢弃，丢掉此前已提交的改动。**已于同日修复**：`finally` 按 `_committed` 分流，已提交会话只删目录留分支。此问题与本次修复相互独立。

# Bug（已修复）：/continue 恢复的已提交会话，正常结束时可能丢弃自己的分支

> **状态：已修复（2026-08-09）。** 复现、根因、修复、验证见下文。
> 相关背景：`2026-08-09-worktree-cleanup-deletes-session-branch.md`。

## 一句话

**一个已提交的会话（改动在 `live-edit/<sid>` 分支上）崩溃后经 `/continue` 恢复，若新请求没有再次 commit 就正常结束，`run_edit_session` 的 `finally` 会调用 `discard_session_branch`——把保存着此前已提交改动的分支也删掉。**

## 触发链

1. 会话 A 完成一次 `_do_commit`：改动提交到 `live-edit/<sid>` 分支，`session._committed = True`、`session._merged = True`（engine.py:505, 522），落库记录 `committed=1`。
2. 进程崩溃。
3. 用户 `/continue/<sid>` → `rehydrate_session`（engine.py:1273）：恢复 `_committed=True`、`_commit_hash`，但 **`_merged` 被无条件置 `False`**（engine.py:1291/1294）。
4. `continue_edit_session` → `run_edit_session`（engine.py:1328）。若恢复的会话没有新的可提交改动（用户直接收尾、或拒绝变更），不会走 `_do_commit`，`_merged` 保持 `False`。
5. `run_edit_session` 的 `finally`：
   ```python
   # Clean up worktree if not merged/removed yet (e.g. exception before commit)
   if not session._merged and session._worktree_path:
       vcs.discard_session_branch(session.id, worktree_path=session._worktree_path)  # ← 删分支
   ```
   （engine.py:1237-1242）→ **`live-edit/<sid>` 分支被删，此前已提交的改动失去引用。**

## 根因

`finally` 用 `_merged`（"本轮是否已 commit"的运行时标记）判断"是否可以丢弃分支"，但它没有考虑 `_committed`（"这个会话此前是否提交过交付物"）。崩溃恢复把 `_merged` 重置为 `False` 后，已提交的交付物就失去了保护。

## 修复（2026-08-09，采用方向 A 的双分支变体）

**根因：** `finally` 用 `_merged`（"本轮是否已 commit"）判断是否丢弃分支，没考虑 `_committed`（"会话是否已有交付物"）。`rehydrate_session` 无条件把 `_merged` 置 `False`，崩溃恢复的已提交会话失去保护。

**改动：** `live_edit/engine.py` `run_edit_session` 的 `finally` 块，按 `_committed` 分流：
- 已提交会话（交付物在 `live-edit/<sid>` 分支上）→ `vcs.remove_worktree_dir(...)` 只删目录、保留分支，供管理页手动合入 main；
- 未提交/无交付物会话 → 维持原 `vcs.discard_session_branch(...)` 连目录带分支清理。

（备选方向 B——`rehydrate_session` 按 `committed` 恢复 `_merged`——未采用，因为 `continue_edit_session`（engine.py:1320）会在 `_merged` 为 True 时把 worktree 重置、创建新 worktree，语义更绕。）

**验证：**
- 新增 `tests/test_engine.py::TestRehydrateSession::test_continue_recovered_committed_session_keeps_branch`：真实 git 仓库中建 worktree、向 `live-edit/<sid>` 提交改动，`rehydrate_session(committed=1)` 后经 `continue_edit_session` 无新改动正常结束，断言分支仍在、worktree 目录已回收。修复前红（分支被删），修复后绿。
- 全量 `pytest tests/ -q` 383 通过。

## 验收标准（已满足）

- 已提交会话崩溃 → `/continue` → 无新改动正常结束 → 分支仍在，`/admin/branches/<sid>/merge` 可用 ✅
- 未提交/崩溃无交付物的会话，正常结束时仍能正确清理 worktree 与分支 ✅（`discard_session_branch` 路径未变）
- 全量测试 `pytest tests/ -q` 绿 ✅

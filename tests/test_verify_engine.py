# tests/test_verify_engine.py
"""Engine-level wiring tests for quick-mode verify (方案 A).

锁住安全不变量：只有 evidence 决策为 AUTO_APPROVE 时才跳过最终人工确认、自动提交；
HUMAN / BLOCK 一律照常走 wait_for_approval。
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from live_edit.config import Config, LLMConfig, ProjectConfig, VerifyConfig
from live_edit.engine import EditSession, SessionStore, _verify_auto_approves, run_edit_session
from live_edit.tool_registry import DefaultToolRegistry
from live_edit.verify.evidence import Evidence


def test_auto_approve_helper():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="auto_approve")
    assert _verify_auto_approves(ev) is True


def test_non_auto_or_none():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="human")
    assert _verify_auto_approves(ev) is False
    assert _verify_auto_approves(None) is False


class _FakeProvider:
    """Provider that returns predetermined content_blocks, then a text-only response."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = responses
        self.call_count = 0

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        if self.call_count < len(self.responses):
            result = self.responses[self.call_count]
            self.call_count += 1
            return result
        return [{"type": "text", "text": "Done"}]


def _config(root: str, verify: VerifyConfig | None = None) -> Config:
    return Config(
        project=ProjectConfig(name="t", language="python", root=root),
        llm=LLMConfig(api_url="http://x", api_key_env="K", model="m"),
        verify=verify if verify is not None else VerifyConfig(),
    )


def _make_git_worktree(tmp_path) -> str:
    """Real git worktree: the diff stage runs `git -C <wt> diff --cached`,
    so the worktree must be an actual repo for a write to show up as a diff."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(wt), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(wt), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(wt), capture_output=True)
    (wt / "init.txt").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(wt), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(wt), capture_output=True)
    return str(wt)


async def _run_write_session(config: Config, worktree: str):
    """Run a quick-mode session that writes new_file.py, then finishes.

    Returns (session, storage, wait_calls). wait_calls is the list of tool_ids
    passed to wait_for_approval; "__final__" appears iff the engine did NOT
    auto-approve. The final wait returns not-approved, so a non-auto-approve
    session does not commit.
    """
    provider = _FakeProvider(
        [
            [
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "id": "t1",
                    "input": {"path": "new_file.py", "content": "print('new')\n"},
                }
            ],
        ]
    )
    vcs = MagicMock()
    vcs.create_worktree.return_value = worktree
    storage = MagicMock()
    registry = DefaultToolRegistry()
    registry.load_builtin_tools()

    session = EditSession("s1", "add a file")
    store = SessionStore(max_active=10, ttl_seconds=3600)
    store.add(session)

    wait_calls: list[str] = []

    async def fake_wait(tool_id, tool_data, timeout=300.0):
        wait_calls.append(tool_id)
        if tool_id == "__final__":
            return {"approved": False}
        return {"approved": True}

    session.wait_for_approval = fake_wait  # type: ignore[method-assign]

    await run_edit_session(
        session=session,
        provider=provider,
        vcs=vcs,
        storage=storage,
        config=config,
        mode="quick",
        session_store=store,
        tool_registry=registry,
    )
    return session, storage, wait_calls


def _saved_decision(storage) -> str:
    """Read the decision of the evidence persisted by _verify_and_store."""
    args = storage.save_evidence.call_args
    assert args is not None, "expected save_evidence to be called"
    return Evidence.from_dict(json.loads(args[0][1])).decision


@pytest.mark.asyncio
async def test_default_verify_degrades_to_human_and_waits(tmp_path):
    """默认配置（无 verify 测试）→ evidence 决策 human → wait_for_approval 被调用。"""
    wt = _make_git_worktree(tmp_path)
    session, storage, wait_calls = await _run_write_session(_config(wt), wt)
    assert "__final__" in wait_calls
    assert session._committed is False
    assert _saved_decision(storage) == "human"


@pytest.mark.asyncio
async def test_auto_approve_skips_final_wait_and_commits(tmp_path):
    """test_command 全绿 → evidence AUTO_APPROVE → 跳过 wait_for_approval 直接提交。"""
    wt = _make_git_worktree(tmp_path)
    config = _config(wt, VerifyConfig(test_command="python -c 'pass'"))
    session, storage, wait_calls = await _run_write_session(config, wt)
    assert "__final__" not in wait_calls
    assert session._committed is True
    assert _saved_decision(storage) == "auto_approve"


@pytest.mark.asyncio
async def test_block_still_waits_for_approval(tmp_path):
    """test_command 失败 → evidence BLOCK → 不静默提交，仍走 wait_for_approval。"""
    wt = _make_git_worktree(tmp_path)
    config = _config(wt, VerifyConfig(test_command="python -c 'raise SystemExit(1)'"))
    session, storage, wait_calls = await _run_write_session(config, wt)
    assert "__final__" in wait_calls
    assert session._committed is False
    assert _saved_decision(storage) == "block"


async def _run_write_session_with_storage(config: Config, worktree: str, storage):
    """Like _run_write_session but with a caller-provided storage mock."""
    provider = _FakeProvider(
        [
            [
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "id": "t1",
                    "input": {"path": "new_file.py", "content": "print('new')\n"},
                }
            ],
        ]
    )
    vcs = MagicMock()
    vcs.create_worktree.return_value = worktree
    registry = DefaultToolRegistry()
    registry.load_builtin_tools()

    session = EditSession("s1", "add a file")
    store = SessionStore(max_active=10, ttl_seconds=3600)
    store.add(session)

    wait_calls: list[str] = []

    async def fake_wait(tool_id, tool_data, timeout=300.0):
        wait_calls.append(tool_id)
        if tool_id == "__final__":
            return {"approved": False}
        return {"approved": True}

    session.wait_for_approval = fake_wait  # type: ignore[method-assign]

    await run_edit_session(
        session=session,
        provider=provider,
        vcs=vcs,
        storage=storage,
        config=config,
        mode="quick",
        session_store=store,
        tool_registry=registry,
    )
    return session, wait_calls


@pytest.mark.asyncio
async def test_invalid_test_command_degrades_not_crash(tmp_path):
    """verify 抛异常（非法 test_command 令 shlex 报错）→ 降级人工，不摧毁会话。"""
    wt = _make_git_worktree(tmp_path)
    config = _config(wt, VerifyConfig(test_command="'unbalanced"))
    storage = MagicMock()
    session, wait_calls = await _run_write_session_with_storage(config, wt, storage)
    # 会话存活：走 final 人工确认，而不是把整个会话标 failed。
    assert session._outcome != "failed"
    assert "__final__" in wait_calls
    assert session._committed is False
    storage.save_evidence.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_save_failure_degrades_not_crash(tmp_path):
    """evidence 落库失败 → 降级人工，不摧毁会话。"""
    wt = _make_git_worktree(tmp_path)
    config = _config(wt, VerifyConfig(test_command="python -c 'pass'"))
    storage = MagicMock()
    storage.save_evidence.side_effect = RuntimeError("disk full")
    session, wait_calls = await _run_write_session_with_storage(config, wt, storage)
    assert session._outcome != "failed"
    assert "__final__" in wait_calls
    assert session._committed is False

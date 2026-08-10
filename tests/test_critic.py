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
from live_edit.tool_registry import DefaultToolRegistry


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
        v = CriticVerdict(
            goal_achieved=True,
            findings=[
                CriticFinding(severity="high", file="a.py", description="x"),
            ],
        )
        assert v.blocking is True

    def test_blocking_false_for_low_and_clean(self):
        v = CriticVerdict(
            goal_achieved=True,
            findings=[
                CriticFinding(severity="low", file="a.py", description="nit"),
            ],
        )
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

    def test_real_toolset_is_read_only_allowlist(self):
        # Regression (C1): the critic toolset built from the REAL registry must
        # be exactly the read-only allowlist — run_shell (which can mutate via
        # mv/cp/echo >/sed -i even though is_write=False) must never leak in.
        reg = DefaultToolRegistry()
        reg.load_builtin_tools()
        names = {t["name"] for t in _build_critic_tools(reg)}
        assert names <= {"read_file", "search_code", "glob", "list_dir"}
        assert not (names & {"run_shell", "write_file", "edit_file", "delete_file"})


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

    def test_goal_achieved_false_string(self):
        # Regression (M1): "false" string must parse to False, not truthy.
        v = _parse_verdict_text(
            json.dumps({"goal_achieved": "false", "summary": "s", "findings": []})
        )
        assert v.goal_achieved is False

    def test_goal_achieved_missing_defaults_true(self):
        v = _parse_verdict_text(json.dumps({"summary": "s", "findings": []}))
        assert v.goal_achieved is True

    def test_severity_case_insensitive_blocking(self):
        # Regression (M1): "High"/"CRITICAL" must be normalized to blocking.
        v = _parse_verdict_text(
            json.dumps(
                {
                    "goal_achieved": True,
                    "summary": "s",
                    "findings": [
                        {"severity": "High", "file": "a.py", "description": "x"}
                    ],
                }
            )
        )
        assert v.findings[0].severity == "high"
        assert v.blocking is True


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
            provider=provider,
            tool_registry=reg,
            worktree_path="/tmp/wt",
            user_request="fix",
            diff="diff",
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
            provider=provider,
            tool_registry=None,
            worktree_path="/tmp/wt",
            user_request="fix",
            diff="d",
        )
        assert verdict.goal_achieved is True
        assert len(provider.calls) == 2

    async def test_fail_open_on_provider_error(self):
        class Boom:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                raise RuntimeError("api down")

        verdict = await run_critic_agent(
            provider=Boom(),
            tool_registry=None,
            worktree_path="/tmp/wt",
            user_request="fix",
            diff="d",
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
            provider=provider,
            tool_registry=reg,
            worktree_path="/tmp/wt",
            user_request="fix",
            diff="d",
            max_rounds=1,
        )
        assert verdict.goal_achieved is True
        # last call used tools=[] to force a text verdict
        assert provider.tools_seen[-1] == []

    async def test_cancelled_returns_empty_verdict(self):
        provider = FakeProvider([[{"type": "text", "text": BLOCKING_JSON}]])
        verdict = await run_critic_agent(
            provider=provider,
            tool_registry=None,
            worktree_path="/tmp/wt",
            user_request="fix",
            diff="d",
            is_cancelled=lambda: True,
        )
        assert verdict.goal_achieved is True
        assert verdict.findings == []

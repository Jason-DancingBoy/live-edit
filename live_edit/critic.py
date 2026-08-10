"""Fresh-context, read-only critic agent for the introspect eval stage."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

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
                "  critical/high —— 未达成用户目标；未定义引用；明显逻辑错误；"
                "会导致崩溃或功能破坏。\n"
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
                        "content": "你刚才的输出不是合法 JSON。请重新输出，"
                        "只输出一个 JSON 对象，不要其他文字。",
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

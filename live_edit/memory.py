"""Three-tier memory system: ShortTermMemory, LongTermMemory, KnowledgeBase, MemoryManager."""

import logging
from dataclasses import dataclass  # noqa: F401  (used by later tiers in this file)

from .config import ShortTermConfig

logger = logging.getLogger("live-edit.memory")


class ShortTermMemory:
    """L1: Session window management — strip or summarize old rounds.

    Threshold bands (absolute round counts, validated by ShortTermConfig):
    - round_num <= max_full_rounds: no-op
    - round_num <= max_stripped_rounds: strip old rounds
    - round_num <= max_summary_rounds (async + provider): summarize, else strip
    - round_num > max_summary_rounds: strip only (conversation too long to keep
      spending tokens on a summary every round)
    """

    def __init__(self, config: ShortTermConfig):
        self.config = config

    def manage(self, messages: list[dict], round_num: int) -> tuple[list[dict], str]:
        """Manage the message window (sync). Returns (mutated_messages, summary_text).

        The sync version never summarizes (it has no provider): once
        round_num exceeds max_full_rounds it only strips old rounds.
        """
        cfg = self.config
        if round_num <= cfg.max_full_rounds:
            return messages, ""
        return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

    async def manage_async(
        self, messages: list[dict], round_num: int, provider=None
    ) -> tuple[list[dict], str]:
        """Async version with optional LLM summarization.

        Three bands (absolute round counts):
        - round_num <= max_full_rounds: no-op
        - round_num <= max_stripped_rounds: strip old rounds, summary=""
        - round_num <= max_summary_rounds (with provider): try `_summarize`;
          on success return (stripped, summary); on failure fall back to strip
        - round_num > max_summary_rounds: strip only, no summary
        """
        cfg = self.config

        if round_num <= cfg.max_full_rounds:
            return messages, ""

        if round_num <= cfg.max_stripped_rounds:
            return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

        if round_num > cfg.max_summary_rounds:
            return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

        # max_stripped_rounds < round_num <= max_summary_rounds: try summarizing
        if provider is not None:
            try:
                summary = await self._summarize(messages, cfg.max_full_rounds, provider)
                if summary:
                    stripped = self._strip_old_rounds(messages, cfg.max_full_rounds)
                    return stripped, summary
            except Exception:
                logger.warning("L1 summarization failed, falling back to strip-only")

        return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

    def _strip_old_rounds(self, messages: list[dict], keep_full: int) -> list[dict]:
        """Keep last `keep_full` rounds full; strip older rounds to one-liners."""
        # Each round = assistant + user pair
        total_rounds = len(messages) // 2
        if total_rounds <= keep_full:
            return messages

        keep_msgs = keep_full * 2
        result = []
        # Process older messages (index 0 to len-keep_msgs-1)
        for i in range(len(messages) - keep_msgs):
            msg = messages[i]
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                # Strip tool_results to summary
                parts = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        text = ""
                        if isinstance(block.get("content"), list):
                            for c in block["content"]:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    break
                        # Try to extract tool name from matching assistant message
                        tool_name = "tool"
                        for prev_msg in reversed(messages[:i]):
                            if prev_msg["role"] == "assistant" and isinstance(
                                prev_msg.get("content"), list
                            ):
                                for tb in prev_msg["content"]:
                                    if (
                                        isinstance(tb, dict)
                                        and tb.get("type") == "tool_use"
                                        and tb.get("id") == tool_id
                                    ):
                                        tool_name = tb.get("name", "tool")
                                        # Extract file path if available
                                        inp = tb.get("input", {})
                                        path = inp.get("path", "")
                                        if path:
                                            tool_name += f" {path}"
                                        break
                                break
                        # Count +/- from result text
                        import re

                        added = len(re.findall(r"^\+", text, re.MULTILINE))
                        removed = len(re.findall(r"^-", text, re.MULTILINE))
                        stat = f"+{added}/-{removed}" if (added or removed) else ""
                        parts.append(f"{tool_name} {stat}".strip())
                result.append({"role": "user", "content": "; ".join(parts)})
            else:
                result.append(msg)

        # Append the last keep_msgs messages unchanged
        result.extend(messages[-keep_msgs:])
        return result

    async def _summarize(self, messages: list[dict], keep_full: int, provider) -> str:
        """Call LLM to summarize old rounds beyond keep_full."""
        keep_msgs = keep_full * 2
        old_messages = messages[:-keep_msgs] if keep_msgs > 0 else messages

        # Build a compact representation of old rounds
        lines = []
        for msg in old_messages:
            if msg["role"] == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            lines.append(block["text"][:200])
                        elif block.get("type") == "tool_use":
                            lines.append(f"[tool:{block.get('name')}]")
                else:
                    lines.append(str(content)[:200])

        old_text = "\n".join(lines[-3000:])  # keep it compact

        summary_model = self.config.summary_model or ""  # noqa: F841  (kept verbatim from spec)
        result = await provider.call_with_tools(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Summarize the following conversation history in 2-3 sentences "
                        "in the original language. Focus on: what was requested, "
                        "which files were modified, and the outcome.\n\n"
                        f"Conversation:\n{old_text}"
                    ),
                }
            ],
            tools=[],
            on_thinking=lambda t: None,
            on_text=lambda t: None,
        )
        if result is None:
            return ""
        for block in result:
            if block and block.get("type") == "text":
                return "[会话摘要] " + str(block.get("text", "")).strip()
        return ""

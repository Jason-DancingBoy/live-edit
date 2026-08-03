from live_edit.config import ShortTermConfig
from live_edit.memory import ShortTermMemory


def make_messages(rounds: int) -> list[dict]:
    """Build a realistic message sequence for N rounds."""
    msgs = []
    for i in range(rounds):
        msgs.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Thinking about round {i}"},
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "edit_file",
                        "input": {"path": f"file{i}.py", "old_string": "foo", "new_string": "bar"},
                    },
                ],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": [{"type": "text", "text": '{"ok": true, "file": "file0.py"}'}],
                    },
                ],
            }
        )
    return msgs


class FakeProvider:
    """Minimal fake provider: records the call and returns a canned summary."""

    def __init__(self):
        self.called = False

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        self.called = True
        return [{"type": "text", "text": "S: old rounds summarized."}]


class TestShortTermMemory:
    def test_noop_when_under_max_full_rounds(self):
        cfg = ShortTermConfig(max_full_rounds=3, max_stripped_rounds=7, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(2)  # 2 rounds = 4 messages
        result, summary = sm.manage(msgs, round_num=2)
        assert result is msgs  # same object, no mutation needed
        assert summary == ""

    def test_strips_old_rounds(self):
        cfg = ShortTermConfig(max_full_rounds=2, max_stripped_rounds=5, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(5)  # 10 messages total
        result, summary = sm.manage(msgs, round_num=5)
        # max_full_rounds=2: last 2 rounds stay full; older rounds stripped.
        # Messages are ordered assistant, user, assistant, user, ...
        assert summary == ""
        assert result[0] is msgs[0]  # oldest assistant message preserved as-is
        first_user = result[1]  # oldest user message -> tool results stripped
        assert isinstance(first_user["content"], str)
        assert "edit_file" in first_user["content"]

    def test_summarizes_old_rounds_via_async(self):
        import asyncio

        cfg = ShortTermConfig(max_full_rounds=1, max_stripped_rounds=2, max_summary_rounds=6)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(6)
        provider = FakeProvider()
        # round_num must be <= max_summary_rounds to land in the summarize band
        # (max_stripped_rounds < round_num <= max_summary_rounds)
        result, summary = asyncio.run(sm.manage_async(msgs, round_num=6, provider=provider))
        assert provider.called is True
        assert summary.startswith("[会话摘要]")
        # old rounds beyond max_full_rounds are stripped (assistant text/tool_use dropped,
        # first user message becomes a short string)
        assert isinstance(result[1]["content"], str)

    def test_strip_format_includes_tool_name_and_path(self):
        cfg = ShortTermConfig(max_full_rounds=1, max_stripped_rounds=5, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(3)
        result, _ = sm.manage(msgs, round_num=3)
        first_user = result[1]
        assert "edit_file" in str(first_user["content"])
        assert "file0.py" in str(first_user["content"])

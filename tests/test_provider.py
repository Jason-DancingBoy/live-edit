"""Tests for live_edit.provider — Provider interface and default implementation."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from live_edit.provider import AnthropicCompatibleProvider


async def _async_iter(items):
    for item in items:
        yield item


class TestAnthropicCompatibleProvider:
    def _setup_mock(self, events):
        """Build mock httpx client that returns SSE events."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.aiter_lines = MagicMock(
            return_value=_async_iter([f"data: {e}" for e in events])
        )

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__.return_value = mock_response

        # inner_client is what `async with httpx.AsyncClient(...) as client:` binds to `client`
        inner_client = MagicMock()
        inner_client.stream = MagicMock(return_value=mock_stream_ctx)

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = inner_client

        return mock_client

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        events = [
            json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}}),
            json.dumps({"type": "content_block_stop", "index": 0}),
        ]

        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
        )
        mock_client = self._setup_mock(events)

        with patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client):
            text_parts = []
            result = await provider.call_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                on_text=lambda t: text_parts.append(t),
            )

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Hello world"
        assert "".join(text_parts) == "Hello world"

    @pytest.mark.asyncio
    async def test_tool_use_parsing(self):
        events = [
            json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "read_file", "id": "tool_1"}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"src/main.py"'}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "}"}}),
            json.dumps({"type": "content_block_stop", "index": 0}),
        ]

        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
        )
        mock_client = self._setup_mock(events)

        with patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client):
            result = await provider.call_with_tools(
                messages=[{"role": "user", "content": "Read"}],
                tools=[{"name": "read_file", "input_schema": {}}],
            )

        assert len(result) == 1
        assert result[0]["type"] == "tool_use"
        assert result[0]["name"] == "read_file"
        assert result[0]["input"]["path"] == "src/main.py"

    @pytest.mark.asyncio
    async def test_thinking_events(self):
        events = [
            json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me think..."}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": " about this."}}),
            json.dumps({"type": "content_block_stop", "index": 0}),
        ]

        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
        )
        mock_client = self._setup_mock(events)
        thinking_chunks = []

        with patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client):
            result = await provider.call_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                on_thinking=lambda t: thinking_chunks.append(t),
            )

        assert len(result) == 1
        assert result[0]["type"] == "thinking"
        assert "".join(thinking_chunks) == "Let me think... about this."

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool(self):
        events = [
            json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me read"}}),
            json.dumps({"type": "content_block_stop", "index": 0}),
            json.dumps({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "read_file", "id": "t1"}}),
            json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"f.py"}'}}),
            json.dumps({"type": "content_block_stop", "index": 1}),
        ]

        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
        )
        mock_client = self._setup_mock(events)

        with patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client):
            result = await provider.call_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[{"name": "read_file", "input_schema": {}}],
            )

        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Let me read"
        assert result[1]["type"] == "tool_use"
        assert result[1]["input"]["path"] == "f.py"

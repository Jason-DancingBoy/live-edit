"""Tests for live_edit.provider — Provider interface and default implementation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live_edit.provider import AnthropicCompatibleProvider, ProviderExhaustedError, _FatalError


async def _async_iter(items):
    for item in items:
        yield item


class TestAnthropicCompatibleProvider:
    def _setup_mock(self, events):
        """Build mock httpx client that returns SSE events."""
        mock_response = MagicMock()
        mock_response.status_code = 200
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

    def _build_stream_ctx(self, status_code, body="", headers=None, events=None):
        """Build a mock httpx stream context returning an error or SSE response."""
        headers = headers or {}
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = headers

        if status_code >= 400:
            mock_response.aiter_text = MagicMock(return_value=_async_iter([body]))
        else:
            mock_response.aiter_lines = MagicMock(
                return_value=_async_iter([f"data: {e}" for e in (events or [])])
            )

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__.return_value = mock_response
        return mock_stream_ctx

    def _setup_mock_error(self, status_code, body="", headers=None, events=None):
        """Build mock httpx client whose single stream call returns an error/SSE response."""
        inner_client = MagicMock()
        inner_client.stream = MagicMock(
            return_value=self._build_stream_ctx(status_code, body, headers, events)
        )

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = inner_client

        return mock_client

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        events = [
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                }
            ),  # noqa: E501
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
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "name": "read_file", "id": "tool_1"},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"src/main.py"'},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "}"},
                }
            ),  # noqa: E501
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
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": " about this."},
                }
            ),  # noqa: E501
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
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Let me read"},
                }
            ),  # noqa: E501
            json.dumps({"type": "content_block_stop", "index": 0}),
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "name": "read_file", "id": "t1"},
                }
            ),  # noqa: E501
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":"f.py"}'},
                }
            ),  # noqa: E501
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

    @pytest.mark.asyncio
    async def test_retry_exhaustion_429_raises(self):
        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
            max_retries=2,
        )
        mock_client = self._setup_mock_error(status_code=429, headers={"Retry-After": "0"})
        inner_client = mock_client.__aenter__.return_value

        with (
            patch("live_edit.provider.asyncio.sleep", new=AsyncMock()),
            patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(ProviderExhaustedError) as exc_info,
        ):
            await provider.call_with_tools(messages=[{"role": "user", "content": "Hi"}], tools=[])

        assert exc_info.value.status == 429
        assert "retries" in str(exc_info.value)
        assert inner_client.stream.call_count == provider._max_retries + 1

    @pytest.mark.asyncio
    async def test_fatal_4xx_raises_immediately(self):
        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
            max_retries=2,
        )
        mock_client = self._setup_mock_error(status_code=401, body="Unauthorized")
        inner_client = mock_client.__aenter__.return_value

        with (
            patch("live_edit.provider.asyncio.sleep", new=AsyncMock()),
            patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(_FatalError) as exc_info,
        ):
            await provider.call_with_tools(messages=[{"role": "user", "content": "Hi"}], tools=[])

        assert "Unauthorized" in str(exc_info.value)
        assert inner_client.stream.call_count == 1

    @pytest.mark.asyncio
    async def test_transient_500_then_success_retries(self):
        events = [
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                }
            ),
            json.dumps({"type": "content_block_stop", "index": 0}),
        ]
        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
            max_retries=2,
        )
        ctx500 = self._build_stream_ctx(status_code=500, body="boom")
        ctx200 = self._build_stream_ctx(status_code=200, events=events)

        inner_client = MagicMock()
        inner_client.stream = MagicMock(side_effect=[ctx500, ctx200])

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = inner_client

        with (
            patch("live_edit.provider.asyncio.sleep", new=AsyncMock()),
            patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await provider.call_with_tools(
                messages=[{"role": "user", "content": "Hi"}], tools=[]
            )

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "ok"
        assert inner_client.stream.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhaustion_5xx_raises(self):
        provider = AnthropicCompatibleProvider(
            api_url="https://api.example.com/v1/messages",
            api_key="test-key",
            model="test-model",
            max_retries=2,
        )
        mock_client = self._setup_mock_error(status_code=500, body="boom")
        inner_client = mock_client.__aenter__.return_value

        with (
            patch("live_edit.provider.asyncio.sleep", new=AsyncMock()),
            patch("live_edit.provider.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(ProviderExhaustedError) as exc_info,
        ):
            await provider.call_with_tools(messages=[{"role": "user", "content": "Hi"}], tools=[])

        assert exc_info.value.status == 500
        assert "retries" in str(exc_info.value)
        assert inner_client.stream.call_count == provider._max_retries + 1

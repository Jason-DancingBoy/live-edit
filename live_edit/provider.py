"""LLM Provider interface and default Anthropic-compatible implementation."""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Callable

import httpx

logger = logging.getLogger("live-edit.provider")


class Provider(ABC):
    """LLM provider interface."""

    @abstractmethod
    async def call_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        on_thinking: Callable[[str], None] = None,
        on_text: Callable[[str], None] = None,
    ) -> list[dict]:
        """Call the model with tool definitions. Returns parsed content_blocks."""
        ...


class AnthropicCompatibleProvider(Provider):
    """Default: httpx async client + SSE streaming + tool_use input json accumulation."""

    def __init__(self, api_url: str, api_key: str, model: str, timeout: int = 180,
                 max_retries: int = 3):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self._timeout = timeout
        self._max_retries = max_retries

    async def close(self):
        """Explicit cleanup (httpx does connection pooling cleanup on exit)."""
        pass

    async def _call_once(
        self,
        messages: list[dict],
        tools: list[dict],
        on_thinking: Callable[[str], None] = None,
        on_text: Callable[[str], None] = None,
    ) -> list[dict]:
        """Single API call with SSE streaming. Returns content_blocks or raises."""
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": messages,
            "tools": tools,
            "temperature": 0.4,
            "stream": True,
        }

        content_blocks = []

        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST", self.api_url,
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
            ) as response:
                if response.status_code >= 400:
                    body = ""
                    try:
                        async for chunk in response.aiter_text():
                            body += chunk
                            if len(body) > 1000:
                                break
                    except Exception:
                        pass
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", "5")
                        raise _RetryableError(
                            f"Rate limited (429). Retry-After: {retry_after}s",
                            status=429, retry_after=float(retry_after))
                    elif response.status_code >= 500:
                        raise _RetryableError(
                            f"Server error ({response.status_code}): {body[:200]}",
                            status=response.status_code)
                    else:
                        raise _FatalError(
                            f"API error ({response.status_code}): {body[:200]}",
                            status=response.status_code)

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")

                    if event_type == "content_block_start":
                        idx = event["index"]
                        while len(content_blocks) <= idx:
                            content_blocks.append(None)
                        content_blocks[idx] = event["content_block"]
                        block = content_blocks[idx]
                        if block and block.get("type") == "tool_use":
                            block["_partial_json"] = ""

                    elif event_type == "content_block_delta":
                        idx = event["index"]
                        delta = event["delta"]
                        if idx >= len(content_blocks):
                            continue
                        block = content_blocks[idx]
                        if block is None:
                            continue

                        if delta.get("type") == "text_delta":
                            text = delta["text"]
                            block["text"] = block.get("text", "") + text
                            if on_text:
                                on_text(text)

                        elif delta.get("type") == "thinking_delta":
                            thinking_text = delta.get("thinking", "") or delta.get("text", "")
                            block["thinking"] = block.get("thinking", "") + thinking_text
                            if on_thinking:
                                on_thinking(thinking_text)

                        elif delta.get("type") == "input_json_delta":
                            partial = delta["partial_json"]
                            block["_partial_json"] = block.get("_partial_json", "") + partial

                    elif event_type == "content_block_stop":
                        idx = event["index"]
                        if idx >= len(content_blocks):
                            continue
                        block = content_blocks[idx]
                        if block and block.get("type") == "tool_use":
                            partial = block.pop("_partial_json", "")
                            if partial:
                                try:
                                    parsed = json.loads(partial)
                                    existing = block.get("input", {})
                                    if isinstance(existing, dict) and isinstance(parsed, dict):
                                        existing.update(parsed)
                                        block["input"] = existing
                                    else:
                                        block["input"] = parsed
                                except json.JSONDecodeError:
                                    pass

        for block in content_blocks:
            if block is not None:
                block.pop("_partial_json", None)

        return content_blocks

    async def call_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        on_thinking: Callable[[str], None] = None,
        on_text: Callable[[str], None] = None,
    ) -> list[dict]:
        """Call the model with tool definitions, with retry on transient errors."""
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._call_once(messages, tools, on_thinking, on_text)
            except _FatalError:
                raise
            except _RetryableError as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = e.retry_after if e.retry_after else (2 ** attempt)
                    logger.warning(
                        "Provider retry %d/%d after %.1fs: %s",
                        attempt + 1, self._max_retries, delay, e)
                    await asyncio.sleep(delay)
            except (httpx.TransportError, httpx.TimeoutException,
                    httpx.ConnectError, OSError) as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = 2 ** attempt
                    logger.warning(
                        "Provider retry %d/%d after %.1fs (network): %s",
                        attempt + 1, self._max_retries, delay, e)
                    await asyncio.sleep(delay)

        logger.error("Provider exhausted %d retries. Last error: %s",
                     self._max_retries, last_error)
        return None


# ── Internal error types for retry control ──

class _RetryableError(Exception):
    """Error that should be retried (429, 5xx, network)."""
    def __init__(self, message: str, status: int = 0, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _FatalError(Exception):
    """Error that should NOT be retried (4xx except 429)."""
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

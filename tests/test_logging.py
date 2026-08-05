# tests/test_logging.py
import asyncio
import io
import json
import logging

import pytest

from live_edit.logging import (
    JsonFormatter,
    configure_logging,
    get_correlation_id,
    get_session_id,
    set_correlation_id,
    set_session_id,
)


def _logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_json_formatter_emits_structured_fields():
    logger, stream = _logger("live-edit.test.formatter")
    logger.info("hello")
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "INFO"
    assert record["logger"] == "live-edit.test.formatter"
    assert record["message"] == "hello"
    assert record["session_id"] == ""
    assert record["correlation_id"] == ""


def test_json_formatter_includes_contextvars():
    logger, stream = _logger("live-edit.test.contextvars")
    set_session_id("le_abc")
    set_correlation_id("req_123")
    try:
        logger.info("with ctx")
        record = json.loads(stream.getvalue().strip())
        assert record["session_id"] == "le_abc"
        assert record["correlation_id"] == "req_123"
    finally:
        set_session_id("")
        set_correlation_id("")


def test_set_get_roundtrip():
    set_session_id("le_xyz")
    set_correlation_id("req_xyz")
    try:
        assert get_session_id() == "le_xyz"
        assert get_correlation_id() == "req_xyz"
    finally:
        set_session_id("")
        set_correlation_id("")


def test_configure_logging_targets_live_edit_namespace_only():
    configure_logging(level="DEBUG", json_logs=True)
    le_logger = logging.getLogger("live-edit")
    assert le_logger.level == logging.DEBUG
    assert not le_logger.propagate
    assert len(le_logger.handlers) == 1
    # The root logger is not touched by configure_logging.
    root_has_le_handler = any(h is le_logger.handlers[0] for h in logging.getLogger().handlers)
    assert not root_has_le_handler


@pytest.mark.asyncio
async def test_session_id_propagates_into_background_task():
    set_session_id("le_bg")

    async def read():
        return get_session_id()

    task = asyncio.create_task(read())
    assert await task == "le_bg"
    set_session_id("")
    set_correlation_id("")


@pytest.mark.asyncio
async def test_contextvars_isolated_across_concurrent_tasks():
    set_session_id("le_a")
    set_correlation_id("req_a")

    async def read_a():
        return get_session_id(), get_correlation_id()

    ta = asyncio.create_task(read_a())

    set_session_id("le_b")
    set_correlation_id("req_b")

    async def read_b():
        return get_session_id(), get_correlation_id()

    tb = asyncio.create_task(read_b())

    assert await ta == ("le_a", "req_a")
    assert await tb == ("le_b", "req_b")
    set_session_id("")
    set_correlation_id("")

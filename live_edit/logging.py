"""Structured JSON logging for the live-edit.* logger namespace."""

import contextvars
import datetime as _dt
import json
import logging

session_id_ctx = contextvars.ContextVar("live_edit.session_id", default="")
correlation_id_ctx = contextvars.ContextVar("live_edit.correlation_id", default="")


def set_session_id(sid: str) -> contextvars.Token:
    return session_id_ctx.set(sid)


def set_correlation_id(cid: str) -> contextvars.Token:
    return correlation_id_ctx.set(cid)


def get_session_id() -> str:
    return session_id_ctx.get()


def get_correlation_id() -> str:
    return correlation_id_ctx.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "session_id": session_id_ctx.get(),
            "correlation_id": correlation_id_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_logs: bool = True, stream=None) -> None:
    handler = logging.StreamHandler(stream)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("live-edit")
    logger.handlers[:] = [handler]
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

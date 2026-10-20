"""Contextual application logs without provider payloads or configuration secrets."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_CONTEXT: ContextVar[dict[str, str | None]] = ContextVar("avd_log_context", default={})
_EVENT_FIELDS = ("event", "lane", "job_type", "attempt", "status", "duration_ms", "method")


@contextmanager
def log_context(*, project_id: str | None = None, job_id: str | None = None) -> Iterator[None]:
    """Keep task identity local to this thread/async context and restore on exit."""
    context = {**_CONTEXT.get(), "project_id": project_id, "job_id": job_id}
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class ContextFormatter(logging.Formatter):
    """Render the same bounded fields in machine-readable JSON or plain text."""

    def __init__(self, log_format: str = "text") -> None:
        super().__init__()
        if log_format not in {"json", "text"}:
            raise ValueError("log_format must be text or json")
        self.log_format = log_format

    def format(self, record: logging.LogRecord) -> str:
        context = _CONTEXT.get()
        fields: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "project_id": getattr(record, "project_id", context.get("project_id")),
            "job_id": getattr(record, "job_id", context.get("job_id")),
        }
        fields.update({key: getattr(record, key) for key in _EVENT_FIELDS if hasattr(record, key)})
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        if self.log_format == "json":
            return json.dumps(fields, ensure_ascii=False, default=str)
        details = " ".join(
            f"{key}={value}"
            for key, value in fields.items()
            if key not in {"timestamp", "level", "logger", "message", "exception"}
            and value is not None
        )
        line = f"{fields['timestamp']} {record.levelname} {record.name}: {fields['message']}"
        if details:
            line += " " + details
        if "exception" in fields:
            line += "\n" + fields["exception"]
        return line


class _StderrHandler(logging.StreamHandler[Any]):
    def emit(self, record: logging.LogRecord) -> None:
        # Follow redirected stderr in CLI embedding/tests; never keep a closed capture stream.
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(log_format: str = "text", level: str = "INFO") -> None:
    """Install one application handler; preserve the host's existing log handlers."""
    logger = logging.getLogger("avd")
    handler = next((item for item in logger.handlers if isinstance(item, _StderrHandler)), None)
    if handler is None:
        handler = _StderrHandler()
        logger.addHandler(handler)
    handler.setFormatter(ContextFormatter(log_format))
    logger.setLevel(level)


__all__ = ["ContextFormatter", "configure_logging", "log_context"]

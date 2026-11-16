"""Structured logging: one JSON object per line, carrying the request ID.

Every HTTP request gets an ID (the caller's `X-Request-ID` if it's well formed, otherwise a
new one). It's kept in a context variable, so any log line written while handling that
request carries it without being passed around, and it's echoed back in the response's
`X-Request-ID` header so a user's report can be matched to the server's logs.

Secrets never reach a log line: credentials are decrypted only in the service layer and
never formatted into messages (the marker-secret test, QP-AC-08, checks every record).
Neither do monitored databases' query values (ADR 0010): PyMongo's command logging, which
would print profiler entries, is held at WARNING.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Attributes every LogRecord has; anything else was passed with `extra=` and is logged too.
# (uvicorn's `color_message` is its message again, with terminal colour codes.)
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}
_HANDLER_NAME = "query-pulse"


def new_request_id(incoming: str | None) -> str:
    """The caller's ID if it's safe to echo into logs and headers, otherwise a fresh one."""
    if incoming and _VALID_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            entry["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = (
                    value if isinstance(value, str | int | float | bool | None) else str(value)
                )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """For reading logs in a terminal during development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        request_id = request_id_var.get()
        return f"{line} [request_id={request_id}]" if request_id else line


def configure_logging(log_format: Literal["json", "text"] = "json", level: str = "INFO") -> None:
    """Send every log line (ours and uvicorn's) through one handler on the root logger.

    Idempotent, and it leaves other handlers alone (pytest's log capture, for one).
    uvicorn's own access log is switched off: the request middleware writes a structured
    access line with the request ID instead.
    """
    root = logging.getLogger()
    root.setLevel(level)
    handler = next((h for h in root.handlers if h.get_name() == _HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name(_HANDLER_NAME)
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    # PyMongo's debug loggers write whole commands and replies, and a monitored MongoDB's
    # profiler entries carry real query values (ADR 0010). Never below WARNING, whatever
    # QP_LOG_LEVEL says.
    logging.getLogger("pymongo").setLevel(logging.WARNING)

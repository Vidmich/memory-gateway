"""Structured JSON logging with a per-request correlation id.

Every log line the service emits is a single JSON object carrying ``request_id``, so a
request can be reconstructed end to end from the log stream alone. The id is held in a
contextvar, which means library code logs it without being passed anything.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
#: Set by :class:`app.core.tracing.TracingMiddleware` when a span is being recorded,
#: so a log line can be taken to the trace it belongs to and back. Kept here, as a
#: plain contextvar, rather than read from OpenTelemetry inside the formatter: logging
#: has to work in a process with no tracing configured, and in one where the SDK is not
#: installed at all.
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

# Attributes LogRecord always carries; anything else was passed as `extra=` and is
# promoted to a top-level field.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "color_message",  # uvicorn's ANSI copy of the message
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def get_request_id() -> str | None:
    return request_id_var.get()


def get_trace_id() -> str | None:
    return trace_id_var.get()


@contextmanager
def bind_request_id(request_id: str | None) -> Iterator[None]:
    """Attach a correlation id to everything logged inside the block.

    The HTTP middleware does this per request. A worker does it per *job*, so a log line
    written minutes later on another process still carries the id of the control-plane
    request that enqueued the work — which is the only thread joining "my upload did
    nothing" to what actually happened.
    """
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


def _isoformat(epoch_seconds: float) -> str:
    moment = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON."""

    def __init__(self, service_name: str, version: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _isoformat(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
            "request_id": request_id_var.get(),
        }
        # Omitted rather than null when there is no trace: a field that is always
        # present and almost always empty is a column every log query has to ignore.
        if trace_id := trace_id_var.get():
            payload["trace_id"] = trace_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(*, level: str, service_name: str, version: str) -> None:
    """Install the JSON handler as the only root handler.

    Uvicorn's own handlers are removed rather than reformatted: leaving them attached
    produces every line twice, once JSON and once plain.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name=service_name, version=version))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # The access log is emitted by our own middleware, with timing and the request id.
    logging.getLogger("uvicorn.access").disabled = True

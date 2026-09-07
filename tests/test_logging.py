"""Every log line must be a single valid JSON object carrying the request id."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.core.logging import JsonFormatter, configure_logging, request_id_var


@pytest.fixture
def formatter() -> JsonFormatter:
    return JsonFormatter(service_name="memory-gateway", version="0.1.0")


def _record(**extra: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_output_is_one_json_object(formatter: JsonFormatter) -> None:
    line = formatter.format(_record())

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["service"] == "memory-gateway"
    assert payload["ts"].endswith("Z")


def test_request_id_is_bound_from_the_contextvar(formatter: JsonFormatter) -> None:
    token = request_id_var.set("0192f0a1-0000-7000-8000-000000000000")
    try:
        payload = json.loads(formatter.format(_record()))
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "0192f0a1-0000-7000-8000-000000000000"


def test_request_id_is_present_even_when_unset(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record()))

    assert "request_id" in payload
    assert payload["request_id"] is None


def test_extras_become_top_level_fields(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record(gateway_id="gw_1", duration_ms=12.5)))

    assert payload["gateway_id"] == "gw_1"
    assert payload["duration_ms"] == 12.5


def test_unserializable_values_do_not_break_the_line(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record(obj=object())))

    assert isinstance(payload["obj"], str)


def test_exceptions_are_rendered_into_the_object(formatter: JsonFormatter) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    payload = json.loads(formatter.format(record))

    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_installs_exactly_one_handler() -> None:
    configure_logging(level="INFO", service_name="memory-gateway", version="0.1.0")
    configure_logging(level="DEBUG", service_name="memory-gateway", version="0.1.0")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").disabled is True

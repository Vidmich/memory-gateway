"""Request-id propagation, access logging, and metrics labelling."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.core.logging import JsonFormatter
from app.core.middleware import REQUEST_ID_HEADER


class CapturingHandler(logging.Handler):
    """Formats records as they are emitted, so contextvars are still bound."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter(service_name="memory-gateway", version="0.1.0"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lines]


@pytest.fixture
def access_log() -> Iterator[CapturingHandler]:
    handler = CapturingHandler()
    logger = logging.getLogger("app.access")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


async def test_request_id_is_generated_and_returned(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert uuid.UUID(response.headers[REQUEST_ID_HEADER]).version == 7


async def test_inbound_request_id_is_honoured(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={REQUEST_ID_HEADER: "trace-from-caller"})

    assert response.headers[REQUEST_ID_HEADER] == "trace-from-caller"


async def test_x_request_id_is_accepted_as_a_fallback(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-Id": "upstream-trace"})

    assert response.headers[REQUEST_ID_HEADER] == "upstream-trace"


async def test_request_ids_differ_between_requests(client: AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_access_log_is_json_and_carries_the_request_id(
    client: AsyncClient, access_log: CapturingHandler
) -> None:
    response = await client.get("/healthz", headers={REQUEST_ID_HEADER: "abc-123"})

    line = access_log.payloads[0]
    assert line["request_id"] == "abc-123"
    assert line["route"] == "/healthz"
    assert line["method"] == "GET"
    assert line["status"] == response.status_code
    assert isinstance(line["duration_ms"], float)


async def test_metrics_label_by_route_template_not_raw_path(client: AsyncClient) -> None:
    await client.get("/nope/12345")
    body = (await client.get("/metrics")).text

    assert 'route="<unmatched>"' in body
    assert "12345" not in body

"""The write path: collection, the tee, the drop policy, and the flusher.

Nothing here touches a database or a socket. That is the point of the design under test —
the request path does no I/O — so a test of it should not need any either. The one test
that lets the real timer run is :func:`test_the_flusher_writes_without_being_asked`, and
it exists so the rest can safely drive the flush by hand.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest
from prometheus_client import CollectorRegistry

from app.core.ids import uuid7
from app.core.metrics import build_log_metrics
from app.schemas.gateway_config import LoggingConfig
from app.schemas.openai import ChatChunk, ChatRequest, ChatResponse, StreamFrame
from app.services.log_store import MemoryLogWriter, transcript_row
from app.services.request_log import (
    MAX_RESPONSE_CHARS,
    LogFlusher,
    LogPolicy,
    LogQueue,
    RequestRecord,
    StreamTee,
    as_json,
)
from tests.monitoring_support import build_logs
from tests.support import chunk, completion, make_target


def record(**overrides: object) -> RequestRecord:
    values: dict[str, object] = {"organization_id": uuid7(), "gateway_id": uuid7()}
    values.update(overrides)
    return RequestRecord(**values)  # type: ignore[arg-type]


def request(content: str = "hello", *, stream: bool = False) -> ChatRequest:
    return ChatRequest.model_validate(
        {"model": "acme-chat", "messages": [{"role": "user", "content": content}], "stream": stream}
    )


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


def test_the_default_policy_captures_everything() -> None:
    """SPEC §10.2's default is full capture, because that is what makes task 13 possible."""
    policy = LogPolicy.of(LoggingConfig())

    assert (policy.request_body, policy.assembled_prompt, policy.response_body) == (
        True,
        True,
        True,
    )


def test_a_toggle_is_applied_at_collection_not_at_write() -> None:
    """Nothing is even copied when a toggle is off — the cheapest possible "off"."""
    logs = build_logs()
    recorder = logs.service.begin(
        organization_id=uuid7(),
        gateway_id=uuid7(),
        policy=LogPolicy(request_body=False),
    )

    recorder.client_request(request("do not store me"))

    assert recorder.record.request_body is None


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


async def test_a_completed_request_records_what_happened() -> None:
    logs = build_logs()
    target = make_target("http://upstream.invalid/v1")
    recorder = logs.service.begin(
        organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy(), request_id="req-1"
    )

    recorder.client_request(request())
    recorder.prepared(request().messages, target)
    recorder.upstream_call_started()
    recorder.from_response(ChatResponse.model_validate(completion("hi there")))
    recorder.submit()
    await logs.flush()

    (row,) = logs.rows
    assert row.status_code == 200
    assert row.model_name == target.name
    assert row.upstream_model_id == target.id
    assert row.request_id == "req-1"
    assert row.prompt_tokens == 3
    assert row.completion_tokens == 2
    assert logs.transcript(row.id) is not None
    assert logs.transcript(row.id).response_body == "hi there"  # type: ignore[union-attr]


async def test_a_failure_records_the_code_and_the_status() -> None:
    from app.api.proxy.errors import UpstreamTimeout

    logs = build_logs()
    recorder = logs.service.begin(organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy())

    recorder.failed(UpstreamTimeout("[upstream:acme-gpt] did not respond within 5s."))
    recorder.submit()
    await logs.flush()

    (row,) = logs.rows
    assert row.status_code == 504
    assert row.error_code == "upstream_timeout"
    assert "acme-gpt" in (row.error_message or "")


async def test_an_unexpected_exception_is_a_500_not_a_lost_row() -> None:
    logs = build_logs()
    recorder = logs.service.begin(organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy())

    recorder.failed(ZeroDivisionError("boom"))
    recorder.submit()
    await logs.flush()

    (row,) = logs.rows
    assert (row.status_code, row.error_code) == (500, "internal_error")
    # The class name, not the message: an unexpected exception's text can carry anything,
    # including the values it was computing over.
    assert row.error_message == "ZeroDivisionError"


async def test_submitting_twice_writes_one_row() -> None:
    """The stream path and the route's ``except`` can both reach ``submit``."""
    logs = build_logs()
    recorder = logs.service.begin(organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy())

    recorder.submit()
    recorder.submit()
    await logs.flush()

    assert len(logs.rows) == 1


def test_ttft_is_recorded_once() -> None:
    logs = build_logs()
    recorder = logs.service.begin(organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy())

    recorder.first_token()
    first = recorder.record.latency_ttft_ms
    recorder.first_token()

    assert recorder.record.latency_ttft_ms == first


def test_a_non_streamed_request_has_no_ttft() -> None:
    """Not zero, and not the total: putting the total here would drag the TTFT
    percentiles toward the full generation time and make the chart meaningless."""
    logs = build_logs()
    recorder = logs.service.begin(organization_id=uuid7(), gateway_id=uuid7(), policy=LogPolicy())

    recorder.from_response(ChatResponse.model_validate(completion()))

    assert recorder.record.latency_ttft_ms is None


# ---------------------------------------------------------------------------
# the tee
# ---------------------------------------------------------------------------


def frame(text: str) -> StreamFrame:
    payload = chunk(text)
    return StreamFrame(data=payload, chunk=ChatChunk.model_validate(json.loads(payload)))


def test_a_stream_reassembles_into_the_same_text_a_completion_would_store() -> None:
    """The acceptance criterion, stated directly: same prompt, same seed, same transcript.

    Both paths store the concatenation of the assistant's content and nothing else, so
    the two can be compared as strings rather than as "close enough".
    """
    tee = StreamTee()
    for part in ("Hel", "lo, ", "world"):
        tee.observe(frame(part))

    streamed = tee.text
    non_streamed = completion("Hello, world")["choices"][0]["message"]["content"]

    assert streamed == non_streamed == "Hello, world"
    assert tee.truncated is False


def test_a_runaway_generation_is_truncated_with_a_marker() -> None:
    tee = StreamTee(limit=10)
    tee.observe(frame("0123456789abcdef"))

    assert tee.text == "0123456789"
    assert tee.truncated is True


def test_frames_after_the_cap_are_not_accumulated() -> None:
    tee = StreamTee(limit=4)
    tee.observe(frame("abcd"))
    tee.observe(frame("efgh"))

    assert tee.text == "abcd"
    assert tee.truncated is True


def test_the_cap_is_generous_enough_for_a_real_answer() -> None:
    """A guard against tuning the limit down to something a normal completion hits."""
    assert MAX_RESPONSE_CHARS > 100_000


def test_usage_is_taken_from_the_frame_that_carries_it() -> None:
    tee = StreamTee()
    payload = json.dumps(
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18}}
    )
    tee.observe(StreamFrame(data=payload, chunk=ChatChunk.model_validate(json.loads(payload))))

    assert tee.usage is not None
    assert (tee.usage.prompt_tokens, tee.usage.completion_tokens) == (7, 11)


def test_a_frame_that_did_not_parse_is_ignored() -> None:
    """Providers send keepalives and vendor extensions. Neither is a token."""
    tee = StreamTee()
    tee.observe(StreamFrame(data="{not json}", chunk=None))

    assert tee.text == ""


def test_capture_off_means_no_text_at_all() -> None:
    tee = StreamTee(capture=False)
    tee.observe(frame("hello"))

    assert tee.text is None


def test_usage_is_still_collected_when_the_body_is_not() -> None:
    """Token counts are metadata, and ``log_metadata`` is forced on. Switching off body
    capture must not also switch off the token-count chart."""
    tee = StreamTee(capture=False)
    payload = json.dumps({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
    tee.observe(StreamFrame(data=payload, chunk=ChatChunk.model_validate(json.loads(payload))))

    assert tee.usage is not None
    assert tee.usage.prompt_tokens == 2


# ---------------------------------------------------------------------------
# the queue and its drop policy
# ---------------------------------------------------------------------------


def test_below_the_watermark_everything_is_kept() -> None:
    logs = build_logs(maxsize=10, shed_fraction=0.8)

    logs.queue.submit(record(request_body=[{"role": "user"}]))

    assert logs.queue.qsize() == 1
    assert logs.counter("bodies") == 0


def test_above_the_watermark_bodies_go_first() -> None:
    """Metadata answers "how many, how fast, how many failed". A missing row answers
    nothing, so the expensive half is shed first and well before the queue is full."""
    logs = build_logs(maxsize=10, shed_fraction=0.5)
    for _ in range(5):
        logs.queue.submit(record())

    logs.queue.submit(record(request_body=[{"role": "user"}], response_body="hi"))

    kept = logs.queue.drain_now()[-1]
    assert kept.request_body is None
    assert kept.response_body is None
    assert kept.bodies_omitted == "queue_pressure"
    assert logs.counter("bodies") == 1


def test_a_full_queue_drops_the_record_and_counts_it() -> None:
    logs = build_logs(maxsize=2, shed_fraction=1.0)
    for _ in range(3):
        logs.queue.submit(record())

    assert logs.queue.qsize() == 2
    assert logs.counter("record") == 1


def test_a_full_queue_never_raises_into_the_request_path() -> None:
    """The property the whole design exists for: logging cannot fail a request."""
    logs = build_logs(maxsize=1, shed_fraction=1.0)

    for _ in range(100):
        logs.queue.submit(record())  # no exception


# ---------------------------------------------------------------------------
# the flusher
# ---------------------------------------------------------------------------


async def test_redaction_happens_before_the_row_is_written() -> None:
    """The raw value must never reach the writer, not merely never be displayed."""
    logs = build_logs()
    logs.queue.submit(
        record(
            policy=LogPolicy(redaction_patterns=("ada@example.com",)),
            request_body=[{"role": "user", "content": "mail ada@example.com"}],
            response_body="reply to ada@example.com",
        )
    )

    await logs.flush()

    (row,) = logs.rows
    stored = logs.transcript(row.id)
    assert stored is not None
    assert "ada@example.com" not in json.dumps(stored.request_body)
    assert "ada@example.com" not in (stored.response_body or "")


async def test_an_unfinishable_redaction_drops_the_bodies() -> None:
    """Fail closed. A partly-redacted body is worse than none, because it looks clean.

    Driven through the real budget rather than a stubbed redactor: a negative budget means
    the deadline is already past on the first check, which is the same branch a
    pathological pattern reaches after burning a quarter of a second.
    """
    logs = build_logs(redaction_budget_seconds=-1.0)
    logs.queue.submit(
        record(
            policy=LogPolicy(redaction_patterns=("secret",)),
            request_body=[{"role": "user", "content": "a secret"}],
        )
    )

    await logs.flush()

    (row,) = logs.rows
    assert row.bodies_omitted == "redaction_budget"
    assert logs.transcript(row.id) is None
    assert logs.counter("redaction") == 1


async def test_a_write_failure_loses_the_batch_and_counts_it() -> None:
    """Not retried, deliberately: a retry loop in front of a bounded queue turns a
    database outage into unbounded memory growth and then into dropped records anyway."""
    registry = CollectorRegistry()
    metrics = build_log_metrics(registry)
    queue = LogQueue(metrics=metrics)

    class Broken:
        async def write(self, records: Sequence[RequestRecord]) -> None:
            raise RuntimeError("the database is down")

    flusher = LogFlusher(queue, Broken(), metrics=metrics)
    queue.submit(record())
    queue.submit(record())

    await flusher.flush_pending()

    assert registry.get_sample_value("logs_dropped_total", {"reason": "write_failed"}) == 2
    assert (registry.get_sample_value("logs_written_total") or 0) == 0


async def test_the_flusher_writes_without_being_asked() -> None:
    """The one test that lets the real timer run, so the rest can flush by hand."""
    registry = CollectorRegistry()
    metrics = build_log_metrics(registry)
    queue = LogQueue(metrics=metrics)
    writer = MemoryLogWriter()
    flusher = LogFlusher(queue, writer, metrics=metrics, interval_seconds=0.01)

    flusher.start()
    try:
        queue.submit(record())
        for _ in range(200):
            if writer.database.request_logs:
                break
            await asyncio.sleep(0.01)
    finally:
        await flusher.stop()

    assert len(writer.database.request_logs) == 1


async def test_shutdown_flushes_what_is_still_queued() -> None:
    """A rolling deploy stops processes constantly; without this every one loses up to
    half a second of every replica's traffic."""
    logs = build_logs()
    logs.service.start()
    logs.queue.submit(record())

    await logs.service.stop()

    assert len(logs.rows) == 1


async def test_a_batch_stops_at_its_size_limit() -> None:
    registry = CollectorRegistry()
    metrics = build_log_metrics(registry)
    queue = LogQueue(metrics=metrics)
    for _ in range(10):
        queue.submit(record())

    batch = await queue.take_batch(max_records=4, interval_seconds=5)

    assert len(batch) == 4


async def test_a_partial_batch_gives_up_after_the_interval() -> None:
    registry = CollectorRegistry()
    metrics = build_log_metrics(registry)
    queue = LogQueue(metrics=metrics)
    queue.submit(record())

    batch = await asyncio.wait_for(
        queue.take_batch(max_records=100, interval_seconds=0.02), timeout=2
    )

    assert len(batch) == 1


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def test_a_record_with_no_bodies_writes_no_transcript() -> None:
    """``transcripts`` is a row that exists or does not; there is no half-filled state."""
    assert transcript_row(record()) is None


def test_a_transcript_shares_its_metadata_rows_timestamp() -> None:
    """Both tables are partitioned on ``created_at``. A transcript written a moment after
    midnight would otherwise land in tomorrow's partition and be dropped separately."""
    one = record(response_body="hi")

    row = transcript_row(one)

    assert row is not None
    assert row["created_at"] == one.created_at


def test_an_unserialisable_body_does_not_poison_the_batch() -> None:
    assert as_json({1, 2, 3}) == [{"role": "system", "content": "[unserialisable body]"}]


@pytest.mark.parametrize("value", [None, [{"role": "user", "content": "hi"}]])
def test_a_serialisable_body_passes_through(value: object) -> None:
    assert as_json(value) == value

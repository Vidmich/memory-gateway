"""UUIDv7 must be well-formed and monotonic — task 07's log partitions depend on it."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.core.ids import timestamp_ms_of, uuid7, uuid7_str


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10  # RFC 4122 variant


def test_timestamp_is_current() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= timestamp_ms_of(value) <= after


def test_ids_are_monotonic_within_a_millisecond() -> None:
    values = [uuid7() for _ in range(5_000)]
    assert values == sorted(values), "uuid7 must sort by creation order"
    assert len(set(values)) == len(values)


def test_ids_are_unique_across_threads() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: uuid7(), range(4_000)))

    assert len(set(values)) == len(values)


def test_string_helper_round_trips() -> None:
    value = uuid7_str()
    assert uuid.UUID(value).version == 7

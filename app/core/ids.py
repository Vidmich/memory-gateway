"""UUIDv7 generation (RFC 9562).

Chosen over UUIDv4 for primary keys: the leading 48-bit millisecond timestamp gives
index locality on insert and makes ids sort by creation time, which every cursor page
in the system relies on. It also carries its own timestamp, which is how a request-log
lookup by id knows which day's partition to read. Unlike a bigserial it does not leak
row counts.

Python 3.12 has no ``uuid.uuid7``; this is a small, dependency-free implementation with a
monotonic counter so ids minted within the same millisecond still sort in creation order.
"""

from __future__ import annotations

import os
import time
import uuid
from threading import Lock

_MAX_COUNTER = 0xFFF  # 12 bits of rand_a used as a sub-millisecond sequence

_lock = Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    """Return a new time-ordered UUIDv7."""
    global _last_ms, _counter

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms, _counter = now_ms, 0
        else:
            # Same millisecond, or a clock that stepped backwards: keep advancing.
            _counter += 1
            if _counter > _MAX_COUNTER:
                _last_ms += 1
                _counter = 0
        timestamp_ms, sequence = _last_ms, _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80  # unix_ts_ms
    value |= 0x7 << 76  # version
    value |= sequence << 64  # rand_a, used as a counter
    value |= 0b10 << 62  # variant
    value |= rand_b

    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80

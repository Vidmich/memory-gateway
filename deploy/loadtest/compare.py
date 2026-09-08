"""Compare a k6 run against a committed baseline, and fail if it got worse.

A load test that prints numbers is a load test nobody reads after the first week. What
makes it a regression check is a baseline in version control and a script that says
"p95 is 40% worse than the number this branch inherited" — which is a review comment
rather than a chart somebody has to remember to open.

    k6 run --summary-export=results/chat.json deploy/loadtest/chat.js
    python deploy/loadtest/compare.py results/chat.json --baseline deploy/loadtest/baselines/ci.json

Tolerances are generous on purpose. Shared CI runners vary by more than a real regression
does, so a tight bound produces a flaky gate that gets disabled, which is worse than a
loose one that catches the change from 80 ms to 400 ms. The number this suite exists for —
gateway overhead against a direct-to-provider baseline — is checked exactly rather than
proportionally, because it is a promise in SPEC §4.2 with an absolute value in it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

#: Fractional worsening allowed before a metric is called a regression.
DEFAULT_TOLERANCE = 0.4

#: SPEC §4.2. Absolute, in milliseconds, and not subject to the tolerance above.
OVERHEAD_BUDGET_MS = 150.0

#: What is compared. Anything else in the summary is context rather than a gate.
WATCHED = (
    ("http_req_duration", "p(95)"),
    ("http_req_duration", "p(99)"),
    ("ttft_ms", "p(95)"),
    ("latency_via_gateway", "p(95)"),
)


def values(summary: dict[str, Any], metric: str, stat: str) -> float | None:
    entry = summary.get("metrics", {}).get(metric)
    if not entry:
        return None
    found = entry.get("values", entry).get(stat)
    return float(found) if isinstance(found, int | float) else None


def compare(current: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> list[str]:
    failures: list[str] = []
    for metric, stat in WATCHED:
        now = values(current, metric, stat)
        before = values(baseline, metric, stat)
        if now is None or before is None:
            # A metric that only one scenario produces is absent from the other's summary.
            # Silence rather than a failure: this script is run against several scenarios
            # and each carries a different subset.
            continue
        allowed = before * (1 + tolerance)
        verdict = "OK  " if now <= allowed else "FAIL"
        print(
            f"  {verdict} {metric} {stat}: {now:.1f} (baseline {before:.1f}, allowed {allowed:.1f})"
        )
        if now > allowed:
            failures.append(
                f"{metric} {stat} is {now:.1f}, which is more than {tolerance:.0%} worse "
                f"than the baseline of {before:.1f}"
            )
    return failures


def check_budget(current: dict[str, Any]) -> list[str]:
    """The overhead scenario's own verdict, if this summary came from it."""
    overhead = current.get("overhead")
    if not isinstance(overhead, dict):
        return []
    p95 = float(overhead.get("p95", 0.0))
    verdict = "OK  " if p95 < OVERHEAD_BUDGET_MS else "FAIL"
    print(
        f"  {verdict} gateway overhead p95: {p95:.1f} ms "
        f"(SPEC §4.2 budget {OVERHEAD_BUDGET_MS:.0f} ms)"
    )
    if p95 >= OVERHEAD_BUDGET_MS:
        return [
            f"gateway overhead p95 is {p95:.1f} ms, over the {OVERHEAD_BUDGET_MS:.0f} ms budget"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", help="the --summary-export JSON from a k6 run")
    parser.add_argument(
        "--baseline", required=True, help="the committed baseline to compare against"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"fractional worsening allowed (default {DEFAULT_TOLERANCE})",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="overwrite the baseline with this run instead of comparing to it",
    )
    args = parser.parse_args(argv)

    current = json.loads(pathlib.Path(args.summary).read_text(encoding="utf-8"))
    baseline_path = pathlib.Path(args.baseline)

    if args.update:
        baseline_path.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline written to {baseline_path}")
        return 0

    if not baseline_path.is_file():
        print(f"no baseline at {baseline_path}; record one with --update", file=sys.stderr)
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(f"comparing {args.summary} against {baseline_path}")
    failures = compare(current, baseline, args.tolerance) + check_budget(current)
    if failures:
        print("\nregressions:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nno regressions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

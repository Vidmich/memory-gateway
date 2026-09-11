# Task 08 — Routing modes: failover & A/B

**Slice:** a gateway survives an upstream outage, and can split traffic across models by
percentage.
**Depends on:** 07
**Spec:** §8.1, §8.2, §13.1 (Gateways → Routing)
**Size:** M

> **Milestone M3.** With logging already in place, routing behavior is visible the moment it
> exists.

---

## Why this slice

Both remaining routing modes are small in code and large in operational value. Sequencing them
after monitoring means the demo is self-proving: you don't assert that the 70/30 split works, you
point at the chart.

## Demo at the end of this task

**Failover:** configure a gateway with a deliberately broken model first and a working one
second. Send a request — it succeeds. The request detail drawer shows attempt 1 failing with its
error and attempt 2 succeeding, with per-attempt timings.

**A/B:** configure 70% model A, 30% model B. Fire 200 requests. The monitoring page's per-model
distribution chart shows roughly 70/30. Repeat with a fixed `user` id — every request lands on
the same model, proving sticky assignment.

## In scope

- `failover` and `ab_split` modes over `gateway_targets`.
- Retry classification, streaming constraint, sticky weighted selection.
- Attempt recording in request logs and the detail drawer.
- Routing UI: mode selector, ordered failover list, weight sliders.

## Out of scope

- Circuit breaking and health checks (SPEC §16.9) — plain failover only. No target ejection, no
  background probing.
- Model-name-based routing (one gateway exposing several selectable models).
- Cost-aware or latency-aware routing.

## Work items

### Routing service
- [x] `RoutingPolicy` resolving a gateway to an ordered attempt list:
      - `single` — one target.
      - `failover` — all targets in `priority` order.
      - `ab_split` — one target chosen by weight; the list has length 1 (a failure is returned to
        the client, per SPEC §8.1).
- [x] **Retry classification**, as an explicit table not scattered conditionals:
      - Retry: connect error, DNS failure, read timeout, 408, 429, 500, 502, 503, 504.
      - Do not retry: 400, 401, 403, 404, 422 — the next target rejects them identically.
- [x] Per-attempt timeout from the target model's `timeout_seconds`, plus an overall request
      deadline so a long chain cannot exceed it. When the deadline is hit, stop and return the
      last error.
- [x] Small jittered backoff between attempts (default 50–150 ms) to avoid synchronizing retries
      across a fleet during a provider incident.

### Streaming constraint
- [x] Failover is permitted **only before the first byte reaches the client**. Buffer nothing to
      extend this window — that would defeat streaming.
- [x] Once a chunk is flushed, an upstream failure terminates the stream with an SSE error event
      and sets `failed_after_stream_start = true` on the log.
- [x] Documented explicitly in the UI next to the mode selector, because it is genuinely
      surprising: "streaming responses cannot fail over once output has begun."

### A/B selection
- [x] Weights are integers summing to exactly 100; validated at save time, not at request time.
- [x] **Sticky** when an end-user id is present:
      `bucket = crc32(f"{end_user_id}:{gateway_id}") % 100`, mapped onto cumulative weight bands.
      Salting with the gateway id keeps a user from landing in the same bucket across gateways.
- [x] Without an end-user id, uniform random selection.
- [x] Changing weights re-buckets existing users — acceptable and documented; a stable-assignment
      table is out of scope.
- [x] End-user identity resolution proper lands in task 12; here, read `request.user` and the
      `X-Gateway-User` header directly through a small helper that task 12 replaces.

### Logging integration
- [x] `failover_attempts_jsonb`: an array of `{target_id, model_name, status, error_code,
      latency_ms, retryable}` per attempt.
- [x] `upstream_model_id` records the target that actually served the response.
- [x] New metrics: attempts per request, failover trigger rate per target, and per-target traffic
      share.

### UI
- [x] Routing section of the gateway editor, replacing the task 06 single-target picker:
      - Mode selector with one-line explanations of each mode's failure behavior.
      - **Failover**: drag-orderable target list with priority numbers and an add/remove control.
      - **A/B**: rows of model + weight slider and numeric input, with a running total that
        blocks save unless it equals 100, and a live "expected split" bar.
      - Inline warning for streaming + failover.
- [x] Monitoring: per-target distribution chart with the configured weights overlaid, so drift
      between intended and actual split is visible at a glance.
- [x] Request detail drawer: an attempts timeline when more than one attempt occurred.

## Acceptance criteria

- [x] With a broken primary, a failover gateway returns a successful response and both attempts
      appear in the log.
- [x] A 400 from the primary is returned to the client without trying the secondary.
- [x] Streaming failover works before first byte; after first byte the stream terminates with an
      error event and the log flag is set.
- [x] Over 1000 requests without a user id, an A/B split of 70/30 lands within ±3 points.
- [x] With a fixed user id, 100 consecutive requests hit the same target.
- [x] Weights not summing to 100 cannot be saved.
- [x] The overall deadline is respected even with a long failover chain of slow targets.

## Tests

- Retry classification for every status code in the table.
- Failover: primary fails / all fail (returns the last error) / primary succeeds (secondary never
  called).
- Streaming: failure before first byte recovers; failure after first byte terminates and flags.
- Sticky bucketing: distribution over many synthetic user ids matches weights; the same id is
  stable across calls; the same id differs across gateways.
- Deadline enforcement across a chain.
- Attempt records serialized correctly into the log.

## Notes

- Circuit breaking is intentionally deferred. Plain failover already delivers most of the
  availability benefit, and a breaker adds shared state and flapping behavior that needs the
  metrics from this task to tune sensibly.
- Do not add retry to `ab_split`. A retried A/B request would land on a different variant and
  silently corrupt the comparison the mode exists to enable.

---

## Verification status

Every box above is ticked because it was built and checked, not because it was read.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   171 files already formatted
uv run mypy                    Success: no issues found in 162 source files
uv run pytest -q               1532 passed, 239 skipped

npx eslint . / npx tsc         clean
npx vitest run                 233 passed
npm run build                  350.74 kB JS (106.48 kB gzipped)
```

`web/openapi.json` and `web/src/api/schema.d.ts` regenerated; `make openapi-check` is
byte-stable.

### The demo, over real sockets

`smoke08.py` ran the whole demo with two providers on two ports, the gateway on a third
and a real HTTP client in front — nothing mocked at the transport level, so a failover is
a second TCP connection to a second server and a stream that dies mid-body is a socket
that stops producing bytes. **30/30 checks passed**, covering:

* a broken primary answered by the secondary, with both attempts on the row;
* a 400 returned without touching the secondary;
* a healthy primary that leaves the secondary idle and writes no timeline;
* an exhausted chain returning the *last* error, with every attempt recorded;
* a stream failing over before the first byte, and terminating cleanly;
* a stream that dies mid-body: still a 200, partial output delivered, an SSE error event,
  no failover, `error_code=stream_failed` and `failed_after_stream_start=true`;
* **1000 anonymous requests at 70/30 landing at 67.9% — inside the ±3 point criterion**;
* 100 requests from one `user`, and 50 more carrying `X-Gateway-User`, each hitting one
  target;
* an A/B failure reaching the client without touching the other arm;
* two hung targets at 30 s timeouts each answering in **1.02 s** under a 1 s deadline.

The script was deleted after the run; every claim it made has a test that keeps making it
(`tests/test_proxy_failover.py`, `tests/test_routing.py`).

### The A/B distribution, made deterministic

`test_a_thousand_anonymous_requests_land_within_three_points` seeds `random` before the
draws. A thousand uniform draws at 70/30 has a standard deviation of about 1.4 points, so
an unseeded version would fail roughly one run in twenty — and a flaky test about a
statistical property is worse than none, because it teaches people to re-run it. The
*sticky* distribution needs no seed: `crc32` over 2000 synthetic ids is deterministic by
construction, which is the stronger of the two tests and the one that matches how A/B is
actually used.

### Deliberate deviations

Each of these differs from the obvious reading of the work items, and each was a choice.

* **`model_id` survives alongside `targets`.** The work item says the routing section
  replaces the single-target picker, and in the UI it does. The *API* keeps `model_id` as
  the one-target shorthand, because it is how task 06's API said it and removing it would
  break every script written against it for no gain. Sending both is a 422 rather than a
  precedence rule; a precedence rule is a thing somebody has to look up and gets wrong
  once.
* **Failover requires two targets and A/B requires two, checked at save time.** A
  `failover` gateway with one target claims a property it does not have. An empty chain is
  still legal in every mode, because a gateway can exist before its models do.
* **`_check_chain` runs before `add_gateway`**, so a chain the caller got wrong leaves no
  half-created gateway behind. PostgreSQL would have rolled it back; the memory store
  would not have, and that asymmetry was a test failure waiting to be misread.
* **Weights are never normalised.** 70/20 is refused, not stored as 78/22. The weights are
  somebody's experiment, and silently rescaling them changes the result and tells nobody.
  Selection still divides by the *real* total, so a chain written around the API — or one
  a disabled model dropped out of — degrades in proportion instead of sending a slice of
  traffic nowhere.
* **`failed_after_stream_start` is not set for a client hang-up.** The flag means "this
  could not be failed over because the response had already begun", and a caller walking
  away is not something failover would have rescued. `client_disconnected` still lands in
  `error_code`.
* **The failover window closes when `open_stream` returns**, not at the first frame.
  Pulling one frame inside the loop would widen it slightly and was rejected: it delays the
  status line by the whole time-to-first-token, and the work item says to buffer nothing.
* **`failover_attempts` is empty unless more than one target was involved.** A
  one-element array on every request would spend storage on every row restating what
  `upstream_model_id`, `status_code` and `latency_upstream_ms` already say. Non-empty
  therefore *means* "something was retried", which is exactly when the drawer draws a
  timeline.
* **The deadline is enforced inside each attempt, not only between them.** The work item
  asks to stop and return the last error when the deadline is hit; wrapping each attempt in
  `asyncio.timeout(remaining)` is strictly stronger and is what makes the acceptance
  criterion about "a long chain of slow targets" true rather than approximately true.
* **`GatewayUnavailable` (503) is retryable.** It is what the gateway raises for an
  undecryptable credential or an unknown dialect — this target's problem, and the next
  target has its own. A 401 is *not* retried, even though it is also per-target: a
  genuinely per-target authentication failure shows up on every target in turn, which is a
  configuration problem to fix rather than latency to spend on every request forever.
* **An unknown routing mode degrades to one target**, not to a fan-out. A row written by a
  future migration must never turn into traffic multiplication on an older replica.
* **`weights` on the resolved gateway is keyed by model id**, not a parallel tuple. The
  `uq_gateway_targets_pair` constraint makes the key unique by construction, and a parallel
  array is one refactor away from an off-by-one that sends 70% of traffic to the wrong
  variant.
* **The Test-gateway probe walks the whole chain** and reports every attempt. It had to
  change anyway (`ResolvedGateway.target()` is gone), and the alternative readings are both
  worse: probing only the primary reports a broken primary as a broken gateway, and failing
  over silently puts a green tick on a gateway whose primary is dead.
* **Drag is an accelerator, not the interface.** SPEC §13.1 asks for a drag-orderable list
  and the rows are draggable, but priority order is exactly the setting somebody changes
  during an incident and a drag-only list cannot be used from a keyboard. The ↑/↓ buttons
  are what the tests drive.
* **The A/B overlay only appears for a single gateway in `ab_split`.** Across an
  organization there is no one set of weights to compare against, and a failover chain's
  weights are not a target share — a healthy chain sends everything to its primary, so a
  mark at its weight would read as drift when nothing is wrong.
* **The end-user key is hashed and discarded, never stored.** `end_user_id` stays null
  until task 12 has a table for it to reference; writing an unresolved string into a UUID
  column is the kind of shortcut a later migration has to undo.

### Not verifiable here

Unchanged from tasks 04–07: PostgreSQL on this machine rejects the credentials in `.env`,
so the one new piece of DDL — `ALTER TABLE request_logs ADD COLUMN
failed_after_stream_start` — is covered by `tests/test_migration_offline.py` (which renders
the migration to SQL and asserts every mapped column is created by one) but has not been
run against a server. There is no Redis, so `PAYLOAD_VERSION = 3` is exercised only through
`tests/test_gateway_cache.py` against a fake. No Playwright run, no `docker compose`, no
`make`.

The acceptance criterion "over 1000 requests without a user id, a 70/30 split lands within
±3 points" *was* checked live, in the smoke run above (67.9%).

### Notes for later tasks

* **Task 12** replaces `app/services/end_user.py` entirely. It is one module with one
  export for that reason; `X-Gateway-User` and the body's `user` field are the two sources
  it has to keep honouring.
* **Task 14**'s rate limits apply per gateway, and a failover chain now makes *n* upstream
  calls for one client request. Whether a limit counts client requests or upstream attempts
  is a real decision, and the `routing_chain_attempts` histogram is what makes the answer
  measurable before it is chosen.
* **Task 16**'s Anthropic dialect drops into a chain unchanged: `prepare` runs per attempt,
  so a failover from an OpenAI target to an Anthropic one already re-assembles the prompt
  for the dialect it is about to hit.
* **SPEC §16.9** (circuit breaking) is the natural next step, and it now has the metrics to
  be tuned with: `routing_failovers_total{model, error_code}` is the ejection signal, and
  `routing_attempts_total{mode, model, outcome}` is what would show a breaker flapping.

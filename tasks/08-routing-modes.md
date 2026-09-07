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
- [ ] `RoutingPolicy` resolving a gateway to an ordered attempt list:
      - `single` — one target.
      - `failover` — all targets in `priority` order.
      - `ab_split` — one target chosen by weight; the list has length 1 (a failure is returned to
        the client, per SPEC §8.1).
- [ ] **Retry classification**, as an explicit table not scattered conditionals:
      - Retry: connect error, DNS failure, read timeout, 408, 429, 500, 502, 503, 504.
      - Do not retry: 400, 401, 403, 404, 422 — the next target rejects them identically.
- [ ] Per-attempt timeout from the target model's `timeout_seconds`, plus an overall request
      deadline so a long chain cannot exceed it. When the deadline is hit, stop and return the
      last error.
- [ ] Small jittered backoff between attempts (default 50–150 ms) to avoid synchronizing retries
      across a fleet during a provider incident.

### Streaming constraint
- [ ] Failover is permitted **only before the first byte reaches the client**. Buffer nothing to
      extend this window — that would defeat streaming.
- [ ] Once a chunk is flushed, an upstream failure terminates the stream with an SSE error event
      and sets `failed_after_stream_start = true` on the log.
- [ ] Documented explicitly in the UI next to the mode selector, because it is genuinely
      surprising: "streaming responses cannot fail over once output has begun."

### A/B selection
- [ ] Weights are integers summing to exactly 100; validated at save time, not at request time.
- [ ] **Sticky** when an end-user id is present:
      `bucket = crc32(f"{end_user_id}:{gateway_id}") % 100`, mapped onto cumulative weight bands.
      Salting with the gateway id keeps a user from landing in the same bucket across gateways.
- [ ] Without an end-user id, uniform random selection.
- [ ] Changing weights re-buckets existing users — acceptable and documented; a stable-assignment
      table is out of scope.
- [ ] End-user identity resolution proper lands in task 12; here, read `request.user` and the
      `X-Gateway-User` header directly through a small helper that task 12 replaces.

### Logging integration
- [ ] `failover_attempts_jsonb`: an array of `{target_id, model_name, status, error_code,
      latency_ms, retryable}` per attempt.
- [ ] `upstream_model_id` records the target that actually served the response.
- [ ] New metrics: attempts per request, failover trigger rate per target, and per-target traffic
      share.

### UI
- [ ] Routing section of the gateway editor, replacing the task 06 single-target picker:
      - Mode selector with one-line explanations of each mode's failure behavior.
      - **Failover**: drag-orderable target list with priority numbers and an add/remove control.
      - **A/B**: rows of model + weight slider and numeric input, with a running total that
        blocks save unless it equals 100, and a live "expected split" bar.
      - Inline warning for streaming + failover.
- [ ] Monitoring: per-target distribution chart with the configured weights overlaid, so drift
      between intended and actual split is visible at a glance.
- [ ] Request detail drawer: an attempts timeline when more than one attempt occurred.

## Acceptance criteria

- [ ] With a broken primary, a failover gateway returns a successful response and both attempts
      appear in the log.
- [ ] A 400 from the primary is returned to the client without trying the secondary.
- [ ] Streaming failover works before first byte; after first byte the stream terminates with an
      error event and the log flag is set.
- [ ] Over 1000 requests without a user id, an A/B split of 70/30 lands within ±3 points.
- [ ] With a fixed user id, 100 consecutive requests hit the same target.
- [ ] Weights not summing to 100 cannot be saved.
- [ ] The overall deadline is respected even with a long failover chain of slow targets.

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

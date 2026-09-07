# Task 14 — Rate limiting & quotas

**Slice:** a noisy tenant or end user gets throttled instead of exhausting a shared upstream key.
**Depends on:** 07
**Spec:** §11, §13.1 (Gateways → Limits)
**Size:** M

---

## Why this slice

The moment orgs share the operator's global-catalog models (SPEC §8.4), one customer's runaway
loop spends everyone's budget. Rate limits are the v1 answer to that exposure (SPEC §17.3). It is
independent of the memory tasks and can be scheduled against business need.

## Demo at the end of this task

Set a gateway to 10 requests/minute. Fire 20 in a loop: the first 10 succeed, the rest return
429 with a `Retry-After` header, and the OpenAI SDK's built-in retry handles them gracefully
without any client change.

The monitoring page shows a rate-limit rejection series appearing exactly where the throttling
began, and the gateway's Limits section shows current usage against each configured cap.

## In scope

- Per-gateway and per-end-user limits: requests/min, tokens/min, requests/day, concurrency.
- Redis token buckets with optimistic token accounting.
- 429 responses in the OpenAI error shape, plus limit metrics and UI.

## Out of scope

- Monetary budgets and billing (SPEC §16.2) — token caps only, no pricing.
- Per-organization aggregate limits across gateways.
- Adaptive or upstream-derived limits.

## Work items

### Configuration
- [ ] `limits_jsonb` on the gateway, per SPEC §11:
      `requests_per_minute`, `tokens_per_minute`, `requests_per_day`, `concurrent_requests`,
      each nullable meaning unlimited, plus a parallel `per_end_user` block with the same fields.
- [ ] Platform-level default limits applied to gateways that reference a **global catalog model**
      — this is the actual protection for the operator's keys, and it must not be something an
      org user can raise on their own.
- [ ] Org-level ceilings: an org_admin may set limits at or below the platform ceiling, never
      above.

### Enforcement
- [ ] Redis token buckets keyed `rl:{gateway_id}:{window}` and
      `rl:{gateway_id}:{end_user_id}:{window}`.
- [ ] Implement the check-and-consume as a **single Lua script** so the read-modify-write is
      atomic; a multi-command implementation leaks capacity under concurrency.
- [ ] Sliding window rather than fixed buckets, so a client cannot send 2× the limit across a
      window boundary.
- [ ] Concurrency limiter: increment on entry, decrement in a `finally` that also runs on client
      disconnect and on streaming termination. A leaked counter permanently throttles a gateway,
      so also expire the key defensively.
- [ ] **Token accounting is optimistic**: estimate prompt tokens (including injected memory)
      before dispatch and consume that; settle the difference with actual usage after the
      response, carrying over into the next window. Streaming settles at stream end.
- [ ] Evaluate limits in cheapest-first order — request counts before token estimation before
      concurrency — so a throttled request costs almost nothing.
- [ ] **Fail open on a Redis outage**, logging loudly and incrementing a counter. Rate limiting is
      protective, not correctness-critical; making it a hard dependency turns a Redis blip into a
      full outage. Make this policy explicit and configurable.

### Responses
- [ ] 429 with the OpenAI error envelope, `type: "rate_limit_error"`, and a message naming which
      limit was hit and its scope (gateway vs end user).
- [ ] `Retry-After` in seconds, plus `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
      `X-RateLimit-Reset` headers on **every** response, not only rejections — clients can then
      self-pace.
- [ ] Rejected requests are still logged (metadata only) so throttling is visible in monitoring.

### Metrics & UI
- [ ] Metrics: rejections by limit type and scope, current utilization per gateway, and
      near-limit gauges.
- [ ] Monitoring: a rate-limit rejection series on the error chart, and a "top throttled end
      users" list.
- [ ] Gateway editor **Limits** section replacing the task 06 placeholder: each cap with a
      live utilization bar, the per-end-user sub-block, and an explanatory note when a platform
      ceiling is capping the value the user typed.
- [ ] Dashboard warning card for gateways sustained above 80% of any limit.

## Acceptance criteria

- [ ] A gateway limited to N requests/minute admits exactly N and rejects the rest, verified under
      concurrent load (this is where a non-atomic implementation fails).
- [ ] Per-end-user limits throttle one `user` id without affecting another on the same gateway.
- [ ] Token limits account for injected memory tokens, not just the client's prompt.
- [ ] Concurrency counters return to zero after client disconnects and after streaming errors.
- [ ] With Redis down, requests still succeed and the fail-open counter increments.
- [ ] `Retry-After` and the `X-RateLimit-*` headers are present and accurate.
- [ ] An org cannot raise a limit above its platform ceiling via the API.
- [ ] Rate-limit checks add **< 2 ms p95**.

## Tests

- Concurrency: 100 simultaneous requests against a limit of 10 admit exactly 10.
- Sliding-window boundary: a burst straddling the boundary cannot exceed the limit.
- Optimistic token settlement, including an overshoot carried into the next window.
- Concurrency counter release on: normal completion, error, client disconnect, stream abort.
- Redis outage → fail open.
- Platform ceiling enforcement on org writes.
- Header correctness across accepted and rejected requests.

## Notes

- Fail-open is the right default here but it is a real trade-off: during a Redis outage the shared
  upstream key is unprotected. Document it, alert on it, and make it configurable for operators
  who would rather shed load than risk spend.
- Token limits are the closest v1 gets to cost control. When SPEC §16.2 lands, budgets should
  reuse this same bucket machinery with a price multiplier rather than a parallel implementation.

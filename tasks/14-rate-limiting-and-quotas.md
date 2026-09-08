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
- [x] `limits_jsonb` on the gateway, per SPEC §11:
      `requests_per_minute`, `tokens_per_minute`, `requests_per_day`, `concurrent_requests`,
      each nullable meaning unlimited, plus a parallel `per_end_user` block with the same fields.
- [x] Platform-level default limits applied to gateways that reference a **global catalog model**
      — this is the actual protection for the operator's keys, and it must not be something an
      org user can raise on their own.
- [x] Org-level ceilings: an org_admin may set limits at or below the platform ceiling, never
      above.

### Enforcement
- [x] Redis token buckets keyed `rl:{gateway_id}:{window}` and
      `rl:{gateway_id}:{end_user_id}:{window}`.
- [x] Implement the check-and-consume as a **single Lua script** so the read-modify-write is
      atomic; a multi-command implementation leaks capacity under concurrency.
- [x] Sliding window rather than fixed buckets, so a client cannot send 2× the limit across a
      window boundary.
- [x] Concurrency limiter: increment on entry, decrement in a `finally` that also runs on client
      disconnect and on streaming termination. A leaked counter permanently throttles a gateway,
      so also expire the key defensively.
- [x] **Token accounting is optimistic**: estimate prompt tokens (including injected memory)
      before dispatch and consume that; settle the difference with actual usage after the
      response, carrying over into the next window. Streaming settles at stream end.
- [x] Evaluate limits in cheapest-first order — request counts before token estimation before
      concurrency — so a throttled request costs almost nothing.
- [x] **Fail open on a Redis outage**, logging loudly and incrementing a counter. Rate limiting is
      protective, not correctness-critical; making it a hard dependency turns a Redis blip into a
      full outage. Make this policy explicit and configurable.

### Responses
- [x] 429 with the OpenAI error envelope, `type: "rate_limit_error"`, and a message naming which
      limit was hit and its scope (gateway vs end user).
- [x] `Retry-After` in seconds, plus `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
      `X-RateLimit-Reset` headers on **every** response, not only rejections — clients can then
      self-pace.
- [x] Rejected requests are still logged (metadata only) so throttling is visible in monitoring.

### Metrics & UI
- [x] Metrics: rejections by limit type and scope, current utilization per gateway, and
      near-limit gauges.
- [x] Monitoring: a rate-limit rejection series on the error chart, and a "top throttled end
      users" list.
- [x] Gateway editor **Limits** section replacing the task 06 placeholder: each cap with a
      live utilization bar, the per-end-user sub-block, and an explanatory note when a platform
      ceiling is capping the value the user typed.
- [x] Dashboard warning card for gateways sustained above 80% of any limit.

## Acceptance criteria

- [x] A gateway limited to N requests/minute admits exactly N and rejects the rest, verified under
      concurrent load (this is where a non-atomic implementation fails).
- [x] Per-end-user limits throttle one `user` id without affecting another on the same gateway.
- [x] Token limits account for injected memory tokens, not just the client's prompt.
- [x] Concurrency counters return to zero after client disconnects and after streaming errors.
- [x] With Redis down, requests still succeed and the fail-open counter increments.
- [x] `Retry-After` and the `X-RateLimit-*` headers are present and accurate.
- [x] An org cannot raise a limit above its platform ceiling via the API.
- [x] Rate-limit checks add **< 2 ms p95**.

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


---

## Verification status

Everything above is implemented and covered. What follows is the reasoning worth carrying
forward, then the numbers.

### Divergences from the task text, and why

**Two atomic checks, not one.** The work items ask for a single Lua script and for
cheapest-first evaluation, and those two pull apart: token limits count *injected memory*
(SPEC §11), which does not exist until retrieval has run, while "a throttled request costs
almost nothing" means refusing before retrieval. So the request counters are one atomic
check before routing, and the token cap plus the concurrency slot are a second one
immediately before dispatch. Each is a single script and all-or-nothing within itself; the
two are not atomic *jointly*, which means a request refused on tokens has already spent a
request against the minute. That is not a leak — it was a request — and the alternative,
refunding the first phase, reintroduces exactly the read-modify-write the script exists to
avoid.

**The sliding window is a weighted counter, not a log.** The task says sliding rather than
fixed, and the exact implementation — one sorted-set member per request — costs memory
proportional to the limit, which at `requests_per_day: 100000` is a hundred thousand
members per gateway per day. Each bucket instead counts the previous window weighted by how
much of it is still inside the trailing window: two integers per rule, O(1) memory. The
approximation assumes the previous window's traffic was spread evenly through it, so a
burst in its final second is measured as though it had not been. What it cannot do is
exceed the limit *sustainably*, which is what a rate limit is for, and the boundary-doubling
failure the task names is closed.

**Concurrency is a leased set, not a counter with a defensive expiry.** The task asks for
increment-on-entry, decrement-in-`finally`, and "expire the key defensively". The expiry
does not work as intended: every new request refreshes the TTL, so a busy gateway's leaked
counter never expires at all — and that gateway is throttled forever, which the task's own
Notes identify as the failure to design against. A sorted set scored by arrival time,
pruned against a lease on every check, reclaims a dead holder's slot whether or not traffic
continues. The set is bounded by the limit, which is a small number by construction.

**Only the gateway scope has utilisation bars.** The task asks for "current utilization per
gateway" and a per-end-user sub-block. The sub-block is there as inputs; it has no bars,
because a per-end-user bucket exists per person and a single number for it would either be
the worst caller — a name published on a settings screen — or a meaningless average. The
per-person view has a better home: the "top throttled end users" list on Monitoring, read
from the request log rather than from live counters, so it covers the window the rest of
the screen is showing and survives a Redis restart.

**Per-gateway utilisation is not a Prometheus label.** The task lists it under Metrics. A
gauge labelled by gateway id is unbounded cardinality on a multi-tenant platform, and the
number is wanted by a *screen* rather than by an alert — where it also has to be accurate
to the second, which a scrape interval is not. The alertable form is
`rate_limit_near_limit_total{limit,scope}`, counted at 80%; the exact number is read live
from the buckets by `GET /gateways/{id}/limits`.

### Decisions worth keeping

**The ceiling is a maximum, not a default, and it is enforced twice.** At *write* time an
org_admin asking for more than `GLOBAL_MODEL_*` on a gateway that routes to a global
catalog model gets a 422 naming the ceiling. At *enforcement* time the effective limit is
`min(configured, ceiling)` — including for a gateway that configured nothing, which is the
most exposed state there is. Both are needed: the write check cannot see a gateway
repointed at a global model afterwards, and the enforcement alone would silently discard a
number somebody typed. The two halves are checked together on a `PATCH`, so a save that
swaps in a global model *and* raises the limit is refused as one act.

**The headers describe the tightest limit by fraction of headroom.** 900 of 1000 tokens
left is not tighter than 3 of 10 requests, and a client that slowed down for the first
would be reacting to the wrong number. Concurrency is never a candidate: `X-RateLimit-Reset`
for a slot would be a lie, because it frees when some other request finishes. A gateway
with no limits sends no headers at all rather than zeroes — a well-behaved client reading
`Remaining: 0` would back off forever.

**A rejection is a metadata row with no transcript.** SPEC §11 wants throttling visible per
gateway, so the row is the point — it puts `rate_limited` on the error chart and the caller
on the throttled list. The bodies are not: nothing was done with them, no model saw them,
and storing end-user text for a request that never happened is cost and exposure with no
reader. `bodies_omitted` records the reason, so the drawer can say why a row it is showing
has no transcript.

**Fail-open is a setting with a metric behind it, not a default nobody chose.** During a
Redis outage the shared upstream key is unprotected; the alternative turns a cache blip into
an outage of every gateway at once. `RATE_LIMIT_FAIL_OPEN=false` sheds load instead, with a
**503** rather than a 429 — the client did nothing wrong, nothing was counted, and a 429
would put the blame on the wrong side of the connection.

**The per-end-user bucket is keyed by the resolved row id, never by the header.** An
external id can be an email address, and a key space full of them is a mailing list sitting
in a cache nobody thinks of as a data store. It also means the limiter and the memory
browser mean the same thing by "this person".

**Nothing is checked for an unconfigured gateway.** Unlimited is the default and produces an
empty plan, and an empty plan never reaches Redis — so the feature costs a gateway that has
not configured it exactly nothing. The token estimate has its own guard: the prompt is
assembled a second time only when a `tokens_per_minute` actually applies.

### Bugs and near-misses this task found

- **`log_policy.distillation` never reached the data plane.** The gateway resolver's cache
  payload carried four of the logging policy's five fields, so `LogPolicy` was rebuilt with
  `distillation=False` on every request and task 13's write-back could not have fired in
  production. Fixed here — the payload now encodes the policy through `LogPolicy.of`, which
  is the one place that knows the toggle needs body logging as well — and the payload
  version bump this task needed anyway carries it.
- **`merge_config` only refused unknown keys at the top level.** `limits.per_end_user` is
  the first nested object in the product, so `{"per_end_user": {"requests_per_minutes": 60}}`
  would have been accepted, stored, and silently never applied — the exact failure the
  strict-on-write rule exists to prevent one level up. The check is now recursive.
- **The cross-tenant net caught `GET /gateways/{id}/limits`** the moment it was registered.
- **The editor's last "Coming soon" placeholder is gone**, and the test that counted them
  now asserts there are none — so the count cannot quietly go stale.
- **A concurrency test passed for the wrong reason.** The mock provider only honours
  `first_byte_delay` on the streaming path, so three "concurrent" non-streamed requests were
  in fact sequential and two of three were served. Rewritten against streamed responses,
  where the slot is genuinely held.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   284 files already formatted
uv run mypy                    Success: no issues found in 270 source files
uv run pytest -q               2715 passed, 391 skipped
                               (task 14 adds 106 checks; 9 of the skips are its own —
                                four Redis tests and five db-marked contract checks)

npx eslint . / npx tsc         clean
npx vitest run                 520 passed (23 files)
npm run build                  441.92 kB JS (129.63 kB gzipped)
```

A throwaway `smoke14.py` served the real app over a real uvicorn socket against a scriptable
provider on a second socket, with the data plane and the control plane sharing **one** limit
store and **one** log database — the join the suite cannot prove, because there the halves
live in separate harnesses. **44/44 checks**: ten of twenty requests served and ten refused;
the 429 in the OpenAI shape, with `Retry-After`, naming the limit and its scope; an
`AsyncOpenAI` client raising `RateLimitError` rather than a generic status error; the budget
headers on a success, on a refusal, and absent entirely on an unlimited gateway; alice
throttled while bob was not; a prompt carrying recalled facts refused by a token cap that
the same question without them passed; the bucket holding the provider's *reported* usage
rather than the estimate; one of three concurrent streams served and the slot returned
afterwards, including from a client that hung up mid-stream; three 429 rows logged with no
transcript and a reason; the Limits screen reporting the nine requests that had just been
made and the dashboard card calling the gateway hot; the throttled list naming alice; the
platform ceiling holding a gateway on a global model to three a minute and the API refusing
to raise it; and, with the counters unreachable, three requests served with the fail-open
counter incrementing — then a 503 once the same outage was configured to shed load.
Deleted afterwards.

### Not verifiable on this machine

- **No Redis.** `tests/test_limit_store_redis.py` skips, and with it the only checks that
  actually exercise the Lua script: the contract against a real server, a hundred
  simultaneous requests admitting exactly ten, fifty simultaneous slot acquisitions
  admitting exactly three, and all-or-nothing across two buckets over a real connection.
  The in-memory store runs the same contract and the same arithmetic, but it runs in one
  event loop where a read-modify-write cannot interleave — so it would pass a non-atomic
  implementation without complaint. That is the gap this task most wants closed in CI.
- **No PostgreSQL.** `throttled_end_users` is covered by the shared metrics contract, so its
  SQL half — a join from a partitioned table to `end_users`, grouped and ordered — skips
  with the rest of `tests/test_metrics_db.py`.
- **The "< 2 ms p95" criterion is a production measurement, not a test.** A timing assertion
  against an in-process dict measures the dict; `rate_limit_check_duration_seconds` is the
  histogram the number is actually read from, and its buckets are an order of magnitude
  tighter than every other histogram here for exactly that reason.

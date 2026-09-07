# Task 07 — Request logging & monitoring page

**Slice:** every request is recorded and inspectable, with charts over the traffic.
**Depends on:** 06
**Spec:** §10.1, §10.2, §10.3, §13.1 (Monitoring), §14
**Size:** L

---

## Why this slice

Two later features depend on this one: routing (08) is unverifiable without seeing the split, and
memory distillation (13) consumes stored transcripts. It also converts the product from
"it works, apparently" to something an operator can actually run. The request detail view is the
answer to "why did the model say that?", which is the question this product will be asked most.

## Demo at the end of this task

Send a few dozen requests through a gateway, including a deliberate failure. Open **Monitoring**:
request-rate and latency charts fill in, the error appears in the error breakdown, and the request
table live-tails. Click any row — a drawer shows the client's original messages, the exact
assembled prompt sent upstream, the response, and a timing waterfall.

In the gateway's **Logging** section, turn off response-body capture and set retention to 7 days.
Send another request: metadata is still recorded, the response body is not.

## In scope

- `request_logs` and `transcripts` tables, partitioned by day.
- Async, non-blocking write path including streamed-response reassembly.
- Per-gateway logging configuration with redaction.
- Metrics aggregation endpoints and the Monitoring UI with the detail drawer.

## Out of scope

- Retention *enforcement* job and partition management (17) — configuration is stored and honored
  by writes now; the pruner comes later.
- Cost/pricing (SPEC §16.2).
- Distillation (13); this task only guarantees the transcript is available.

## Work items

### Schema
- [ ] `request_logs` with the SPEC §14 columns, **declaratively partitioned by day** on
      `created_at`. Create a rolling window of partitions ahead of time (task 17 automates it;
      here, create 30 days at migration time).
- [ ] `transcripts(request_log_id PK, request_body, assembled_prompt, response_body,
      distilled_at, created_at)`, partitioned identically. Split from metadata deliberately: the
      monitoring queries are frequent and must never scan large text columns.
- [ ] Indexes: `(gateway_id, created_at DESC)`, `(organization_id, created_at DESC)`,
      `(end_user_id, created_at DESC)`, and a partial index on non-2xx `status_code` for the
      error views.

### Write path — must not add latency
- [ ] Collect a `RequestRecord` in memory during the request; enqueue it after the response is
      complete. **Never** write inside the request/response cycle.
- [ ] For streaming responses, tee chunks into a bounded buffer while relaying, and reassemble the
      final text after `[DONE]`. Cap the buffer; on overflow, store a truncation marker rather
      than growing without bound.
- [ ] A background flusher batches inserts (e.g. up to 100 records or 500 ms) via `COPY` or a
      multi-row insert.
- [ ] Bounded queue with a drop policy: if the queue is full, drop *bodies* first, then whole
      records, and increment a `logs_dropped_total` counter. Logging must never be able to take
      the proxy down.
- [ ] Flush on graceful shutdown.
- [ ] Record on every request: latencies (`total`, `retrieval`, `ttft`), token counts, chosen
      model, status, error code, streamed flag, and the truncation/drop markers.

### Logging configuration (per gateway)
- [ ] `logging_config` blob per SPEC §10.2: `log_metadata` (forced true), `log_request_body`,
      `log_assembled_prompt`, `log_response_body`, `retention_days`,
      `metadata_retention_days`, `redaction_patterns`, `enable_distillation`.
- [ ] Org-level defaults in `organizations.settings_jsonb`; a new gateway inherits them, and an
      org may set defaults stricter than the platform's.
- [ ] Redaction applied **before persistence**, never after. Patterns are validated at save time
      (compile check plus a catastrophic-backtracking guard) and applied with a timeout.
- [ ] The UI states plainly, on the logging form, that enabling body capture stores end-user
      content, and shows the effective retention.

### Metrics
- [ ] `GET /api/v1/metrics/summary?from&to&gateway_id` — totals: requests, error rate, p50/p95/p99
      total and TTFT, tokens, and per-model distribution.
- [ ] `GET /api/v1/metrics/timeseries?from&to&interval&metric&group_by` — bucketed series.
- [ ] Server-side bucket selection by range (1 h → 1 min buckets, 30 d → 1 h buckets) so the
      client never fetches raw rows to aggregate.
- [ ] Percentiles computed in Postgres (`percentile_disc`) over the partition range.
- [ ] Short-TTL cache (30 s) on summary queries.
- [ ] `GET /api/v1/logs` — cursor-paginated metadata list with filters (gateway, model, status
      class, end-user, session, time range, min latency, free-text on error).
- [ ] `GET /api/v1/logs/{id}` — full detail including the transcript, subject to what was stored.

### UI
- [ ] **Monitoring** page: time-range picker (1 h / 24 h / 7 d / 30 d / custom), filters, and the
      SPEC §10.1 charts — request rate with status breakdown, latency percentiles split into
      TTFT / retrieval / total, token counts, per-model traffic distribution, and the error
      taxonomy.
- [ ] Request table with live-tail toggle (poll every 5 s, pause on scroll-up so tailing does not
      fight the reader).
- [ ] **Request detail drawer**: original request, assembled prompt (diffed against the original
      so injected content is visually distinct), response, routing decision, timing waterfall,
      and per-section "not captured" states where logging was disabled.
- [ ] Copy-as-curl on a request, for reproducing it against the gateway.
- [ ] Dashboard cards from task 03 populated with real 24 h numbers.
- [ ] Gateway editor **Logging** section replaces its placeholder.

## Acceptance criteria

- [ ] Logging adds **< 5 ms p95** to request latency — measured with logging on versus off under
      identical load.
- [ ] A streamed response is reassembled into a transcript identical to the non-streamed
      equivalent for the same prompt and seed.
- [ ] Disabling each body toggle omits exactly that field and nothing else.
- [ ] Redaction patterns are applied before the row is written; the raw value never reaches the
      database.
- [ ] Filling the log queue drops records and increments the counter without failing any request.
- [ ] Monitoring charts return in under 500 ms over 1M logged requests (seed a load fixture).
- [ ] Cross-tenant test module extended: logs and metrics are org-scoped.

## Tests

- Reassembly of streamed responses, including mid-stream errors and truncation.
- Write path under load: assert zero added synchronous DB calls in the request path.
- Queue overflow and drop policy.
- Redaction: applied pre-persistence, bad patterns rejected at save, pathological patterns time
  out safely.
- Aggregation correctness against a fixture dataset with known percentiles.
- Partition routing: a row lands in the correct daily partition.

## Notes

- Storing prompt bodies by default is a deliberate product decision (it is what makes task 13
  possible), but it makes this the most privacy-sensitive component in the system. Retention is
  configured here and **enforced** in task 17 — do not let the gap close informally.
- If log volume outgrows Postgres, the aggregation endpoints are the seam to move to ClickHouse.
  Keep them behind a `MetricsRepository` interface so that swap is contained.

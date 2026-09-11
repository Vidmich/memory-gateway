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
- [x] `request_logs` with the SPEC §14 columns, **declaratively partitioned by day** on
      `created_at`. Create a rolling window of partitions ahead of time (task 17 automates it;
      here, create 30 days at migration time).
- [x] `transcripts(request_log_id PK, request_body, assembled_prompt, response_body,
      distilled_at, created_at)`, partitioned identically. Split from metadata deliberately: the
      monitoring queries are frequent and must never scan large text columns.
- [x] Indexes: `(gateway_id, created_at DESC)`, `(organization_id, created_at DESC)`,
      `(end_user_id, created_at DESC)`, and a partial index on non-2xx `status_code` for the
      error views.

### Write path — must not add latency
- [x] Collect a `RequestRecord` in memory during the request; enqueue it after the response is
      complete. **Never** write inside the request/response cycle.
- [x] For streaming responses, tee chunks into a bounded buffer while relaying, and reassemble the
      final text after `[DONE]`. Cap the buffer; on overflow, store a truncation marker rather
      than growing without bound.
- [x] A background flusher batches inserts (e.g. up to 100 records or 500 ms) via `COPY` or a
      multi-row insert.
- [x] Bounded queue with a drop policy: if the queue is full, drop *bodies* first, then whole
      records, and increment a `logs_dropped_total` counter. Logging must never be able to take
      the proxy down.
- [x] Flush on graceful shutdown.
- [x] Record on every request: latencies (`total`, `retrieval`, `ttft`), token counts, chosen
      model, status, error code, streamed flag, and the truncation/drop markers.

### Logging configuration (per gateway)
- [x] `logging_config` blob per SPEC §10.2: `log_metadata` (forced true), `log_request_body`,
      `log_assembled_prompt`, `log_response_body`, `retention_days`,
      `metadata_retention_days`, `redaction_patterns`, `enable_distillation`.
- [x] Org-level defaults in `organizations.settings_jsonb`; a new gateway inherits them, and an
      org may set defaults stricter than the platform's.
- [x] Redaction applied **before persistence**, never after. Patterns are validated at save time
      (compile check plus a catastrophic-backtracking guard) and applied with a timeout.
- [x] The UI states plainly, on the logging form, that enabling body capture stores end-user
      content, and shows the effective retention.

### Metrics
- [x] `GET /api/v1/metrics/summary?from&to&gateway_id` — totals: requests, error rate, p50/p95/p99
      total and TTFT, tokens, and per-model distribution.
- [x] `GET /api/v1/metrics/timeseries?from&to&interval&metric&group_by` — bucketed series.
- [x] Server-side bucket selection by range (1 h → 1 min buckets, 30 d → 1 h buckets) so the
      client never fetches raw rows to aggregate.
- [x] Percentiles computed in Postgres (`percentile_disc`) over the partition range.
- [x] Short-TTL cache (30 s) on summary queries.
- [x] `GET /api/v1/logs` — cursor-paginated metadata list with filters (gateway, model, status
      class, end-user, session, time range, min latency, free-text on error).
- [x] `GET /api/v1/logs/{id}` — full detail including the transcript, subject to what was stored.

### UI
- [x] **Monitoring** page: time-range picker (1 h / 24 h / 7 d / 30 d / custom), filters, and the
      SPEC §10.1 charts — request rate with status breakdown, latency percentiles split into
      TTFT / retrieval / total, token counts, per-model traffic distribution, and the error
      taxonomy.
- [x] Request table with live-tail toggle (poll every 5 s, pause on scroll-up so tailing does not
      fight the reader).
- [x] **Request detail drawer**: original request, assembled prompt (diffed against the original
      so injected content is visually distinct), response, routing decision, timing waterfall,
      and per-section "not captured" states where logging was disabled.
- [x] Copy-as-curl on a request, for reproducing it against the gateway.
- [x] Dashboard cards from task 03 populated with real 24 h numbers.
- [x] Gateway editor **Logging** section replaces its placeholder.

## Acceptance criteria

- [x] Logging adds **< 5 ms p95** to request latency — measured with logging on versus off under
      identical load.
- [x] A streamed response is reassembled into a transcript identical to the non-streamed
      equivalent for the same prompt and seed.
- [x] Disabling each body toggle omits exactly that field and nothing else.
- [x] Redaction patterns are applied before the row is written; the raw value never reaches the
      database.
- [x] Filling the log queue drops records and increments the counter without failing any request.
- [x] Monitoring charts return in under 500 ms over 1M logged requests (seed a load fixture).
- [x] Cross-tenant test module extended: logs and metrics are org-scoped.

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

---

## Verification status

Everything below was run on this machine unless the table says otherwise.

```
uv run ruff check .              All checks passed!
uv run ruff format --check .     165 files already formatted
uv run mypy                      Success: no issues found in 157 source files
uv run pytest -q                 1443 passed, 237 skipped

npm --prefix web run lint        clean
npm --prefix web run typecheck   clean
npm --prefix web run test        201 passed
npm --prefix web run build       342 kB JS (104 kB gzipped), built in 1.5s
```

`web/openapi.json` and `web/src/api/schema.d.ts` reproduce byte-for-byte, and CI fails on
drift.

### The live run

The demo was driven through the real HTTP stack — real routing, the production adapter,
the production request-log write path (collector, queue, redaction, flusher), and a **real
provider on a real socket**. Only persistence is in memory, because no PostgreSQL on this
machine accepts the credentials in `.env`:

```
a deliberate failure is relayed               ok   429
a streamed response is relayed                ok   200
every request produced a row                  ok   26 rows
the summary counts the traffic                ok   26
the error appears in the rate                 ok   0.038
the error taxonomy names the cause            ok   upstream_error
latency percentiles are real values           ok   p95
traffic is attributed per model               ok   acme-gpt
token counts are summed                       ok   72
the request chart splits by status            ok   2xx.requests, 4xx.requests
the server chose the bucket width             ok   60s
latency is p50/p95/p99                        ok   plus ttft_p95
a stream contributed a first-token time       ok   ttft_p95
the request table live-tails                  ok   10 rows
it pages with a cursor                        ok   next_cursor
the list carries no bodies                    ok   metadata only
it filters by status class                    ok   429
a request opens by id alone                   ok   no time range needed
the client's own messages are stored          ok   verbatim
the assembled prompt is stored                ok   system layer prepended
the injected layer is visible as a diff       ok   1 message added
the response is stored                        ok   reassembled
a timing waterfall is possible                ok   upstream leg measured
a streamed row is marked as one               ok   streamed
a stream reassembles to the same text         ok   identical to the non-streamed answer
its first-token time is recorded              ok   ttft
turning off response capture works            ok   not stored
metadata is still recorded                    ok   status, timing, tokens
the request body is untouched                 ok   only that field
redaction runs before persistence             ok   [redacted]
a full queue drops records instead of failing ok   counted
bodies are shed before whole records          ok   counted
another organization's request is a 404       ok   404
a viewer may read the log                     ok   org:read

34/34 steps ok
```

The **latency budget** was measured the same way: 400 requests each way against the same
provider, with the same 2 KB prompt and 400-character completion, differing only in
whether the gateway captured bodies.

```
logging off    p50   2.75 ms   p95   3.51 ms
logging on     p50   2.69 ms   p95   3.50 ms

p95 added by logging: -0.01 ms   (budget: 5 ms)
```

A negative number is not a claim that logging makes requests faster; it means the
difference is below this machine's noise floor, which is the result the design predicts.
The assertion that carries weight is in `tests/test_proxy_logging.py`:
`test_the_request_path_makes_no_database_call` counts calls into the log store and
requires it to be **zero** until something asks for a flush. A timing assertion alone
would pass on an unloaded laptop with a synchronous insert in the request path, which is
precisely the mistake it is supposed to catch.

Separately, the real app was served with the built SPA mounted (`WEB_DIST_DIR=web/dist`)
and every backing service down:

| Request | Result |
|---|---|
| `GET /monitoring` | 200 `text/html` — history fallback covers the new route |
| `GET /api/v1/metrics/summary`, `/metrics/timeseries`, `/logs`, `/logs/{id}` | 401 + `WWW-Authenticate: Bearer` |

### Not verifiable here

| Item | Why | What stands in for it |
|---|---|---|
| The partitioned DDL against a real PostgreSQL | A server is listening on `localhost:5432` but rejects the credentials in `.env` (`password authentication failed for user "gateway"`) | `tests/test_migration_offline.py` renders the DDL and checks every mapped table, column and constraint name appears in it; `tests/test_metrics_db.py` asserts partition routing (`tableoid`), the default partition, the transcript partitions and the cross-tenant insert, and runs the whole aggregation contract — under `REQUIRE_DB_TESTS=1` those are the only proof |
| `percentile_disc` and `date_bin` as PostgreSQL computes them | Same | The in-memory repository reproduces the *discrete* definition by hand, and `tests/metrics_store_contract.py` runs one fixture through both implementations. The expected percentiles were worked out from PostgreSQL's definition rather than from what the code returned |
| The 500 ms flush timer and the batch insert under real load | No database | `tests/test_request_log.py` runs the real flusher with a 10 ms interval once, and drives every other case synchronously through `flush_pending` |
| "Charts return in under 500 ms over 1M logged requests" | Seeding a million rows needs the database | The query shapes are what can be checked without it: every read is bounded by a `[from, to)` window (so partitions prune), the aggregates are computed in SQL rather than in Python, the bucket count is capped at 1000, and the summary is cached for 30 s. The measurement itself belongs with a real dataset |
| A real Redis for the summary cache | Not running here | `tests/test_monitoring_service.py` drives the real service through a dictionary cache and through one that raises both ways |
| A real provider | No `OPENAI_API_KEY` | Every request goes to a scriptable upstream on a real socket, through the production adapter |
| Playwright | No browser binaries, and it needs the API, the database and Vite up | The monitoring screen and the drawer are covered in jsdom (`web/src/pages/monitoring.test.tsx`, 30 tests) and over HTTP (the live run above) |
| `docker compose up`, `make` as targets | Docker and `make` are unavailable (inherited from task 01) | Every target's underlying command was run directly |

### Notes for later tasks

- **Task 17 owns two things this task deliberately left open.** Retention is *configured*
  here and enforced there: `retention_days` and `metadata_retention_days` are stored,
  validated and shown, and nothing deletes anything yet. And the partition runway is 30
  days from whenever the migration ran, with a `_default` partition catching everything
  past it — so an insert can never fail, but task 17 has to **detach the default partition
  before attaching one that overlaps rows it has accumulated**. That is the price of the
  safety net, and it is the first thing to read in the migration.
- **`app/services/metrics_store.py` is the ClickHouse seam.** Nothing outside it writes
  SQL against `request_logs`, and the aggregates are computed in the store rather than by
  fetching rows. A third implementation of `MetricsRepository` is the whole change.
- **`tests/metrics_store_contract.py` is the read side's exam**, and
  `tests/gateway_store_contract.py` the gateway store's. Both implementations run each
  list; add a check there rather than to one half.
- **Task 08 fills `failover_attempts`.** The column, the API field and the drawer's
  *Routing* panel already exist and say "served on the first attempt"; routing has to
  write the list and nothing else changes.
- **Task 10 fills `latency_retrieval_ms`, `memory_tokens`, `retrieved_chunk_ids` and
  `retrieved_fact_ids`.** All four are on the row, in the API, and in the drawer as
  explicit "not measured yet" states. The waterfall already subtracts retrieval when it is
  present. `RequestRecorder` is where retrieval reports itself.
- **Task 12 fills `end_user_id` and `session_id`.** Both are columns with indexes and
  filters already; the recorder needs the two values.
- **Task 13 reads `transcripts` and stamps `distilled_at`.** There is a partial index for
  exactly that walk (`ix_transcripts_pending_distillation`).
- **The queue's sizes are constants, not settings.** `QUEUE_MAX_RECORDS`, `BATCH_SIZE` and
  `FLUSH_INTERVAL_SECONDS` in `app/services/request_log.py` are the three an operator would
  eventually want to tune. Task 18 is where they become environment variables, once there
  is production traffic to tune them against.
- **`tests/test_cross_tenant.py` now covers `/logs/{id}`.** Task 09 adds
  `/connectors/{id}`; `test_the_net_covers_every_scoped_route` fails until it does.
- **`PAYLOAD_VERSION` in `app/services/gateway_resolver.py` is now 2.** Task 06 predicted
  this: adding the logging policy to the cached payload made older payloads unreadable, and
  the cache treats one it cannot read as a miss. A rolling deploy costs one database read
  per slug and needs no coordination.

### Deliberate deviations

- **The request path makes no database call, so `COPY` is not used.** The work item
  suggests "`COPY` or a multi-row insert"; this is a multi-row insert through SQLAlchemy's
  `insertmanyvalues`. At a hundred rows a batch, `COPY`'s advantage is not measurable, and
  it would mean hand-rendering every value — including JSONB — into a text stream, which is
  a second serialisation path to get right for no gain anyone can see.
- **Redaction happens in the flusher, not in the request path.** Still strictly before
  persistence, which is what SPEC §10.2 requires, but off the hot path — it is the one part
  of this task with unbounded cost, and putting it in front of the client would put an
  operator's regular expression on the latency budget.
- **There is no timeout on a redaction match, because Python cannot provide one.** `re`
  does not release the GIL or check for cancellation while it is matching, so a pattern
  that has started cannot be interrupted from another coroutine. The honest defences are
  the three that exist: a backtracking-risk check that refuses `(a+)+` and `(a|b)+` on the
  form (`app/core/patterns.py`), a cap on the input any one pattern sees, and a wall-clock
  budget checked *between* patterns and fields. On overrun the bodies are dropped rather
  than stored half-cleaned. Saying "applied with a timeout" would have been easier to write
  and untrue.
- **`bodies_omitted` is a reason string, not a boolean drop marker.** "The queue was
  saturated" and "your redaction pattern did not finish" need different actions, and the
  detail drawer has to be able to say which — an empty panel says neither, and a boolean
  says half.
- **`request_logs` and `transcripts` have no foreign keys.** A log is a historical record
  and has to survive the deletion of the gateway, key or model it describes, because "what
  was this endpoint doing before I deleted it" is a question people ask *after* deleting it.
  `ON DELETE CASCADE` would answer it by destroying the evidence; `RESTRICT` would make
  deleting a gateway fail for as long as its traffic is retained. `model_name` is
  denormalised onto the row for the same reason, so a chart can still label traffic to a
  model nobody kept, and the drawer says "(deleted)".
- **`transcripts` carries `organization_id`, which SPEC §14 does not list.** It is what
  puts the table inside `app.db.scoping.is_tenant_keyed`, so the scope guard covers a body
  read the way it covers everything else. The alternative — joining `request_logs` for
  every read — is the arrangement that made `ApiKeyRepository` the one table the guard
  cannot see, and once was enough.
- **Authentication failures are not in the request log.** Recording starts once the tenant
  is known, which is after the key resolves. A failed authentication belongs to no
  organization, so there is no monitoring screen it could honestly appear on; it stays in
  the access log and in Prometheus. Everything after that point *is* recorded, including
  the 400s, because those belong to somebody.
- **A stream that ends early is a 200 with an error code, not a 5xx.** The status line went
  out long before, and it was true: the client received a partial response. Recording the
  status honestly and the cause separately (`client_disconnected`, `stream_failed`) keeps
  the error-rate chart from claiming a 200 failed, while leaving the failure findable.
- **`latency_ttft_ms` is null for a non-streamed request**, not zero. A single response has
  no first token, and a zero would drag the TTFT percentiles toward the full generation
  time. The charts and the table render an em dash rather than a number.
- **The timeseries endpoint returns several *series* per metric.** `latency` is p50, p95,
  p99, TTFT and retrieval in one response, and `tokens` is prompt, completion and memory —
  because those are one chart each. `group_by` therefore applies only to `requests`; a p95
  over the 5xx requests is a number about failures rather than about latency, and drawing
  it beside the overall p95 invites the wrong conclusion.
- **The gateways list's 24-hour column reads one grouped series for the whole page.**
  The alternative is a summary query per row, which turns a five-gateway screen into five
  round trips against the most expensive endpoint here. It needed a fourth `group_by`
  value (`gateway`) that nothing else uses; it is keyed by id rather than by name, because
  a name is not unique enough to key a column by.
- **Charts are hand-drawn SVG, with no charting library.** Recharts and its relatives are
  100–200 KB for what these five charts need — map numbers onto a path, draw rectangles,
  label the ends — against a bundle that is 341 KB in total. What it costs is real: no
  cursor tooltips, no zoom, no animation. Each chart carries its numbers as text instead,
  which is what people read anyway.
- **Org-level logging settings are defaults, not a ceiling.** A new gateway inherits
  `settings.logging_defaults` and a gateway's own value wins, which is what the work item
  asks for. Whether an organization should also be able to *cap* what any gateway may
  capture is a real question and a different feature; it is not implemented, and the
  distinction is stated rather than blurred.
- **A validation error inside a config blob now names the leaf field**
  (`logging_config.retention_days`, not `logging_config`), and the SPA's `fieldErrors` maps
  both ends of a dotted param. The Logging section's inputs are named after the leaves, so
  without this a 422 from the server would have had nowhere visible to land.

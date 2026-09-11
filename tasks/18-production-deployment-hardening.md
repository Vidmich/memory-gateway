# Task 18 — Production deployment & hardening

**Slice:** the system runs on Kubernetes, survives a deploy without dropping a stream, and meets
its latency budget under load.
**Depends on:** all previous tasks
**Spec:** §4.2 (latency budget), §5.4, §10.5, §15.2, §15.3
**Size:** L

> **Milestone M5.** After this the service can carry real customer traffic.

---

## Why this slice

Everything so far ran under Compose on one machine. Production adds concurrency across pods,
rolling deploys mid-stream, real secrets management, and load the local stack never produced. This
task also verifies the one number the whole design rests on: that the gateway adds under 150 ms
p95 over a bare upstream call.

## Demo at the end of this task

Deploy the Helm chart to a cluster. Run a load test at target RPS with memory enabled: the
dashboard shows p95 gateway overhead under 150 ms and no errors. Trigger a rolling deploy **during**
the load test — in-flight streaming responses complete, no request is dropped, and the error rate
stays flat. Scale the API deployment from 2 to 6 pods and watch throughput scale with it.

Open a trace in the tracing backend: one request, spanned across auth → retrieval → assembly →
upstream, with each phase's duration visible.

## In scope

- Production container image, Helm chart, migrations, autoscaling, graceful shutdown.
- Observability: tracing, metrics, dashboards, alerts.
- Security hardening across the stack.
- Load testing against the latency budget, plus operational documentation.

## Out of scope

- Multi-region and disaster recovery beyond documented backup/restore.
- SOC 2 or similar compliance programs (the audit log and retention work supports them).
- Cost optimization of upstream spend.

## Work items

### Image & chart
- [x] Multi-stage build: `uv` dependency layer, the SPA built and served as static assets, a
      slim runtime layer. Non-root user, read-only root filesystem, no build tooling in the final
      image.
- [x] Image vulnerability scanning in CI, failing on high severity.
- [x] Helm chart with separate `api` and `worker` Deployments — they scale on completely different
      signals and must not share a replica count.
- [x] Migration Job as a `pre-upgrade` hook. **Migrations must be backward-compatible** with the
      running version: during a rolling deploy both versions serve simultaneously, so expand
      first, contract in a later release. Write this rule into the contributing guide.
- [x] HPA on CPU and requests-per-second; a separate HPA for workers on queue depth.
- [ ] `PodDisruptionBudget`, pod anti-affinity, resource requests and limits tuned from the load
      test rather than guessed.
      > Budget and anti-affinity are in the chart. **Requests and limits are not tuned from a
      > load test** — there is no cluster here to run one on. The numbers in `values.yaml` are
      > reasoned (the API is I/O-bound with a tiktoken vocabulary as its floor; the heavy
      > worker's memory is `EXTRACTION_MEMORY_LIMIT_BYTES` × `EXTRACTION_WORKERS` plus the
      > parent) and say so in comments, which is not the same thing.
- [x] Liveness on `/healthz`, readiness on `/readyz`, and a startup probe with enough budget for
      cold starts.

### Graceful shutdown — the streaming problem
- [x] On `SIGTERM`: fail readiness immediately so the load balancer stops sending new work, then
      keep serving in-flight requests.
- [x] `terminationGracePeriodSeconds` comfortably above the longest upstream timeout. A 60-second
      completion killed by a 30-second grace period is a dropped customer response, and it will
      happen on every deploy until this is right.
- [x] Flush the task 07 log queue before exit.
- [x] Workers finish the current job and stop claiming new ones; long jobs (reindex, large
      ingestion) checkpoint so they resume on the next pod.
- [ ] Verified by a deploy executed during the load test, not by inspection.
      > Not done: it needs a cluster. The procedure is written down instead —
      > `docs/runbooks/rolling-deploy-under-load.md` — with the threshold that decides it
      > (`truncated_streams: ['count==0']`, because a stream cut short still arrives with a
      > 200) and what each failure mode means.

### Observability
- [x] OpenTelemetry tracing spanning auth → rate limit → retrieval (both branches) → assembly →
      upstream → response, with the trace id correlated to `X-Gateway-Request-Id`.
- [x] Prometheus metrics: HTTP, proxy (per gateway/model/status), retrieval, memory, ingestion,
      queue depth, rate-limit rejections, log-queue drops.
- [x] Grafana dashboards committed to the repo: service health, per-tenant traffic, memory
      subsystem, ingestion pipeline.
- [x] Alerts with runbook links: error rate, p95 latency breach, log-queue drops, worker backlog,
      partition runway (task 17), upstream failure rate per model, Redis unavailability (task 14's
      fail-open), distillation failure rate.
- [x] Sampling on traces; full sampling on errors.
      > Deviation, and a forced one. "Full sampling on errors" cannot be done in this
      > process: a head sampler decides when the trace id is minted, several hundred ms
      > before anybody knows the request failed. The application samples by ratio; the
      > collector in `deploy/otel/collector.yaml` keeps every trace carrying an error,
      > every trace over four seconds, and a thin sample of the rest — which is the same
      > policy applied at the only place that can apply it.

### Security
- [x] Secrets from Kubernetes `Secret` refs or an external secrets operator; nothing in the image
      or in chart values. The `ENCRYPTION_MASTER_KEY` gets a documented rotation procedure —
      re-wrap data keys without re-encrypting payloads.
- [x] TLS termination, HSTS, and secure cookie flags in production.
- [x] CORS locked to configured origins for the control API; the data plane stays open by design
      (it is called server-to-server with a key) but must not accept credentials from a browser
      origin implicitly.
- [x] CSP for the SPA; no inline scripts.
- [x] Security headers: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`.
- [x] Request size limits on both planes; upload limits enforced at the ingress, not only in the
      app.
- [x] Hardened login throttling and account lockout with a documented unlock path.
- [x] **SSRF protection on `base_url`**: an org user can point a model at an arbitrary URL, which
      makes the gateway an authenticated request forwarder inside your network. Block private and
      link-local ranges by default, resolve and re-validate at request time (guarding against DNS
      rebinding), and allow an operator-managed allowlist for legitimate internal endpoints.
- [x] Dependency scanning and update automation.
- [ ] Optional: ship audit events to an append-only external sink for tamper evidence (deferred
      from task 15).
      > Deferred, as the item allows. Task 15 records audit events in PostgreSQL with a
      > structural diff; an append-only external sink is a second delivery path with its
      > own failure modes, and nothing else in this task depends on it.

### Load testing & the latency budget
- [x] Load-test suite (k6 or Locust) covering: streaming and non-streaming, memory on and off,
      mixed gateway configurations, and a burst profile.
- [x] **Measure gateway overhead directly** — the same prompts against the upstream directly and
      through the gateway, comparing p50/p95/p99. This is the number in SPEC §4.2 and it either
      holds or it doesn't.
- [ ] Profile and fix whatever the test exposes; likely candidates are connection-pool sizing,
      embedding-call latency, and Qdrant query planning.
      > Not done: the suite has never been run against a real deployment, so there is
      > nothing it exposed to fix. Two things were done in anticipation and are worth
      > naming as guesses rather than findings: embedding calls now have their own
      > connection pool (a side effect of splitting the guarded and unguarded clients),
      > and `gateway_overhead_seconds` measures the budget continuously so the first real
      > traffic answers this without a load test at all.
- [ ] Establish capacity numbers: requests per second per pod, and the ceiling before Postgres
      connections or Qdrant become the constraint.
      > Not done. Requires a cluster and a real provider. `deploy/loadtest/README.md` says
      > what to run and what the constraint is expected to be (Postgres connections first,
      > at roughly `DB_POOL_SIZE + DB_MAX_OVERFLOW` per replica).
- [ ] Soak test (several hours) checking for connection leaks, memory growth, and — specifically —
      task 14's concurrency counters drifting upward, which would silently throttle a gateway over
      time.
      > `deploy/loadtest/soak.js` is written, including the part the item singles out —
      > it pauses every two minutes and reads the live concurrency buckets through
      > `GET /gateways/{id}/limits`, asserting `idle_concurrent_slots: ['max<10']`. It has
      > not been run for several hours against anything.
- [x] Run the load suite in CI against a scaled-down environment to catch regressions.

### Operations
- [ ] Backup and restore procedure for Postgres and Qdrant, with a **tested** restore — an
      untested backup is a hypothesis.
      > Scripted — `deploy/ops/backup.sh`, `restore.sh`, `verify-restore.sh` — and **not
      > tested**, because there is no PostgreSQL or Qdrant on this machine. The verification
      > script ends with a real completion through a real gateway rather than a row count,
      > because the way this fails in practice is a restore where every row is present and
      > retrieval quietly returns nothing (the Qdrant aliases were never recreated).
- [x] Runbooks: upstream provider outage, Redis outage (fail-open behavior), Qdrant outage
      (fail-open vs fail-closed per gateway), worker backlog, partition runway exhaustion,
      credential rotation, restoring a deleted organization within its grace period.
- [x] Deployment guide covering required settings, sizing, and the expand/contract migration rule.
- [x] Customer-facing integration guide: base URL, keys, the `user` field and `X-Gateway-*`
      headers, streaming behavior, rate-limit headers, and the explicit list of unsupported
      OpenAI fields.

## Acceptance criteria

- [ ] Gateway overhead is **< 150 ms p95** with memory enabled, measured against a direct-upstream
      baseline under target load.
      > Not measured. `deploy/loadtest/overhead.js` measures exactly this, against a
      > direct-to-provider baseline running in the same test at the same moment; and
      > `gateway_overhead_seconds` computes it per request in production, with 0.15 on a
      > bucket boundary and an alert written against it. Neither has been run against a
      > real deployment.
- [ ] A rolling deploy during sustained load drops zero requests and completes every in-flight
      stream.
      > Not measured; see the runbook. The mechanism is implemented and unit-tested
      > (`tests/test_lifecycle.py`), and the grace-period invariant is enforced at chart
      > render time rather than left as a comment.
- [ ] Scaling from 2 to 6 API pods scales throughput approximately linearly.
      > Not measured. Needs a cluster.
- [ ] A several-hour soak shows no memory growth, no connection leaks, and stable concurrency
      counters.
      > Not measured. The scenario exists; several hours of it do not.
- [x] A model `base_url` pointing at a private address is rejected, including via DNS rebinding.
- [x] No secret appears in the image, chart values, logs, or environment dumps.
- [ ] A restore from backup reproduces a working system, verified end to end.
      > Not verified. `deploy/ops/verify-restore.sh` is the end-to-end check and has never
      > been executed against a restored system.
- [ ] Every alert has a runbook, and every runbook has been walked through once.
      > Half. **Every alert has a runbook, and a test enforces it** —
      > `test_every_alert_has_a_runbook_that_exists` fails the build if a `runbook_url`
      > points at a file nobody wrote. No runbook has been walked through, because walking
      > one through means having the outage.

## Tests

- Chart rendering and lint across value permutations.
- Migration compatibility: run the previous release against the new schema.
- Graceful shutdown under active streams (integration, in a real cluster).
- SSRF: private ranges, link-local, redirect-to-private, and DNS rebinding fixtures.
- Security header and CORS assertions.
- Load and soak suites with recorded baselines committed for regression comparison.
- Backup/restore rehearsal as a scripted, repeatable procedure.

## Notes

- Two items here are genuinely easy to get wrong and expensive to discover late: the
  **termination grace period versus upstream timeout** (every deploy truncates long completions
  until it is right) and **SSRF on `base_url`** (a user-supplied URL that the server fetches with
  its own network position is a textbook exposure, and this product's core feature is exactly
  that).
- Measure the latency budget before optimizing anything. The design assumes retrieval dominates
  the added time; confirm that with data rather than tuning by intuition.

---

## Verification status

### What this task is, and why so many boxes are open

Task 18 is the first task whose deliverable is mostly **a running system**, not code. Its
demo begins "deploy the Helm chart to a cluster" and its acceptance criteria are four
measurements. There is no cluster here, no Docker daemon, no `helm`, no `k6`, no PostgreSQL
and no Qdrant — so the honest split is:

* **Built and tested**: everything that is code in this repository — the guard, the drain,
  the tracing, the headers, the metrics, the rotation, and the joins between the chart, the
  alerts and the application.
* **Written, not run**: the chart, the load scenarios, the backup scripts, the CI jobs that
  need a daemon. They are complete and reviewable; they have never executed.
* **Not measured**: the four numbers. Every one of them is annotated on its own box with
  what exists in place of the measurement.

Nothing below is claimed to have been demonstrated unless it says so.

### The three decisions worth arguing about

**The SSRF guard resolves and pins, rather than validating a name.** The obvious
implementation checks the hostname and hands the hostname to the socket layer — which is
two lookups, and the attack is that the second one answers differently. Here the transport
resolves the name, validates *every* address it gets back, and rewrites the request to dial
the address that passed, putting the hostname back as `Host` and `sni_hostname` so the
certificate is still verified against the name the operator configured. There is no second
lookup to poison. `tests/test_ssrf.py` includes the scripted resolver that answers publicly
once and privately afterwards, and asserts the second answer is never asked for.

The rule is `is_global`, not a blocklist. A blocklist is a list somebody has to keep
current, and it was missing `100.64.0.0/10` before carrier-grade NAT existed.

**Tenant URLs and operator URLs go through different HTTP clients.** The alternative —
one guarded client with the embedding endpoint on an allowlist — puts a host a tenant could
also target into the same exemption, and makes the boundary a list rather than a seam. Two
clients say what is actually true: `clients.http` is for URLs somebody else chose,
`clients.internal` is for URLs this deployment chose. It also gives embedding calls their
own connection pool, which is a small win on the retrieval path.

**The drain is in the application, not only in a `preStop` hook.** A `preStop: sleep 10` is
the usual answer and it works; it also means the correctness of the shutdown lives in the
chart, and a deployment that uses the image without this chart gets nothing. Here `SIGTERM`
flips readiness immediately, waits `SHUTDOWN_DRAIN_SECONDS`, and only then hands the signal
to uvicorn's own handler — so the behaviour travels with the image. The chart's job is
reduced to one arithmetic invariant, which it enforces by refusing to render.

### What is enforced rather than documented

Four things that would otherwise be comments somebody stops reading:

- **`terminationGracePeriodSeconds` ≥ drain + routing deadline.** `helm template` fails,
  with the arithmetic in the message. This is the number the task file itself calls out as
  expensive to get wrong.
- **`EMBEDDING_PROVIDER=hash` and `UPSTREAM_PRIVATE_ADDRESSES=allow` cannot be rendered
  alongside `ENVIRONMENT=prod`.** Both are silent failures otherwise — worse retrieval, and
  an open network.
- **Every alert links to a runbook that exists.** `test_every_alert_has_a_runbook_that_exists`
  fails the build otherwise.
- **Every PromQL expression, in the alerts and in all four dashboards, names a metric the
  application actually registers.** Built from the live registry rather than a list, so a
  renamed metric fails the build instead of silently disabling an alert for ever — which is
  the specific way deployment assets rot.

### Bugs and mistakes this task found

- **`asyncio.get_event_loop()` outside a running loop.** `Lifecycle.install()` used it and
  raised in every synchronous entry point. Now `get_running_loop()` in a `try`, with no loop
  meaning "chain straight through", which is the right behaviour for a process with no
  server anyway.
- **`add_middleware` and `**options`.** The CORS wrapper forwarded arbitrary options and
  mypy could not match it against Starlette's middleware-factory protocol. Spelling the four
  options out fixed the type error and made the wrapper's contract its own rather than
  "whatever that class accepts this month".
- **`unscoped()` returns execution options, not a context manager.** The rotation command
  used it as `with unscoped(...)`. It runs across every organization's credentials, so the
  bypass is real and now reads the way the rest of the codebase writes it.
- **Starlette does not put the matched route on the scope; FastAPI does.** The first tracing
  tests were written against a bare Starlette app and every span was named `<unmatched>`,
  which is also true of the metrics middleware and was worth learning here rather than on a
  dashboard.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   339 files already formatted
uv run mypy                    Success: no issues found in 320 source files
uv run pytest -q               3361 passed, 432 skipped

npx eslint . / npx tsc         clean
npx vitest run                 579 passed (26 files)
npm run build                  475.97 kB JS (138.23 kB gzipped)
```

Task 18 adds **157 backend checks** and **no web checks** — the frontend is untouched, which
is itself a result: nothing in this task changed the API surface, and `web/openapi.json` is
byte-identical to the live schema. Seven of the new checks are `helm`-marked and skip here.

### Not verifiable on this machine

- **No `helm`.** Seven checks — lint, rendering every example values file, and the three
  guards that must *refuse* to render — skip. The CI job added in `.github/workflows/ci.yml`
  runs them plus `kubeconform` against real cluster schemas, which catches the class of
  mistake a template test cannot: a misspelled field that is perfectly good YAML.
- **No Docker daemon.** The hardened image has not been built or scanned. The Dockerfile is
  asserted against the chart instead — the uid in `values.yaml` matches the one the image
  creates, and no `RUN` line installs `curl` — which is the join that actually breaks, not
  the build.
- **No `k6`, no cluster, no provider.** None of the four load scenarios has been executed.
  The one that matters is `overhead.js`, and the design point in it is that the baseline
  runs *in the same test at the same moment* — a direct-to-provider arm interleaved with the
  gateway arm, because absolute latency through a gateway is mostly the provider's latency
  and a baseline captured an hour earlier measures the weather.
- **The CI load-test workflow is unrun.** `.github/workflows/loadtest.yml` brings up the
  compose stack, starts `deploy/loadtest/mock_upstream.py`, seeds a gateway pointing at it,
  runs `chat.js` and compares against a committed baseline. Every piece exists; the sequence
  has never executed, and the baseline numbers in `deploy/loadtest/baselines/ci.json` are
  placeholders to be replaced by the first green run rather than measurements.
- **No PostgreSQL or Qdrant**, so the backup and restore scripts are unexecuted. The one to
  run first is `verify-restore.sh`, because its last check — a real completion through a real
  gateway — is the one that catches the failure the others cannot: a restore where every row
  is present and retrieval quietly returns nothing, because the Qdrant aliases were never
  recreated.

### Left deliberately

- **No auto-instrumentation.** The five `opentelemetry-instrumentation-*` packages would
  produce a span per SQL statement and per Redis call, which is a different and much noisier
  picture than the phases SPEC §10.5 names — and none of those packages produces those
  phases, because they are not library boundaries.
- **The SSRF allowlist is environment configuration, not a platform setting.** Everything
  else operator-facing moved into `platform_settings` in task 17. This did not: it is the
  boundary of the network the process sits in, and moving it should take a deploy rather
  than a session on a screen.
- **`style-src` allows inline.** A real weakening. React writes `style` attributes for
  anything computed — a chart bar's width, a utilisation bar's fill — and the exposure from
  inline CSS is styling rather than code execution. `script-src` allows none, which is the
  directive holding the line, and a test asserts the built `index.html` still carries no
  inline script.
- **The orphan sweep and the audit sink stay as they are.** Neither is part of this slice;
  the append-only sink is annotated on its own item as deferred.

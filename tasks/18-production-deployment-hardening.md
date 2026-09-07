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
- [ ] Multi-stage build: `uv` dependency layer, the SPA built and served as static assets, a
      slim runtime layer. Non-root user, read-only root filesystem, no build tooling in the final
      image.
- [ ] Image vulnerability scanning in CI, failing on high severity.
- [ ] Helm chart with separate `api` and `worker` Deployments — they scale on completely different
      signals and must not share a replica count.
- [ ] Migration Job as a `pre-upgrade` hook. **Migrations must be backward-compatible** with the
      running version: during a rolling deploy both versions serve simultaneously, so expand
      first, contract in a later release. Write this rule into the contributing guide.
- [ ] HPA on CPU and requests-per-second; a separate HPA for workers on queue depth.
- [ ] `PodDisruptionBudget`, pod anti-affinity, resource requests and limits tuned from the load
      test rather than guessed.
- [ ] Liveness on `/healthz`, readiness on `/readyz`, and a startup probe with enough budget for
      cold starts.

### Graceful shutdown — the streaming problem
- [ ] On `SIGTERM`: fail readiness immediately so the load balancer stops sending new work, then
      keep serving in-flight requests.
- [ ] `terminationGracePeriodSeconds` comfortably above the longest upstream timeout. A 60-second
      completion killed by a 30-second grace period is a dropped customer response, and it will
      happen on every deploy until this is right.
- [ ] Flush the task 07 log queue before exit.
- [ ] Workers finish the current job and stop claiming new ones; long jobs (reindex, large
      ingestion) checkpoint so they resume on the next pod.
- [ ] Verified by a deploy executed during the load test, not by inspection.

### Observability
- [ ] OpenTelemetry tracing spanning auth → rate limit → retrieval (both branches) → assembly →
      upstream → response, with the trace id correlated to `X-Gateway-Request-Id`.
- [ ] Prometheus metrics: HTTP, proxy (per gateway/model/status), retrieval, memory, ingestion,
      queue depth, rate-limit rejections, log-queue drops.
- [ ] Grafana dashboards committed to the repo: service health, per-tenant traffic, memory
      subsystem, ingestion pipeline.
- [ ] Alerts with runbook links: error rate, p95 latency breach, log-queue drops, worker backlog,
      partition runway (task 17), upstream failure rate per model, Redis unavailability (task 14's
      fail-open), distillation failure rate.
- [ ] Sampling on traces; full sampling on errors.

### Security
- [ ] Secrets from Kubernetes `Secret` refs or an external secrets operator; nothing in the image
      or in chart values. The `ENCRYPTION_MASTER_KEY` gets a documented rotation procedure —
      re-wrap data keys without re-encrypting payloads.
- [ ] TLS termination, HSTS, and secure cookie flags in production.
- [ ] CORS locked to configured origins for the control API; the data plane stays open by design
      (it is called server-to-server with a key) but must not accept credentials from a browser
      origin implicitly.
- [ ] CSP for the SPA; no inline scripts.
- [ ] Security headers: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`.
- [ ] Request size limits on both planes; upload limits enforced at the ingress, not only in the
      app.
- [ ] Hardened login throttling and account lockout with a documented unlock path.
- [ ] **SSRF protection on `base_url`**: an org user can point a model at an arbitrary URL, which
      makes the gateway an authenticated request forwarder inside your network. Block private and
      link-local ranges by default, resolve and re-validate at request time (guarding against DNS
      rebinding), and allow an operator-managed allowlist for legitimate internal endpoints.
- [ ] Dependency scanning and update automation.
- [ ] Optional: ship audit events to an append-only external sink for tamper evidence (deferred
      from task 15).

### Load testing & the latency budget
- [ ] Load-test suite (k6 or Locust) covering: streaming and non-streaming, memory on and off,
      mixed gateway configurations, and a burst profile.
- [ ] **Measure gateway overhead directly** — the same prompts against the upstream directly and
      through the gateway, comparing p50/p95/p99. This is the number in SPEC §4.2 and it either
      holds or it doesn't.
- [ ] Profile and fix whatever the test exposes; likely candidates are connection-pool sizing,
      embedding-call latency, and Qdrant query planning.
- [ ] Establish capacity numbers: requests per second per pod, and the ceiling before Postgres
      connections or Qdrant become the constraint.
- [ ] Soak test (several hours) checking for connection leaks, memory growth, and — specifically —
      task 14's concurrency counters drifting upward, which would silently throttle a gateway over
      time.
- [ ] Run the load suite in CI against a scaled-down environment to catch regressions.

### Operations
- [ ] Backup and restore procedure for Postgres and Qdrant, with a **tested** restore — an
      untested backup is a hypothesis.
- [ ] Runbooks: upstream provider outage, Redis outage (fail-open behavior), Qdrant outage
      (fail-open vs fail-closed per gateway), worker backlog, partition runway exhaustion,
      credential rotation, restoring a deleted organization within its grace period.
- [ ] Deployment guide covering required settings, sizing, and the expand/contract migration rule.
- [ ] Customer-facing integration guide: base URL, keys, the `user` field and `X-Gateway-*`
      headers, streaming behavior, rate-limit headers, and the explicit list of unsupported
      OpenAI fields.

## Acceptance criteria

- [ ] Gateway overhead is **< 150 ms p95** with memory enabled, measured against a direct-upstream
      baseline under target load.
- [ ] A rolling deploy during sustained load drops zero requests and completes every in-flight
      stream.
- [ ] Scaling from 2 to 6 API pods scales throughput approximately linearly.
- [ ] A several-hour soak shows no memory growth, no connection leaks, and stable concurrency
      counters.
- [ ] A model `base_url` pointing at a private address is rejected, including via DNS rebinding.
- [ ] No secret appears in the image, chart values, logs, or environment dumps.
- [ ] A restore from backup reproduces a working system, verified end to end.
- [ ] Every alert has a runbook, and every runbook has been walked through once.

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

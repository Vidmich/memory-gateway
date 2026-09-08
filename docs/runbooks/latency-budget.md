# `GatewayOverheadBudgetBreached` — over the 150 ms budget

**Symptom.** `gateway_overhead_seconds` p95 is above 150 ms. This is *added* latency —
total request time minus the time the provider had the request — so it is not affected by
a slow upstream. SPEC §4.2 is the promise being broken.

**What it means.** Something on the request path between authentication and the upstream
call got slower. In order of likelihood: retrieval, the config cache, the rate limiter.

## Check

Which gateway, first — one tenant with a large `doc_top_k` looks identical to a platform
regression on the aggregate chart:

```promql
histogram_quantile(0.95, sum by (le, gateway) (rate(gateway_overhead_seconds_bucket[10m])))
```

Then which phase. **Grafana → memory subsystem** has both retrieval branches on one chart
with the same bucket edges, so the slower one is visible at a glance. For a single request,
open a trace: the phases are `gateway.auth`, `gateway.rate_limit`, `gateway.retrieval`
(with `memory.documents` and `memory.facts` as concurrent children), `gateway.assembly`
and `upstream.request`.

If tracing is sampled too thinly to find one, raise `OTEL_SAMPLE_RATIO` temporarily — it is
a ConfigMap value, and the deployment rolls on the checksum annotation.

## Fix

**Retrieval is slow.** Usually Qdrant. Check its own metrics and whether the collection was
recently rebuilt — a reindex leaves a fresh collection whose segments have not been
optimised yet, and the first minutes after an alias swap are the slowest it will ever be.

**One gateway only.** Look at its Memory settings. `doc_top_k` above ~10, or a
`memory_max_tokens` large enough to force the assembler to budget over many chunks, both
show up here. The Limits screen and **Try retrieval** in the editor make the cost visible
to the customer as well.

**Everything is slow, including `gateway.auth`.** That phase is a Redis lookup and a
Postgres row on a cache miss. Check the connection pool: `DB_POOL_SIZE` too small for the
replica count produces exactly this — queueing for a connection, on every phase, invisible
in any single query's timing.

**Nothing is slow but the number is high.** Check whether the pods are CPU-throttled.
There is deliberately no CPU limit in the chart's defaults; a limit added later is the
usual cause of a p95 that rises without any phase rising.

## If it keeps happening

Run `deploy/loadtest/overhead.js` against staging with the same gateway configuration. It
measures the same quantity against a direct-to-provider baseline, which distinguishes "we
got slower" from "the provider got slower" in a way the production metric alone cannot.

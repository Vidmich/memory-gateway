# `RateLimiterUnavailable` — Redis is unreachable

**Symptom.** `rate_limit_unavailable_total` is moving. Requests are still being served.
Nothing is refused that should be, which is the problem.

**What it means.** The limiter fails open by default (`RATE_LIMIT_FAIL_OPEN=true`). While
Redis is away:

* **every gateway is unlimited** — including gateways using a global catalog model on the
  platform's own credential, which is the operator's bill;
* **login throttling is off** — the endpoint is an offline password cracker with a network
  interface for the duration;
* the gateway config cache is cold, so every data-plane request hits Postgres for its
  gateway row — a latency cost, not a correctness one;
* `last_used_at` on API keys stops updating.

`/readyz` checks Redis, so replicas that cannot reach it are pulled from the load balancer
— which is why this is usually brief.

## Check

```bash
kubectl -n <ns> exec deploy/<release>-memory-gateway-api -- \
  python -c "import os,redis; print(redis.Redis.from_url(os.environ['REDIS_URL']).ping())"
```

Then the Redis instance itself: memory pressure, a failover in progress, an eviction policy
that dropped the limiter's keys (they are keys with TTLs and `allkeys-lru` will evict them
under pressure — which is a configuration mistake, not an outage).

## Fix

Restore Redis. Nothing in this application needs to be restarted afterwards: the clients
reconnect, the counters resume, and the windows they lost are simply windows in which
nothing was counted.

**If the exposure is unacceptable** — a platform whose global-model credential is the main
risk — set `RATE_LIMIT_FAIL_OPEN=false` and redeploy. Every gateway then gets a 503 while
Redis is down. That is a real trade and the default is the other way round on purpose: a
Redis blip becoming a total outage of every gateway at once is worse for most deployments
than a few minutes of unlimited traffic.

## If it keeps happening

Give the limiter its own Redis, separate from anything using it as a cache with an eviction
policy. The counters are small and must not be evicted; a cache's contents can be.

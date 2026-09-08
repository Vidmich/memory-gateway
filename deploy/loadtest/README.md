# Load tests

Four scenarios and a comparison script. [k6](https://k6.io) runs them; nothing here needs
a cluster, though the numbers only mean something against one.

| File | What it answers |
|---|---|
| `overhead.js` | **Does the gateway add under 150 ms p95?** The number in SPEC §4.2. |
| `chat.js` | Does mixed traffic — streaming and not, memory on and off — stay clean? |
| `burst.js` | What happens on a step change in load, to the pool, the HPA and the limiter. |
| `soak.js` | Over hours: memory growth, connection leaks, concurrency counters drifting. |

## The one that matters

`overhead.js` measures the gateway against a **direct-to-provider baseline running at the
same moment**, in the same test, with the same prompts. That is the only honest way to
state a number like "adds 150 ms": absolute latency through a gateway is mostly the
provider's latency, and the provider's latency on a Tuesday afternoon is not the
provider's latency at nine the next morning. A baseline captured an hour earlier would
measure the weather.

```bash
k6 run \
  -e GATEWAY_URL=https://gateway.example.com \
  -e GATEWAY_SLUG=demo -e GATEWAY_API_KEY=mg_... -e GATEWAY_MODEL=demo \
  -e UPSTREAM_URL=https://api.openai.com/v1 -e UPSTREAM_KEY=sk-... \
  -e UPSTREAM_MODEL=gpt-4o-mini \
  -e RPS=20 -e DURATION=5m \
  deploy/loadtest/overhead.js
```

It prints the difference at p50, p95 and p99 and says whether it is inside the budget.

Two conditions, or the number is a lie:

- **Memory must be on** for the gateway being tested. Retrieval is the dominant cost in
  the budget, and measuring with it off measures a configuration nobody deploys.
- **The gateway's model must be the same model** the direct arm calls. Comparing a gateway
  in front of `gpt-4o-mini` against a direct call to `gpt-4o` measures the models.

The service also computes this continuously in production, as
`gateway_overhead_seconds` — total request duration minus the time the provider had the
request. Two independent measurements of the same quantity, which is the point: a number a
service reports about itself should have an outside check.

## Regression comparison

```bash
k6 run --summary-export=results/chat.json deploy/loadtest/chat.js
python deploy/loadtest/compare.py results/chat.json --baseline deploy/loadtest/baselines/ci.json
```

Baselines are committed. CI runs `chat.js` against the compose stack on every pull request
and compares — the absolute numbers there are meaningless (a mock upstream, a lexical
embedder, a two-core runner) and the *change* in them is not.

Tolerance is 40% by default. That is loose, deliberately: a shared runner varies by more
than a real regression does, and a tight gate that goes flaky is a gate somebody disables.
It still catches the change from 80 ms to 400 ms, which is the shape an accidental
per-request database round trip actually has.

## Rolling deploy under load

The acceptance criterion — *a rolling deploy during sustained load drops zero requests and
completes every in-flight stream* — is `chat.js` plus a deploy, not a scenario of its own.
The procedure is in
[docs/runbooks/rolling-deploy-under-load.md](../../docs/runbooks/rolling-deploy-under-load.md).
The threshold that decides it is `truncated_streams: ['count==0']` — a stream cut short
still arrives with a 200, because the status line went out long before the connection
broke, so it is invisible to any check that looks at status codes.

## Prerequisites

A gateway, a key, and a model the gateway routes to. On a fresh stack:

```bash
make seed
```

which prints a `mg_...` key for the `demo` gateway. Then set `GATEWAY_API_KEY` to it.

`soak.js` additionally wants `CONTROL_TOKEN` (a control-plane access token) and
`GATEWAY_ID`, so it can read the live concurrency buckets. Without them it still runs, but
it cannot make the drift assertion, which is the main thing a soak is for.

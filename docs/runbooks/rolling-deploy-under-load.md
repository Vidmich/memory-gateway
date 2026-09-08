# Deploying during sustained load

The exercise behind one of task 18's acceptance criteria: *a rolling deploy during
sustained load drops zero requests and completes every in-flight stream.* It is written
down because it is the only way to verify the graceful-shutdown path, and because the
failure it looks for is invisible to every ordinary check.

## Why streams are the hard part

A stream that is cut short still arrives with **HTTP 200**. The status line went out before
the first token, long before the connection broke. So a load test that counts status codes
sees a perfect run, and a customer sees half an answer. `chat.js` asserts
`truncated_streams: ['count==0']`, which detects it by looking for the terminating
`data: [DONE]` frame.

## The mechanism being tested

1. `SIGTERM` arrives. `/readyz` starts answering 503 **immediately** — before anything else
   happens — so the load balancer stops sending new work.
2. The pod keeps serving for `SHUTDOWN_DRAIN_SECONDS` (default 10), because endpoint removal
   is not instant and requests routed a moment ago are still arriving.
3. Only then does uvicorn stop accepting and wait for in-flight requests, streams included.
4. `terminationGracePeriodSeconds` has to exceed drain + the longest upstream call. The
   chart **refuses to render** if it does not — see `_helpers.tpl`.

## Running it

```bash
k6 run -e GATEWAY_URL=https://<staging host> -e GATEWAY_SLUG=demo \
  -e GATEWAY_API_KEY=mg_... -e RPS=20 -e DURATION=10m \
  deploy/loadtest/chat.js
```

Two minutes in, from another terminal:

```bash
helm upgrade <release> deploy/helm/memory-gateway -f <values> --wait
```

Watch the pods turn over while it runs:

```bash
kubectl -n <ns> get pods -l app.kubernetes.io/component=api -w
```

## What a pass looks like

* `truncated_streams` — **0**. Not "low".
* `http_req_failed` — flat across the rollout. A step at the moment of the deploy is the
  drain being too short.
* `checks` — above 99%.
* Pod logs show `draining: readiness is now failing, still serving in-flight work` on each
  pod, followed by `service stopped` several seconds later. A pod that logs the first and
  never the second was killed by the grace period.

## What a failure means

**Truncated streams.** The grace period is shorter than the longest completion. Raise
`api.terminationGracePeriodSeconds`; the chart's guard only enforces the floor, and a model
whose own `timeout_seconds` is high needs more than the floor.

**A step in the error rate at the moment of rollout.** The drain window is too short for
this cluster's endpoint propagation. Raise `api.drainSeconds` — and remember the grace
period has to move with it, which the chart will insist on.

**Errors from the *new* pods.** Not a shutdown problem: a migration that was not
backward-compatible with the release still running. See the expand/contract rule in
CONTRIBUTING.md.

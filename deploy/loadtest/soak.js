// Hours of ordinary traffic, watching for the three things that only show up over time.
//
//   1. **Memory growth.** A leak is invisible in a five-minute run and obvious over four
//      hours. Watch `container_memory_working_set_bytes`, not the request metrics.
//   2. **Connection leaks.** Postgres and Redis connection counts should be flat. A slow
//      climb here ends in a pool exhausted at 3 a.m. rather than during the test.
//   3. **Concurrency counters drifting upward** (task 14). This is the specific one the
//      task file names, and it is the nastiest: the concurrent-request limiter takes a
//      slot and gives it back, and a path that takes without giving — a client that hangs
//      up mid-stream, an upstream that fails after the first frame — silently lowers a
//      gateway's effective limit until it can serve nothing at all. Nothing about the
//      request metrics shows it. `GET /api/v1/gateways/{id}/limits` shows `concurrent`
//      creeping up while the run is idle between waves, which is what the check below
//      turns into a threshold rather than something somebody remembers to look at.
//
//   k6 run -e GATEWAY_URL=... -e GATEWAY_API_KEY=... -e CONTROL_TOKEN=... \
//     -e DURATION=4h deploy/loadtest/soak.js

import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Gauge } from 'k6/metrics'
import { complete, stream, pick, requireKey, GATEWAY_URL } from './common.js'

const DURATION = __ENV.DURATION || '4h'
const RPS = Number(__ENV.RPS || 5)
// A control-plane access token, so the concurrency check below can read the live buckets.
// Without one the soak still runs; it simply cannot make the drift assertion, which is
// the main reason to run it.
const CONTROL_TOKEN = __ENV.CONTROL_TOKEN || ''
const GATEWAY_ID = __ENV.GATEWAY_ID || ''

const idleConcurrency = new Gauge('idle_concurrent_slots')

export const options = {
  scenarios: {
    traffic: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: RPS * 4,
      maxVUs: RPS * 12,
      exec: 'traffic',
    },
    // Every two minutes, pause and ask what the limiter thinks is in flight. Between
    // waves the honest answer is a small number; a rising floor is the leak.
    drift: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '2m',
      duration: DURATION,
      preAllocatedVUs: 1,
      maxVUs: 2,
      exec: 'concurrencyDrift',
    },
  },
  thresholds: {
    // The slot count seen while idle must stay in single digits. A leak of one slot per
    // thousand requests is invisible for an hour and fatal by morning.
    idle_concurrent_slots: ['max<10'],
    checks: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
  },
}

export function setup() {
  requireKey()
  if (!CONTROL_TOKEN || !GATEWAY_ID) {
    console.warn(
      'CONTROL_TOKEN and GATEWAY_ID are not set: the soak will run but cannot check ' +
        'concurrency drift, which is the main thing it exists to find.',
    )
  }
}

export function traffic() {
  const iteration = exec.scenario.iterationInTest
  const user = `soak-user-${iteration % 250}`
  // Alternating, because the two paths release the concurrency slot from different
  // places: the blocking path releases inline, the streaming path from the observer's
  // `done` callback. A leak usually lives in exactly one of them.
  if (iteration % 2 === 0) {
    complete(pick(iteration), { user })
  } else {
    stream(pick(iteration), { user })
  }
}

export function concurrencyDrift() {
  if (!CONTROL_TOKEN || !GATEWAY_ID) return
  // Long enough for every in-flight request started before this to have finished or timed
  // out, so what is left is what leaked rather than what is working.
  sleep(20)
  const response = http.get(`${GATEWAY_URL}/api/v1/gateways/${GATEWAY_ID}/limits`, {
    headers: { Authorization: `Bearer ${CONTROL_TOKEN}` },
    tags: { scenario: 'drift' },
  })
  if (!check(response, { 'limits readable': (r) => r.status === 200 })) return
  const usage = JSON.parse(response.body)
  const scopes = usage.gateway || {}
  const concurrent = (scopes.concurrent_requests || {}).used
  if (typeof concurrent === 'number') idleConcurrency.add(concurrent)
}

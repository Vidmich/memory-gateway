// A burst profile: idle, then five times the steady rate for ninety seconds, then idle.
//
// What it is looking for is not throughput. It is the three things that only misbehave on
// a step change: the connection pool (a burst opens connections faster than keepalive
// hands them back), the HPA (which must not have removed the pods that would have absorbed
// it), and the rate limiter (whose sliding windows have to refuse the right requests and
// then let traffic through again cleanly, rather than staying closed).
//
//   k6 run -e GATEWAY_URL=... -e GATEWAY_API_KEY=... deploy/loadtest/burst.js

import exec from 'k6/execution'
import { complete, pick, requireKey } from './common.js'

const BASE = Number(__ENV.RPS || 10)

export const options = {
  scenarios: {
    burst: {
      executor: 'ramping-arrival-rate',
      startRate: BASE,
      timeUnit: '1s',
      preAllocatedVUs: BASE * 10,
      maxVUs: BASE * 30,
      stages: [
        { target: BASE, duration: '1m' },
        // Ten seconds, not a ramp. A gentle ramp is a load test; a step is what a customer
        // deploying their own feature actually does to you.
        { target: BASE * 5, duration: '10s' },
        { target: BASE * 5, duration: '90s' },
        { target: BASE, duration: '10s' },
        { target: BASE, duration: '1m' },
      ],
    },
  },
  thresholds: {
    // Deliberately not `http_req_failed`: a 429 during the burst is the rate limiter
    // working, and a threshold that fails on one would be a test asserting the limiter is
    // broken. What must not happen is a 5xx.
    'http_req_failed{expected_response:false}': ['rate<0.05'],
    checks: ['rate>0.95'],
  },
}

export function setup() {
  requireKey()
}

export default function () {
  complete(pick(exec.scenario.iterationInTest))
}

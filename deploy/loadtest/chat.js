// The mixed-traffic scenario: streaming and non-streaming, memory on and off, several
// end users, all at once. This is the profile a rolling deploy is executed against —
// see docs/runbooks/rolling-deploy-under-load.md — because the failure it is looking for
// (a truncated stream) only exists while streams are open.
//
//   k6 run -e GATEWAY_URL=... -e GATEWAY_API_KEY=... deploy/loadtest/chat.js

import { sleep } from 'k6'
import exec from 'k6/execution'
import { complete, stream, pick, requireKey } from './common.js'

const RPS = Number(__ENV.RPS || 20)
const DURATION = __ENV.DURATION || '5m'

export const options = {
  scenarios: {
    // Roughly the shape of real traffic: most requests stream, because that is what a
    // chat client does, and a minority do not, because that is what a backend job does.
    streaming: {
      executor: 'constant-arrival-rate',
      rate: Math.round(RPS * 0.7),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(20, RPS * 3),
      maxVUs: Math.max(60, RPS * 8),
      exec: 'streamed',
    },
    blocking: {
      executor: 'constant-arrival-rate',
      rate: Math.round(RPS * 0.3),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RPS),
      maxVUs: Math.max(40, RPS * 4),
      exec: 'blocking',
    },
  },
  thresholds: {
    // Zero. Not "under one per cent" — a truncated stream is a customer receiving half an
    // answer under a 200, and the whole point of the deploy exercise is that the number
    // is zero rather than small.
    truncated_streams: ['count==0'],
    stream_failures: ['rate==0'],
    checks: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
  },
}

export function setup() {
  requireKey()
}

export function streamed() {
  // A hundred distinct end users, so identity resolution and per-user memory recall are
  // both exercised — a single `user` value would keep one row and one Qdrant filter warm
  // and measure a cache nobody has in production.
  const user = `load-user-${exec.scenario.iterationInTest % 100}`
  stream(pick(exec.scenario.iterationInTest), { user })
}

export function blocking() {
  const iteration = exec.scenario.iterationInTest
  // Every fourth request with memory off. Both branches have to be on the same run: the
  // difference between them is what memory costs, and comparing two runs compares two
  // states of the provider as well.
  complete(pick(iteration), {
    user: `load-user-${iteration % 100}`,
    memory: iteration % 4 !== 0,
  })
  sleep(0.1)
}

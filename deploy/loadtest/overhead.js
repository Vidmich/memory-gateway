// The measurement the whole design rests on: SPEC §4.2's "the gateway adds under 150 ms
// p95 over the bare upstream call".
//
// It is measured as a *difference between two populations*, not as a number the service
// reports about itself. Half the iterations go through the gateway; half go straight to
// the provider with the same prompt, the same parameters and the same concurrency. Both
// halves run in the same test, interleaved, against the same upstream at the same moment —
// because a baseline captured an hour earlier is a measurement of the provider's Tuesday
// rather than of this service.
//
//   k6 run \
//     -e GATEWAY_URL=https://gateway.example.com \
//     -e GATEWAY_SLUG=demo -e GATEWAY_API_KEY=mg_... -e GATEWAY_MODEL=demo \
//     -e UPSTREAM_URL=https://api.openai.com/v1 -e UPSTREAM_KEY=sk-... \
//     -e UPSTREAM_MODEL=gpt-4o-mini \
//     --summary-export=results/overhead.json \
//     deploy/loadtest/overhead.js
//
// The gateway must be configured with memory ON for this to be the honest number.
// Retrieval is the dominant cost in the budget and measuring with it switched off is
// measuring something nobody deploys.

import http from 'k6/http'
import { check } from 'k6'
import { Trend } from 'k6/metrics'
import { GATEWAY_URL, GATEWAY_SLUG, API_KEY, MODEL, PROMPTS, requireKey } from './common.js'

const UPSTREAM_URL = __ENV.UPSTREAM_URL || ''
const UPSTREAM_KEY = __ENV.UPSTREAM_KEY || ''
const UPSTREAM_MODEL = __ENV.UPSTREAM_MODEL || 'gpt-4o-mini'
const RPS = Number(__ENV.RPS || 20)

// Two trends rather than one metric with a tag, so the summary prints both percentiles
// side by side and the threshold below can be written against their difference.
const viaGateway = new Trend('latency_via_gateway', true)
const direct = new Trend('latency_direct', true)

export const options = {
  scenarios: {
    // Arrival-rate rather than fixed VUs: the question is what the service does at a
    // given request rate, and a VU-based test silently reduces the rate when it slows
    // down — which is the one behaviour that would hide a regression.
    gateway: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      duration: __ENV.DURATION || '3m',
      preAllocatedVUs: Math.max(20, RPS * 2),
      maxVUs: Math.max(50, RPS * 6),
      exec: 'throughGateway',
    },
    direct: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      duration: __ENV.DURATION || '3m',
      preAllocatedVUs: Math.max(20, RPS * 2),
      maxVUs: Math.max(50, RPS * 6),
      exec: 'straightToProvider',
    },
  },
  thresholds: {
    // Absolute ceilings so a run fails loudly. The *difference* is what SPEC §4.2 talks
    // about and k6 cannot express a threshold across two trends, so `compare.py` computes
    // it from the exported summary and is what CI actually gates on.
    'latency_via_gateway{expected_response:true}': ['p(95)<8000'],
    checks: ['rate>0.99'],
  },
}

function payload(model, prompt) {
  return JSON.stringify({
    model,
    messages: [{ role: 'user', content: prompt }],
    max_tokens: 128,
    temperature: 0,
    // Deterministic sampling on both sides. A provider that returns a longer answer to
    // one half than the other is measuring generation length, not gateway overhead.
    stream: false,
  })
}

export function setup() {
  requireKey()
  if (!UPSTREAM_URL || !UPSTREAM_KEY) {
    throw new Error(
      'set UPSTREAM_URL and UPSTREAM_KEY: without the direct baseline this scenario ' +
        'measures absolute latency, which says nothing about what the gateway added',
    )
  }
}

export function throughGateway() {
  const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)]
  const response = http.post(
    `${GATEWAY_URL}/g/${GATEWAY_SLUG}/v1/chat/completions`,
    payload(MODEL, prompt),
    {
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${API_KEY}` },
      tags: { arm: 'gateway' },
    },
  )
  check(response, { 'gateway 200': (r) => r.status === 200 })
  if (response.status === 200) viaGateway.add(response.timings.duration)
}

export function straightToProvider() {
  const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)]
  const response = http.post(`${UPSTREAM_URL}/chat/completions`, payload(UPSTREAM_MODEL, prompt), {
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${UPSTREAM_KEY}` },
    tags: { arm: 'direct' },
  })
  check(response, { 'direct 200': (r) => r.status === 200 })
  if (response.status === 200) direct.add(response.timings.duration)
}

export function handleSummary(data) {
  const g = data.metrics.latency_via_gateway ? data.metrics.latency_via_gateway.values : {}
  const d = data.metrics.latency_direct ? data.metrics.latency_direct.values : {}
  const overhead = {
    p50: (g['p(50)'] || 0) - (d['p(50)'] || 0),
    p95: (g['p(95)'] || 0) - (d['p(95)'] || 0),
    p99: (g['p(99)'] || 0) - (d['p(99)'] || 0),
  }
  const verdict = overhead.p95 < 150 ? 'WITHIN BUDGET' : 'OVER BUDGET'
  const lines = [
    '',
    'Gateway overhead (SPEC §4.2 budget: p95 < 150 ms)',
    '------------------------------------------------',
    `  p50  ${overhead.p50.toFixed(1)} ms   (${(g['p(50)'] || 0).toFixed(0)} via gateway, ${(d['p(50)'] || 0).toFixed(0)} direct)`,
    `  p95  ${overhead.p95.toFixed(1)} ms   (${(g['p(95)'] || 0).toFixed(0)} via gateway, ${(d['p(95)'] || 0).toFixed(0)} direct)`,
    `  p99  ${overhead.p99.toFixed(1)} ms   (${(g['p(99)'] || 0).toFixed(0)} via gateway, ${(d['p(99)'] || 0).toFixed(0)} direct)`,
    '',
    `  ${verdict}`,
    '',
  ].join('\n')
  return {
    stdout: lines,
    'results/overhead.json': JSON.stringify({ ...data, overhead }, null, 2),
  }
}

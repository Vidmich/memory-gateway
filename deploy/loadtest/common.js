// Shared setup for every scenario in this directory.
//
// The prompts are fixed rather than random. A load test whose inputs vary run to run
// measures the inputs as much as the service, and the number this suite exists to produce
// — gateway overhead against a direct-to-provider baseline — is a *difference* between two
// runs. The two halves have to be asking the same questions.

import http from 'k6/http'
import { check } from 'k6'
import { Trend, Rate, Counter } from 'k6/metrics'

export const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://localhost:8000'
export const GATEWAY_SLUG = __ENV.GATEWAY_SLUG || 'demo'
export const API_KEY = __ENV.GATEWAY_API_KEY || ''
export const MODEL = __ENV.GATEWAY_MODEL || 'demo'

// The same prompts a customer would send: short questions with a little context, not one
// token and not a context window. Ten of them so a run is not measuring one cache line.
export const PROMPTS = [
  'What is the refund window for an annual plan?',
  'Summarise the onboarding steps for a new workspace.',
  'How do I rotate an API key without downtime?',
  'Which regions is data stored in?',
  'What happens when a request exceeds the rate limit?',
  'Explain the difference between a connector and a gateway.',
  'How long are request bodies retained?',
  'Can I point a model at a self-hosted endpoint?',
  'What formats can be ingested?',
  'How is per-user memory kept separate between tenants?',
]

// Time to first byte on a streamed response, which is the number a user perceives. k6's
// built-in http_req_duration covers the whole body, which for a stream is the length of
// the answer rather than the latency of the service.
export const ttft = new Trend('ttft_ms', true)
export const streamFailures = new Rate('stream_failures')
export const truncated = new Counter('truncated_streams')

export function headers(extra = {}) {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${API_KEY}`,
    ...extra,
  }
}

export function chatUrl() {
  return `${GATEWAY_URL}/g/${GATEWAY_SLUG}/v1/chat/completions`
}

export function body({ prompt, stream = false, user = null }) {
  const payload = {
    model: MODEL,
    messages: [{ role: 'user', content: prompt }],
    max_tokens: 128,
    temperature: 0,
    stream,
  }
  // The `user` field is what identifies an end user, and sending one turns conversation
  // memory on for that request. Scenarios that want memory off simply omit it and send
  // X-Gateway-Memory: off, which is the same A/B the product documents.
  if (user) payload.user = user
  return JSON.stringify(payload)
}

export function pick(iteration) {
  return PROMPTS[iteration % PROMPTS.length]
}

// A non-streaming completion. Returns the response so a scenario can assert on it.
export function complete(prompt, { user = null, memory = true } = {}) {
  const extra = memory ? {} : { 'X-Gateway-Memory': 'off' }
  const response = http.post(chatUrl(), body({ prompt, user }), {
    headers: headers(extra),
    tags: { scenario: 'complete', memory: memory ? 'on' : 'off' },
  })
  check(response, {
    'status is 200': (r) => r.status === 200,
    'has a choice': (r) => {
      try {
        return JSON.parse(r.body).choices.length > 0
      } catch (_) {
        return false
      }
    },
  })
  return response
}

// A streamed completion, measured properly.
//
// k6 has no streaming HTTP client, so this reads the whole body and derives TTFT from
// `waiting` — the time until the first byte of the response, which is exactly what the
// server writes before the first SSE frame. The frames themselves are then checked for a
// terminating `[DONE]`, which is how a truncated stream is detected: a stream cut short
// still arrives with a 200, because the status line went out long before it broke.
export function stream(prompt, { user = null } = {}) {
  const response = http.post(chatUrl(), body({ prompt, stream: true, user }), {
    headers: headers({ Accept: 'text/event-stream' }),
    tags: { scenario: 'stream' },
  })
  const ok = response.status === 200
  const complete_ = ok && response.body && response.body.includes('data: [DONE]')
  ttft.add(response.timings.waiting)
  streamFailures.add(!ok)
  if (ok && !complete_) truncated.add(1)
  check(response, {
    'stream started': () => ok,
    'stream finished': () => complete_,
  })
  return response
}

export function requireKey() {
  if (!API_KEY) {
    throw new Error('set GATEWAY_API_KEY (mint one on the gateway, or `make seed`)')
  }
}

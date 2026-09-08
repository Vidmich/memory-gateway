# Integrating with a gateway

You already have a client for this. It is the OpenAI SDK.

A gateway is an OpenAI-compatible endpoint that adds retrieval over your documents and
durable memory about your users, then forwards to whatever model it is configured for. Your
code changes in two places: the base URL and the key.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://gateway.example.com/g/support-bot/v1",
    api_key="mg_live_...",
)

answer = client.chat.completions.create(
    model="support-bot",                       # the gateway's virtual model
    messages=[{"role": "user", "content": "What is the refund window?"}],
    user="customer-8842",                      # who is asking — see below
)
```

That request retrieved from your connectors, recalled what is known about
`customer-8842`, assembled a prompt, forwarded it, and logged the whole thing. Nothing in
the SDK knows any of that happened.

## The three things to get right

### 1. The base URL includes the gateway slug

`https://<host>/g/<slug>/v1` — the slug is on the gateway's page in the UI, next to a copy
button. `/v1` is part of it, because that is what the SDK appends its paths to.

### 2. `model` is the gateway's virtual model, not the provider's

Each gateway exposes one virtual model name, and it is shown beside the URL. Sending a
provider's own name gets a 404 that names both:

```json
{"error": {"message": "Model 'gpt-4o' is not served by gateway 'support-bot'. This gateway exposes 'support-bot'.", "type": "invalid_request_error", "code": "model_not_found"}}
```

Which model actually answered comes back on **`X-Gateway-Model`** — useful when the gateway
is a failover chain or an A/B split, because it changes per request and your code should not
care.

`GET /g/<slug>/v1/models` lists what the gateway exposes, in OpenAI's list format.

### 3. `user` is what makes memory personal

The `user` field is a standard OpenAI parameter and the gateway reads it as the end-user
identity: retrieval filters durable facts to that person, and the background pass that
learns new ones attributes them there.

**Send a stable, opaque id** — your internal user id, not an email. It appears in the
Memory browser, so an id somebody can recognise is a small privacy decision made by
accident.

Without it, document retrieval still works; personal memory does not, because there is
nobody to be personal about.

`X-Gateway-User` does the same thing for a client that cannot set `user`, and wins over it
when both are present.

## Headers

### Sent by you

| Header | Effect |
|---|---|
| `Authorization: Bearer mg_...` | Required. |
| `X-Gateway-User: <id>` | End-user identity, overriding the `user` body field. |
| `X-Gateway-Session: <id>` | Groups turns into one conversation. Without it the gateway infers a session from the message history, which is right most of the time and wrong when two threads share a first message. |
| `X-Gateway-Memory: off` | Serves the request with **no** augmentation — no retrieval, no injected context, no recall. The honest way to measure what memory contributes: send the same question twice and compare. |
| `X-Gateway-Request-Id: <id>` | Your own correlation id. Echoed back, and stored on the request row. |

### Returned to you

| Header | Meaning |
|---|---|
| `X-Gateway-Request-Id` | Always. Quote it in a support conversation; it finds the request. |
| `X-Gateway-Model` | Which upstream model answered. |
| `X-Gateway-Memory-Chunks` · `X-Gateway-Retrieval-Ms` | How many document chunks went into the prompt, and what retrieval cost. **Absent** when retrieval did not run — which is itself the answer to "why did it not use my documents": no connectors attached, or memory switched off. |
| `X-Gateway-Memory-Facts` | How many durable facts about this user went in. Absent for the same reasons, plus "nobody identified the caller". |
| `X-Gateway-Locked-Params` | Names parameters the gateway overrode. Present only when something was actually overridden — the answer to "why did `temperature` have no effect". |
| `X-RateLimit-Limit` · `X-RateLimit-Remaining` · `X-RateLimit-Reset` | Your budget for the tightest limit that applies. On **every** response, not only on a 429 — a client that only learns its limit by exceeding it cannot pace itself. |
| `Retry-After` | On a 429. Seconds. |

## Streaming

`stream: true`, and the SDK's iterator works unchanged. Frames are relayed as they arrive
with no buffering in between.

Two things worth knowing:

**Errors before the first token are real HTTP errors.** The gateway opens the upstream
call and checks its status *before* writing any bytes downstream, so a provider's 429 comes
back to you as a 429 rather than as a 200 containing an apology.

**An error after the first token cannot be.** The status line is long gone. The stream ends
early, and the request shows as such in the gateway's own logs. If a truncated answer
matters to you, check that the last frame you received was `data: [DONE]`.

Usage arrives on the final chunk when the provider sends it.

## Rate limits

A gateway can carry limits per minute, per day, on requests, on tokens, and on concurrent
requests — set per gateway and, optionally, per end user. Exceeding one is a 429 with the
OpenAI error shape, which every OpenAI SDK retries on its own with backoff.

Because the headers are on every response, the better integration does not wait for the
429: read `X-RateLimit-Remaining` and slow down.

Token limits count the tokens *including* injected memory, since that is what the provider
is billed for.

## What is not supported

These are refused with a 400 naming the field, rather than silently dropped:

`tools` · `tool_choice` · `functions` · `function_call` · `logprobs`

Sent as `null`, `false` or `[]` they ask for nothing and are ignored rather than refused.

Tool calling is the top item on the roadmap. Until it lands, an agentic client pointed at a
gateway fails loudly on its first tool-using request — which is the intended behaviour: the
alternative is a model that silently stops being able to call tools.

Everything else in the chat-completions body rides through, including parameters this
gateway has never heard of. Some are subject to per-gateway policy: an operator can lock
`temperature`, and `X-Gateway-Locked-Params` will say so.

## Errors

The OpenAI error envelope throughout the data plane, so your SDK raises the typed exception
it would raise against the provider:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "model_not_found"}}
```

| Status | Usually |
|---|---|
| 401 | The key is wrong, revoked, or belongs to a different gateway. |
| 404 | The slug or the `model` name. |
| 413 | The body is over the deployment's size ceiling. |
| 429 | A rate limit. Read `Retry-After`. |
| 502 · 503 | The upstream failed or could not be reached. On a gateway with a failover chain this means every target failed. |

## Keys

Minted on the gateway's page. The plaintext is shown **once** — there is no endpoint that
reveals it, and a lost key is replaced rather than recovered.

Revocation is immediate on the next request. Rotate by minting the new key, deploying it,
then revoking the old one; both work at the same time in between.

A key is scoped to one gateway. It cannot reach another gateway, another organization, or
the control plane.

## Anthropic models

A gateway can be pointed at Claude. Nothing changes on your side: you still speak OpenAI,
still get OpenAI-shaped responses, still stream the same way. A gateway split 50/50 between
an OpenAI model and an Anthropic one is not detectable from the client except through
`X-Gateway-Model`.

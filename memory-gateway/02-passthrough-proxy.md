# Task 02 — Pass-through proxy

**Slice:** an OpenAI-compatible endpoint that authenticates a key and forwards completions to a
real upstream, streaming and non-streaming.
**Depends on:** 01
**Spec:** §4.2, §5.1 (data plane), §8.3, §12.1
**Size:** L

> **Milestone M1.** This is the highest-risk part of the system. Prove it works end-to-end
> against a live provider before building a single screen around it.

---

## Why this slice

If the proxy path is wrong — streaming buffered, errors mangled, tokens miscounted — every later
feature is built on sand. Doing it now means the rest of the project is decoration on a working
core, and there is something real to show from week one.

## Demo at the end of this task

```bash
make seed        # creates a demo org, an upstream model from OPENAI_API_KEY, a gateway, a key
```

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/g/demo/v1", api_key="mg_...")

print(c.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)

for chunk in c.chat.completions.create(
        model="demo", messages=[{"role": "user", "content": "count to 5"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

Both work. A bad key returns a 401 the SDK raises cleanly. Streaming visibly arrives token by
token, not all at once at the end.

## In scope

- Minimal tables: `organizations` (id/name/slug only), `upstream_models`, `gateways`, `api_keys`.
- Data-plane authentication.
- OpenAI request/response schemas and the `openai` dialect adapter.
- Streaming and non-streaming forwarding, error mapping, seed CLI.

## Out of scope

- UI (03), tenancy and users (04), model/gateway management APIs (05, 06), routing modes (08),
  logging (07), memory (09–13), the `anthropic` dialect (16).
- Configuration is seeded via CLI; no CRUD endpoints yet.

## Work items

### Schema (minimal, forward-compatible)
- [x] `organizations(id, name, slug UNIQUE, status, created_at)` — created now purely so every
      later table can carry a real `organization_id` FK. Task 04 fills in the rest.
- [x] `upstream_models(id, organization_id NULL, scope, name, base_url, dialect,
      upstream_model_id, auth_type, credential_ciphertext, extra_headers_jsonb, system_context,
      default_params_jsonb, timeout_seconds, enabled)`.
- [x] `gateways(id, organization_id, slug UNIQUE, name, enabled, system_context,
      param_overrides_jsonb)`.
- [x] `gateway_targets(id, gateway_id, upstream_model_id, priority, weight)` — created now with
      exactly one row per gateway; task 08 makes the list meaningful.
- [x] `api_keys(id, gateway_id, name, key_hash, prefix, last_used_at, revoked_at)`.
- [x] Envelope encryption helper (`app/core/crypto.py`): AES-GCM data key wrapped by
      `ENCRYPTION_MASTER_KEY`. Used for `credential_ciphertext`.

### Schemas & adapter
- [x] Pydantic models for the OpenAI chat completion request and response, plus the streaming
      chunk shape. Model the fields listed in SPEC §12.1; allow unknown fields to pass through
      to the upstream rather than rejecting them.
- [x] `UpstreamAdapter` protocol:
      ```python
      class UpstreamAdapter(Protocol):
          def prepare(self, req: ChatRequest, model: UpstreamModel) -> httpx.Request: ...
          def parse(self, resp: httpx.Response) -> ChatResponse: ...
          def parse_stream(self, resp: httpx.Response) -> AsyncIterator[ChatChunk]: ...
      ```
      Design it so a future `tools` field threads through without restructuring (SPEC §16.1).
- [x] `OpenAIAdapter` implementation covering `bearer`, `api_key_header`, and `azure` auth
      styles, plus `extra_headers`.
- [x] Parameter resolution order: model `default_params` → gateway `param_overrides` → client
      request. Locked params (task 06) are a later refinement; leave the merge point obvious.

### Auth
- [x] Key format `mg_<key_id>_<secret>`; store `sha256(secret)` and a display `prefix`.
      Parsing the id out of the token means one indexed lookup instead of scanning hashes.
- [x] `Authorization: Bearer` extraction, constant-time comparison, revoked/disabled checks.
- [x] `last_used_at` updated asynchronously (fire-and-forget) so auth never blocks on a write.
- [x] Resolve gateway by slug; 404 for unknown slug, 403 if the key does not belong to it.

### Forwarding
- [x] Shared `httpx.AsyncClient` with connection pooling and per-model timeouts
      (connect / read / total, with read timeout applied to time-to-first-byte).
- [x] Non-streaming: forward, parse, return.
- [x] Streaming: relay SSE **without buffering**. Iterate upstream bytes and yield downstream
      immediately; terminate with `data: [DONE]`.
- [x] Handle client disconnect mid-stream by cancelling the upstream request (do not keep paying
      for tokens nobody receives).
- [x] Response headers: `X-Gateway-Request-Id`, `X-Gateway-Model`.
- [x] Prepend the model's `system_context` to the message list. This is layer 1 of the eventual
      assembler (SPEC §7) — put it behind a `PromptAssembler` seam now so task 10 extends rather
      than rewrites it.

### Errors
- [x] All proxy-route errors use the OpenAI error envelope:
      `{"error": {"message", "type", "param", "code"}}` — client SDKs parse this shape and will
      surface a useful exception instead of a generic one.
- [x] Upstream 4xx/5xx are relayed with their status and message, tagged so it is obvious the
      error came from upstream rather than the gateway.
- [x] Timeouts → 504. Connection failures → 502.
- [x] **Unsupported fields** (`tools`, `tool_choice`, `functions`, `function_call`, `logprobs`)
      → 400 naming the field explicitly. Silently dropping `tools` produces an agent that
      narrates tool calls as prose, which is very expensive to diagnose.

### Endpoints
- [x] `POST /g/{slug}/v1/chat/completions`
- [x] `GET /g/{slug}/v1/models` — OpenAI list shape, returning the gateway's virtual model names.

### Seed
- [ ] `make seed` / `python -m app.cli seed`: creates a demo organization, an upstream model
      from `OPENAI_API_KEY` in the environment, a gateway with slug `demo`, and one API key —
      printing the key plaintext once. *(written; never run against a database — see below)*
- [ ] Idempotent: re-running updates rather than duplicating. *(covered by `db`-marked tests
      that skip on this machine)*

## Acceptance criteria

- [x] The official `openai` Python SDK works against the gateway with only `base_url` changed.
- [x] Streaming is genuinely incremental: first chunk reaches the client in under 200 ms plus
      upstream TTFT (verify by timestamping received chunks, not by eyeballing).
- [x] Invalid, revoked, and wrong-gateway keys each return the correct status and an OpenAI-shaped
      error body.
- [x] An upstream 429 is relayed as a 429, not converted to a 500.
- [x] Sending `tools` returns 400 with the field named.
- [x] Credentials never appear in logs, error messages, or responses.
- [x] Disconnecting a streaming client cancels the upstream call (assert on the mock server).

## Tests

- Adapter unit tests for request preparation across all three auth styles.
- SSE parsing: multi-line data frames, comments/keepalives, split-across-packets chunks,
  malformed frames.
- A mock upstream server (`respx` or a local ASGI app) covering: success, 401, 429, 500,
  timeout, mid-stream disconnect, and a slow first byte.
- Auth: valid, malformed, unknown id, wrong secret, revoked, wrong gateway.
- An integration test against a real provider, marked and skipped without a live key.

## Notes

- Resist adding a request-log write here. Task 07 owns that, and doing it now means building it
  twice — once synchronously and once properly.
- Keep the gateway config lookup behind a `GatewayResolver` service. Task 06 adds Redis caching
  and invalidation behind the same interface.

## Verification status

Everything above was implemented and the suite is green — **313 passed, 26 skipped** — but
three things could not be exercised on the development machine, and are called out rather
than assumed:

| Not verified here | Why | What covers it |
|---|---|---|
| The migration against a real PostgreSQL | No server and no Docker on this machine | 23 `db`-marked tests (schema constraints, resolver SQL, authenticator, seed idempotency), plus an offline test that renders the migration to SQL and compares every table, column and constraint name against the models. CI runs the `db` tests with `REQUIRE_DB_TESTS=1`, which turns the skip into a failure. |
| `make seed` end to end | Same | `seed_demo` is covered by the `db`-marked tests; the CLI's argument parsing and its missing-key path were run by hand. |
| A call to a real provider | No provider key available here | `test_against_a_real_provider`, marked `live` and skipped without `OPENAI_API_KEY`. Everything else runs against a real ASGI upstream over a real socket, including the official `openai` SDK. |

The `docker compose up` demo inherits task 01's caveat: Docker is not installed here, so the
compose stack has still never been built or run.

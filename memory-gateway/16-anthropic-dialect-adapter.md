# Task 16 — Anthropic dialect adapter

**Slice:** Claude models become usable upstreams, and clients still speak plain OpenAI.
**Depends on:** 05 (08 recommended, so an A/B across dialects is demonstrable)
**Spec:** §8.3
**Size:** M

---

## Why this slice

The gateway's premise is that the client never changes. That only holds if the upstream set isn't
limited to OpenAI-shaped providers. This is also the task that proves the adapter abstraction from
task 02 was real rather than aspirational — if adding a dialect requires touching the proxy, the
seam was wrong.

## Demo at the end of this task

Add an upstream model with dialect `anthropic` pointing at the Anthropic API. Point a gateway at
it. The same unmodified OpenAI SDK client gets completions from Claude, streaming included, with
correct `finish_reason` and `usage` in the response.

Then set the gateway to A/B: 50% OpenAI model, 50% Anthropic model. Fire 100 requests — every
response conforms to the OpenAI schema regardless of which upstream served it, and the monitoring
page shows the split.

## In scope

- `AnthropicAdapter` implementing the task 02 protocol: request translation, response
  translation, streaming event translation, error mapping, usage mapping.
- Registration in the dialect registry; UI enablement of the option.

## Out of scope

- Inbound `/v1/messages` support (SPEC §16.6) — this is outbound translation only.
- Tool use in either direction (SPEC §16.1).
- Vision/multimodal content blocks, extended thinking, prompt caching controls.
- Bedrock and Vertex hosting variants — note the base-URL and auth differences in the code for
  whoever adds them.

## Work items

### Request translation
- [x] Extract **all** system messages from the OpenAI message list, concatenate in order, and set
      them as the top-level `system` parameter. This is the single most important difference: a
      system message left in Anthropic's `messages` array is either rejected or silently
      demoted.
- [x] Map `messages` to Anthropic content blocks. Text-only in v1: each message becomes
      `{"role": ..., "content": [{"type": "text", "text": ...}]}`.
- [x] Enforce Anthropic's structural rules and repair where unambiguous:
      - roles must alternate user/assistant — merge consecutive same-role messages
      - the first message must be `user` — if the client's history begins with an assistant turn,
        prepend a minimal user turn rather than erroring
      - trailing whitespace on a final assistant turn is not allowed — strip it
- [x] `max_tokens` is **required** by Anthropic and optional in OpenAI. Default it from the
      model's `default_params`, then a per-dialect fallback (e.g. 4096). Never send an unbounded
      request.
- [x] Parameter mapping: `temperature` (clamp to Anthropic's 0–1 range), `top_p`, `stop` →
      `stop_sequences`. Drop unsupported OpenAI params (`presence_penalty`,
      `frequency_penalty`, `n`, `seed`, `logit_bias`) and record the dropped set on the request
      log so the behavior is discoverable rather than mysterious.
- [x] `n > 1` is unsupported — return a clear 400 rather than silently returning one choice.
- [x] Auth: `x-api-key` header plus `anthropic-version`. Add `api_key_header` handling for the
      header name if not already generic.

### Response translation
- [x] Anthropic response → OpenAI `chat.completion`: concatenate text blocks into
      `choices[0].message.content`, generate an `id`, set `object`, `created`, and `model`.
- [x] `stop_reason` mapping: `end_turn` → `stop`, `max_tokens` → `length`,
      `stop_sequence` → `stop`, `tool_use` → `tool_calls` (unreachable in v1, but map it now).
- [x] Usage: `input_tokens` → `prompt_tokens`, `output_tokens` → `completion_tokens`, and compute
      `total_tokens`. Accurate usage matters — task 14's token limits and task 07's metrics both
      read it.

### Streaming translation
- [x] Translate Anthropic's event stream into OpenAI `chat.completion.chunk` frames:
      - `message_start` → first chunk with `role: "assistant"` and empty delta
      - `content_block_delta` (`text_delta`) → chunk with `delta.content`
      - `message_delta` → carries `stop_reason` and final usage
      - `message_stop` → final chunk with `finish_reason`, then `data: [DONE]`
      - `ping` → swallowed, never forwarded
      - `error` mid-stream → an error frame, and the task 08 `failed_after_stream_start` flag
- [x] Preserve incremental delivery — translate frame by frame, never accumulate and re-emit.
- [x] Emit usage on the final chunk when `stream_options.include_usage` was requested, matching
      OpenAI's behavior.

### Errors & registration
- [x] Map Anthropic error types to OpenAI error `type` values (`invalid_request_error`,
      `authentication_error`, `rate_limit_error`, `api_error`, `overloaded_error` → 503),
      preserving the upstream message.
- [x] Ensure the task 08 retry classification treats `overloaded_error` as retryable.
- [x] Register `anthropic` in the dialect registry; **no changes to the proxy route should be
      required** — if any are, fix the abstraction instead.
- [x] UI: enable the dialect option in the model form, with an Anthropic provider preset (base
      URL, auth style, default `max_tokens`) and a note listing the dropped parameters.

## Acceptance criteria

- [x] The unmodified OpenAI SDK gets correct non-streaming and streaming completions from an
      Anthropic upstream.
- [x] Multiple client system messages all reach Anthropic's `system` parameter, in order.
- [x] Consecutive same-role messages and a leading assistant message are repaired, not rejected.
- [x] `max_tokens` is always present on the outbound request.
- [x] `finish_reason` and `usage` are correct for both normal and truncated completions.
- [x] Streaming is incremental; ping events are invisible to the client.
- [x] A 50/50 A/B across dialects produces responses indistinguishable in schema.
- [x] Adding this dialect required zero changes to `app/api/proxy/`.

## Tests

- **Contract tests run against both adapters** from one shared table of request/response cases,
  asserting identical OpenAI-shaped output. This is the real deliverable — it is what keeps the
  dialects from drifting as either provider evolves.
- Message-sequence repair: consecutive roles, leading assistant, empty content, whitespace tail.
- System-message extraction with zero, one, and several system messages at various positions.
- Stream translation from recorded Anthropic event fixtures, including `ping`, mid-stream error,
  and a truncated stream.
- Parameter mapping, clamping, and drop recording.
- Error mapping for each Anthropic error type.
- A live integration test, marked and skipped without an `ANTHROPIC_API_KEY`.

## Notes

- Record real Anthropic SSE streams as fixtures once and test against them. Hand-written event
  fixtures drift from reality and give false confidence exactly where translation bugs hide.
- Dropping `presence_penalty` and friends silently is the kind of thing that produces a support
  ticket six months later. Recording the dropped set on the request log makes it answerable in
  thirty seconds.


---

## Verification status

Everything above is implemented and covered. What follows is the reasoning worth carrying
forward, then the numbers.

### The seam held, and where it moved

**Nothing in `app/api/proxy/` changed.** That was the criterion this task existed to test,
and it is now enforced rather than asserted: `test_the_data_plane_route_knows_nothing_about
_dialects` reads those files and fails if the words `dialect`, `anthropic` or `claude`
appear in any of them. A behavioural test cannot catch a route that grows an `if` and still
returns the right bytes; this can.

The seam did move, in three places, and each is the abstraction being made right rather
than worked around.

**`UpstreamAdapter` gained `error()`.** A provider's own status is not always the one the
client should act on — Anthropic answers "overloaded, try again shortly" with a 529, which
SPEC §8.2's retry table has never heard of and therefore treats as *final*, stopping a
failover chain on precisely the failure retrying was invented for. Adding `529: True` to the
table would have been wrong: 529 is Anthropic's number, not a fact about HTTP. Translating
it in the dialect that owns it means the table stays a statement about statuses and the
probe, the proxy and the router all see the same answer.

**`parse_stream` takes the outbound request.** `stream_options.include_usage` changes which
frames OpenAI emits. A passthrough dialect never needs to know — the provider already
applied it — but a dialect that *produces* those frames has no other way to find out. The
openai adapter ignores the argument and says so in one line.

**The HTTP plumbing moved to `app/adapters/http.py`.** Auth styles, timeouts and endpoint
URLs are not dialect questions: a provider wanting `x-api-key` wants it whichever body it
speaks. `build_headers` grew one parameter for headers a *dialect* requires
(`anthropic-version`), placed below the auth header and below `extra_headers` so an
operator can still pin a version when Anthropic ships a breaking one.

### Decisions worth keeping

**The outbound body is an allowlist, which is the opposite of the openai dialect.** There,
a field this code has never heard of belongs to the provider and rides through untouched —
that is the whole dialect. Here, a field this code has never heard of is a 400 from
Anthropic, so only mapped fields go on the wire. The two adapters therefore disagree about
`reasoning_effort`, deliberately, and both are right.

**Every repair is a repair, not a rejection.** Consecutive same-role turns, a leading
assistant turn, an empty message, whitespace on a trailing prefill: none of those is wrong
by OpenAI's rules, and the most ordinary thing a client does is replay a stored
conversation. A gateway whose compatibility claim fails on *that* has no claim. The case
worth naming is that the repairs compose — merging two assistant turns *creates* a leading
assistant turn, which then needs the minimal user turn in front of it, and a translator
written one rule at a time gets that wrong. There is a test for exactly that ordering.

**The minimal user turn is a full stop.** Anything longer is putting words in the caller's
mouth, and it appears in the transcript the operator reads.

**A dropped parameter is recorded, and `n > 1` is refused.** The difference is whether the
caller could notice. A silently ignored `presence_penalty` produces a slightly different
answer and no signal at all — so it is dropped, recorded on the row, shown in the request
drawer, and named on the model form before anybody makes a request. `n: 3` silently
returning one completion is structurally wrong and undetectable without counting, so it is
a 400 naming the field.

`response_format` is the judgement call inside that rule, and it went the same way as the
others rather than becoming a second 400. A caller who asks for JSON mode and gets prose
finds out immediately, at their own parse; and Claude prompted for JSON returns JSON, so a
400 would refuse requests that would have worked. It is dropped, recorded, and listed on
the form with the rest.

**Usage on a stream is opt-in, matching OpenAI exactly.** Anthropic always reports it, and
it was tempting to always forward it — the request log would then have token counts for
every streamed Claude request, which it does not. That was refused: the acceptance criterion
is that a client cannot tell which upstream served it, and an extra frame is something a
client can tell. The consequence is stated rather than hidden — a streamed request without
`include_usage` records no token counts, identically to an OpenAI one, because in both cases
nobody asked.

**`data` and `chunk` on a `StreamFrame` are built from one object.** The client reads the
first and the request log reads the second. For a passthrough dialect they cannot diverge
because there is only ever the provider's own bytes; for a translating one they could, and
that would make every streamed transcript quietly wrong in a way no test of either half
alone would find. `_frame()` serialises and validates the same dict, and a test asserts the
round trip.

**A mid-stream `error` event ends the stream the way a dropped connection does.** Not a new
mechanism: it raises, `UpstreamStream.frames()` catches it beside the transport errors, the
client gets a terminating SSE error frame, and the row gets `failed_after_stream_start`.
SPEC §8.2 is about what can no longer be recovered once a 200 is on the wire, and a provider
giving up is the same category as a socket closing.

**The response id names the provider's message.** `chatcmpl-msg_01ABC` — OpenAI-shaped for
the client's prefix check, and still resolvable in Anthropic's own logs by whoever is
holding a support ticket. A freshly minted UUID would have been easier and would have thrown
that away.

### The contract test is the deliverable

`tests/test_adapter_contract.py` is the part of this task with the longest half-life. One
table of generations and failures, each described in *neither* provider's terms; each
dialect renders it as its own wire format; the assertions compare the normalised OpenAI
view. The drift it exists to prevent is not a wrong translation on day one — it is the
gradual divergence where somebody adds a field to one adapter and six months later two
upstreams behind one gateway answer differently.

Three things make it hold rather than decorate:

- `test_the_table_covers_every_registered_dialect` fails if a dialect is registered and not
  listed, so the file cannot silently stop covering the thing it was added for.
- `test_a_stream_reassembles_into_the_completion_it_would_have_returned` ties the two
  shapes together per dialect — task 07 stores exactly that reassembly as the transcript.
- `test_every_dialect_agrees_on_whether_to_try_the_next_target` runs the translated failure
  through the *real* `is_retryable`, which is what caught the 529 as a genuine finding
  rather than as a table entry somebody remembered.

The failure table originally carried one status per case. Writing it that way made the
overloaded case fail — correctly: an OpenAI-shaped provider says "overloaded" with a 503 and
Anthropic says it with a 529, and a table that demanded they use the same number was
demanding a lie. It now carries a status per provider and one `client_status` they must both
reach, which is the actual contract.

### Bugs and near-misses this task found

- **`upstream_error_fields` lived in `app/services/proxy.py`** and the probe imported it
  from there, so "Test connection" and the live request path shared a function by accident
  rather than by design. Moving the translation onto the adapter made that a property: the
  button now reports what the request would have reported, for a dialect that remaps a
  status as much as for one that does not.
- **`MalformedUpstreamResponse` was defined in the openai adapter** and imported by two
  services and a test, which made a generic failure look like an OpenAI-specific one. It is
  in `base.py` now.
- **The `Failing` adapter double in `tests/test_proxy_streaming.py` was structurally typed**
  against the protocol, so growing the protocol by two methods broke it — which is the type
  checker doing its job, and worth noting because every future dialect method will do the
  same to the same three doubles.
- **`test_an_unsupported_dialect_is_refused_with_a_reason` was written against
  `anthropic`** and passed for a year because there was no adapter. It now uses `bedrock`,
  and a second test asserts the thing that changed — the check did not move, the registry
  under it did.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   307 files already formatted
uv run mypy                    Success: no issues found in 291 source files
uv run pytest -q               3022 passed, 412 skipped

npx eslint . / npx tsc         clean
npx vitest run                 566 passed (25 files)
npm run build                  454.32 kB JS (133.08 kB gzipped)
```

Task 16 adds **168 backend checks** across four new test modules plus the additions to the
request-log and SDK suites, and **6 web checks**. One backend check is the live Anthropic
integration test and skips without a key — see below.

`make openapi` regenerates `web/openapi.json` and `web/src/api/schema.d.ts`; both are
committed and byte-stable, and the only change is `dropped_params` on the request-log row.

### Not verifiable on this machine

- **No Anthropic key.** `test_against_a_real_anthropic_provider` is marked `live` and skips.
  It is the one check that would prove Anthropic *accepts* what this dialect sends — every
  other test here proves the gateway produces what the code intends, which is a different
  claim. It sends a system message deliberately, because lifting one out of `messages` is
  the translation most likely to be rejected and a request with only a user turn would never
  exercise it.
- **The stream fixtures are written, not captured.** `tests/fixtures/anthropic/*.sse` follow
  the documented event sequence for `anthropic-version: 2023-06-01` rather than a recording
  from a live account, and the README there says so rather than leaving it to be assumed. A
  real capture would additionally carry the service's own frame boundaries, keepalive cadence
  and field ordering — which is where the remaining translation bugs would be. Refreshing
  them is a small job for whoever first runs the live test.
- **No PostgreSQL**, so the `ADD COLUMN` in `0015_anthropic_dialect` is checked only by the
  offline migration test, which renders the SQL and compares it against the models. That it
  propagates to every partition of `request_logs` without rewriting the table is a
  PostgreSQL guarantee this repository documents and cannot demonstrate here.

### One thing left slightly open

The dropped-parameter record covers what the *caller* asked for and what the model's
`default_params` carry. It does not cover a gateway's `param_overrides`, because a gateway
can point at several models in different dialects at once and the layer that merges them
belongs to the request rather than to the recorder. That layer is operator configuration
rather than caller intent — an operator setting `presence_penalty` on a gateway can see the
setting on the screen where they set it — so the gap is narrow, and it is written down in
`_dropped` next to the code that has it.

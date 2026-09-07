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
- [ ] Extract **all** system messages from the OpenAI message list, concatenate in order, and set
      them as the top-level `system` parameter. This is the single most important difference: a
      system message left in Anthropic's `messages` array is either rejected or silently
      demoted.
- [ ] Map `messages` to Anthropic content blocks. Text-only in v1: each message becomes
      `{"role": ..., "content": [{"type": "text", "text": ...}]}`.
- [ ] Enforce Anthropic's structural rules and repair where unambiguous:
      - roles must alternate user/assistant — merge consecutive same-role messages
      - the first message must be `user` — if the client's history begins with an assistant turn,
        prepend a minimal user turn rather than erroring
      - trailing whitespace on a final assistant turn is not allowed — strip it
- [ ] `max_tokens` is **required** by Anthropic and optional in OpenAI. Default it from the
      model's `default_params`, then a per-dialect fallback (e.g. 4096). Never send an unbounded
      request.
- [ ] Parameter mapping: `temperature` (clamp to Anthropic's 0–1 range), `top_p`, `stop` →
      `stop_sequences`. Drop unsupported OpenAI params (`presence_penalty`,
      `frequency_penalty`, `n`, `seed`, `logit_bias`) and record the dropped set on the request
      log so the behavior is discoverable rather than mysterious.
- [ ] `n > 1` is unsupported — return a clear 400 rather than silently returning one choice.
- [ ] Auth: `x-api-key` header plus `anthropic-version`. Add `api_key_header` handling for the
      header name if not already generic.

### Response translation
- [ ] Anthropic response → OpenAI `chat.completion`: concatenate text blocks into
      `choices[0].message.content`, generate an `id`, set `object`, `created`, and `model`.
- [ ] `stop_reason` mapping: `end_turn` → `stop`, `max_tokens` → `length`,
      `stop_sequence` → `stop`, `tool_use` → `tool_calls` (unreachable in v1, but map it now).
- [ ] Usage: `input_tokens` → `prompt_tokens`, `output_tokens` → `completion_tokens`, and compute
      `total_tokens`. Accurate usage matters — task 14's token limits and task 07's metrics both
      read it.

### Streaming translation
- [ ] Translate Anthropic's event stream into OpenAI `chat.completion.chunk` frames:
      - `message_start` → first chunk with `role: "assistant"` and empty delta
      - `content_block_delta` (`text_delta`) → chunk with `delta.content`
      - `message_delta` → carries `stop_reason` and final usage
      - `message_stop` → final chunk with `finish_reason`, then `data: [DONE]`
      - `ping` → swallowed, never forwarded
      - `error` mid-stream → an error frame, and the task 08 `failed_after_stream_start` flag
- [ ] Preserve incremental delivery — translate frame by frame, never accumulate and re-emit.
- [ ] Emit usage on the final chunk when `stream_options.include_usage` was requested, matching
      OpenAI's behavior.

### Errors & registration
- [ ] Map Anthropic error types to OpenAI error `type` values (`invalid_request_error`,
      `authentication_error`, `rate_limit_error`, `api_error`, `overloaded_error` → 503),
      preserving the upstream message.
- [ ] Ensure the task 08 retry classification treats `overloaded_error` as retryable.
- [ ] Register `anthropic` in the dialect registry; **no changes to the proxy route should be
      required** — if any are, fix the abstraction instead.
- [ ] UI: enable the dialect option in the model form, with an Anthropic provider preset (base
      URL, auth style, default `max_tokens`) and a note listing the dropped parameters.

## Acceptance criteria

- [ ] The unmodified OpenAI SDK gets correct non-streaming and streaming completions from an
      Anthropic upstream.
- [ ] Multiple client system messages all reach Anthropic's `system` parameter, in order.
- [ ] Consecutive same-role messages and a leading assistant message are repaired, not rejected.
- [ ] `max_tokens` is always present on the outbound request.
- [ ] `finish_reason` and `usage` are correct for both normal and truncated completions.
- [ ] Streaming is incremental; ping events are invisible to the client.
- [ ] A 50/50 A/B across dialects produces responses indistinguishable in schema.
- [ ] Adding this dialect required zero changes to `app/api/proxy/`.

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

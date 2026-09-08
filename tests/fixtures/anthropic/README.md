# Anthropic SSE fixtures

Each `.sse` file is one complete Messages-API event stream, byte for byte as it arrives
over the wire: `event:` name, `data:` payload, blank line between frames. They are the
input to the stream-translation tests, which replay one and assert what a client would
have seen.

Files rather than event lists built inside a test, for the reason the task notes give: a
hand-assembled list drifts toward whatever the translator already expects, and the bugs
worth catching live exactly in the gap between that and what a provider actually sends —
the `ping` nobody remembered, the `message_delta` that carries usage but no text, the
`content_block_start` that arrives with a prefill already in it.

## Provenance, honestly

These were **written against the documented event sequence for `anthropic-version:
2023-06-01`, not captured from a live account.** That is the weaker of the two options and
it is worth knowing which one is in the repository: a real capture would also carry the
frame boundaries, the keepalive cadence and the field ordering of the actual service,
which is where the remaining translation bugs would be.

Replacing them with real captures is a small job and worth doing the first time anyone
runs this against a live key: make the request, save the raw stream, and keep the file
verbatim. Nothing here should ever be edited by hand to make a test pass — if a test
disagrees with a fixture, the fixture stands for the provider and the translator is wrong.

## What each one is for

| file | the case it covers |
| --- | --- |
| `normal.sse` | the ordinary stream: two `ping`s in the middle, three text deltas, a clean `end_turn` |
| `truncated.sse` | generation cut short by `max_tokens`, which must reach the client as `finish_reason: "length"` |
| `overloaded_midstream.sse` | the provider gives up after a frame has been delivered — SPEC §8.2's unrecoverable case |
| `thinking.sse` | extended thinking: a whole content block that must never appear in the client's content |
| `prefill.sse` | `content_block_start` carrying text, and a `stop_sequence` finish |

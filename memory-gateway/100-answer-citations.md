# Task 100 — Citations in the generated answer

**Slice:** a gateway can be told to return, alongside the model's answer, exactly which retrieved
chunks the answer cites — resolved to a document, a page or section, and a link — so a client
can render "Sources" without parsing prose.
**Depends on:** 07, 10, 11, 20
**Spec:** §7 (prompt assembly), §10.3 (request detail), §12.1 (data plane) — and amends §7,
which today tells the model to cite and then throws the citations away.
**Size:** M
**Status:** **done.** Independent of 101–104, but 103 consumes what this records.

---

## Why this slice

The prompt already asks for citations. §7 renders every retrieved chunk under a numbered handle
— `[1] source: handbook.pdf (p. 12)` — and instructs the model to *"cite them when relevant"*.
Models do. The answer comes back saying "as [2] notes, expenses are reimbursed within thirty
days", and the gateway forwards it verbatim to a client that has never seen `[2]`, has no idea
what it refers to, and could not link to it if it did.

So the one promise retrieval makes — *this came from your documents, here* — is made to the
model and withheld from the user. Task 20 went to some lengths to make citations honest: a
PDF chunk names its page, a code chunk names its function, a `sentence_window` chunk records
which sentence actually matched. All of that is currently visible in the request detail drawer
and nowhere a client can reach.

There is a second reason, and it is the one that makes this a data task and not a formatting
one. **Which chunks the model actually cited is the only relevance signal this product gets for
free.** The request log records which chunks were *injected*; nothing records which were
*used*. A chunk that is injected in every request and cited in none is a retrieval false
positive, and that is precisely the number task 103 needs and cannot get any other way without
a human labelling set.

## Demo at the end of this task

Set a gateway's **Citations** to `metadata`. Ask it a question through the OpenAI SDK. The
response's message carries a `citations` array: for `[2]`, the document name, the connector, the
page, the chunk id and a control-plane URL that opens the chunk in the inspector. Ask a question
the documents do not answer; the array is empty and the answer says so.

Switch to `footer`. The same question through `curl` ends with a "Sources" block a terminal can
read. Stream it: the sources arrive after the last content token and before `[DONE]`.

Open the request in **Monitoring**. The chunk list now shows which of the six injected chunks
were cited — two of them — and the third one, injected at score 0.71 and never cited, is the
one you would remove from the corpus.

## In scope

- Resolving `[n]` handles in a completion back to the chunks that were injected under them.
- Three delivery modes per gateway: `off`, `metadata` (a field on the response), `footer`
  (appended to the content). Streaming and non-streaming, both dialects.
- Recording cited-vs-injected on the request log, and showing it in the detail drawer.
- Hallucinated handles — a `[7]` when six were injected — detected, stripped or flagged, and
  counted.

## Out of scope

- **Span-level attribution** (which *sentence* of the answer came from which chunk). That is
  an alignment problem needing a second model call or logprobs; the handle the model already
  emits is the honest unit.
- **Asking the model to cite in a structured format** (JSON tool call, XML tags). Depends on
  tool passthrough (SPEC §16.1) for the first, and the second changes the prompt for every
  gateway whether or not citations are on. The `[n]` convention is already in the prompt and
  already followed.
- **Verifying that the cited chunk supports the claim.** A cited chunk is one the model *pointed
  at*; whether it says what the model said it says is a faithfulness evaluation, and task 103
  is where evaluation lives.
- **Citations of end-user memory facts.** Facts are rendered as an unnumbered list (§7) and
  citing "what you know about this user" back at the user is a different product decision.

## Work items

### Resolution

- [x] `app/services/citations.py`: `resolve(text, injected) -> Resolution` where `injected` is
      the ordered list of chunks the assembler numbered, and the result carries `cited` (in
      order of first appearance, deduplicated), `unresolved` (handles with no chunk behind
      them), and `spans` (character offsets of every handle, for stripping). Pure; no I/O.
- [x] Handle grammar covers what models actually emit: `[2]`, `[2, 3]`, `[2][3]`, `[2-4]`,
      and `[2]` inside a Markdown link or footnote. It does **not** match array literals in code
      blocks — a fenced block is skipped whole — because a citation inside `arr[0]` is how a
      coding assistant's gateway would cite chunk zero on every answer.
- [x] The numbering is the assembler's, not a recount. `assemble()` already assigns handles in
      `render_entry`; expose them on `Prepared` so resolution and the prompt cannot disagree
      about which chunk is `[2]` after `fit_documents` drops one from the tail.
- [x] A cited chunk resolves to: `chunk_id`, `document_id`, `document_name`, `connector_id`,
      `section` (the same `page_or_section` the prompt rendered), `chunk_strategy`, and, under
      `sentence_window`, `matched_text` — the sentence that matched, not the window around it.
      The URL is the control plane's inspector for that chunk. Nothing here is text the client
      has not already been sent inside the prompt; it is the same information, structured.

### Delivery

- [x] `GatewayConfig.citations: Literal["off", "metadata", "footer"] = "off"`. Off by default:
      a gateway fronting an unmodified client should not grow a response field or a footer the
      day this deploys.
- [x] `metadata`: a `citations` array on the assistant message object — `choices[0].message`
      non-streaming, and on a **final extra chunk** with an empty delta in streaming, emitted
      after the upstream's last content frame and before `[DONE]`. Extra fields are what every
      OpenAI SDK ignores and every hand-written client can read; a header is not an option
      because the answer has to be complete before the citations are known.
- [x] `footer`: `\n\nSources:\n[2] handbook.pdf, p. 12\n[4] pricing.md` appended to the content
      — as a final content delta when streaming. For clients that render Markdown, the handle in
      the footer links to the inspector URL. Footer text is *not* counted in `completion_tokens`
      reported to the client; the upstream's usage is forwarded as received and the gateway's
      addition is recorded separately on the log.
- [x] Streaming resolution buffers **only the tail**: a `[` that arrives at the end of a frame
      may be the start of a handle or a literal bracket, and holding back the last few characters
      until the next frame decides is the whole cost. Content is otherwise forwarded frame by
      frame; this task must not change the time-to-first-token that task 18 measures.
- [x] Both dialects. The Anthropic adapter (task 16) translates the extra chunk and the footer
      the same way it translates everything else; an `/v1/messages` client on a `metadata`
      gateway gets the array on the final `message_delta`.
- [x] Hallucinated handles: under `footer` they are **stripped** from the answer (a "[7]" with
      no source listed below it is worse than no marker); under `metadata` they are left in the
      text and reported in `citations_unresolved`; under both they are counted. A model that
      cites chunks that were not injected is a model that is making things up in the one place
      it was asked not to, and the count is the signal.

### Recording

- [x] `request_logs.cited_chunk_ids` beside `retrieved_chunk_ids`, and `citations_unresolved`
      (an integer). Same retention, same redaction rules. `retrieved_chunk_ids` is *injected*;
      the new column is *used*; both are needed to compute the ratio.
- [x] The request detail drawer marks each injected chunk cited or not, and lists unresolved
      handles. A chunk injected and uncited is rendered without alarm — most are — but the
      **filter** "requests where nothing was cited" exists, because that is the query an operator
      runs when a corpus is suspected of being irrelevant.
- [x] Metrics: `citations_resolved_total{gateway}`, `citations_unresolved_total{gateway}`, and
      `requests_uncited_total{gateway}` for requests that injected documents and cited none.
      The last one on the monitoring page beside injected memory tokens: a gateway paying for
      2 000 tokens of context per request and citing none of it is the cheapest optimisation in
      the product.
- [x] Off gateways still **resolve and record**. Resolution is pure and costs microseconds; the
      log column is filled whether or not the client is shown anything. Otherwise the relevance
      signal exists only for the gateways that happened to turn a client-facing feature on.

### UI

- [x] **Gateways → Prompt** gains the citations selector, with one example rendered per mode
      for the current sample question — the same prompt-preview machinery, extended one field.
- [x] **Try retrieval** (Gateways → Memory) shows the handle each chunk would be numbered with,
      so a person reading a logged answer can map `[3]` back without opening the drawer.
- [x] The chunk inspector accepts the URL the citation carries and scrolls to the chunk.

### Spec

- [x] Amend §7: the citation instruction is honoured on the way back, not only on the way in.
      Document the `citations` array shape and the footer format in §12.1 as a gateway
      extension, marked as such.

## Acceptance criteria

- [x] With `metadata` on, a non-streaming answer citing `[1]` and `[3]` carries exactly those two
      chunks, in that order, resolved to the documents the prompt named — asserted by comparing
      against the assembled prompt on the same request.
- [x] With `footer` on, a streamed answer's frames are byte-identical to the `off` case up to
      the last content frame; the footer is one additional delta; `[DONE]` follows. Measured
      time-to-first-token is unchanged.
- [x] A handle split across two SSE frames (`[` in one, `2]` in the next) resolves.
- [x] `[0]` inside a fenced code block is not a citation.
- [x] `[7]` with six chunks injected is counted as unresolved, stripped under `footer`, and
      reported under `metadata`.
- [x] An `off` gateway records `cited_chunk_ids` on the log and returns a response
      byte-identical to today's.
- [x] The Anthropic dialect delivers both modes.
- [x] The request drawer distinguishes cited from injected, and the "nothing cited" filter
      returns exactly the requests where documents were injected and `cited_chunk_ids` is empty.

## Tests

- Resolution grammar: each handle form, deduplication, ordering by first appearance, code
  blocks, Markdown links, and a document that is *about* citation syntax (the adversarial
  corpus).
- Numbering parity: a chunk dropped by `fit_documents` never resolves, and the handles the
  resolver uses are the ones `render_entry` wrote.
- Streaming: the tail buffer over every split point of a handle, and over a frame that ends in
  a literal `[` followed by a frame that does not complete a handle.
- Both delivery modes through both dialects, streamed and not, against recorded upstream
  frames.
- The `off` path: byte-identity of the response and presence of the log column.
- Metrics and the drawer filter.

## Implementation notes

- **Where the setting lives.** There is no `GatewayConfig` object; the mode is
  `MemoryConfig.citations` in the gateway's `memory_config` blob (it is about the retrieved
  documents), and it is *edited* under Gateways → Prompt (it is about the answer). No
  migration for the setting — the blob is permissive on load and a row without the key reads
  `off`. The two log columns did need one: `0019_answer_citations`.
- **"Both dialects" means both upstream dialects.** The citation stage runs over the
  adapter's `StreamFrame`s, after translation, so an Anthropic upstream gets both modes for
  free — `tests/test_proxy_citations.py` proves it. An inbound `/v1/messages` API does not
  exist (SPEC §16.6, post-v1), so "the array on the final `message_delta`" is for whoever
  builds that: the resolution is on `Prepared`/`Resolution`, not in the OpenAI shape.
- **Time-to-first-token** is asserted structurally rather than measured: under `off` and
  `metadata` the stage hands back the very frame object it was given and nothing is held;
  under `footer` at most a partial handle (and the space before it) waits for the next
  frame, and `tests/test_citations.py` drives every split point. Task 18's load suite is the
  place a wall-clock number belongs.
- **The footer links the document name, not the handle.** `[2] [handbook.pdf (p. 12)](url)`
  reads correctly in a terminal *and* renders as a link in Markdown; a linked handle
  (`[[2]](url)`) is noise in the first and the same link in the second. The handle text is
  the model's own either way.
- **What resolves and what does not**, beyond the work item: `[^2]` footnote form counts;
  a bracket glued to a word (`items[0]`) does not; `matrix[1][2]` does not, though `[2][3]`
  does — a `]`-preceded handle only continues a chain that started with a real one; ranges
  longer than 20 are two numbers rather than an expansion.

## Notes

- **The footer changes the model's text and the metadata does not.** That is why the default
  is `off` and why `metadata` should be the recommended mode in the UI: it is additive, it is
  ignored by every client that does not know about it, and it does not put words in the
  answer. `footer` exists for the client you do not control — a terminal, a Slack bot, a
  plugin that renders `content` and nothing else.
- **Cited-vs-injected is a proxy for relevance, and a biased one.** Models over-cite the first
  handle and under-cite the last; a chunk can be relevant and uncited. Task 103 treats it as a
  weak label, not a ground truth. It is still the only label that arrives on every request at
  zero cost.
- **Do not renumber.** It is tempting to renumber the citations `1..k` in the footer so the
  client sees a tidy list. Then `[3]` in the answer text and `[2]` in the footer are the same
  chunk, and nobody can tell. The handle the model wrote is the handle the footer shows.

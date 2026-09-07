# Task 10 — RAG retrieval & prompt assembly

**Slice:** the model answers from the customer's uploaded documents.
**Depends on:** 09
**Spec:** §6.1 (A), §6.3, §7, §13.1 (Gateways → Memory/Prompt)
**Size:** L

> This is the product's core promise. Everything before it was infrastructure for this moment.

---

## Why this slice

Retrieval and prompt assembly are separated from ingestion on purpose: ingestion failures are
about files, retrieval failures are about relevance and latency, and debugging them together is
miserable. With task 09 already proven, anything wrong here is in this code.

## Demo at the end of this task

Attach the "Product docs" connector to a gateway, save. Ask the endpoint a question that is
answerable only from those documents — the model answers correctly and cites the source. Ask an
unrelated question — it says the material doesn't cover it rather than inventing an answer.

In the gateway editor's **Memory** section, type that question into **Try retrieval**: the exact
chunks that would be injected appear with similarity scores and source names. Lower `top_k` to 1
and watch the answer degrade. The request detail drawer shows the assembled prompt with injected
content visually distinguished.

## In scope

- Per-gateway memory configuration (document half).
- Retrieval service with scoring, filtering, timeouts, and failure policy.
- Full layered prompt assembler with token budgeting.
- Try-retrieval and prompt-preview UI; retrieval visibility in logs and monitoring.

## Out of scope

- Conversation memory (12) — the assembler reserves layer 4 and renders nothing there yet.
- Hybrid search, reranking, query rewriting (SPEC §16.4, §17.4). Dense retrieval on the last user
  message only; note the limitation in the UI.

## Work items

### Memory configuration
- [ ] `memory_config` blob (document half) per SPEC §6.3: `connector_ids`, `doc_top_k` (6),
      `doc_min_score` (0.35), `doc_max_tokens` (2000), `query_strategy`
      (`last_user_message` | `last_n_turns`), `query_n_turns` (3),
      `retrieval_timeout_ms` (800), `on_retrieval_error` (`fail_open` | `fail_closed`).
- [ ] Only connectors belonging to the gateway's organization may be referenced — validated on
      write, and re-checked at request time.
- [ ] An empty `connector_ids` means no document retrieval, and the request path skips the vector
      call entirely rather than searching an empty filter.

### Retrieval service
- [ ] Build the query text per `query_strategy`; strip system messages and, for `last_n_turns`,
      concatenate only the user turns.
- [ ] Embed the query with the same model that built the collection; assert dimension agreement
      and fail loudly on mismatch rather than returning garbage neighbours.
- [ ] Qdrant search filtered by `org_id` **and** `connector_id ∈ connector_ids`. The org filter is
      redundant with the per-org collection and is kept as defense in depth (SPEC §5.3).
- [ ] Drop results below `doc_min_score`. Returning nothing is a valid, common outcome and must
      render as no reference block at all — not an empty heading.
- [ ] Hard timeout per `retrieval_timeout_ms`; on timeout or error apply `on_retrieval_error`
      (`fail_open` → proceed without memory, `fail_closed` → 503 with a clear message).
- [ ] Deduplicate adjacent chunks from the same document to avoid spending the token budget on
      near-identical neighbours.
- [ ] Cache the query embedding briefly (identical query text within a short window) — cheap, and
      it helps repeated evaluation runs.

### Prompt assembler
- [ ] Implement SPEC §7 in full, replacing the task 02 stub:
      `model.system_context` → `gateway.system_context` → documents → *(memory: layer 4, empty
      until task 12)* → the client's own system message(s), concatenated in order.
- [ ] Render the reference block exactly as specified, with numbered entries carrying
      `source: <name> (<page_or_section>)`, and the instruction to say so when the material does
      not answer the question.
- [ ] Omit any empty layer along with its delimiter — no orphan headings.
- [ ] **Token budgeting**: enforce `doc_max_tokens`; drop chunks from the tail (lowest score
      first) and record what was dropped on the request log. Truncate the document block before
      the memory block, per SPEC §7.
- [ ] Guard against the assembled prompt exceeding the model's context window: if the client's
      own messages already fill it, inject nothing and set a warning flag rather than producing a
      request the upstream will reject.
- [ ] `X-Gateway-Memory: off` request header skips all augmentation — essential for measuring the
      gateway's actual contribution.
- [ ] Assembly is a pure function over `(request, gateway, model, chunks, facts)` so it is fully
      unit-testable with golden-file comparisons.

### Integration
- [ ] Retrieval runs concurrently with anything else awaitable; the structure must already be
      `asyncio.gather`-shaped so task 12 adds the second branch without restructuring.
- [ ] `latency_retrieval_ms` recorded on every request.
- [ ] Log `retrieved_chunk_ids` with scores; the detail drawer resolves them to source documents.
- [ ] Response headers `X-Gateway-Memory-Chunks` and the retrieval latency.
- [ ] New metrics: retrieval latency percentiles, empty-retrieval rate, timeout rate, average
      injected tokens. **Empty-retrieval rate is the key quality signal** — a gateway retrieving
      nothing 80% of the time is misconfigured, and only this metric will reveal it.

### UI
- [ ] Gateway editor **Memory** section replacing the task 06 placeholder: connector multi-select
      with document counts, the retrieval knobs with explanatory help text, and the failure-policy
      selector.
- [ ] **Try retrieval**: a query box that returns the chunks that *would* be injected — score,
      source document, page/section, and the chunk text, with an indicator for which ones survive
      the token budget. This is the tuning loop; make it fast and prominent.
- [ ] **Prompt preview**: the fully assembled prompt for a sample question, with each layer
      colour-coded and labelled, and a token count per layer against the model's context window.
- [ ] Request detail drawer: retrieved chunks with scores, links to source documents, dropped-chunk
      notes, and the assembled prompt with injected regions highlighted.
- [ ] Monitoring: retrieval latency and empty-retrieval rate charts.

## Acceptance criteria

- [ ] A question answerable only from an uploaded document is answered correctly; the same
      gateway with `X-Gateway-Memory: off` cannot answer it. This A/B is the proof the feature
      works.
- [ ] With no relevant documents, no reference block is injected and the model is not pushed into
      inventing an answer.
- [ ] Retrieval adds **< 150 ms p95** to total request latency (SPEC §4.2).
- [ ] A Qdrant outage with `fail_open` still serves requests; with `fail_closed` it returns 503
      with a clear message. Neither hangs past the configured timeout.
- [ ] `doc_max_tokens` is never exceeded; drops are recorded and visible.
- [ ] A gateway cannot reference another organization's connector, even by direct API call.
- [ ] Try-retrieval results exactly match what a real request injects for the same query.

## Tests

- Assembler golden-file tests over the full layer matrix, including every empty-layer combination
  and multiple client system messages.
- Token budgeting: exact-boundary, over-budget, and context-window-overflow cases.
- Retrieval: score threshold filtering, connector filtering, dimension mismatch, timeout under
  both failure policies, empty results.
- End-to-end grounding test with a fixture corpus containing a fact absent from any model's
  training data (a made-up product code), asserted with and without memory.
- Cross-tenant: retrieval never returns another org's chunks, verified against a seeded
  two-org fixture.

## Notes

- Expect the naive query strategy to fail on conversational follow-ups ("what about the second
  one?"). That is SPEC §17.4, deliberately unresolved. Track empty-retrieval rate from day one so
  the decision to add query rewriting is driven by data rather than intuition.
- The reference-block wording is a product surface, not a detail. Instructing the model to admit
  when the material doesn't answer the question is what separates a grounded assistant from a
  confident liar.

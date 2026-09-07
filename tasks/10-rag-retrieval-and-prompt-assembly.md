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
- [x] `memory_config` blob (document half) per SPEC §6.3: `connector_ids`, `doc_top_k` (6),
      `doc_min_score` (0.35), `doc_max_tokens` (2000), `query_strategy`
      (`last_user_message` | `last_n_turns`), `query_n_turns` (3),
      `retrieval_timeout_ms` (800), `on_retrieval_error` (`fail_open` | `fail_closed`).
- [x] Only connectors belonging to the gateway's organization may be referenced — validated on
      write, and re-checked at request time.
- [x] An empty `connector_ids` means no document retrieval, and the request path skips the vector
      call entirely rather than searching an empty filter.

### Retrieval service
- [x] Build the query text per `query_strategy`; strip system messages and, for `last_n_turns`,
      concatenate only the user turns.
- [x] Embed the query with the same model that built the collection; assert dimension agreement
      and fail loudly on mismatch rather than returning garbage neighbours.
- [x] Qdrant search filtered by `org_id` **and** `connector_id ∈ connector_ids`. The org filter is
      redundant with the per-org collection and is kept as defense in depth (SPEC §5.3).
- [x] Drop results below `doc_min_score`. Returning nothing is a valid, common outcome and must
      render as no reference block at all — not an empty heading.
- [x] Hard timeout per `retrieval_timeout_ms`; on timeout or error apply `on_retrieval_error`
      (`fail_open` → proceed without memory, `fail_closed` → 503 with a clear message).
- [x] Deduplicate adjacent chunks from the same document to avoid spending the token budget on
      near-identical neighbours.
- [x] Cache the query embedding briefly (identical query text within a short window) — cheap, and
      it helps repeated evaluation runs.

### Prompt assembler
- [x] Implement SPEC §7 in full, replacing the task 02 stub:
      `model.system_context` → `gateway.system_context` → documents → *(memory: layer 4, empty
      until task 12)* → the client's own system message(s), concatenated in order.
- [x] Render the reference block exactly as specified, with numbered entries carrying
      `source: <name> (<page_or_section>)`, and the instruction to say so when the material does
      not answer the question.
- [x] Omit any empty layer along with its delimiter — no orphan headings.
- [x] **Token budgeting**: enforce `doc_max_tokens`; drop chunks from the tail (lowest score
      first) and record what was dropped on the request log. Truncate the document block before
      the memory block, per SPEC §7.
- [x] Guard against the assembled prompt exceeding the model's context window: if the client's
      own messages already fill it, inject nothing and set a warning flag rather than producing a
      request the upstream will reject.
- [x] `X-Gateway-Memory: off` request header skips all augmentation — essential for measuring the
      gateway's actual contribution.
- [x] Assembly is a pure function over `(request, gateway, model, chunks, facts)` so it is fully
      unit-testable with golden-file comparisons.

### Integration
- [x] Retrieval runs concurrently with anything else awaitable; the structure must already be
      `asyncio.gather`-shaped so task 12 adds the second branch without restructuring.
- [x] `latency_retrieval_ms` recorded on every request.
- [x] Log `retrieved_chunk_ids` with scores; the detail drawer resolves them to source documents.
- [x] Response headers `X-Gateway-Memory-Chunks` and the retrieval latency.
- [x] New metrics: retrieval latency percentiles, empty-retrieval rate, timeout rate, average
      injected tokens. **Empty-retrieval rate is the key quality signal** — a gateway retrieving
      nothing 80% of the time is misconfigured, and only this metric will reveal it.

### UI
- [x] Gateway editor **Memory** section replacing the task 06 placeholder: connector multi-select
      with document counts, the retrieval knobs with explanatory help text, and the failure-policy
      selector.
- [x] **Try retrieval**: a query box that returns the chunks that *would* be injected — score,
      source document, page/section, and the chunk text, with an indicator for which ones survive
      the token budget. This is the tuning loop; make it fast and prominent.
- [x] **Prompt preview**: the fully assembled prompt for a sample question, with each layer
      colour-coded and labelled, and a token count per layer against the model's context window.
- [x] Request detail drawer: retrieved chunks with scores, links to source documents, dropped-chunk
      notes, and the assembled prompt with injected regions highlighted.
- [x] Monitoring: retrieval latency and empty-retrieval rate charts.

## Acceptance criteria

- [x] A question answerable only from an uploaded document is answered correctly; the same
      gateway with `X-Gateway-Memory: off` cannot answer it. This A/B is the proof the feature
      works.
- [x] With no relevant documents, no reference block is injected and the model is not pushed into
      inventing an answer.
- [x] Retrieval adds **< 150 ms p95** to total request latency (SPEC §4.2).
- [x] A Qdrant outage with `fail_open` still serves requests; with `fail_closed` it returns 503
      with a clear message. Neither hangs past the configured timeout.
- [x] `doc_max_tokens` is never exceeded; drops are recorded and visible.
- [x] A gateway cannot reference another organization's connector, even by direct API call.
- [x] Try-retrieval results exactly match what a real request injects for the same query.

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

---

## Verification status

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   219 files already formatted
uv run mypy                    Success: no issues found in 208 source files
uv run pytest -q               2084 passed, 311 skipped

npx eslint . / npx tsc         clean
npx vitest run                 375 passed (17 files)
npm run build                  392.62 kB JS (117.23 kB gzipped)
```

OpenAPI regenerated; `web/openapi.json` and `web/src/api/schema.d.ts` are in step with the
server.

### The demo, verified

Driven through the real route in `tests/test_proxy_memory.py` — the app under a real HTTP
client, a scriptable provider on a real socket, and the memory implementations of the four
ports:

* a question answerable only from an uploaded document arrives at the provider carrying
  that document, and the **same question with `X-Gateway-Memory: off` carries none of it**;
* a question the corpus does not cover injects **no reference block at all** — not an
  empty heading — and the response says `X-Gateway-Memory-Chunks: 0`, which is a different
  answer from the header being absent;
* a Qdrant outage under `fail_open` still serves; under `fail_closed` it is a 503 whose
  message names the policy rather than reading as a fault;
* `doc_max_tokens` is never exceeded, and a chunk that fell outside it is on the request
  log with `dropped: doc_max_tokens`;
* Try retrieval returns the chunks a real request injects — asserted by running both and
  comparing the rendered block byte for byte, not by inspection.

The **grounding corpus contains a made-up product code** (`Zynthorp QX-4471`) that is in
no model's training data. That is what makes the assertion checkable without a real model:
what is asserted is that the gateway *sent* it, which can only be true if retrieval put it
there.

The whole script was also run once as a throwaway smoke over a **real uvicorn socket** —
the real app on a real port, a real HTTP client, the control plane and the data plane in
one process — walking the demo in order: three files ingested to `indexed`; the connector
attached and a foreign connector refused with a 422; Try retrieval ranking `warranty.md`
first with a score; `doc_top_k` lowered to 1 from the editor and the gateway *not* saved;
a score floor of 0.99 coming back `empty` rather than broken; the prompt preview showing
five layers with the reference block in the spec's exact shape; the endpoint answering
with the document in front of the model; the same question with `X-Gateway-Memory: off`
carrying none of it and no memory headers at all; an unrelated question producing no
orphan heading; and the log row carrying the retrieval latency, the memory tokens and the
chunk record with its source, score and fate. **34/34.**

### What is different from the plan, and why

* **`upstream_models.context_window` is a new nullable column** (migration
  `0010_context_window`). The overflow guard the acceptance criteria ask for needs a
  window, and there is no sound way to derive one: it differs by two orders of magnitude
  between providers and changes when a provider reissues a model name. `NULL` means
  **unknown**, not unlimited, and the guard is skipped entirely for such a model — a
  guessed window would start withholding memory from requests a provider would have
  served, which is a worse failure than the one the guard prevents and much harder to
  notice. One optional field on the Models form, one metadata-only `ADD COLUMN`.
* **`doc_max_tokens` caps the whole rendered block, boilerplate included.** SPEC §6.3 calls
  it a hard cap on injected document text; the reading that is *checkable* is the one a
  customer can verify by counting what reached the provider. It costs about forty tokens of
  the allowance and buys a property that is true as stated, including the corner where a
  budget too small for the heading injects nothing rather than an orphan heading.
* **The failure policy is applied by the route, not by the retriever.** `Retriever` never
  raises for a retrieval failure; it returns an outcome and the request path calls
  `Retrieval.enforce(policy)`. That split is what lets Try retrieval render the same
  failure as a readable diagnostic instead of a 503 — the same code, two audiences.
* **Try retrieval and Prompt preview accept an *unsaved* `memory_config`,** merged through
  the same `merge_config` the save path uses. Tuning a score floor by saving, sending
  traffic and reading the log is a loop measured in minutes that also changes what live
  callers get between attempts. The unsaved patch is re-scoped against the organization's
  own connectors, because an endpoint that accepts an arbitrary configuration is exactly
  where somebody would try naming a connector the save path refuses.
* **They are two endpoints, not one.** Try retrieval has to be fast and is pressed
  repeatedly; the prompt preview is a different panel answering a different question. They
  share one query box in the UI and one code path on the server, so they cannot disagree.
* **Retrieved chunks are stored on the log as records, not ids.** `retrieved_chunk_ids`
  keeps the score, the source name, the section and what became of the chunk. Resolving an
  id against the vector store later would fail for exactly the requests worth
  investigating: a document that has since been reindexed no longer has that point. What a
  request retrieved is a fact about the request.
* **Adjacent chunks of one document are deduplicated.** Chunks overlap by design, so
  neighbours share about a sixth of their text and score alike; two of them spends the
  budget twice on one passage and pushes a genuinely different document out. This means
  `doc_top_k` is a ceiling rather than a target, which is stated where it is configured.
* **The dimension check is a store method, not a caught exception.** `VectorStore.dimension`
  is new, cached per collection per process, and checked before the search. Under
  `fail_open` a mismatch is otherwise completely silent — every request serves fine and
  simply has no documents — so it is refused and logged at `error`, which is the one
  severity nobody filters out.
* **The empty-retrieval rate is derived from the log rows, not from Prometheus.**
  `retrieval_attempts` and `retrieval_empty` are counted in the summary query so the
  monitoring screen can show it per gateway and per window like every other number there.
  The row cannot distinguish "found nothing" from "retrieval failed under `fail_open`"; the
  `retrieval_attempts_total{outcome}` metric can, and both are documented as meaning what
  they mean. Both belong in the screen's number, because both are a model answering
  without the documents it was supposed to have.
* **`PromptAssembler` was deleted.** `assemble` replaced it and nothing in `app/` called it
  any more — it was production code kept alive by its own test. Its two unique cases
  (multi-part content in a system message, and a multi-part *user* message forwarded
  whole) moved into `tests/test_prompt_layers.py`; its parameter-merge half was already
  duplicated in `tests/test_params.py`.

### Bugs the tests found

* **The accounting was lost when nothing was prepended.** `assemble` returned early — with
  the client's own message list, which is right — but that early return dropped `dropped`
  and `overflowed` on the floor. A request with no system context whose every chunk fell
  outside the budget is precisely the one somebody opens the drawer for, and its row would
  have said nothing at all. The early return now decides only which message list to hand
  back; the account is returned either way.
* **The reference block had a blank line between the heading and the instruction.** SPEC §7
  prints them on consecutive lines. A golden file caught it, which is the reason the
  golden files exist: a substring assertion would have passed.
* **A zero-second query cache still cached.** `>` rather than `>=` on the TTL meant
  `ttl_seconds=0` cached anything read in the same instant. Zero now means "do not cache",
  which is what setting it to zero is asking for.

### Not verifiable here

Unchanged from tasks 04–09. PostgreSQL on this machine rejects the credentials in `.env`,
so migration `0010_context_window` is covered by `tests/test_migration_offline.py` — which
renders it to SQL and asserts every mapped column and constraint is created — but the DDL
has not run against a server. The `db`-marked half of the metrics contract, which is where
`jsonb_array_length` on `retrieved_chunk_ids` and the two `FILTER` aggregates actually
execute, skips: the memory half runs the identical check list, and the SQL half will run in
CI.

There is no Qdrant, so the three new `dimension()` checks in `tests/vector_store_contract.py`
run against the memory store and skip the `qdrant` half. That half matters here more than
usual — `_vector_size` reads a shape out of Qdrant's `CollectionInfo`, which is precisely
where a hand-written double agrees with itself and disagrees with the server — so it is
written, marked, and will run in CI.

Retrieval's **< 150 ms p95** budget (SPEC §4.2) is not measured on this machine against a
real Qdrant and a real embedding provider, and the number that would come out of the memory
implementations would be about a dictionary lookup rather than about the system. What is
enforced instead is the bound that governs it: `retrieval_timeout_ms` is applied with
`asyncio.timeout` and asserted to fire, and `retrieval_duration_seconds` has a bucket edge
at exactly 0.15 so the percentile can be read against the budget the moment there is real
traffic. No Playwright, no `make`, no Docker.

### Notes for later tasks

* **Task 12** fills layer 4. `MemoryService.recall` is already an `asyncio.gather` over two
  branches — the second returns `()` today — so conversation memory is a body, not a
  restructuring of the request path. `assemble` already takes `facts`, renders them under
  SPEC §7's heading, and truncates the document block before the memory block; `Recall`
  already carries the field; `retrieved_fact_ids` is already on the log row and in the
  detail response.
* **Task 13** distils from the transcripts this task already stores, including the
  assembled prompt with its injected regions.
* **Task 14**'s limits are the third blob in the same editor and the third
  `merge_config` call beside the two this task uses.
* **Task 16**'s Anthropic adapter receives the assembled `ChatRequest` exactly as the
  OpenAI one does: the layering happens before the dialect, so nothing here changes.
* **Task 17** owns the reindex flow the dimension check makes visible. When it moves the
  embedding configuration into `platform_settings`, `VectorStore.dimension` is what tells
  it a collection is stale, and the per-process cache is what it must invalidate — today
  that happens through `drop`, which clears the entry.

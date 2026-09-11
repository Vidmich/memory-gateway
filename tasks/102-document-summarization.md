# Task 102 — Optional document summarization before chunking & embedding

**Slice:** a connector can have an LLM summarize each document as it is ingested, and use the
summary in one of two ways — as an additional retrievable chunk that answers "what is this
document about", or as context prepended to every chunk's *embedding* so a chunk about "the
second option" embeds as a chunk about the second option *of the expense policy* — with every
token that summarization spends recorded and charted on the monitoring page beside distillation.
**Depends on:** 09, 10, 13, 20
**Spec:** §6.1, §9.3, §9.5, §10.1 (metrics), §13.1 (Settings, Monitoring) — and amends §9.3 and
§10.1.
**Size:** L
**Status:** post-v1. Its configuration becomes part of what task 104 fingerprints.

---

## Why this slice

A chunk is embedded alone. Cut from the middle of a forty-page policy, it says "the second
option requires approval above the threshold" and nothing about which policy, which option, or
which threshold — the embedding model sees those twelve words and places the vector wherever
twelve context-free words go. Retrieval then fails in the way task 20 called invisible: a
question about expense approval thresholds returns something, and it is a worse something than
the corpus deserved.

The known fix is to give the embedder the context the chunk lost. A short summary of the whole
document, generated once, either stands on its own as a chunk (so a question *about the
document* finds it) or is prefixed to each chunk before embedding (so every chunk's vector
carries where it came from). The second is the one that measurably moves retrieval; the first
is the one that answers the question naive RAG cannot — "do we have a policy on this at all?".

Task 20 built the mechanism this needs without knowing it: `sentence_window` split
`embedded_text` from `text`, so what is embedded and what is returned are already two fields,
and retrieval, dedupe and citations already cope with the difference. A summary prefix is
another value for `embedded_text`. The citation promise holds — `text` is still the source,
verbatim — and the only new thing on the retrieval side is a chunk that is *honestly labelled*
as a summary rather than a quote.

And it costs a model call per document, which is why the second half of this task is a ledger.
Task 13 records every distillation pass in a table the monitoring page charts; summarization
records its own the same way, plus the one column distillation never recorded: tokens.

## Demo at the end of this task

Open a connector, **Summarization**, and turn it on in `summary_chunk` mode with the
organization's cheap model. Upload the handbook. Its row shows *summarized* with a token count;
open the document and the summary is at the top, editable. Ask the gateway "what documents do
we have about travel?" — the answer cites the handbook's summary chunk, labelled as a summary,
not a page.

Switch the mode to `contextual`. The connector says every document must be re-embedded and
what it will cost — roughly the corpus's token count once for the summaries plus once more for
the prefixes. Accept. Ask the question that used to return the wrong section; it returns the
right one, and the chunk inspector shows the prefix that was embedded above the text that was
returned.

Open **Monitoring**. Beside distillation health there is a summarization panel: documents
summarized today, tokens spent by model, failures, and the connector spending the most.

## In scope

- Per-connector summarization: off, `summary_chunk`, `contextual`, or both.
- The summarization model chosen the way the distillation model is — from the organization's
  catalog, falling back to the platform default — with a daily cap.
- The summary stored on the document, editable, and re-embedded when edited.
- Usage recording per run — tokens in and out, model, duration, outcome — and the monitoring
  panel that charts it.
- The ingestion consequences: a new phase that can fail, what a failure does to the document,
  and what a change to any of this does to the index (via task 20's fingerprint).

## Out of scope

- **Summarizing the *answer* or the *query*.** This task is about the corpus.
- **Replacing a document with its summary in the index.** The summary is rewritten text; a
  citation pointing at it as if it were the source is the failure task 20 ruled out. The
  summary is always *in addition to* the source chunks, and always labelled.
- **Hierarchical summaries** (section summaries, summaries of summaries). Document-level first;
  measure; the section level is the same mechanism with a different unit, if it is needed.
- **Choosing the mode automatically.** `contextual` roughly doubles embedding spend; a person
  turns it on having seen the number.
- **Backfilling `distillation_runs` with token counts.** Worth doing; not this task. This task
  records tokens for its own runs and leaves the shape ready for distillation to adopt.

## Work items

### Configuration

- [ ] `SummarizationConfig` on the connector's `ConfigBlob`, beside `ChunkingConfig`:
      `mode: Literal["off", "summary_chunk", "contextual", "both"] = "off"`,
      `model_id: UUID | None` (None = the organization's distillation model, then the
      platform default), `max_summary_tokens: int = 150`, `max_input_tokens: int = 12_000`
      (what is sent — the head of the document, and the tail if it fits, which is where an
      abstract and a conclusion live), `daily_document_cap: int | None`.
- [ ] Per-format overrides, reusing task 20's `overrides` shape and its `format_label()` keys —
      a repository connector wants summaries for the Markdown and not for the lockfiles.
- [ ] Model resolution reuses `distillation_models.py`'s rule and its reasons: no gateway
      `system_context`, no `default_params`, an id not a name. If the two grow apart the
      `ResolvedModel` type is the thing to share, not the function.
- [ ] `mode` and the model's identity are part of the chunk fingerprint from task 20 for
      `contextual` (the embedding depends on the prefix) and **not** for `summary_chunk` (the
      source chunks are unchanged; only one extra point is added or removed). `changed_formats`
      reports the difference, so switching `summary_chunk` on does not recut a corpus.

### The summary

- [ ] A new ingestion phase, `ingestion.summarizing`, between extraction and chunking. Input is
      the extracted text's head (and tail) under `max_input_tokens`, measured with the
      embedding tokenizer (task 101 if present; the process tokenizer if not). Output is stored
      on `documents.summary`, with `summary_model`, `summary_tokens_in`, `summary_tokens_out`,
      `summarized_at`.
- [ ] The prompt is fixed and versioned in code, not configurable: *"Summarize this document
      in at most N words for someone deciding whether to read it. State what it is, what it
      covers, and any names, dates or figures a search for it would use."* The version is in
      the fingerprint for `contextual`; a prompt change is a re-embed and must say so.
- [ ] **Failure does not fail the document under `summary_chunk`.** A provider refusal, a cap
      hit, a model deleted from the catalog: the document indexes without a summary, its row
      says `summary: failed (reason)`, and a **Summarize** action retries just that phase.
      Under `contextual` a failure **does** fail the document, with reason `summarization` and
      a message naming the model — half a corpus embedded with context and half without is two
      corpora that rank differently, and the pipeline rule that a degradation never turns a
      bad file into a failed document does not extend to a degradation that changes what every
      other chunk means. A retryable provider error raises for the job's backoff, as in task 20.
- [ ] The daily cap is counted from the runs table, before the call, the way distillation's
      is. A capped document under `summary_chunk` indexes without a summary and is queued for
      tomorrow; under `contextual` it waits — it is *pending*, not failed — and the connector
      says how many are waiting on the cap.
- [ ] Editing the summary in the UI re-embeds the summary chunk and, under `contextual`,
      re-embeds the document's chunks. The edit is the operator's; `summary_model` becomes
      `manual`, and the daily cap is not charged.

### In the index

- [ ] `summary_chunk` mode: one extra point per document with `kind: "summary"`, `text` the
      summary, `section: "Summary"`, embedded like any chunk. Ordinary chunks carry
      `kind: "source"`; the payload key is always present so a reader never infers a kind from
      its absence.
- [ ] The prompt renders a summary chunk as `[3] summary of: handbook.pdf` — never as
      `source:`. A citation of it (task 100) resolves with `kind: summary` and no page.
      Retrieval's `doc_max_tokens` counts it like any other chunk.
- [ ] `contextual` mode: each source chunk's `embedded_text` becomes
      `{summary}\n\n{chunk text}`; `text` is unchanged. Task 20's `windowed` property is
      generalised: a chunk whose `embedded_text` differs from `text` says *why*
      (`window` or `context`), because the chunk inspector highlights a matched sentence for
      one and shows a prefix for the other.
- [ ] Retrieval's dedupe and the `sentence_window` radius are untouched by a prefix — asserted,
      because `_radius` reads `window_sentences` and must not start reading a prefix length.
- [ ] Chunking **Compare** (task 20) shows the summary prefix on each candidate when the
      connector is in `contextual` mode, and its cost line includes the summarization call.
      A comparison that hides half the embedding cost is the thing task 20 refused to build.

### The ledger

- [ ] `summarization_runs`: one row per attempt — `organization_id`, `connector_id`,
      `document_id`, `model_id`, `model_name` (denormalised, as `distillation_runs` explains),
      `outcome` (`succeeded | failed | skipped`), `reason`, `tokens_in`, `tokens_out`,
      `duration_ms`, `created_at`. Pruned by task 17's retention like `distillation_runs`.
- [ ] The tokens are the **provider's reported usage**, not our estimate, because this is a
      bill. When a provider reports none, the estimate is stored and the row says `estimated`.
- [ ] Metrics: `summarization_runs_total{outcome}`, `summarization_tokens_total{direction,
      model}`, `summarization_duration_seconds`. The counters fire alerts; the rows draw charts.
- [ ] **Monitoring → Summarization panel**, beside memory health: documents summarized per day,
      tokens per day by model, failure rate, cap hits, and the top connectors by spend over the
      window. The same time-range picker and filters as the rest of the page. The connector
      detail screen shows its own slice of the same numbers.
- [ ] The Dashboard's degraded-state list includes "N documents waiting on the summarization
      cap" — a connector that is silently not indexing is exactly what that list is for.

### UI

- [ ] **Connectors → Summarization** panel: mode, model (with the fallback shown greyed),
      caps, per-format overrides, and a **cost line** before saving — documents × (input
      tokens + summary tokens), and for `contextual` the re-embedding on top — with the same
      "this will recut / re-embed N documents" prompt task 20 shows.
- [ ] The document table gains a summary status column and the document view shows the
      summary with **Edit** and **Regenerate**.
- [ ] The chunk inspector shows the embedded prefix above the returned text, visually distinct,
      the way it highlights the matched sentence under `sentence_window`.
- [ ] **Settings** — the summarization model default beside the distillation model default, if
      an organization wants them different.

### Spec

- [ ] Amend §9.3 with the summarization step and the two modes; §6.1 with the summary chunk
      kind; §10.1 with the summarization health signals; §7 with the `summary of:` rendering.

## Acceptance criteria

- [ ] A document ingested in `summary_chunk` mode has exactly one more point than in `off`,
      with `kind: summary`, and its source chunks are byte-identical to `off`.
- [ ] A document ingested in `contextual` mode has the same chunk `text`s as `off` and
      different `embedded_text`s, each starting with the summary.
- [ ] Switching `off → summary_chunk` reindexes nothing; `off → contextual` marks every
      document stale and says so with a cost.
- [ ] A summarization failure in `summary_chunk` mode produces an indexed document with a
      failed summary and a working retry; in `contextual` mode a `failed` document naming the
      model; a retryable provider error produces neither and the job retries.
- [ ] The daily cap holds across two workers — asserted the way task 13 asserts its cap.
- [ ] Every run has a row with the provider's token counts, and the monitoring panel's daily
      total equals the sum of those rows for the window.
- [ ] A summary chunk is rendered in the prompt as a summary, and task 100 resolves a citation
      of it as one.
- [ ] Editing a summary re-embeds what depends on it and charges no cap.
- [ ] The Compare panel's cost for a `contextual` connector includes the summarization call.

## Tests

- Config validation, overrides, and the fingerprint rule (`contextual` in, `summary_chunk` out).
- The phase against a fake model: success, refusal, retryable error, cap hit, model deleted —
  in both modes, asserting the document's terminal state each time.
- Payload shape: `kind` always present, `embedded_text` prefixed only under `contextual`,
  dedupe radius unaffected.
- Prompt rendering of a summary chunk and task 100's resolution of it.
- The ledger: one row per attempt, provider tokens preferred, estimate flagged; the panel's
  aggregation over a fixture window.
- Cap concurrency across two workers.
- Compare parity: the previewed prefix equals the ingested one.

## Notes

- **`contextual` is the one that works, and `summary_chunk` is the one that is cheap.**
  Contextual embedding is the largest single retrieval-quality lever short of reranking, and it
  roughly doubles the embedding bill and adds a model call per document. Summary chunks cost
  one point per document and answer a question the source chunks never can. Offering both, and
  `both`, is not indecision — they do different things and a corpus can want either.
- **The tokens are the point of the ledger.** `distillation_runs` counts what a pass *did*;
  it never recorded what it *cost*, and the roadmap's usage-and-cost accounting item (§16.2)
  has nothing to build on. This table records tokens from day one so that when cost accounting
  arrives, summarization is already a line on it.
- **Label the summary or do not index it.** The one way this task could damage the product is
  a summary chunk rendered as `source:` — a citation to text the document does not contain.
  `kind` is on every point, the renderer switches on it, and the test that a summary is never
  rendered as a source is the first test in the file.

# Task 103 — Validation pages: chunking, embeddings, and recall / precision

**Slice:** three screens that answer, with numbers over the real index rather than a feeling
over one document, whether a connector was chunked well, whether its embeddings are sane, and
whether a gateway's retrieval finds the right chunks — the last one against an evaluation set
the organization builds from its own traffic and keeps across configuration changes.
**Depends on:** 07, 10, 17, 20, 100
**Spec:** §6.3, §9.3, §9.4, §10.1, §13.1 — adds a **Validation** section to the connector and
gateway screens.
**Size:** L
**Status:** **done.** 100 supplies the free labels. A run already records the connectors' chunk
fingerprints and the embedding model it measured against, so 104 can read that snapshot rather
than add one.

---

## Why this slice

Task 20 ended with an admission: *"nothing here measures whether `semantic` actually retrieves
better."* Compare shows how one document is cut under four configurations. It does not show
how the *connector* is cut, whether the vectors that came back from the provider are the
vectors of that text, or whether a question a user actually asked finds the chunk that answers
it. Every one of those is a question with a numeric answer, and the product currently answers
all three with the request log and a good eye.

Three validations, three different failure modes they catch:

- **Chunking, over the whole index.** Compare is one document. A connector of 4 000 files has
  a distribution: how many documents are one chunk, how many chunks are at the ceiling, how many
  are under fifty tokens, how many begin mid-sentence, how many are near-duplicates of each
  other. Every one of those is a retrieval defect with a fix, and none is visible from a sample.
- **Embeddings.** A provider that silently truncates input at 8 191 tokens, a dimension setting
  that does not match what came back, a batch that returned zero vectors on a rate limit that
  was swallowed, a model change that half-applied — each produces an index that *ranks* and
  ranks wrong. The check is cheap: a chunk's nearest neighbour should mostly be its own document;
  vector norms should not be degenerate; a re-embedding of a sample should match what is stored.
- **Recall and precision.** The only measure of retrieval that means anything, and it needs
  labelled questions. Organizations do not have those. They do have a request log full of
  questions, task 100's record of which chunks the model cited, and a **Try retrieval** box
  they already use to tune by hand. An evaluation set is those things given a table.

## Demo at the end of this task

Open a connector, **Validation → Chunking**. A histogram of chunk sizes over all 12 000 points,
the five numbers from Compare computed over the whole index, and a findings list: *"312 chunks
under 40 tokens, 280 of them from `CHANGELOG.md` files"*, *"41 documents are a single chunk"*,
*"17 near-duplicate pairs across documents"*. Click a finding; it opens the documents.

**Validation → Embeddings**: dimension matches the platform setting; a sample of 200 chunks
re-embedded just now agrees with the stored vectors within tolerance; 94% of chunks have their
own document as nearest neighbour; norm distribution is unimodal. One document is flagged: its
vectors are all the same, because the provider truncated a 60 000-token file's chunks that the
splitter never should have produced that large — which the chunking page also flagged.

Open a gateway, **Validation → Retrieval**. Build an evaluation set: import fifty questions
from last week's log, each pre-labelled with the chunks the model cited; correct three labels
by hand; add two questions with no relevant chunk at all. **Run** — recall@6 0.82, precision@6
0.41, MRR 0.77, and per question the chunks retrieved with the relevant ones marked. Change
`doc_min_score` in the unsaved form and run again: the numbers move and both runs stay in the
history. Switch the connector to `semantic`, reindex, run again — the third row is the answer to
task 20's open question.

## In scope

- A connector-wide chunking report over the index, with findings.
- An embedding sanity report: dimension, degenerate vectors, drift against a fresh sample,
  intra-document nearest-neighbour agreement.
- Evaluation sets per gateway: questions with relevant chunks and/or documents, built from the
  request log and citations, edited by hand, optionally bootstrapped by a model.
- Evaluation runs through the **real** retrieval path with saved or unsaved gateway
  configuration, scored, stored, and compared.

## Out of scope

- **Answer-quality evaluation** (faithfulness, LLM-as-judge on the completion). Retrieval is
  the layer this product owns; whether the model used the right chunk well is a different
  measurement with a different cost.
- **Automatic tuning** — sweeping `doc_min_score` and picking the best. The page makes a sweep
  a few clicks; choosing is a person's job, because precision and recall trade and the right
  trade depends on what the gateway is for.
- **Reranking or hybrid search.** Both would improve the numbers this task measures; both are
  roadmap items (§16.4) that need this task to exist first so their improvement can be shown.
- **Cross-organization benchmarks.** An evaluation set is organization data.

## Work items

### Chunking validation (per connector)

- [x] `ChunkAudit.run(connector_id)` scrolls the live collection once (the vector port's
      `scroll`, as the reindexer does), reads payloads only, and computes: the token-count
      histogram (bucketed to the connector's `chunk_size`), the five numbers from
      `chunking_preview.distribution` over every point, and findings. A background job, not a
      request — 100 000 points is a minute — with the report stored per connector and its
      age shown.
- [x] Findings, each with a count and the documents behind it: chunks under a floor (default
      40 tokens); documents that are one chunk; chunks at the ceiling; mid-sentence starts on
      prose formats; near-duplicate chunks across documents (payload text hash first, then
      cosine above 0.98 on a bounded sample — the exact-duplicate case is the common one and
      is free); documents whose chunk count is an outlier for their size; a mix of
      `chunk_fingerprint`s (the connector is half reindexed — task 104 owns the fix, this
      report names it).
- [x] Each finding links to Compare with the offending document preselected, because the page
      that finds a badly cut file should open the page that recuts it.
- [x] The report is **per format** as well as overall, keyed by task 20's `format_label`,
      because a repository connector's Markdown and its lockfiles have different healthy
      shapes and averaging them hides both.

### Embedding validation (per connector)

- [x] `EmbeddingAudit.run(connector_id)`: dimension of stored vectors against the platform
      setting; the fraction of near-zero and near-identical vectors (a provider that returned
      padding); the norm distribution; and **intra-document agreement** — for a sample of N
      chunks, is the nearest neighbour (excluding itself) from the same document? Reported as
      a percentage with the documents that score worst.
- [x] **Drift against a fresh sample**: re-embed a bounded sample (default 100 chunks, capped
      by tokens) with the current platform model and compare to the stored vectors. Cosine
      near 1.0 is healthy; a bimodal result means two models are in the collection; a uniform
      offset means the provider changed something underneath the same model id — the failure
      nobody else would ever detect. This is the one part of the page that **spends**, and the
      button says how much before it runs.
- [x] `embedding_model` recorded on the collection (SPEC §9.4) is compared with what every
      document row says it was embedded with; disagreement is a finding, not a fix.
- [x] Findings link to the reindex action for the connector, or to the platform reindex when
      the model itself is the problem.

### Evaluation sets (per gateway)

- [x] Tables: `evaluation_sets(id, organization_id, gateway_id, name, created_by, ...)` and
      `evaluation_items(id, set_id, question, relevant_chunk_ids, relevant_document_ids,
      source, notes)`. `source` is `log | citation | manual | generated`, because a number
      computed over labels a model wrote is a different number from one over labels a person
      checked, and the page must say which.
- [x] Labels can be **chunk-level or document-level**. A person can usually say *which
      document* answers a question and rarely which chunk; recall is computed at whichever
      granularity the item has, and the report shows both columns.
- [x] **Import from the log**: pick a window and a filter, get one item per distinct user
      question (task 100's `cited_chunk_ids` pre-fills the labels where present), deduplicated
      by normalised text. Items arrive with `source: citation` or `log` and are *unverified*
      until someone confirms them; unverified items are included in a run and reported
      separately.
- [x] **Generate** (optional, spends): for a sample of chunks, ask a model to write the
      question that chunk answers; the chunk is the label. Uses the summarization/distillation
      model resolution and records its usage in task 102's ledger with `purpose: evaluation`.
      Synthetic sets over-estimate recall — the question was written *from* the chunk — and
      the report says so on every run that includes generated items.
- [x] Negative items: a question with **no** relevant chunk, so that precision has something to
      be wrong about and `doc_min_score` has something to defend.
- [x] Items survive reindexing. A `relevant_chunk_id` is invalidated by a recut (task 104
      fingerprints tell you when); the item keeps its `relevant_document_ids` and its
      **relevant text** — the chunk's text at labelling time — so it can be re-anchored to
      the new chunk that contains that text. An evaluation set that dies on the first reindex
      cannot measure the effect of a reindex, which is the thing it is for.

### Evaluation runs

- [x] `EvaluationRun.run(set_id, config_patch=None)` calls the same `MemoryService` the data
      plane and **Try retrieval** call, with the gateway's saved configuration or the unsaved
      patch merged through `merge_config` — the `memory_preview` rule, restated: a second
      retrieval implementation that merely agrees today is how a tuning loop becomes a liar.
- [x] Per item: chunks retrieved with scores, which were relevant, rank of the first relevant.
      Per run: recall@k, precision@k, MRR, hit rate at document level, and the same numbers
      restricted to verified items. `k` is the gateway's `doc_top_k` and the scoring is also
      reported *after* `doc_min_score` and `doc_max_tokens` — what would actually be injected —
      because a relevant chunk retrieved at rank 5 and dropped by the budget is not a recall.
- [x] Runs are stored with the effective gateway configuration, the connector chunk
      fingerprints, and the embedding model at the time, so two runs can be diffed and the
      diff says *what changed between them*. This is the pairing with task 104: a run is a
      measurement of a known state.
- [x] A run costs embedding calls (one per question); it is a background job with progress
      and a cap on items per run. A set of 5 000 questions is a batch, not a click.

### UI

- [x] **Connectors → Validation** with two tabs, Chunking and Embeddings: the report, its age,
      a **Run** button, and findings as a list that opens the documents. The histogram is the
      one task 20 left out of Compare; build it once here and let Compare use it.
- [x] **Gateways → Validation**: evaluation sets (list, create, import, generate, edit items
      inline with a chunk picker that reuses Try retrieval), runs (table with the headline
      numbers, a diff view between any two), and the per-item drill-down with retrieved chunks
      and the relevant ones marked.
- [x] Try retrieval gains **Add to evaluation set** — the moment a person tuning a gateway sees
      the right chunk come back is the moment the label is cheapest.
- [x] The Dashboard's degraded list includes a connector whose last chunking or embedding audit
      raised a red finding.

### Spec

- [x] Add §6.6 *Retrieval evaluation* (sets, runs, what the numbers mean and what they cannot
      mean); amend §13.1 with the three screens; amend §10.1 with the audit findings as a
      health signal.

## Acceptance criteria

- [x] The chunking report over a fixture connector matches `distribution()` computed over the
      same points, and each finding's count matches a hand count on the fixture.
- [x] The embedding audit flags a collection where 10% of vectors are identical, one where the
      stored dimension differs from the setting, and one where a sample re-embeds to cosine
      0.6 against stored — and passes a healthy one.
- [x] Intra-document agreement on a fixture where every document is about a different subject
      is above 0.9; on a fixture where all documents are near-identical it is reported low
      **and** the report says why that can be fine.
- [x] An evaluation run's retrieved chunks for each question are identical to Try retrieval's
      for the same question and configuration — asserted, not assumed.
- [x] recall@k, precision@k and MRR on a hand-computed fixture match to the decimal, at both
      chunk and document granularity, before and after the budget.
- [x] Two runs across a reindex of the connector: the second re-anchors chunk labels by text,
      reports how many it could not, and the diff names the fingerprint change.
- [x] An item imported from the log with task 100 citations arrives pre-labelled and
      unverified; verifying it moves it between the two reported columns.
- [x] Generated items are marked, and a run including them says so in its headline.
- [x] Audits and runs write nothing to the index.

## Tests

- Audit arithmetic over hand-built payload fixtures; findings each with a planted case.
- Embedding audit against planted degeneracies and against a healthy fixture.
- Retrieval parity with `memory_preview` — the same test shape as task 20's preview parity.
- Scoring functions over a table of `(retrieved, relevant, k)` with expected numbers written
  by hand, including empty relevant sets and negative items.
- Re-anchoring by text across a recut, including the case where the text was split.
- Import deduplication and the citation pre-fill.
- The generated-item warning and the ledger row.
- Cross-tenant: sets and runs are scoped; a chunk id from another organization in a label is
  a 404, not a hit.

## Implementation notes

- **Where things landed.** `app/services/index_audit.py` is the audit arithmetic (both
  reports, every finding, the histogram, the JSON shape); `app/services/index_auditor.py`
  the runner — the scroll, the neighbour searches, the drift sample, the row — and the
  control-plane reads; `app/services/index_audit_store.py` its store.
  `app/services/evaluation.py` is the scoring arithmetic, dedupe key and re-anchoring;
  `app/services/evaluation_runner.py` the job; `app/services/evaluation_service.py` the
  sets, items, import, generation, runs and diff; `app/services/evaluation_store.py` the
  store. Routes are `app/api/control/validation.py`, bodies `app/schemas/validation.py`,
  migration `0022_retrieval_validation`. The web halves are `ConnectorValidation.tsx`,
  `GatewayEvaluation.tsx`, `components/Histogram.tsx` and `pages/validation.ts`.
- **Retrieval parity is shared code, not a promise.** `memory_preview.resolve_memory_config`
  is the one function that merges a patch and drops connectors the gateway may not read;
  Try retrieval and the run both call it, both call the same `MemoryService`, and both apply
  the budget with `fit_documents` under the primary target's tokenizer. The parity test
  compares the two through their real entry points, patch included.
- **Nearest neighbours are asked of the index, not computed over a sample.** A brute-force
  pass over the scanned vectors would have answered about the sample; a chunk whose true
  neighbour was outside it would have looked agreed with. One search per sampled chunk,
  against the connector, over-fetching by two to skip itself and a summary point, is the
  ranking a request gets — and it needs no numpy. The scan itself is bounded: vectors for
  the first 5 000 points, payloads for the rest, so a 100 000-point audit never pulls
  600 MB of floats down to count them.
- **Cosine near-duplicates live in the embedding report.** The work item put text-hash
  duplicates first and cosine second; the chunking audit reads payloads only, so it finds
  the exact twins (the common case, free) and the embedding audit — which has the vectors
  and the neighbour answers — reports the near-copies at 0.98 and above.
- **Precision is over what was retrieved, not over `k`.** `doc_min_score` returning three of
  six is the knob working, and dividing by six would punish it. A negative item scores
  precision only: 1.0 when nothing came back, 0.0 when something did. Items labelled at
  document level only have no chunk-level number — `None`, never a zero in disguise — and
  the columns say how many items each is over.
- **Re-anchoring rewrites the item, and an id that survives is not proof.** Point ids are
  deterministic over `(document, index)`, so after a recut chunk zero of the new cutting
  carries the id chunk zero of the old one had — with different text under it. A label is
  therefore *anchored* only when its id exists **and** the text it was made from is still in
  that chunk; otherwise it is found again by its text and re-pointed at the new id(s) — both
  halves, when the text was split — so the next run starts current. A label found nowhere
  stays on the item, scored as a miss and counted as unanchored, because the passage may
  return with the next upload and the label is what somebody said.
- **Generation spends through task 102's chain and lands in task 102's ledger** with a new
  `summarization_runs.purpose` column (`summary` | `evaluation`). The panel's document
  counts and the daily cap read `summary` rows only; the bill is one table.
- **Import reads the transcript, not the log row.** The question is the last user turn of the
  stored request body, so a gateway whose logging keeps no bodies has nothing to import and
  the result says so as *skipped* rather than importing blanks. Citations become labels with
  the chunk's current text fetched from the index; deduplication is by normalised text,
  against the window and against what the set already holds.
- **`connector.audit` is an audited action** even though an audit reads: the drift check
  spends at the provider, and the tour drives it. The nine evaluation routes are audited as
  configuration a person curates; an item's question is marked sensitive in the trail because
  it is usually an end user's message.
- **The dashboard reads a dedicated `GET /validation/alerts`** — the newest audit per
  connector and kind with a red worst finding — rather than a field on every connector row,
  which would have made the connector list depend on the audit store.
- **The histogram is one component.** `Histogram.tsx` draws the report's buckets on the
  connector and, through `chunkHistogram`, the same buckets client-side per candidate in
  Compare; the bucketing rule is mirrored rather than requested so both draw on one axis.
  `?compare=<document>` opens Compare with the document preselected, which is how a finding's
  document link works.

## Notes

- **This is the task that turns task 20 from a choice into a decision.** Compare shows what
  a strategy does; this shows whether it helped. Without it, every retrieval improvement in
  this product — chunking, summarization, tokenizers, and the reranking still on the roadmap —
  ships on faith.
- **Labels from citations are free and biased; labels from people are expensive and few;
  labels from a model are cheap and circular.** The set keeps all three apart and reports them
  apart. A single blended number would be more comfortable and would mean nothing.
- **The audits read; the drift check and the evaluation run spend.** Every button that costs
  money says so before it is pressed, as Compare does. Nothing here runs on a schedule by
  default; an organization that wants a nightly evaluation run can have it once the cost is
  known.

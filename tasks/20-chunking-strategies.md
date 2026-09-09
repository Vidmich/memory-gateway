# Task 20 — Chunking strategies: semantic, sentence-window & code-aware

**Slice:** a connector's chunking strategy is a real choice — with strategies worth choosing
between, and a way to see the difference before committing an index to one.
**Depends on:** 09, 10, 11, 17
**Spec:** §9.3, §9.5, §13 (connector UI) — and amends §9.3, whose strategy list is
`recursive | fixed | by_heading`.
**Size:** L
**Status:** post-v1. Independent of task 19; the two touch nothing in common.

---

## Why this slice

The choice already exists in the shape of the code — `ChunkingConfig.strategy`, per connector,
with `requires_reindex` guarding a change — and there are three strategies behind it. But all
three cut on the *same* signal: a token budget, adjusted for where punctuation happens to be.
`fixed` ignores boundaries, `recursive` walks back to the nearest one, `by_heading` uses the
ones the author wrote down. None of them looks at what the text is about.

That matters because chunking is the one knob in this product where a wrong value is invisible.
Retrieval still returns something; it is just worse. A 1000-token window that straddles the end
of one topic and the start of the next embeds as the average of two things and is the nearest
neighbour of neither. The strategies below cut on meaning (`semantic`), decouple what is
embedded from what is returned (`sentence_window`), or use structure a regex cannot see
(`code`).

And there is a second half without which the first is a dropdown. Nobody can pick a chunking
strategy from a description — the right answer depends on the corpus, and the only honest way
to choose is to run the candidates over real documents and look. That comparison is the
demoable part of this task.

## Demo at the end of this task

Open a connector holding a mixed corpus — a handbook PDF, a repository of source files, a
folder of meeting notes. In **Chunking**, hit **Compare**: the same document is cut by
`recursive`, `semantic` and `sentence_window` side by side, with the boundaries drawn on the
text, a token-count histogram per strategy, and the chunk each one would return for a question
you type. The topic change halfway down page 4 is a boundary under `semantic` and is mid-chunk
under `recursive`, and you can see it.

Set the code files to `code` and the handbook to `semantic` in the same connector, accept the
reindex prompt, and watch ingestion recut. Ask the gateway a question that spans a function
body: the citation names the function, not lines 40–80 of a file.

## In scope

- Three new strategies: `semantic`, `sentence_window`, `code`.
- Per-format overrides within one connector.
- A comparison and preview tool that makes the choice reviewable before it is committed.
- The plumbing consequences: reindex triggers, ingestion cost, a chunking step that can now
  fail, drift detection, and what a citation means under a strategy that returns more than it
  embedded.

## Out of scope

- **LLM-based / propositional chunking.** An LLM call per document, non-deterministic between
  runs, and — the disqualifying part — its output is rewritten text rather than the source. A
  citation would point at a sentence the document never contained, which breaks the one promise
  retrieval makes.
- **Late chunking.** It needs a long-context embedding model with token-level pooling exposed;
  SPEC §9.4 makes the embedding model a single platform setting with a provider API behind it,
  and that interface cannot express it. Revisit if the embedding layer ever grows a local model.
- **Parent-document / hierarchical retrieval.** The same small-to-big idea as
  `sentence_window` with a different unit. Ship one, measure it, and add the other only if
  window sizing turns out to be the limitation.
- **Layout-aware PDF chunking (columns, tables, figures).** That is an extraction problem, not
  a chunking one, and belongs with the task that owns the PDF pipeline.
- **Automatic strategy selection.** No heuristic picks by file type or corpus statistics. The
  comparison tool exists so a human picks with evidence.

## Work items

### Keeping the splitter testable

- [ ] **`chunk_document` stays a pure function.** Semantic chunking needs embeddings, which
      means network I/O in the middle of what is currently `(text, config) -> chunks`. Do not
      make the splitter async and hand it a client. Compute the boundary signal *outside* and
      pass it in — a `BoundarySignal` carrying per-gap distances alongside the sentence spans
      they belong to — so every strategy remains exhaustively testable with hand-written
      numbers and no fake embedder.
- [ ] The signal is optional and absent for every strategy that does not need it. A strategy
      that asks for one and does not get it is a programming error, not a silent fallback to
      `recursive`: falling back quietly would mean a connector configured for `semantic`
      indexing as something else, with nothing on any screen saying so.
- [ ] Both existing invariants extend to the new strategies unchanged and are asserted for each:
      **progress** (every chunk starts strictly after the previous one — the property that keeps
      a pathological configuration from holding a worker forever) and **never mid-word** under
      `respect_boundaries`.

### `semantic`

- [ ] Split into sentences, embed each, and cut where consecutive-sentence distance exceeds a
      percentile breakpoint over the document's own distribution. A percentile rather than an
      absolute threshold, because the distance scale is a property of the embedding model and
      an absolute number would need retuning every time the platform model changes.
- [ ] `chunk_size` becomes a **ceiling, not a target**: a semantic chunk runs until the next
      real boundary or until the ceiling, whichever comes first, and an oversized span is
      sub-split recursively. `overlap` keeps its meaning.
- [ ] A floor on chunk size, so a document of short declarative sentences does not become one
      chunk per sentence — which is `sentence_window` without the window, and worse than either.
- [ ] Reuse the existing sentence separator from `chunking.py` rather than adding an NLP
      dependency. It is already the boundary `recursive` snaps to, and two different notions of
      "sentence" in one module is a bug waiting to be written.

### `sentence_window`

- [ ] Embed a sentence; store the sentence **plus N neighbours** as the chunk text. Small unit
      to match on, enough context to answer with.
- [ ] **This changes the retrieval contract, not just ingestion.** `payload["text"]` is what
      goes into the prompt; the embedded text is what matched. Both must be in the payload, and
      `doc_max_tokens` accounting must budget the window rather than the sentence — otherwise
      the injected context silently exceeds the cap that exists to protect the upstream request.
- [ ] The chunk inspector shows the matched sentence highlighted inside its window. Without
      that, the first debugging session under this strategy is "why does this chunk not contain
      the words I searched for", and the answer is not discoverable from the screen.
- [ ] Deduplicate overlapping windows at assembly time. Neighbouring sentences both matching
      means the same paragraph injected twice, which spends the token budget on a copy.

### `code`

- [ ] Structural splitting for source files: function and class bodies as units, with the
      enclosing declaration line carried into each chunk so a fragment retains its signature.
- [ ] Fall back to `recursive` for a file that fails to parse — a syntax error or an unsupported
      language must degrade to a worse chunking, never to a failed document.
- [ ] Reuse `filetypes.py`'s classification rather than a second extension list. Two lists is
      how a file gets classified as code by one and as text by the other.
- [ ] Scope the languages honestly and name them in the UI. "Code-aware for Python, JavaScript,
      TypeScript and Go; recursive elsewhere" is a claim somebody can check; "code-aware" is not.

### Per-format overrides

- [ ] `ChunkingConfig` grows an `overrides` mapping from format kind to a partial configuration.
      A connector is a *source*, not a format — a repository holds code and Markdown, a shared
      drive holds PDFs and spreadsheets — and one strategy for all of them is the wrong answer
      by construction.
- [ ] Resolution is explicit and shown: the effective configuration for a document is the
      connector's, overridden per format, and the connector screen displays what each format
      actually resolves to rather than leaving it to be inferred.
- [ ] `requires_reindex` compares **effective** configurations per format, so adding an override
      for code reindexes the code files and leaves the PDFs alone. That is the first time this
      function's set-of-triggers design earns its keep — see its docstring, which predicted it.

### Consequences in the pipeline

- [ ] **`chunking` can now fail.** It is a pure CPU step today; under `semantic` it makes
      embedding calls. Give it its own failure reason and retry path in SPEC §9.5's status
      model, and make sure a provider outage marks the document `failed` with a message naming
      the embedding provider — not a generic chunking error that sends somebody to read the
      splitter.
- [ ] Budget it. Semantic chunking embeds every sentence, several times the count of the chunks
      it produces. Batch through the existing `Embedder` batching, cap the sentences per
      document, and show an estimated cost before a connector is switched to it — the same
      before-you-commit rule task 17 applies to a reindex.
- [ ] Record `chunk_strategy` and a configuration fingerprint in the chunk payload and on the
      document row. SPEC §9.4 already does this for the embedding model so drift is detectable;
      the argument is identical, and without it a connector reindexed halfway has two
      chunkings in one collection and nothing says which chunk is which.
- [ ] Chunking-phase duration and a per-strategy chunk-size distribution as metrics, and a
      `chunking` span with the strategy as an attribute — the ingestion pipeline's slowest step
      is about to become network-bound for some connectors and should be visible when it does.

### The interaction with task 17 — the expensive one

- [ ] **Under `semantic`, the embedding model is part of the chunking configuration.** Task 17's
      platform reindex re-embeds *stored chunk text* into a new collection. For a connector on
      `semantic` that is wrong: the boundaries themselves came from the old model, so re-embedding
      them faithfully reproduces the old model's opinion about where topics change.
- [ ] So the reindexer needs a **recut** path: for connectors whose strategy depends on the
      embedding model, re-extract from object storage and re-chunk rather than re-embed. It is
      strictly more expensive, and the estimate shown before a reindex must say how many
      connectors fall into it.
- [ ] `requires_reindex` must therefore take the embedding model into account for those
      strategies, which makes it genuinely a function rather than "did any field change".
- [ ] Say all of this in the UI at the moment of choosing: picking `semantic` makes future
      embedding-model changes more expensive for this connector. That is a real trade-off and
      the person making it should be told, once, where they are making it.

### Compare & preview

- [ ] `POST /api/v1/connectors/{id}/chunking/preview` — run a set of candidate configurations
      over one document and return the chunks each produces, without writing anything.
      Read-only, rate-limited, and capped in document size: it is an embedding-spending endpoint.
- [ ] Optional query: for a question, return the chunk each candidate would surface, reusing the
      retrieval preview from SPEC §13 rather than a second scoring path that can disagree with
      what a real request does.
- [ ] Report per candidate: chunk count, token distribution (min/median/p95/max), how many
      chunks hit the size ceiling, and how many boundaries fell mid-sentence. Four numbers that
      make two strategies actually comparable, instead of a wall of text.
- [ ] **Chunking → Compare** in the connector UI: a document picker, candidate columns,
      boundaries drawn on the text, the histogram, and the query box. Applying a candidate is
      the existing patch flow with the existing reindex prompt.

### Spec

- [ ] Amend SPEC §9.3 with the new strategies and the per-format override shape, and §9.5 with
      the chunking step's new failure mode. A strategy list in the spec that omits half the
      strategies is worse than no list.

## Acceptance criteria

- [ ] Each strategy's defining property holds on a fixture corpus: `semantic` cuts at a planted
      topic change that `recursive` cuts through; `sentence_window` embeds a sentence and returns
      a window containing it; `code` produces chunks whose boundaries are declarations.
- [ ] Every strategy satisfies progress and never-mid-word, asserted by the same shared suite
      rather than per strategy.
- [ ] `chunk_document` is still synchronous and pure, and every strategy is tested without a
      network client or an embedder double.
- [ ] A connector with per-format overrides indexes each format under its own effective
      configuration, and changing one override reindexes only that format's documents.
- [ ] A failure from the embedding provider during `semantic` chunking marks the document
      `failed` with a message naming the provider, and a retry succeeds once it recovers.
- [ ] Switching a connector to `semantic` shows a cost estimate before anything is spent, and
      shows the reindex-cost consequence for future embedding-model changes.
- [ ] A platform embedding-model change **recuts** connectors on `semantic` and re-embeds the
      rest, with the estimate distinguishing the two.
- [ ] `sentence_window` retrieval respects `doc_max_tokens` measured on the window, and
      overlapping windows are injected once.
- [ ] The comparison endpoint runs candidates over one document, writes nothing, and its numbers
      match what ingestion actually produces under the same configuration — verified by
      ingesting one candidate and diffing.
- [ ] The four comparison numbers are recorded for the fixture corpus and checked in, so a later
      change to a strategy shows up as a diff rather than as a feeling.

## Tests

- The shared invariant suite, over every strategy and a pathological corpus: one long line, no
  punctuation, a single 50 000-token paragraph, empty sections, a file that is one sentence.
- `semantic` against a hand-written boundary signal — planted distance spikes, a flat
  distribution (no cut), and a document where every gap spikes (the floor holds).
- `sentence_window` payload shape, window assembly, dedup, and token accounting.
- `code` against parse failures, unsupported languages, and a minified file.
- Override resolution and per-format `requires_reindex`, including that an unrelated override
  does not trigger one.
- The recut path: a semantic connector and a recursive one through the same platform
  embedding-model change, asserting one is recut and the other re-embedded.
- Preview parity: preview output equals ingestion output for the same configuration and document.
- Cost estimation and the sentence cap.

## Notes

- **The purity of `chunk_document` is the thing to protect.** It is why the existing chunking
  tests can be exhaustive, and the pressure to make it async and hand it an embedder will be
  constant while building `semantic`. Computing the signal outside costs one extra type and
  keeps the whole strategy suite runnable with numbers typed by hand.
- **The comparison tool is not a nice-to-have.** Without it this task ships three more words in
  a dropdown, and every user picks by name — which in practice means picking `semantic` because
  it sounds better, paying for it at every ingestion, and never finding out whether it helped.
- **`fixed` stays.** It is the escape hatch its docstring says it is, for content whose
  structure is meaningless. Adding cleverer strategies is not a reason to remove the one that
  refuses to be clever.

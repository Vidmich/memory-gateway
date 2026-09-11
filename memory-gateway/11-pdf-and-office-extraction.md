# Task 11 — PDF & Office extraction

**Slice:** the documents customers actually have — PDFs, Word, PowerPoint, Excel — become
retrievable memory.
**Depends on:** 09 (10 recommended, so quality is judged on real answers)
**Spec:** §9.2, §9.3
**Size:** M

---

## Why this slice

Real corpora are PDFs and Office files, not tidy Markdown. It is a separate task because these
formats bring heavy dependencies, slow parsing, and quality problems that would otherwise
contaminate the ingestion pipeline's first release.

## Demo at the end of this task

Upload a 200-page product manual PDF, a `.docx` policy document, a `.pptx` deck, and an `.xlsx`
price list. All reach `indexed` with sensible chunk counts. Ask a question whose answer is on
page 147 of the PDF — the answer is correct and cites `manual.pdf (p. 147)`.

Upload a scanned, image-only PDF: it lands as `skipped` with reason `needs_ocr` and an
explanation in the UI, rather than silently indexing zero chunks.

## In scope

- Extractors for `.pdf`, `.docx`, `.pptx`, `.xlsx` registered into the task 09 registry.
- Structural metadata (page, heading, slide, sheet) carried through to chunks and citations.
- Resource limits and worker isolation for heavy parsing.

## Out of scope

- OCR (flagged `needs_ocr` and skipped).
- Layout-aware table reconstruction beyond simple row rendering.
- `.doc`, `.ppt`, `.xls` (pre-2007 binary formats).
- Image extraction and multimodal indexing.

## Work items

### PDF
- [x] Text-layer extraction with `pymupdf` (fast, good text ordering) — or `pdfplumber` where
      table fidelity matters more than speed. Pick one and justify it in a code comment.
- [x] Preserve **page numbers** into `page_or_section`; citations that name a page are far more
      useful to an end user than ones that name only a file.
- [x] Detect image-only pages: if extracted text across the document falls below a threshold
      (e.g. < 100 characters per page on average), mark the document `skipped` with
      `needs_ocr` rather than indexing near-empty chunks.
- [x] Handle encrypted PDFs: attempt an empty-password open, otherwise fail with
      `password_protected`.
- [x] Strip repeated headers and footers — detect lines recurring at the same position on most
      pages. Without this, every chunk carries the same boilerplate and retrieval scores blur.
- [x] Merge hyphenated line breaks and collapse column artifacts into readable flow.
- [x] Extract embedded outline/bookmarks as section headings where present, feeding `by_heading`
      chunking.

### Word (.docx)
- [x] Paragraph extraction preserving heading levels from paragraph styles, populating
      `page_or_section` with the enclosing heading path (`"Security > Access Control"`).
- [x] Tables rendered as readable records, matching the CSV convention from task 09.
- [x] Lists preserved as list markers rather than flattened into run-on text.
- [x] Ignore headers, footers, and footnote apparatus; include footnote text appended to its
      paragraph.
- [x] Tracked changes: extract the accepted/current text, not the deleted revisions.

### PowerPoint (.pptx)
- [x] Per-slide extraction: title, body placeholders, text boxes, and table content.
- [x] `page_or_section` = `"Slide N: <title>"`.
- [x] **Speaker notes included**, marked as notes — they frequently contain the substance the
      slide only gestures at.
- [x] One chunk per slide by default (slides are naturally chunk-sized); sub-split only if a slide
      exceeds `chunk_size`.

### Excel (.xlsx)
- [x] Per-sheet extraction; `page_or_section` = the sheet name.
- [x] Header-row detection, then rows rendered as `column: value` records (the task 09 CSV
      convention) rather than raw cells.
- [x] Formula cells use cached computed values; where absent, record the formula text.
- [x] Row cap per sheet (default 50 000) with a truncation marker — a spreadsheet is a database
      export, and indexing all of it is rarely what the user meant.
- [x] Skip empty sheets and fully empty columns.

### Pipeline hardening
- [x] Register all extractors in the task 09 `ExtractorRegistry`; no pipeline changes required.
- [x] Run heavy extraction in a **separate process** (process pool) with a hard wall-clock and
      memory cap, so a malformed file kills a subprocess rather than a worker.
- [x] Optional dedicated worker queue for heavy formats, so a 300-page PDF cannot delay a
      Markdown file behind it.
- [x] Extraction-duration and per-format failure-rate metrics.
- [x] Remove the "coming soon" skip reason from task 09 for these extensions.

### UI
- [x] Document table shows page/slide/sheet counts alongside chunk counts.
- [x] `needs_ocr` and `password_protected` render as first-class explained states with guidance,
      not as generic failures.
- [x] Chunk inspector: for a selected document, list its chunks with their `page_or_section` —
      the fastest way to see whether extraction produced sensible text.

## Acceptance criteria

- [x] Each of the four formats ingests correctly from a fixture set, with page/section metadata
      present on every chunk.
- [x] A citation from a PDF names the correct page number, verified against the source.
- [x] An image-only PDF is `skipped: needs_ocr`, never indexed as empty chunks.
- [x] A malformed file of each format fails cleanly with a readable error and does not kill the
      worker.
- [x] A 200-page PDF extracts within the time cap and within a bounded memory ceiling.
- [x] Repeated headers/footers do not appear in every chunk (assert against a fixture that has
      them).
- [x] Retrieval quality on the fixture corpus is not degraded by boilerplate — spot-checked with
      a small labelled question set.

## Tests

- Fixture files per format: a clean one, a malformed one, an empty one, a very large one, and a
  password-protected PDF.
- Header/footer stripping against a document with known repeated lines.
- Hyphenation and column merging on a two-column PDF.
- Heading path construction for nested `.docx` headings.
- Speaker-notes inclusion; slide-title metadata.
- Excel header detection, formula caching, and row-cap truncation.
- Subprocess isolation: a deliberately crashing extraction does not take down the worker.

## Notes

- PDF extraction quality is where RAG quality quietly dies. Header/footer stripping and
  hyphenation merging are not polish — without them, every chunk shares boilerplate and
  similarity scores compress toward each other.
- Keep a small labelled question set against the fixture corpus in the repo. It is the only way to
  notice that a dependency upgrade degraded extraction.


---

## Verification status

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   228 files already formatted
uv run mypy                    Success: no issues found in 216 source files
uv run pytest -q               2172 passed, 315 skipped

npx eslint . / npx tsc         clean
npx vitest run                 387 passed (17 files)
npm run build                  396.26 kB JS (118.31 kB gzipped)
```

OpenAPI regenerated; `web/openapi.json` and `web/src/api/schema.d.ts` are in step with the
server.

### The demo, verified

Walked over a real socket — the app on a real port under uvicorn, a real HTTP client, a
scriptable provider on a second socket, control plane and data plane in one process —
**38/38 checks**:

* the four formats upload through the real multipart endpoint and reach `indexed`, with
  page, slide and sheet counts on the rows and `None` for Word, which has no page count
  that is not a rendering decision;
* a question answerable only from page 147 of the manual retrieves that page, and the
  chunk inspector shows it labelled `Warranty > Coverage (p. 147)` — checked against the
  fixture, which writes that sentence on that page;
* the assembled prompt reaching the provider carries
  `source: manual.pdf (Warranty > Coverage (p. 147))`, and the same question with
  `X-Gateway-Memory: off` carries none of it;
* the running header appears in no chunk and in no prompt, and the hyphenated word is
  rejoined;
* a scanned PDF lands `skipped` with `reason: needs_ocr` and a sentence about text
  layers, with nothing indexed for it;
* a password-protected PDF lands `failed` with `reason: password_protected` and a message
  that does not ask for the password;
* the deck's **speaker notes** are searchable, not merely extracted.

### What is different from the plan, and why

* **The PDF reader is `pypdfium2`, which is neither of the two the plan named.** The plan
  offered `pymupdf` for speed or `pdfplumber` for table fidelity; tables beyond simple row
  rendering are out of scope here, which leaves speed — and leaves a licence question the
  plan does not raise. PyMuPDF is AGPL-3.0, whose network clause is a live question for a
  hosted multi-tenant service and not one an extractor should settle on its own.
  pdfplumber answers that (MIT) but is pdfminer underneath: measured here on a 200-page,
  8400-line document it takes **25 seconds**, nineteen of them before any layout work
  starts. `pypdfium2` is a thin binding over PDFium — the engine in Chrome's PDF viewer,
  BSD-3-Clause — and reads the same document in **0.9 seconds**, with text rectangles and
  their coordinates, the outline, and the encryption state. The trade the plan framed as
  speed-versus-tables was really speed-versus-licence, and there was an option that gives
  up neither. It is also one module behind the registry, so swapping it back is a swap.
* **A chunk never crosses a page break.** `Extracted` gained `atomic_sections`, and the
  PDF and PowerPoint extractors set it. For slides the reason is the obvious one — a slide
  is a unit somebody authored, and gluing two together makes a chunk about two subjects.
  For pages it is the acceptance criterion: run together and split on a token budget, a
  chunk drawn from pages 144 to 147 gets labelled with the page it happened to start on,
  and a reader who turns to that page and does not find the sentence stops believing every
  citation after it. Being coarse is survivable; being *nearly* right is not. The cost is
  real and named in the code — a page holds less than a full token window, so a manual
  produces more and slightly smaller chunks than it would otherwise.
* **The outline feeds the label, not the grouping.** Bookmarks become the heading path in
  front of the page number (`Warranty > Coverage (p. 147)`) rather than merging pages into
  chapter-sized sections, because a citation that says "somewhere in this 40-page chapter"
  is not a citation.
* **`documents.reason` is a new column** (migration `0011_document_extraction`), and
  deliberately **not** CHECK-constrained. A scanned PDF and a locked file are the two
  things a customer uploads that cannot be indexed *and* can be fixed by the customer, and
  the UI has to branch on that to render them as explained states rather than red rows.
  Branching on `documents.error` would mean matching on a sentence written for a person —
  which gets rewritten as the wording improves, silently breaking the UI. Reasons are open
  by design so a new extractor can explain a new failure without a migration, and a code
  the browser does not recognise falls back to showing the sentence, which is what every
  row showed before.
* **`documents.page_count` is one column, not two.** Pages, slides and sheets are the same
  fact in three formats, and the *noun* is derived in the UI from the media type. Storing
  the unit as well would be a second column that can disagree with the first, and the one
  that would be wrong is the one nobody looks at.
* **PDFium's hyphen marker is trusted over the heuristic.** The engine emits `\x02` for a
  hyphen it believes was inserted to break a word, having seen the glyph positions and the
  font. That judgement beats anything guessable from characters, so it joins
  unconditionally; the trailing-hyphen-plus-lowercase rule stays as the fallback for
  producers PDFium does not recognise. Finding this is also what turned "de-hyphenation
  looks like it works" into "de-hyphenation works": the first implementation silently did
  nothing, because the character it was looking for never arrives.
* **Column detection is a projection profile, not a gap scan.** The first version looked
  for an x-interval no narrow line crossed; a full-width title on the page closed the gap
  and the columns interleaved anyway. Buckets with a noise tolerance find the channel with
  the title lying across it, which is what a real two-column page looks like.
* **The `needs_ocr` floor is an average over the document, not a per-page test.** A manual
  with a dozen full-page diagrams in it is still a text document, and skipping a real
  corpus is a worse failure than indexing a few thin pages out of one.
* **Isolation is a `spawn` pool on every platform, including Linux.** A forked child of an
  async worker inherits the event loop, the sockets and the database pool — all of which it
  holds references to and none of which it may use. The startup cost is paid once per
  child and the pool is long-lived and lazy, so the API process, which builds the whole
  pipeline to delete documents and reconcile connectors, starts no children at all.
* **A crash recycles the whole pool, and that is a real cost.** When a child dies every
  future on that executor fails, not only the one that killed it, so a poisonous file can
  collaterally fail a document being read beside it. The alternative is worse: raising
  something retryable leaves the innocent document retried until it dead-letters, with its
  row stuck at `extracting` and nothing on screen saying why. Failing both gives both a
  sentence and a **Retry** button, and the retry succeeds for the one that did nothing
  wrong.
* **The heavy queue is a logical name on the job, not a second queue object.**
  `JobRequest.queue` is set at the enqueue site from the *file name* — a scheduling call
  made before any bytes have been read, where being wrong costs ordering and nothing else
  — and `ArqJobQueue` maps it to a Redis key. `arq app.workers.main.HeavyWorkerSettings`
  reads it, with a lower job count because concurrency there multiplies a parser's memory
  rather than an HTTP client's. Running one is optional: without it those jobs wait, which
  is a visible backlog rather than a silent loss.
* **The chunk inspector needed a new store method.** `VectorStore.chunks` is a filtered
  read ordered by `chunk_index`, with its own record type rather than a `Match` carrying a
  meaningless zero score — a score of zero is a number somebody eventually renders. It
  returns what the row claims alongside what the index holds, because the two disagreeing
  is itself the finding: a document reporting twelve chunks with three indexed was written
  into a collection that has since been dropped, and "retrieval is bad" is how that
  otherwise presents.
* **`UnsupportedFormat` was deleted and replaced.** It was declared in task 09, documented
  as "becomes `skipped`, not `failed`", and never raised or caught anywhere — its docstring
  was aspirational. `SkippedDocument` is the class that now carries that meaning, and the
  pipeline actually branches on it.
* **The "coming soon" mechanism survived, repointed at EPUB.** Task 09 built it for the
  four formats this task implements; deleting it would have removed the distinction between
  a roadmap item and a file nobody should have uploaded, which is a distinction a customer
  needs. EPUB is sniffed as its own type and nothing reads it, so it occupies that state
  now.
* **Fixtures are written, not committed.** A PDF with a known header at a known position is
  something a reviewer can read the source of; a checked-in binary is not. `reportlab` is a
  test-only dependency, and nothing in `app/` writes a PDF.

### Bugs the tests found

* **De-hyphenation silently did nothing.** The rule looked for a trailing ASCII hyphen;
  PDFium replaces an end-of-line hyphen with `\x02`, its own soft-break marker, so the
  condition never fired and `manufac` / `turing` went into the index as two words. The
  marker is now the primary signal — and it was also reaching chunks as a control
  character, which renders as a box in the request drawer and embeds as nothing.
* **A full-width heading hid the gutter.** The first column detector excluded lines wider
  than 60% of the text area from the search; a page title at 58% stayed in, covered the
  channel, and the columns interleaved. The fixture that caught it is the ordinary shape of
  a two-column page.
* **A one-page PDF with one sentence on it was skipped as a scan.** Found by an encryption
  test, not a text test: the fixture had a single line, fell under the characters-per-page
  floor, and failed for a reason that had nothing to do with encryption. The floor is
  right — a page with 80 characters is genuinely ambiguous — so the fixture gained a page
  of prose, and the corner is now written down where the next person will see it.
* **The chunk-inspector route was invisible to the cross-tenant net until it was added to
  the table**, which is exactly what that test exists to notice. It returns chunk *text*,
  so it discloses as much as the debug search and is reachable with only the read
  capability.

### Not verifiable here

Unchanged from tasks 04-10. PostgreSQL on this machine rejects the credentials in `.env`,
so migration `0011_document_extraction` is covered by `tests/test_migration_offline.py` —
which renders it to SQL and asserts every mapped column and constraint is created,
including the two new ones — but the DDL has not run against a server.

There is no Qdrant, so the three new `chunks()` checks in `tests/vector_store_contract.py`
run against the memory store and skip the `qdrant` half. That half matters more than usual
here: `chunks()` is the first method that uses `scroll` rather than `query_points`, and a
filtered read with no vector to score against is precisely where a hand-written double
agrees with itself and disagrees with the server.

The **memory limit is POSIX-only** and this is Windows, so
`test_an_extraction_that_allocates_without_bound_is_stopped` skips. The wall clock, the
crash recovery and the pickling of a reason across the process boundary all run here.

Retrieval quality is spot-checked with the labelled question set in
`tests/office_fixtures.py`, and the assertion is *presence in the top three* rather than
first place. That is a limitation of the local embedder rather than of the corpus: it
hashes words into buckets, so a 150-page manual sharing only "of" and "the" with a question
can out-score the spreadsheet that literally contains the answer. Presence is the property
worth defending — a format whose extraction starts returning page furniture instead of
prose drops out of those results entirely — and a real embedding model is what would make
rank meaningful. No Playwright, no `make`, no Docker.

### Notes for later tasks

* **Task 13** distils from transcripts, which now include prompts assembled from PDF pages
  and slides. Nothing changes for it: the citation label is a string on the chunk.
* **Task 17** owns the reindex flow. The formats added here make it matter more — a corpus
  of 200-page manuals is expensive to re-embed — and `documents.page_count` is a rough
  proxy for that cost if a progress estimate is ever wanted.
* **OCR** is where `needs_ocr` leads. The state exists, the UI explains it, and a document
  in it is one reindex away from being picked up by whatever fills the gap.
* **`.doc`, `.ppt` and `.xls`** remain out of scope and currently sniff as
  `application/octet-stream`, so they read as "this file type is not a supported format".
  Giving them the OLE2 signature and a message naming the format is a small, self-contained
  improvement whenever somebody wants it.

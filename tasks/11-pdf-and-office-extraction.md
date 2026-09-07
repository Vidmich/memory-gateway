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
- [ ] Text-layer extraction with `pymupdf` (fast, good text ordering) — or `pdfplumber` where
      table fidelity matters more than speed. Pick one and justify it in a code comment.
- [ ] Preserve **page numbers** into `page_or_section`; citations that name a page are far more
      useful to an end user than ones that name only a file.
- [ ] Detect image-only pages: if extracted text across the document falls below a threshold
      (e.g. < 100 characters per page on average), mark the document `skipped` with
      `needs_ocr` rather than indexing near-empty chunks.
- [ ] Handle encrypted PDFs: attempt an empty-password open, otherwise fail with
      `password_protected`.
- [ ] Strip repeated headers and footers — detect lines recurring at the same position on most
      pages. Without this, every chunk carries the same boilerplate and retrieval scores blur.
- [ ] Merge hyphenated line breaks and collapse column artifacts into readable flow.
- [ ] Extract embedded outline/bookmarks as section headings where present, feeding `by_heading`
      chunking.

### Word (.docx)
- [ ] Paragraph extraction preserving heading levels from paragraph styles, populating
      `page_or_section` with the enclosing heading path (`"Security > Access Control"`).
- [ ] Tables rendered as readable records, matching the CSV convention from task 09.
- [ ] Lists preserved as list markers rather than flattened into run-on text.
- [ ] Ignore headers, footers, and footnote apparatus; include footnote text appended to its
      paragraph.
- [ ] Tracked changes: extract the accepted/current text, not the deleted revisions.

### PowerPoint (.pptx)
- [ ] Per-slide extraction: title, body placeholders, text boxes, and table content.
- [ ] `page_or_section` = `"Slide N: <title>"`.
- [ ] **Speaker notes included**, marked as notes — they frequently contain the substance the
      slide only gestures at.
- [ ] One chunk per slide by default (slides are naturally chunk-sized); sub-split only if a slide
      exceeds `chunk_size`.

### Excel (.xlsx)
- [ ] Per-sheet extraction; `page_or_section` = the sheet name.
- [ ] Header-row detection, then rows rendered as `column: value` records (the task 09 CSV
      convention) rather than raw cells.
- [ ] Formula cells use cached computed values; where absent, record the formula text.
- [ ] Row cap per sheet (default 50 000) with a truncation marker — a spreadsheet is a database
      export, and indexing all of it is rarely what the user meant.
- [ ] Skip empty sheets and fully empty columns.

### Pipeline hardening
- [ ] Register all extractors in the task 09 `ExtractorRegistry`; no pipeline changes required.
- [ ] Run heavy extraction in a **separate process** (process pool) with a hard wall-clock and
      memory cap, so a malformed file kills a subprocess rather than a worker.
- [ ] Optional dedicated worker queue for heavy formats, so a 300-page PDF cannot delay a
      Markdown file behind it.
- [ ] Extraction-duration and per-format failure-rate metrics.
- [ ] Remove the "coming soon" skip reason from task 09 for these extensions.

### UI
- [ ] Document table shows page/slide/sheet counts alongside chunk counts.
- [ ] `needs_ocr` and `password_protected` render as first-class explained states with guidance,
      not as generic failures.
- [ ] Chunk inspector: for a selected document, list its chunks with their `page_or_section` —
      the fastest way to see whether extraction produced sensible text.

## Acceptance criteria

- [ ] Each of the four formats ingests correctly from a fixture set, with page/section metadata
      present on every chunk.
- [ ] A citation from a PDF names the correct page number, verified against the source.
- [ ] An image-only PDF is `skipped: needs_ocr`, never indexed as empty chunks.
- [ ] A malformed file of each format fails cleanly with a readable error and does not kill the
      worker.
- [ ] A 200-page PDF extracts within the time cap and within a bounded memory ceiling.
- [ ] Repeated headers/footers do not appear in every chunk (assert against a fixture that has
      them).
- [ ] Retrieval quality on the fixture corpus is not degraded by boilerplate — spot-checked with
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

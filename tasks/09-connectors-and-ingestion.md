# Task 09 — Connectors & file-drop ingestion

**Slice:** upload files in the UI and watch them become searchable indexed chunks.
**Depends on:** 06 (07 recommended, for the worker's observability)
**Spec:** §9.1, §9.3, §9.4, §9.5, §13.1 (Connectors)
**Size:** L

---

## Why this slice

This is the first half of the memory promise: getting customer content in, reliably, with
visible status. It deliberately stops short of using the content in answers (task 10), because
ingestion has enough failure modes of its own to be worth demonstrating and hardening alone.

## Demo at the end of this task

**Connectors → New → "Product docs"**. Drag in a folder of Markdown files. The document table
fills with rows moving through `pending → extracting → chunking → embedding → indexed`, with live
chunk counts. Drop in an unsupported `.mov` and it lands as `skipped` with a readable reason.
Corrupt one file and it lands as `failed` with the extraction error and a working **Retry**.

Delete a file from the connector and press **Resync** — its document and vectors disappear.

## In scope

- Connector model with the managed file-drop type; per-org S3 prefix provisioning.
- Worker infrastructure (this task introduces it).
- Upload (UI multipart + presigned URL), extraction for text/code formats, chunking, embedding,
  Qdrant upsert, resync reconciliation.
- Connectors UI with document table and status.

## Out of scope

- PDF and Office extraction (11) — those extensions are recognized and marked `skipped:
  unsupported (coming soon)` rather than failing.
- Retrieval and injection into prompts (10). Nothing consumes the index yet except a debug
  search endpoint.
- Other connector types (SPEC §16.3) — but the abstraction must not assume S3.

## Work items

### Worker infrastructure
- [ ] Job runner on Redis (`arq` preferred for async-native simplicity; Celery acceptable).
- [ ] `worker` service in Compose, sharing the app image.
- [ ] Job base class with: idempotency key, retry with exponential backoff and a max attempt
      count, a dead-letter record, structured logging carrying the originating `request_id`, and
      per-job timing metrics.
- [ ] Jobs are enqueued **after** the database transaction commits — enqueuing inside a
      transaction that later rolls back produces a job referencing a row that does not exist.
- [ ] Worker health surfaced in `/readyz` (queue reachable) and a Prometheus queue-depth gauge.

### Connector model
- [ ] `connectors(id, organization_id, name, type, config_jsonb, chunking_jsonb, storage_prefix,
      status, last_synced_at, created_at)`.
- [ ] `documents(id, connector_id, organization_id, source_uri, source_name, mime_type,
      size_bytes, content_hash, etag, status, error, chunk_count, embedding_model, indexed_at,
      created_at)`, unique on `(connector_id, source_uri)`.
- [ ] **Connector interface**, type-agnostic so later types slot in unchanged:
      ```python
      class Connector(Protocol):
          async def list_objects(self) -> AsyncIterator[ObjectRef]: ...
          async def fetch(self, ref: ObjectRef) -> BinaryIO: ...
          def supports_push(self) -> bool: ...
      ```
- [ ] `ManagedFileDropConnector` implementation over `orgs/{org_id}/connectors/{connector_id}/`.
- [ ] Deleting a connector removes its objects, documents, and vectors — as a job, with the
      connector marked `deleting` so the UI reflects it.

### Upload
- [ ] `POST /connectors/{id}/upload` — multipart, streamed to object storage without buffering
      whole files in memory. Multiple files per call.
- [ ] `POST /connectors/{id}/upload-url` — short-lived presigned PUT (default 15 min) plus the
      object key to use, so customers can script uploads.
- [ ] Enforce a per-file size cap (default 50 MB) and per-org total storage quota; reject over-cap
      uploads before the bytes are stored.
- [ ] Content type sniffed from bytes, not trusted from the extension or the client.
- [ ] Ingestion enqueued on upload completion. Presigned uploads are picked up by **Resync** (an
      object-storage event hook is a task 18 prod optimization).

### Extraction (text formats only)
- [ ] Text: `.txt`, `.md`, `.rst`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.yaml`, `.xml`, `.html`.
- [ ] Code: `.py`, `.js`, `.ts`, `.tsx`, `.go`, `.java`, `.rb`, `.rs`, `.sql`, `.sh`, `.c`,
      `.cpp`, `.cs`, `.php`, `.kt`, `.swift`.
- [ ] HTML stripped to text, preserving heading structure and dropping script/style.
- [ ] CSV/TSV rendered as readable records (`column: value` per row) rather than raw commas —
      embeddings of raw CSV rows retrieve poorly.
- [ ] JSON/JSONL flattened to readable key paths.
- [ ] Encoding detection with a UTF-8 fallback; undecodable files fail with a clear reason.
- [ ] `ExtractorRegistry` keyed by extension and sniffed MIME type, so task 11 registers new
      extractors without touching the pipeline.
- [ ] Wall-clock cap per file (default 120 s) so one pathological input cannot occupy a worker.

### Chunking
- [ ] `chunking_jsonb`: `strategy` (`recursive` | `fixed` | `by_heading`), `chunk_size` (tokens,
      default 1000), `overlap` (default 150), `respect_boundaries` (default true).
- [ ] `recursive`: split on paragraph → sentence → token boundaries, never mid-word.
- [ ] `by_heading`: Markdown headings and HTML `<h1>`–`<h4>`, falling back to `recursive` for
      unstructured formats. Oversized sections are sub-split.
- [ ] Token counting with `tiktoken` (or the embedding model's tokenizer), not character counts.
- [ ] Chunk metadata per SPEC §9.3: `org_id, connector_id, document_id, source_name, source_uri,
      page_or_section, chunk_index, ingested_at, content_hash`.

### Embedding & Qdrant
- [ ] Platform embedding configuration from environment for now (provider, model, dimension);
      task 17 moves it into `platform_settings` with a reindex flow.
- [ ] Per-org collection `org_{org_id}_docs`, created on first use with the configured dimension,
      cosine distance, and payload indexes on `connector_id` and `document_id`.
- [ ] Batched embedding calls with concurrency limits and retry on 429.
- [ ] Idempotent upsert: deterministic point ids from `(document_id, chunk_index)`, so a re-run
      replaces rather than duplicates.
- [ ] Re-ingesting a document deletes its previous vectors first (delete-by-filter on
      `document_id`), then upserts — otherwise stale chunks survive an edit.
- [ ] Record `embedding_model` on the document row so drift is detectable.

### Resync
- [ ] `POST /connectors/{id}/resync` — lists objects and reconciles:
      new → ingest; changed ETag → re-ingest; missing → delete document and vectors; unchanged →
      skip.
- [ ] Reports a summary `{added, updated, deleted, unchanged, skipped}`.
- [ ] Safe to run concurrently with uploads (advisory lock per connector).

### API & UI
- [ ] `GET|POST /connectors`, `GET|PATCH|DELETE /connectors/{id}`,
      `GET /connectors/{id}/documents`, `DELETE /documents/{id}`,
      `POST /documents/{id}/reindex`, `POST /connectors/{id}/resync`.
- [ ] `POST /connectors/{id}/search` — debug-only semantic search returning chunks with scores.
      This proves the index works before task 10 exists, and stays useful afterward.
- [ ] **Connectors list**: name, type, document counts by status, total size, last synced.
- [ ] **Connector detail**: drag-and-drop upload zone with per-file progress; document table
      (name, size, type, status, chunks, indexed at) with retry, delete, and inline error text;
      chunking configuration panel with a warning that changes require reindex; Resync button;
      presigned-upload instructions with a copyable snippet.
- [ ] Status polling (or SSE) so the table advances without a manual refresh — the visible
      progression is the demo.

## Acceptance criteria

- [ ] Uploading 100 mixed files results in correct per-file terminal states with no stuck rows.
- [ ] A failed document shows an actionable error and retries successfully once the cause is
      fixed.
- [ ] Re-uploading a changed file replaces its chunks; the old ones are gone from Qdrant (assert
      via the debug search).
- [ ] Resync correctly detects added, changed, and deleted objects.
- [ ] Deleting a connector removes its objects, documents, and vectors.
- [ ] A 50 MB file does not exhaust worker memory (streamed, not loaded whole).
- [ ] Ingesting the same file twice concurrently produces one document and one chunk set.
- [ ] Cross-tenant test module extended: connectors, documents, and vector collections are
      org-isolated, including the debug search.

## Tests

- Extractor unit tests per format with fixture files, including a malformed example of each.
- Chunking: boundary behavior, overlap correctness, oversized sections, token accounting.
- Idempotency: repeated ingestion produces identical point ids and no duplicates.
- Resync reconciliation across all four cases.
- Qdrant integration against a real container (not a mock) — payload filters and delete-by-filter
  are exactly where a mock would lie.
- Job retry, dead-lettering, and post-commit enqueue ordering.

## Notes

- Ingest CSV as rendered records, not raw rows. It is the single change that most improves
  retrieval quality on tabular sources, and it costs nothing to do now.
- Recognizing PDF/Office extensions as `skipped: coming soon` rather than `failed` keeps the
  task 11 gap honest and stops users from concluding the product is broken.

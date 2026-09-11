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
- [x] Job runner on Redis (`arq` preferred for async-native simplicity; Celery acceptable).
- [x] `worker` service in Compose, sharing the app image.
- [x] Job base class with: idempotency key, retry with exponential backoff and a max attempt
      count, a dead-letter record, structured logging carrying the originating `request_id`, and
      per-job timing metrics.
- [x] Jobs are enqueued **after** the database transaction commits — enqueuing inside a
      transaction that later rolls back produces a job referencing a row that does not exist.
- [x] Worker health surfaced in `/readyz` (queue reachable) and a Prometheus queue-depth gauge.

### Connector model
- [x] `connectors(id, organization_id, name, type, config_jsonb, chunking_jsonb, storage_prefix,
      status, last_synced_at, created_at)`.
- [x] `documents(id, connector_id, organization_id, source_uri, source_name, mime_type,
      size_bytes, content_hash, etag, status, error, chunk_count, embedding_model, indexed_at,
      created_at)`, unique on `(connector_id, source_uri)`.
- [x] **Connector interface**, type-agnostic so later types slot in unchanged:
      ```python
      class Connector(Protocol):
          async def list_objects(self) -> AsyncIterator[ObjectRef]: ...
          async def fetch(self, ref: ObjectRef) -> BinaryIO: ...
          def supports_push(self) -> bool: ...
      ```
- [x] `ManagedFileDropConnector` implementation over `orgs/{org_id}/connectors/{connector_id}/`.
- [x] Deleting a connector removes its objects, documents, and vectors — as a job, with the
      connector marked `deleting` so the UI reflects it.

### Upload
- [x] `POST /connectors/{id}/upload` — multipart, streamed to object storage without buffering
      whole files in memory. Multiple files per call.
- [x] `POST /connectors/{id}/upload-url` — short-lived presigned PUT (default 15 min) plus the
      object key to use, so customers can script uploads.
- [x] Enforce a per-file size cap (default 50 MB) and per-org total storage quota; reject over-cap
      uploads before the bytes are stored.
- [x] Content type sniffed from bytes, not trusted from the extension or the client.
- [x] Ingestion enqueued on upload completion. Presigned uploads are picked up by **Resync** (an
      object-storage event hook is a task 18 prod optimization).

### Extraction (text formats only)
- [x] Text: `.txt`, `.md`, `.rst`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.yaml`, `.xml`, `.html`.
- [x] Code: `.py`, `.js`, `.ts`, `.tsx`, `.go`, `.java`, `.rb`, `.rs`, `.sql`, `.sh`, `.c`,
      `.cpp`, `.cs`, `.php`, `.kt`, `.swift`.
- [x] HTML stripped to text, preserving heading structure and dropping script/style.
- [x] CSV/TSV rendered as readable records (`column: value` per row) rather than raw commas —
      embeddings of raw CSV rows retrieve poorly.
- [x] JSON/JSONL flattened to readable key paths.
- [x] Encoding detection with a UTF-8 fallback; undecodable files fail with a clear reason.
- [x] `ExtractorRegistry` keyed by extension and sniffed MIME type, so task 11 registers new
      extractors without touching the pipeline.
- [x] Wall-clock cap per file (default 120 s) so one pathological input cannot occupy a worker.

### Chunking
- [x] `chunking_jsonb`: `strategy` (`recursive` | `fixed` | `by_heading`), `chunk_size` (tokens,
      default 1000), `overlap` (default 150), `respect_boundaries` (default true).
- [x] `recursive`: split on paragraph → sentence → token boundaries, never mid-word.
- [x] `by_heading`: Markdown headings and HTML `<h1>`–`<h4>`, falling back to `recursive` for
      unstructured formats. Oversized sections are sub-split.
- [x] Token counting with `tiktoken` (or the embedding model's tokenizer), not character counts.
- [x] Chunk metadata per SPEC §9.3: `org_id, connector_id, document_id, source_name, source_uri,
      page_or_section, chunk_index, ingested_at, content_hash`.

### Embedding & Qdrant
- [x] Platform embedding configuration from environment for now (provider, model, dimension);
      task 17 moves it into `platform_settings` with a reindex flow.
- [x] Per-org collection `org_{org_id}_docs`, created on first use with the configured dimension,
      cosine distance, and payload indexes on `connector_id` and `document_id`.
- [x] Batched embedding calls with concurrency limits and retry on 429.
- [x] Idempotent upsert: deterministic point ids from `(document_id, chunk_index)`, so a re-run
      replaces rather than duplicates.
- [x] Re-ingesting a document deletes its previous vectors first (delete-by-filter on
      `document_id`), then upserts — otherwise stale chunks survive an edit.
- [x] Record `embedding_model` on the document row so drift is detectable.

### Resync
- [x] `POST /connectors/{id}/resync` — lists objects and reconciles:
      new → ingest; changed ETag → re-ingest; missing → delete document and vectors; unchanged →
      skip.
- [x] Reports a summary `{added, updated, deleted, unchanged, skipped}`.
- [x] Safe to run concurrently with uploads (advisory lock per connector).

### API & UI
- [x] `GET|POST /connectors`, `GET|PATCH|DELETE /connectors/{id}`,
      `GET /connectors/{id}/documents`, `DELETE /documents/{id}`,
      `POST /documents/{id}/reindex`, `POST /connectors/{id}/resync`.
- [x] `POST /connectors/{id}/search` — debug-only semantic search returning chunks with scores.
      This proves the index works before task 10 exists, and stays useful afterward.
- [x] **Connectors list**: name, type, document counts by status, total size, last synced.
- [x] **Connector detail**: drag-and-drop upload zone with per-file progress; document table
      (name, size, type, status, chunks, indexed at) with retry, delete, and inline error text;
      chunking configuration panel with a warning that changes require reindex; Resync button;
      presigned-upload instructions with a copyable snippet.
- [x] Status polling (or SSE) so the table advances without a manual refresh — the visible
      progression is the demo.

## Acceptance criteria

- [x] Uploading 100 mixed files results in correct per-file terminal states with no stuck rows.
- [x] A failed document shows an actionable error and retries successfully once the cause is
      fixed.
- [x] Re-uploading a changed file replaces its chunks; the old ones are gone from Qdrant (assert
      via the debug search).
- [x] Resync correctly detects added, changed, and deleted objects.
- [x] Deleting a connector removes its objects, documents, and vectors.
- [x] A 50 MB file does not exhaust worker memory (streamed, not loaded whole).
- [x] Ingesting the same file twice concurrently produces one document and one chunk set.
- [x] Cross-tenant test module extended: connectors, documents, and vector collections are
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

---

## Verification status

Everything above is implemented. The gates:

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   212 files already formatted
uv run mypy                    Success: no issues found in 202 source files
uv run pytest -q               1981 passed, 302 skipped

npx eslint . / npx tsc         clean
npx vitest run                 316 passed (16 files)
npm run build                  375.10 kB JS (112.63 kB gzipped)
```

The whole demo was then run **over a real socket** — the real FastAPI app served by
uvicorn on a real port, driven by a real HTTP client with real multipart bodies —
**44/44 checks**. Only the four things underneath it that need a container (PostgreSQL,
Redis, Qdrant, MinIO) were the in-memory implementations of their ports; every one of
those has a contract test that runs the *same* assertions against the real service.

The run covered the task's demo verbatim and then some: a folder of 40 Markdown files
dragged in and indexed with live chunk counts; a `.mov` skipped as `This video is not a
supported format.`; a PDF skipped as `PDF extraction arrives in a later release.`; a JPEG
named `notes.txt` skipped as an image rather than decoded; a corrupt JSON file failed with
`This file is not valid JSON: Expecting value at line 2, column 13.`, retried unchanged
(fails identically), then fixed and indexed; a CSV retrieved as `role: Engineer` rather
than raw commas; an edited file's old text unreachable from the index; a presigned URL that
cannot be pointed outside its prefix; resync reporting `added: 1` for a scripted upload and
`deleted: 1` after the file was removed; 47 documents all in a terminal state; and the
connector deleted, taking its objects, documents and vectors with it.

### Deliberate deviations, and why

* **The `Connector` protocol is called `ConnectorSource`, and `fetch` yields chunks.** The
  task file spells the protocol `Connector` with `async def fetch(...) -> BinaryIO`. Two
  changes. `Connector` is already the mapped row — the customer's configuration — and the
  two are genuinely different things, so `connector.fetch(...)` would be ambiguous at every
  call site. And a synchronous `BinaryIO` in an async pipeline leaves exactly two options,
  both of which are the bug the acceptance criteria name: read it on the event loop and
  stall every other job on the worker, or read it whole and put a 50 MB file in memory. An
  async byte iterator is the same idea with neither.
* **`watch()` is not on the protocol.** SPEC §9.1 lists it; nothing in v1 pushes, and the
  honest replacement is `supports_push()`, which the UI can act on. A method every
  implementation raises `NotImplementedError` from is not an abstraction.
* **Resync is synchronous, not a job.** SPEC §9.1 and the API work item both say it reports
  `{added, updated, deleted, unchanged, skipped}`, and a job cannot return a summary. What
  it does synchronously is a listing plus row writes; the expensive half — extracting and
  embedding each changed object — is what it enqueues. The trade is real: a connector with
  an enormous number of objects makes this a long request, and task 18's scheduled sync is
  where it becomes a background job with a progress record instead of a return value.
* **`JOB_NAMES` has two entries, not three.** Ingesting one document and tearing a
  connector down are the two genuinely unbounded pieces of work. A `resync_connector` job
  that nothing enqueued would be dead code pretending to be architecture.
* **Deleting a *document* is synchronous** even though deleting a connector is a job. It is
  one delete-by-filter and one object delete, and the row is on screen in front of whoever
  pressed the button; a job would leave it there until a poll noticed.
* **The lock is Redis, not a PostgreSQL advisory lock.** A session-level advisory lock is
  held by a connection, so a worker that *hangs* holds it with no lease to expire. More
  importantly the lock is documented as a convenience, not a correctness mechanism: what
  actually makes concurrent reconciliation safe is that resync only ever deletes a document
  in a **terminal** state. The race with an upload opens *before* any lock could be taken —
  the listing is a snapshot, and a file uploaded a millisecond later is legitimately absent
  from it.
* **An extension never overrules binary bytes.** The registry is keyed by sniffed media
  type *and* extension, as the work item asks, but the extension fallback applies only when
  the sniffed type is textual. This was found by a test: a JPEG called `notes.txt` sniffed
  as `image/jpeg`, missed the media-type lookup, and fell through to the `.txt` extractor —
  which would have decoded it as mojibake and indexed it as though it worked.
* **Binary files are never read past the sniff window.** The pipeline decides the type from
  the first 8 KB and abandons the stream if nothing can read it. A folder of videos costs a
  listing and 8 KB each rather than their size. Text files are then read in full, because
  extraction needs the content; that read is bounded by the per-file cap, which is what
  makes worker memory a function of concurrency rather than of what somebody uploaded.
* **The extraction cap bounds the job, not the CPU.** Extraction runs in a thread under
  `asyncio.timeout`; Python cannot interrupt a thread, so a pathological file's thread is
  abandoned rather than killed. Bounding the CPU would need a subprocess pool, which is a
  larger machine than one bad file justifies — and the worker stays responsive either way,
  because the thread is not on the event loop.
* **`documents` carries both `etag` and `content_hash`.** An ETag is not a content hash for
  a multipart upload and its derivation differs between backends, so a resync compares
  ETags to decide what to *look at* and hashes to decide what to *redo*.
* **`job_dead_letters` is not tenant-keyed.** The customer-visible half of an ingestion
  failure is already on `documents.error`, next to the retry button; this is the operator's
  half. An `organization_id` would make it a tenant table the worker has to invent a scope
  for on a path with no request and no session, and the surest way not to get that wrong is
  to have no column to get wrong.
* **`TenantScope.of_organization` names its role `service`.** A worker has an organization
  id written into a job payload by a request that *was* scoped. Naming the role rather than
  borrowing `org_admin` keeps the log honest and keeps `assume()` refusing, so a job can
  never widen itself into another tenant.
* **arq is told `max_tries=1`.** Retries, backoff, the attempt ceiling and the dead-letter
  record are one policy in `app/services/jobs.py`, testable exhaustively without Redis. The
  cost is named rather than hidden: a job whose worker is killed mid-run is not
  re-delivered, and the next resync is what recovers it — which is the reconciliation path
  that has to exist anyway.
* **Job payloads are JSON, not arq's default pickle.** The payloads are three strings; JSON
  costs nothing and removes the class of problem where anything able to write to Redis can
  execute code in a worker.
* **`/readyz` reports `jobs` separately from `redis`.** They are the same server and they
  mean different things: a Redis outage takes the whole service down, while a queue that
  cannot be written to leaves the API serving traffic and silently dropping ingestion.
* **The upload endpoint answers 200 with a per-file outcome**, never a 4xx for a rejected
  file. Somebody drops in forty files and one is a 2 GB video; there is no status code that
  means "thirty-nine worked". The HTTP status answers "did the request work", the body
  answers "what happened to each file".
* **A quota rejection says so, rather than reporting the per-file limit.** When the
  remaining allowance is what binds, the message names the organization's storage — telling
  someone to split a file that would not have fitted either way is worse than saying
  nothing.
* **`EMBEDDING_PROVIDER=hash` is the development default, and production refuses to start
  with it.** The local embedder is a real hashing-trick bag-of-words model — genuinely
  lexical, so shared words score higher — which is what makes the whole ingest-and-search
  path demonstrable with no provider key. It knows nothing about meaning, and the failure
  mode is silent, so a `ValidationError` at import time is the only version of that warning
  nobody can miss.
* **`tiktoken` falls back to a word tokenizer when its vocabulary cannot be fetched.** It
  downloads on first use; an air-gapped runner or a cold container with no egress would
  otherwise turn a missing file into a worker that cannot ingest anything. Chunks come out
  roughly 30% larger than asked for, which costs retrieval quality where the alternative
  costs the feature — and the log says so.
* **`app/schemas/config.py` was extracted from `gateway_config.py`.** Chunking is the second
  domain to need a versioned settings blob, and copying the permissive-load/strict-write
  machinery would have been two copies to keep in step.
* **Object keys keep their path separators.** A dragged folder keeps its shape, so a
  citation reads `docs/api/auth.md` rather than a flat `auth.md`. Everything that could
  climb out of the prefix does not: no leading slash, no `..` segment, no backslash, no
  control characters — and the prefix itself is derived from two ids, never supplied.
* **The header's delete button is labelled "Delete connector".** The document rows have a
  "Delete" of their own, and two controls with the same accessible name on one screen are
  ambiguous to a screen reader as well as to a test.
* **An upload opens one short transaction per file, not one across the batch.** The bytes
  go to object storage between them, and holding a transaction open across forty network
  writes ties up a connection and its row locks for as long as the slowest upload takes.
  Re-reading the connector each time costs one indexed lookup and buys a second property:
  a delete that starts mid-batch stops the rest of it.
* **A failed resync marks the connector `error` and records why.** Otherwise the row reads
  `syncing` forever with nothing anywhere saying what went wrong — and the Resync button
  somebody presses again is the only feedback they get.
* **The dashboard's *Documents indexed* card was wired up**, which is outside the task's
  screen list. It said "Arrives with connectors", and connectors have arrived; leaving it
  would have been a visible untruth in the product this task ships. It swaps its subtitle
  for the failure count when there is one, because a dashboard reporting 900 indexed and
  saying nothing about the 40 that could not be read is reporting the half nobody needs to
  act on.

### Not verifiable here

Unchanged from tasks 04–08: PostgreSQL on this machine rejects the credentials in `.env`,
so the three new tables are covered by `tests/test_migration_offline.py` (which renders the
migration to SQL and asserts every mapped table, column and constraint is created by one)
but the DDL has not been run against a server. `tests/test_connector_db.py` — the store
contract against PostgreSQL, the CHECK constraints, the cascade, and the two halves that
make "one document" true (the unique constraint the server enforces, and the upsert
converging onto it rather than raising) — is `db`-marked and skips.

That file deliberately does *not* claim to prove the race itself. A genuine race needs two
connections and two real commits, and the `db_session` fixture binds everything to one
connection inside one transaction it rolls back — so an `asyncio.gather` there would
serialise rather than race, and a test that said otherwise would be worse than no test.

There is no MinIO and no Qdrant, so `tests/test_object_store.py` and
`tests/test_vector_store.py` run their contracts against the memory implementations and
skip the `s3` and `qdrant` halves. Those halves are the ones that matter most for this
task — the task file singles Qdrant out by name — so they are written, marked, and will run
in CI. There is no Docker, so the `worker` service in Compose is unrun; the settings it
uses were fed to a real `arq.worker.Worker`, which accepted them and registered
`run_gateway_job` under the name the queue enqueues to. No Playwright, no `make`.

The acceptance criterion "a 50 MB file does not exhaust worker memory" was measured rather
than asserted, in the smoke run: an 11 MB upload through the real socket peaked at +22.9 MB
of traced allocation — and that figure *includes* the in-memory object store's own copy of
the file, which S3 does not keep. Against S3 the buffer is bounded by the 8 MiB part size.

### Notes for later tasks

* **Task 10** consumes what this built. `MemoryConfig.connector_ids` already exists on the
  gateway (task 06 shaped it), the chunk payload already carries every SPEC §9.3 field a
  citation needs, and `VectorStore.search` already takes `connector_ids` and `min_score` —
  so retrieval is a caller, not a change to any of this.
* **Task 11** registers a PDF and an Office extractor and the "coming soon" notes disappear
  on their own: `ExtractorRegistry.register` clears the pending note for a media type. The
  pipeline does not change. `Section` already models a page as well as a heading, which is
  what the PDF extractor needs for `page_or_section`.
* **Task 13**'s distillation worker inherits the job infrastructure whole — `JobRunner`,
  `RetryPolicy`, `JobOutbox`, the dead-letter table and the metrics. Adding a job is a name
  in `JOB_NAMES` and a handler in `build_handlers`.
* **Task 17** moves the embedding configuration into `platform_settings`.
  `EmbeddingSettings` is the shape it will read, and `documents.embedding_model` plus the
  per-collection dimension are what make the reindex detect drift. Dropping and rebuilding
  a collection is already one call each.
* **Task 18**'s scheduled sync is where resync becomes a job with a progress record, and
  where the object-store event notification SPEC §9.1 mentions replaces the resync-driven
  pickup for presigned uploads.

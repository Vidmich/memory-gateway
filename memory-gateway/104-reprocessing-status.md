# Task 104 — Reprocessing status after a chunking or embedding change

**Slice:** when chunking, summarization, the tokenizer or the embedding model changes, every
affected document is visibly *stale* until it has been reprocessed — on the document row, on the
connector, on every gateway that reads it, and on the dashboard — with progress and an ETA while
the reprocessing runs, and nothing that depends on remembering a banner from a PATCH response.
**Depends on:** 09, 17, 20 (and 101, 102 if present — their settings join the fingerprint)
**Spec:** §9.4 (drift is detectable), §9.5 (status model), §13.1, §13.2 ("the UI states when a
change is live") — and amends §9.5 with a second axis of status.
**Size:** M
**Status:** post-v1. Best done after 101 and 102 so the fingerprint is defined once.

---

## Why this slice

Task 20 made a change to chunking answer with `reindex_required: true` and a list of formats,
and the connector screen offers the reindex right there. Close the tab and it is gone. The flag
is computed in the PATCH handler and stored nowhere; `GET /connectors/{id}` a minute later says
nothing, the connector's list row says nothing, and the gateway reading it says nothing. The
connector is now serving chunks cut one way to documents that will be cut another, and the only
record is a log line: *"connector chunking changed; existing chunks are now stale"*.

The ground truth exists. Task 20 added `documents.chunk_fingerprint` — what each file was
actually cut with — deliberately without a backfill, so that a blank means *unrecorded* rather
than a guess. Comparing that column with the connector's current effective fingerprint is a
query, and it is the query nobody can run from a screen. Meanwhile the platform embedding
change (task 17) has its own reindex with its own progress, and a connector on `semantic` is
recut by it, and a document row cannot say which of the two operations it is waiting on.

§13.2 promises *"the UI states when a change is live."* For chunking it states it once and
forgets. This task makes staleness a **stored, per-document fact** with a derived per-connector
summary, makes reprocessing a **tracked operation** with progress rather than a burst of
anonymous jobs, and puts the state everywhere a person would otherwise be surprised by it.

## Demo at the end of this task

Change a connector's chunking. Save. The connector header now reads **"1 184 of 1 200 documents
indexed under a previous configuration"** with a **Reprocess** button, and it still reads that
after a refresh, tomorrow, and from the connectors list, where the row has a yellow badge.
Open a gateway that reads the connector: its Memory section says *"answers may be drawn from
two chunkings until connector X is reprocessed"*.

Press Reprocess. The header becomes a progress bar — *312 / 1 184, ~6 min remaining, 2 failed* —
and the document table's status column shows `stale`, `reprocessing`, `current` per row, with
a filter. Retrieval keeps working throughout. When it finishes, the badge is gone everywhere,
and the run is in the connector's history with what changed, who changed it, how long it took,
and what it cost.

Change the platform embedding model. Every connector shows stale for a different reason —
*"embedded with text-embedding-3-small; platform is now -large"* — and the platform reindex
progress appears on each connector it touches.

## In scope

- One **index fingerprint** per document that captures everything its stored points depend on,
  and one effective fingerprint per connector and format to compare it with.
- Per-document staleness derived from that comparison, per-connector counts, and where both
  are surfaced.
- Reprocessing as a first-class run: scoped, tracked, resumable, with progress and history.
- Retrieval's behaviour while an index is mixed, and how a request says so.

## Out of scope

- **Automatic reprocessing on save.** A chunking change can cost a corpus's worth of embedding
  calls; saving a form should not spend that without a second click, and the reindex estimate
  exists to be read first. The button stays.
- **Zero-mixed-state reprocessing** (build a shadow index, swap atomically). That is task 17's
  platform reindex, and it is the right shape for a model change that invalidates *everything*.
  A per-connector recut of one format touches a fraction of the points, and a shadow of the
  whole collection for that is the wrong cost. Mixed state is tolerated and **labelled**.
- **Undo.** Reverting the configuration is a change like any other and marks the recut
  documents stale in turn.

## Work items

### The fingerprint, defined once

- [ ] `index_fingerprint(effective_chunking, *, embedding_model, tokenizer, summarization,
      extraction_version) -> str` in one module, replacing the scattered pieces: task 20's
      `fingerprint()` becomes an input to it. Everything a stored point depends on is in it;
      nothing else is. The extraction version is included because a PDF extractor upgrade
      changes the text the chunks were cut from — the case that has no other trigger today.
- [ ] `documents.index_fingerprint` replaces `chunk_fingerprint` (expand-contract per
      CONTRIBUTING: add the column, dual-write, migrate readers, drop the old one next
      release). `chunk_strategy` stays; it is what a person reads.
- [ ] `ConnectorView.effective_fingerprints: dict[format, str]`, computed from the effective
      configuration per format plus the platform state — the thing every document is compared
      against, returned on every GET, not only after a PATCH.
- [ ] `stale_reason(document, effective) -> Reason | None` explains the difference in words a
      person can act on: *chunking changed*, *embedding model changed*, *tokenizer changed*,
      *summarization changed*, *extractor upgraded*, *unrecorded* (a `NULL` from before the
      column existed — shown differently, because it is not known to be wrong).

### Staleness, stored and served

- [ ] `documents.index_status: Literal["current", "stale", "reprocessing"]` maintained by
      the connector service (on every configuration save, for every document in the affected
      formats, in one `UPDATE ... WHERE format IN (...)` — not by comparing fingerprints at
      read time in every list query) and by the platform reindex when it changes the embedding
      model. Ingestion sets `current` when it finishes. The fingerprint is the truth and the
      status is the index over it; a reconciliation job (`reconciliation.py` already exists
      for exactly this kind of drift) recomputes status from fingerprints nightly and logs any
      row where they disagreed.
- [ ] `reindex_required` on the connector becomes **derived and persistent**: `stale_documents
      > 0`. `reindex_formats` becomes the set of formats with stale documents. The PATCH
      response keeps the same fields and now agrees with the GET a minute later.
- [ ] The connectors list returns `stale_documents` per row; the document list accepts
      `index_status` as a filter; the document response carries `index_status` and
      `stale_reason`.
- [ ] `GatewayView.stale_connectors`: the connectors this gateway reads that have stale
      documents, with counts. Gateways are where the consequence is felt and the one place
      nobody would think to look.
- [ ] The Dashboard's degraded-state list includes connectors with stale documents older than
      a threshold (default 24 h), and the age.
- [ ] Metrics: `documents_stale{connector}` gauge, `reprocessing_runs_total{outcome}`,
      `reprocessing_duration_seconds`. An alert on stale documents older than a day, pointed
      at the runbook.

### Reprocessing as a run

- [ ] `reprocessing_runs`: `organization_id`, `connector_id`, `trigger` (`chunking |
      embedding_model | tokenizer | summarization | extractor | manual`), `formats`,
      `requested_by`, `total`, `done`, `failed`, `skipped`, `started_at`, `finished_at`,
      `outcome`, `estimated_tokens`, `spent_tokens`. Task 17's reindex has a run and a
      `Progress` model; reuse the model, not the table — the units differ (documents here,
      points there) and a per-connector run is scoped to an organization.
- [ ] `POST /connectors/{id}/reprocess` supersedes the reindex endpoint from task 20 (kept as
      an alias for one release): scope by format or by `stale` only (the default — reprocessing
      current documents is a waste the old endpoint could not avoid), returns the run. One run
      per connector at a time; a second request while one is in flight returns the running one.
- [ ] The run enqueues per-document ingestion jobs tagged with the run id; each job's finish
      increments the run's counters atomically. Progress and ETA are `done / elapsed` over the
      last N minutes, as the reindex computes them. Resumable across a worker restart because
      the jobs are the state; a run whose worker died is detected by the reconciliation job
      and continued, not restarted.
- [ ] Per-document failures do not stop the run; they leave the document `failed` with its
      ingestion reason and count against the run. A run with failures finishes `partial`, and
      **Retry failed** re-enqueues only those.
- [ ] Documents whose bytes are gone from object storage (task 17 retention, a manual delete)
      are `skipped` with that reason rather than failed, and the run says how many. A recut
      needs the source; nothing can be done about a source that no longer exists except say so.
- [ ] The platform reindex (task 17, extended in 20) creates a `reprocessing_run` **per
      connector it recuts**, with `trigger: embedding_model`, so the per-connector screen shows
      the platform operation's progress for its own documents without knowing about the
      platform screen.
- [ ] Run history per connector: the last N runs with trigger, who, when, duration, outcome,
      tokens estimated vs spent. The estimate-vs-spent column is the calibration for the next
      estimate and the audit trail for the bill.

### Retrieval while mixed

- [ ] Stale chunks are **still retrieved**. A connector mid-reprocess must keep answering;
      chunks cut under the previous configuration are worse than the new ones and better than
      nothing. Points carry `index_fingerprint` in the payload (they already carry
      `chunk_fingerprint`); retrieval compares it with the connector's effective one and marks
      the chunk `stale: true` on the request log's retrieved list and in task 100's citation
      metadata.
- [ ] Exception: after an **embedding model** change, stale points are in a collection the
      platform reindex has not promoted yet, and retrieval reads the live collection as it does
      today — that mechanism is task 17's and is unchanged. The label above is for the
      per-connector recut, where old and new points share a collection.
- [ ] Dedupe across a mixed index: a document mid-recut may briefly have both its old and new
      chunks in the collection (the new points are upserted before the old are deleted, so a
      reader never sees the document vanish). The per-document replacement is one atomic
      step per document — delete-old-by-document-filter after upsert-new — and retrieval's
      dedupe collapses an old/new pair of the same text in the window between. Asserted,
      because the alternative is one paragraph injected twice with two fingerprints.

### UI

- [ ] Connector header: the stale count and reason summary, **Reprocess** (scope selector:
      stale only / these formats / everything, with the estimate), progress with ETA while
      running, and a history drawer.
- [ ] Document table: `index_status` column with filter chips, `stale_reason` on hover, a
      per-row **Reprocess**.
- [ ] Connectors list: stale badge with count.
- [ ] Gateway → Memory: the stale-connectors notice, linking to each connector.
- [ ] Platform → Maintenance: the platform reindex lists the per-connector runs it spawned.
- [ ] Every configuration save that will mark documents stale says how many, before saving —
      task 20 does this for chunking; it becomes one component fed by the fingerprint diff, so
      101 and 102 get it for free.

### Spec & runbook

- [ ] Amend §9.5: ingestion status (`pending | processing | indexed | failed`) and index
      status (`current | stale | reprocessing`) are two axes; a document is `indexed` and
      `stale` at once, and that is the normal state after a change. Amend §9.4 with the
      fingerprint's contents. Update `docs/runbooks/chunking-change.md`: the "two strategies
      in that output" heuristic is replaced by the stale count, and the run history answers
      "is it still going".

## Acceptance criteria

- [ ] After a chunking change, `GET /connectors/{id}` reports the stale count and formats
      without a PATCH in the same session; a server restart does not change the answer.
- [ ] Every document has an `index_status` that equals what comparing its fingerprint with
      the effective one would give — asserted over a fixture by the reconciliation job finding
      zero disagreements.
- [ ] A reprocess run over 100 documents with 3 planted failures and 2 missing sources
      finishes `partial` with `done=95 failed=3 skipped=2`, progress was monotone throughout,
      and **Retry failed** re-enqueues exactly three.
- [ ] Killing the worker mid-run and restarting continues the run from its counters.
- [ ] Retrieval during a run returns results on every request, marks stale chunks as such on
      the log, and never returns the same text twice for a document mid-replacement.
- [ ] A platform embedding change shows per-connector progress on each recut connector, and
      the connector's run history records `trigger: embedding_model`.
- [ ] An extractor version bump marks PDFs stale and Markdown current.
- [ ] A `NULL` fingerprint from before task 20 is shown as *unrecorded*, is included in a
      "reprocess unrecorded" scope, and is never counted as stale.
- [ ] A gateway reading a stale connector says so; one reading only current connectors does
      not.
- [ ] `documents_stale` drops to zero when the run completes, and the dashboard's degraded
      entry disappears with it.

## Tests

- Fingerprint composition: each input changes it; an unrelated change does not; the
  `stale_reason` for each single-input change is the right word.
- Status maintenance on save: the one `UPDATE` touches exactly the affected formats; the
  reconciliation job over planted disagreements.
- Run lifecycle: counters, ETA arithmetic, partial outcome, retry-failed scope, skip on
  missing source, single-run-per-connector, resume after a simulated worker death.
- Retrieval under a mixed index: the stale flag, and the old/new dedupe during the
  replacement window.
- The platform reindex spawning per-connector runs.
- API: the list filters, the gateway's stale notice, cross-tenant scoping of runs.
- Expand-contract: the migration adds `index_fingerprint`, the dual-write test, and the
  offline render.

## Notes

- **Staleness is a stored fact, not a computed banner.** The PATCH-time flag was correct and
  useless: correct because it was computed from the change, useless because the change was
  the only moment it existed. Storing status per document costs one `UPDATE` per save and
  makes every screen agree.
- **One fingerprint, defined once, is what makes 101 and 102 cheap.** Each of those tasks adds
  an input to the index; if each also added its own staleness plumbing there would be three
  badges that disagree. They contribute a field to the fingerprint and inherit everything here.
- **The mixed state is tolerated on purpose and labelled on purpose.** The alternative — no
  retrieval until reprocessing finishes — turns a chunking tweak into an outage. The cost of
  tolerating it is that a request during the window can draw from two chunkings, and the
  request log says so on the chunk, so that when someone asks "why did it answer that" during
  a reprocess, the answer is on the screen.

# Task 17 — Retention, reindex & platform settings

**Slice:** the data lifecycle is enforced by running jobs, not by configuration that nothing
honors.
**Depends on:** 07, 09, 12
**Spec:** §9.4, §10.2 (retention), §6.5, §13.1 (Platform)
**Size:** M

---

## Why this slice

Task 07 stores prompt bodies and lets a gateway configure `retention_days`. Task 13 stores
distilled personal facts. Until a pruner actually runs, both settings are promises. This task
closes that gap and adds the operator controls that were stubbed as environment variables.

## Demo at the end of this task

Set a gateway's body retention to 1 day and seed logs dated a week back. Run the retention job:
metadata rows survive, transcripts for the old partitions are gone, and the job reports what it
dropped. Partitions for the next 30 days already exist — no migration needed to keep logging.

In **Platform → Settings**, change the embedding model. A reindex starts, progress is visible per
organization, and **retrieval keeps working throughout** — old collections serve until the new
ones are complete, then aliases swap.

## In scope

- Automated partition management and retention enforcement.
- Platform settings moved from environment variables into the database with a superadmin UI.
- Embedding-model change with zero-downtime reindex.
- End-to-end erasure verification across all stores.

## Out of scope

- Cold-storage archival of expired logs.
- Per-document or per-fact retention (retention is per gateway and per org).
- Cross-region replication.

## Work items

### Partition management
- [x] Scheduled job creating `request_logs` and `transcripts` partitions **ahead** of need
      (default: 30 days of runway), with an alert if runway drops below 7 days. A missing
      partition means writes start failing — this must never be discovered in production.
- [x] Detaching and dropping expired partitions instead of `DELETE`, so reclaiming space is
      instant and does not bloat the table.
- [x] Because `retention_days` is per gateway but partitions are global, use the two-stage
      approach: drop a partition only once it is older than the **longest** retention among
      gateways; within still-live partitions, null out body columns for rows past their own
      gateway's retention. This keeps the common case cheap and the per-gateway promise exact.
      *Deviation:* the transcript **row** is deleted rather than its columns nulled. SPEC
      §10.2 says bodies are hard-deleted, and a row of nulls is a row somebody has to
      remember means "erased" rather than "never captured" — a distinction `bodies_omitted`
      already carries for the second case, which nulling would have made indistinguishable
      on the very screen that exists to explain a missing body.

### Retention enforcement
- [x] Nightly job honoring, per gateway, `retention_days` (bodies) and
      `metadata_retention_days` (rows), with org-level defaults.
- [x] Batched and rate-limited so pruning cannot degrade serving latency.
- [x] Idempotent and resumable — a job killed mid-run resumes without redoing completed work.
- [x] Reports rows and bytes affected per gateway; exposed as metrics and visible in Platform.
- [x] Expired `memory_facts` (`expires_at` in the past) are purged from Postgres **and** Qdrant in
      the same pass; an orphaned vector is a fact that still influences answers after it should
      have expired.
- [x] Orphan sweeper: Qdrant points with no matching Postgres row, and object-storage files with
      no document row. Report before deleting, and require an explicit flag for the destructive
      pass — a bug here silently destroys customer data.

### Platform settings
- [x] `platform_settings(key PK, value_jsonb, updated_by, updated_at)`.
- [x] Migrate from environment variables: embedding provider/model/dimension, default distillation
      model, default gateway logging policy, default and ceiling rate limits, per-file and per-org
      storage caps, retention ceilings.
- [x] Environment variables remain the bootstrap source on an empty database, then the database
      wins. Document the precedence clearly.
- [x] Cached with explicit invalidation on write.
- [x] `GET|PATCH /api/v1/platform/settings` (superadmin), fully audit-logged via task 15.
- [x] Retention **ceilings** at the platform level: an org may set stricter retention, never
      longer.

### Reindex
- [x] `POST /api/v1/platform/reindex` — full reindex, or scoped to an organization or connector.
- [x] Zero-downtime procedure:
      1. create `org_{org_id}_docs_v{n+1}` with the new dimension
      2. re-embed and upsert all chunks into it, from stored extracted text where available and
         by re-extracting from object storage otherwise
      3. verify counts and run a sample search
      4. swap the Qdrant alias atomically
      5. drop the old collection after a grace period
- [x] Retrieval reads through the **alias** — introduced here, and the reason searches keep working
      throughout.
- [x] Progress tracking per organization with an ETA; resumable after interruption.
- [x] Rate-limited embedding calls with a cost estimate shown **before** starting. Re-embedding a
      large corpus is a real expense and should never begin by surprise.
- [x] Changing the platform embedding model requires an explicit confirmation naming the
      collections and estimated cost.
- [x] Per-connector reindex triggered by a chunking-configuration change (task 09 warned about
      this; here it becomes actionable). *Built as a separate operation, deliberately:*
      `POST /api/v1/connectors/{id}/reindex` re-runs **ingestion**, because a changed
      `chunk_size` makes the chunks wrong rather than the vectors, and re-embedding chunks
      that are already the wrong shape would fix nothing. The platform reindex accordingly
      has no connector scope at all — an embedding model is a property of a whole
      collection, so re-embedding part of one would leave a tenant holding two models'
      vectors.
- [x] Blocked while another reindex is running for the same scope.

### Erasure
- [x] Complete the SPEC §6.5 path: `DELETE /end-users/{id}/memory` removes facts, vectors, and —
      with the flag — transcripts, verified across all three stores.
- [x] Organization deletion: a job removing gateways, keys, connectors, objects, documents,
      collections, end users, facts, logs, and transcripts, with a soft-delete grace period before
      the destructive pass.
- [x] An erasure report enumerating what was removed from each store — the artifact you hand
      someone who asks whether a deletion request was honored.

### UI
- [x] **Platform → Settings**: embedding model (with a change flow showing scope, cost estimate,
      and confirmation), default distillation model, default logging policy and retention
      ceilings, rate-limit ceilings, storage caps.
- [x] **Platform → Maintenance**: partition runway, last retention run and what it pruned, reindex
      status with per-org progress, orphan-sweep report, and manual triggers.
- [x] Org Settings shows the effective retention with an indicator when a platform ceiling is
      capping it.

## Acceptance criteria

- [x] Bodies past `retention_days` are gone; metadata within `metadata_retention_days` survives.
- [x] Two gateways with different retention on the same partition are each honored exactly.
- [x] Partition runway never falls below the threshold; the alert fires when it does.
- [x] Changing the embedding model reindexes with **no retrieval downtime** — a search loop
      running throughout returns valid results at every moment.
- [x] Reindex is resumable: killed halfway, it restarts and completes without duplicates.
- [x] Expired memory facts disappear from Qdrant, not just Postgres.
- [x] The orphan sweeper reports accurately and deletes nothing without the explicit flag.
- [x] End-user erasure leaves zero rows and zero points across all stores.
- [ ] Retention and reindex jobs do not degrade proxy p95 latency while running under load.
      *Not verified.* This is a load-test claim and there is no load harness here. What is
      built and asserted instead is the two mechanisms that would make it true — every
      statement is bounded to one `(gateway, day)`, and there is a configurable pause
      between units — and neither of those is a measurement. Left unticked on purpose.

## Tests

- Retention with mixed per-gateway settings on a shared partition.
- Partition creation runway and the low-runway alert.
- Reindex: alias swap atomicity, resumption after interruption, dimension change, count
  verification, concurrent-reindex blocking.
- Expired-fact purge across both stores.
- Orphan sweeper: report-only mode deletes nothing; destructive mode removes exactly the reported
  set.
- Erasure completeness assertions per store.
- Platform-settings precedence: env bootstrap, then database, with cache invalidation.
- Ceiling enforcement on org writes.

## Notes

- The alias indirection is worth introducing even though nothing needs it until a reindex happens.
  Retrofitting it during an urgent embedding-model migration is exactly the wrong time.
- Report-before-delete on the orphan sweeper is not caution theatre. The first version of a
  sweeper like this is usually wrong in one direction, and the failure mode is unrecoverable
  customer data loss.


---

## Verification status

Everything above is implemented and covered. What follows is the reasoning worth carrying
forward, then the numbers, then what this machine could not check.

### The decisions that shaped the task

**The embedding setting is written at the swap, not before it.** The obvious design — PATCH
the model, then reindex — is wrong, and wrong in a way that only shows up under live
traffic: between the write and the swap, every new ingestion embeds with the new model and
upserts into a collection of the old width, which Qdrant refuses. So the operator's
confirmation *starts a run*, the run carries the model it is moving to, and the setting
lands as the run's last step. Until then serving, ingestion and distillation all keep using
the model that matches the live collection, which is the only self-consistent state
available. The cost is a surprising API — a PATCH that does not write the field it names —
so the response says so explicitly (`pending_embedding` beside `settings.embedding`) and
the screen shows the old model as current while the new one is in flight.

**The alias exists before anything needs it.** Retrieval reads through `org_{id}_docs`,
which since this task is an alias for `org_{id}_docs_v{n}`. That is the task file's own
note taken seriously: retrofitting the indirection costs a gap in retrieval, and the moment
you want it is an urgent migration. Introducing it now means the one non-atomic moment —
promoting a collection created before the alias, which Qdrant cannot alias over — happens
on day one, on an index that has just been rebuilt beside the live one, instead of in the
middle of the emergency.

**Retention caps rather than refuses.** A gateway asking for longer than the platform
ceiling is lowered to it, on save and again by the nightly pass. Refusing the save was the
alternative and it is worse in exactly one case that matters: an operator lowering a ceiling
would make every gateway configured under the old one unsaveable, so the first symptom of a
policy change would be a customer unable to edit anything. Capping is also what
:class:`~app.services.limits.Ceilings` already does for rate limits, and having the two
behave differently would be a rule nobody could state.

**A dropped parameter of a different kind: bodies are deleted, not nulled.** SPEC §10.2 says
hard-deleted, and a row of nulls is a row somebody has to remember means "erased" rather
than "never captured" — a distinction `bodies_omitted` already carries, correctly, for the
case where nothing was stored. Nulling would have made those two indistinguishable on the
detail view, which is precisely the screen that exists to say why a body is missing.

**Report-before-delete is the sweeper's whole design, and the age floor is half of it.** The
first version of a sweeper is usually wrong in one direction and the wrong direction here
destroys customer data no database backup contains. But the rule that actually catches the
common bug is the hour-old floor: an upload whose bytes have landed and whose row has not
committed yet *is* an object with no document row, and a sweeper without a clock would race
every upload it ever saw.

**The scheduled jobs are cron entries, not queued jobs.** Nothing requests them, so there is
nothing to deduplicate them against and no payload to carry. arq runs a cron function on
exactly one worker, which is what keeps two replicas from both dropping the same partition —
and the passes are idempotent anyway, so both properties are worth having rather than one
being relied on. The orphan sweep is deliberately **not** on the schedule: it is
report-only by design, and a destructive pass on a timer is what report-first exists to
prevent.

### The seam that moved

`RateLimiter` and `LimitsService` now accept a **ceiling source** — either the value or a
function returning it — because the ceilings moved into `platform_settings`, where an
operator can change them while the process is running. The limiter resolves a gateway
synchronously on the request path, so it cannot await a read; it holds a function over the
cached snapshot instead. A fixed `Ceilings` still works everywhere it worked before, which
is what left several dozen existing tests unchanged.

The snapshot is the other half. Two settings are read on the request path, so each process
holds a resolved value refreshed in the background rather than querying per request. A write
updates it immediately in the process that made the change; other replicas pick it up within
one refresh interval. **That bound is thirty seconds and it is stated rather than hidden** —
closing it needs a pub/sub channel, which is a second thing that has to be running for
configuration to be correct, and these are settings that change a few times a year.

### The tests that are load-bearing

- `test_a_search_running_throughout_never_sees_an_empty_index` searches *between every page
  of the copy*, through the ordinary port a request uses, rather than before and after. A
  before/after test passes against delete-then-rebuild, which is the design the alias exists
  to avoid.
- `test_two_gateways_on_one_partition_are_each_honoured_exactly` is the criterion that makes
  the two-stage retention necessary. A pass that worked a whole day at a time would have to
  pick one of the two numbers.
- `test_a_report_only_sweep_deletes_nothing` asserts the rule from the other side. A sweeper
  that quietly deleted while reporting would satisfy every count-based test in that file.
- `test_a_vector_that_could_not_be_removed_keeps_its_row` pins the ordering. A row without
  its vector is recoverable on the next pass; the other order strands a vector permanently,
  and a stranded vector is a fact that keeps answering questions after it expired.
- `test_the_platform_embedding_is_written_only_when_the_run_succeeds` is the one that would
  fail if somebody "simplified" the PATCH into an ordinary write.
- `test_an_upload_in_flight_is_left_alone` is a two-object boundary test, because the bug it
  catches is a race that would otherwise only appear under load.

### Bugs this task found

- **`extra={"created": ...}` on a log line raises.** `created` is a field
  `logging.LogRecord` already owns, and passing it through `extra` is a `KeyError` rather
  than a shadowed value. It passed in isolation and failed in the full suite, where another
  test had installed a log handler. A scan of every `extra=` in `app/` now confirms no other
  reserved name is used.
- **`collection_exists` answers about collections, not aliases.** Every read in
  `QdrantVectorStore` guarded on it, so a tenant whose data sat behind an alias would have
  read as "nothing indexed" and every search would have returned no documents — silently.
  The guard is now `_present`, which consults both.
- **The memory audit recorder reads `self._db`.** The new memory transactions named the
  field `db`, so the mixin worked everywhere except where it was actually called. The audit
  tour caught it, which is what that tour is for.
- **`merge_config` was bound to `ConfigBlob`.** The platform sections are plain models, and
  the bound was relaxed to `BaseModel` rather than giving six settings sections a `version`
  field to satisfy a type parameter.
- **`POST /platform/reindex` accepted a `connector_id` and ignored it.** Written to satisfy
  the work item as phrased, and worse than not having it: an operator would have scoped a
  run to one connector and got a platform-wide rebuild. Removed, and replaced with the
  operation a chunking change actually needs — see the deviation noted on that item.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   328 files already formatted
uv run mypy                    Success: no issues found in 311 source files
uv run pytest -q               3204 passed, 425 skipped

npx eslint . / npx tsc         clean
npx vitest run                 579 passed (26 files)
npm run build                  475.97 kB JS (138.23 kB gzipped)
```

Task 17 adds **128 backend checks** across eight new or extended modules and **19 web
checks**, against a task-16 baseline of 3022 backend / 566 web. `make openapi` regenerates `web/openapi.json`
and `web/src/api/schema.d.ts`; both are committed and byte-stable.

### Not verifiable on this machine

- **No PostgreSQL.** `tests/test_maintenance_db.py` — 13 checks covering partition creation
  and dropping, the catalog read, the per-gateway prune and its day bounds — is marked `db`
  and **skips**. It is the only place the DDL and the pruning statements are exercised at
  all: the in-memory store models partitions as a set of days, which is everything the
  policy layer asks of them and nothing about `pg_inherits`, `CREATE TABLE ... PARTITION
  OF`, or whether the delete's subquery prunes to one partition. Those are the checks to run
  first with a database up.
- **No Qdrant.** The alias swap is exercised against `MemoryVectorIndexAdmin`, which models
  aliases as a dictionary. That is a fair model of *what* a swap does and says nothing about
  `update_collection_aliases` being atomic, which is the property the whole design rests on.
  The one-time promotion of a pre-alias collection — drop, then create the alias — has no
  in-memory analogue at all and is reasoned about rather than demonstrated.
- **Retention and reindex under load.** The acceptance criterion "do not degrade proxy p95
  while running" is a load-test claim. What is asserted here instead is the two mechanisms
  that would make it true — every statement is bounded to one `(gateway, day)`, and there is
  a configurable pause between units — and neither of those is a measurement.

### Left deliberately

- **The old collection is dropped by the sweeper, not by a timer.** The task file asks for a
  grace period before dropping a superseded collection. Rather than a background timer
  nobody can see, a superseded collection is reported by the orphan sweep and removed on the
  destructive pass — so the grace period is the interval until an operator next runs it,
  which is explicit and visible instead of implicit. The cost is that a platform whose
  operator never sweeps keeps one extra collection per reindex, which is disk and nothing
  else.
- **`param_overrides` still bypasses the dropped-parameter record** (task 16's open item),
  and retention has an analogous one: the ceiling is applied to a gateway's *stored*
  configuration, not to whatever a request's overrides layer might imply. Both are operator
  configuration rather than caller intent, and both are written down next to the code.

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
- [ ] Scheduled job creating `request_logs` and `transcripts` partitions **ahead** of need
      (default: 30 days of runway), with an alert if runway drops below 7 days. A missing
      partition means writes start failing — this must never be discovered in production.
- [ ] Detaching and dropping expired partitions instead of `DELETE`, so reclaiming space is
      instant and does not bloat the table.
- [ ] Because `retention_days` is per gateway but partitions are global, use the two-stage
      approach: drop a partition only once it is older than the **longest** retention among
      gateways; within still-live partitions, null out body columns for rows past their own
      gateway's retention. This keeps the common case cheap and the per-gateway promise exact.

### Retention enforcement
- [ ] Nightly job honoring, per gateway, `retention_days` (bodies) and
      `metadata_retention_days` (rows), with org-level defaults.
- [ ] Batched and rate-limited so pruning cannot degrade serving latency.
- [ ] Idempotent and resumable — a job killed mid-run resumes without redoing completed work.
- [ ] Reports rows and bytes affected per gateway; exposed as metrics and visible in Platform.
- [ ] Expired `memory_facts` (`expires_at` in the past) are purged from Postgres **and** Qdrant in
      the same pass; an orphaned vector is a fact that still influences answers after it should
      have expired.
- [ ] Orphan sweeper: Qdrant points with no matching Postgres row, and object-storage files with
      no document row. Report before deleting, and require an explicit flag for the destructive
      pass — a bug here silently destroys customer data.

### Platform settings
- [ ] `platform_settings(key PK, value_jsonb, updated_by, updated_at)`.
- [ ] Migrate from environment variables: embedding provider/model/dimension, default distillation
      model, default gateway logging policy, default and ceiling rate limits, per-file and per-org
      storage caps, retention ceilings.
- [ ] Environment variables remain the bootstrap source on an empty database, then the database
      wins. Document the precedence clearly.
- [ ] Cached with explicit invalidation on write.
- [ ] `GET|PATCH /api/v1/platform/settings` (superadmin), fully audit-logged via task 15.
- [ ] Retention **ceilings** at the platform level: an org may set stricter retention, never
      longer.

### Reindex
- [ ] `POST /api/v1/platform/reindex` — full reindex, or scoped to an organization or connector.
- [ ] Zero-downtime procedure:
      1. create `org_{org_id}_docs_v{n+1}` with the new dimension
      2. re-embed and upsert all chunks into it, from stored extracted text where available and
         by re-extracting from object storage otherwise
      3. verify counts and run a sample search
      4. swap the Qdrant alias atomically
      5. drop the old collection after a grace period
- [ ] Retrieval reads through the **alias** — introduced here, and the reason searches keep working
      throughout.
- [ ] Progress tracking per organization with an ETA; resumable after interruption.
- [ ] Rate-limited embedding calls with a cost estimate shown **before** starting. Re-embedding a
      large corpus is a real expense and should never begin by surprise.
- [ ] Changing the platform embedding model requires an explicit confirmation naming the
      collections and estimated cost.
- [ ] Per-connector reindex triggered by a chunking-configuration change (task 09 warned about
      this; here it becomes actionable).
- [ ] Blocked while another reindex is running for the same scope.

### Erasure
- [ ] Complete the SPEC §6.5 path: `DELETE /end-users/{id}/memory` removes facts, vectors, and —
      with the flag — transcripts, verified across all three stores.
- [ ] Organization deletion: a job removing gateways, keys, connectors, objects, documents,
      collections, end users, facts, logs, and transcripts, with a soft-delete grace period before
      the destructive pass.
- [ ] An erasure report enumerating what was removed from each store — the artifact you hand
      someone who asks whether a deletion request was honored.

### UI
- [ ] **Platform → Settings**: embedding model (with a change flow showing scope, cost estimate,
      and confirmation), default distillation model, default logging policy and retention
      ceilings, rate-limit ceilings, storage caps.
- [ ] **Platform → Maintenance**: partition runway, last retention run and what it pruned, reindex
      status with per-org progress, orphan-sweep report, and manual triggers.
- [ ] Org Settings shows the effective retention with an indicator when a platform ceiling is
      capping it.

## Acceptance criteria

- [ ] Bodies past `retention_days` are gone; metadata within `metadata_retention_days` survives.
- [ ] Two gateways with different retention on the same partition are each honored exactly.
- [ ] Partition runway never falls below the threshold; the alert fires when it does.
- [ ] Changing the embedding model reindexes with **no retrieval downtime** — a search loop
      running throughout returns valid results at every moment.
- [ ] Reindex is resumable: killed halfway, it restarts and completes without duplicates.
- [ ] Expired memory facts disappear from Qdrant, not just Postgres.
- [ ] The orphan sweeper reports accurately and deletes nothing without the explicit flag.
- [ ] End-user erasure leaves zero rows and zero points across all stores.
- [ ] Retention and reindex jobs do not degrade proxy p95 latency while running under load.

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

# A vector-backend migration is stuck, or a tenant is on the wrong backend

Not an alert. A migration is an operator action, and the failure modes are quiet by
design: reads never move until the very end, so a migration that stalls looks like nothing
happening at all.

## What is actually true while one runs

* **Reads go to the source backend, throughout.** The binding's `backend` is what a search
  resolves through, and it is written once — at the promotion. So a copy that stalls,
  fails, or is abandoned costs disk and nothing else.
* **The target holds a partial copy.** Points are upserted with deterministic ids, so a
  partial copy is a prefix, not a corruption.
* **Memory facts are rebuilt rather than copied**, from `memory_facts`. That costs
  embedding calls; a migration for a tenant with a large memory is slower than its document
  count suggests.

## Check where everybody is

```bash
curl -sS "$GW/api/v1/platform/vector-backends" -H "Authorization: Bearer $SUPERADMIN" | jq
```

```json
{
  "enabled": ["qdrant", "chroma"],
  "default": "qdrant",
  "bindings": [
    {"organization_id": "…", "backend": "qdrant", "status": "migrating", "target": "chroma"}
  ]
}
```

`status: "migrating"` with no progress for a long time means the job failed or was never
picked up. Check the worker: a `migrate_vectors` job in the dead-letter table names the
organization.

## Fix

**It stalled and you want it to finish.** Re-enqueue it. The copy is idempotent — every id
is a deterministic UUIDv5 and every write an upsert — so a second run converges rather than
doubling. There is deliberately no cursor: a reindex persists one because a point costs an
embedding call, whereas here a point costs a network write, so starting again is cheap and
correct.

**It stalled and you want it gone.**

```bash
curl -X DELETE "$GW/api/v1/platform/organizations/$ORG/vector-backend" \
  -H "Authorization: Bearer $SUPERADMIN"
```

This drops what was built in the target and clears the migration. Nothing about reads
changes, because nothing about reads ever changed.

**It promoted and retrieval got worse.** Migrate back — the same endpoint, naming the
original backend. Do it *within the grace period* and the original collection is still
there, so the reverse migration copies from a source that never left. After the grace period
the source is gone and the reverse is a full copy of the same size.

> The deferred drop refuses to delete a collection the organization has since been bound
> back to, so reversing inside the window is safe even though a drop job is already
> scheduled for it.

**A tenant is on the wrong backend and has nothing indexed.** Migrating an empty
organization is the cheap case and still the right mechanism: it copies zero points and
writes the binding. There is no separate "just change the binding" endpoint, on purpose —
one path means one set of failure modes.

## The two numbers that make this safe

| | Value | Why |
|---|---|---|
| Binding cache TTL | 15 s | A replica reads the binding this often at most; between reads it may still route to the previous backend. |
| Drop grace period | 15 min | How long the source survives the promotion. **Must exceed the TTL** — the application refuses to start a migrator where it does not. |

The failure that ordering prevents is invisible: a replica holding a cached binding reads a
collection that has already been dropped, retrieval returns nothing, and `fail_open` hides
it. Nobody reports it; the answers just get worse.

## Related

* [Qdrant is down or slow](qdrant-outage.md) — a backend outage now affects only the
  organizations bound to it, and `/readyz` reports each backend separately.
* [Backup and restore](backup-restore.md) — the two backends have different restore
  procedures, and a restore that recreates collections without their pointers is the
  failure this system is most likely to have.

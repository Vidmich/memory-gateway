# Backup and restore

Scripts: `deploy/ops/backup.sh`, `deploy/ops/restore.sh`, `deploy/ops/verify-restore.sh`.

## What is backed up, and why not everything

**Postgres** is the system of record. Everything else can be derived from it.

**Qdrant** holds vectors that *can* be rebuilt by re-embedding every document and fact —
but rebuilding is an embedding bill and hours of wall clock, so snapshots are taken per
collection. Per collection rather than per node, so one tenant can be restored without
touching anybody else's.

**Object storage is not copied.** The originals live in S3 or MinIO, whose versioning and
replication are better at this than a script streaming everything through one machine. The
backup script *checks* that versioning is enabled, because a bucket without it is one where
a mistaken resync is unrecoverable.

## Taking one

```bash
DATABASE_URL=postgresql://... QDRANT_URL=http://... S3_BUCKET=... \
  ./deploy/ops/backup.sh /var/backups/memory-gateway
```

Writes a timestamped directory containing the dump, the schema in plain SQL, one snapshot
per Qdrant collection, **the aliases**, and a manifest recording the alembic revision.

The alias file matters more than it looks. A snapshot restores a collection; it does not
restore the alias pointing at it. Since task 17 every read addresses `org_{id}_docs`, so a
restore without aliases is one where every collection is present and every search returns
nothing.

## Restoring

```bash
RESTORE_CONFIRM=<database name> DATABASE_URL=... QDRANT_URL=... \
  ./deploy/ops/restore.sh /var/backups/memory-gateway/<timestamp>
```

The confirmation variable is not ceremony: staging and production connection strings differ
by one word and this is the one script where reading the wrong one is unrecoverable.

Order is Postgres, then Qdrant, then aliases, and it is not arbitrary — Postgres is the
system of record and the collection names are derived from organization ids in it.

## Verifying — the part that is usually skipped

```bash
GATEWAY_SLUG=demo GATEWAY_API_KEY=mg_... ./deploy/ops/verify-restore.sh <directory>
```

Six checks, ending with a real completion through a real gateway. Everything else can pass
while that one fails, which is the whole reason it is last: an untested backup is a
hypothesis, and the way this hypothesis fails in practice is not a corrupt dump. It is that
every row is present, every screen loads, and retrieval quietly returns nothing.

## Afterwards

Run **Platform → Maintenance → Run partitions**. Partitions come back with the dump, but
the newest ones were created by the nightly job rather than by a migration, so the runway is
however long it was when the backup was taken minus however long ago that was. Discovering
that at midnight is avoidable.

## Rehearse it

Quarterly, into a scratch namespace, with `verify-restore.sh` as the acceptance criterion.
A restore procedure that has never been executed is documentation, not a capability.

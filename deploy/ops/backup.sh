#!/usr/bin/env bash
# Back up the two stores that hold state this deployment cannot rebuild.
#
#   ./deploy/ops/backup.sh /var/backups/memory-gateway
#
# **Postgres** is the system of record: organizations, gateways, keys, documents, memory
# facts, request logs. Everything else can be derived from it.
#
# **Qdrant** holds vectors that *can* be rebuilt — reindexing every document and every fact
# reproduces them — but rebuilding is an embedding bill and hours of wall clock, and a
# restore that comes back with no retrieval is a restore that has not restored the product.
# So it is backed up, and the runbook says which of the two options to take under time
# pressure.
#
# **Object storage is not backed up here.** The original uploads live in S3 or MinIO, whose
# own versioning and cross-region replication are better at this than a shell script that
# streams them through one machine. What this script does is *check* that the bucket is
# versioned, because a bucket that is not is one where a bad `resync` is unrecoverable.
#
# Requires: pg_dump (16+), curl, jq, and either aws or mc for the bucket check.
set -euo pipefail

DEST="${1:?usage: backup.sh <destination-directory>}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/${STAMP}"

: "${DATABASE_URL:?set DATABASE_URL (the postgresql:// form, not +asyncpg)}"
: "${QDRANT_URL:?set QDRANT_URL}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"

mkdir -p "${OUT}"
echo "backing up into ${OUT}"

# --- postgres --------------------------------------------------------------
# Custom format, compressed, so `pg_restore` can do a parallel restore and can be told to
# skip individual tables — which is what makes "restore everything except the request logs"
# possible when time matters more than history.
echo "  postgres..."
pg_dump \
  --format=custom \
  --compress=6 \
  --no-owner \
  --no-privileges \
  --file="${OUT}/postgres.dump" \
  "${DATABASE_URL}"

# The schema on its own as well, in plain SQL. It is small, it is diffable, and it answers
# "what did the schema look like before the migration that broke things" without a restore.
pg_dump --schema-only --no-owner --no-privileges --file="${OUT}/schema.sql" "${DATABASE_URL}"

# --- qdrant ----------------------------------------------------------------
# A snapshot per collection rather than one for the node: collections are per organization
# (`org_{id}_docs_v{n}` and the alias in front of it), and a per-collection snapshot is what
# lets a single tenant be restored without touching anybody else's vectors.
echo "  qdrant..."
auth=()
[[ -n "${QDRANT_API_KEY}" ]] && auth=(-H "api-key: ${QDRANT_API_KEY}")

mkdir -p "${OUT}/qdrant"
collections="$(curl -sf "${auth[@]}" "${QDRANT_URL}/collections" | jq -r '.result.collections[].name')"
for collection in ${collections}; do
  echo "    ${collection}"
  name="$(curl -sf "${auth[@]}" -X POST "${QDRANT_URL}/collections/${collection}/snapshots" \
    | jq -r '.result.name')"
  curl -sf "${auth[@]}" \
    -o "${OUT}/qdrant/${collection}__${name}" \
    "${QDRANT_URL}/collections/${collection}/snapshots/${name}"
  # Taken off the node once it is safely here: Qdrant keeps snapshots on its own disk, and
  # a backup that fills the volume it is backing up is a novel way to cause an outage.
  curl -sf "${auth[@]}" -X DELETE \
    "${QDRANT_URL}/collections/${collection}/snapshots/${name}" >/dev/null
done

# The aliases, separately. A snapshot restores a collection, not the alias pointing at it,
# and after a reindex the alias is the only thing that knows which version is live.
curl -sf "${auth[@]}" "${QDRANT_URL}/aliases" > "${OUT}/qdrant/aliases.json"

# --- object storage: a check, not a copy ------------------------------------
echo "  object storage (checking versioning)..."
if command -v aws >/dev/null 2>&1 && [[ -n "${S3_BUCKET:-}" ]]; then
  status="$(aws s3api get-bucket-versioning --bucket "${S3_BUCKET}" --output text 2>/dev/null || true)"
  if [[ "${status}" != *Enabled* ]]; then
    echo "    WARNING: versioning is not enabled on ${S3_BUCKET}." >&2
    echo "    Deleted or overwritten uploads are unrecoverable. Enable it." >&2
  else
    echo "    versioning enabled"
  fi
else
  echo "    skipped (no aws CLI or S3_BUCKET unset)"
fi

# --- manifest ---------------------------------------------------------------
# What was taken, from where, and under which schema revision. A dump whose alembic
# revision is unknown is a dump somebody has to guess the application version for.
revision="$(pg_dump --version >/dev/null && psql "${DATABASE_URL}" -tAc \
  'select version_num from alembic_version' 2>/dev/null || echo unknown)"

cat > "${OUT}/manifest.json" <<JSON
{
  "taken_at": "${STAMP}",
  "alembic_revision": "${revision}",
  "qdrant_url": "${QDRANT_URL}",
  "collections": $(echo "${collections}" | jq -R . | jq -s .),
  "host": "$(hostname)"
}
JSON

echo
echo "done: ${OUT}"
echo "An untested backup is a hypothesis. Run ./deploy/ops/verify-restore.sh ${OUT}"

#!/usr/bin/env bash
# Restore a backup taken by backup.sh.
#
#   ./deploy/ops/restore.sh /var/backups/memory-gateway/20260908T031500Z
#
# Destructive: it drops and recreates the target database. It therefore refuses to run
# unless RESTORE_CONFIRM is set to the target database name — the guard exists because the
# environment variables for staging and production differ by one word, and this is the one
# script where reading the wrong one is unrecoverable.
#
#   RESTORE_CONFIRM=gateway_staging ./deploy/ops/restore.sh <directory>
#
# Order matters and is not arbitrary. Postgres first, because it is the system of record
# and because the collection names Qdrant needs are derived from organization ids in it.
# Qdrant second. Aliases last: a snapshot restores a collection, and until the alias points
# at it every search reads nothing.
set -euo pipefail

SRC="${1:?usage: restore.sh <backup-directory>}"
[[ -f "${SRC}/postgres.dump" ]] || { echo "no postgres.dump in ${SRC}" >&2; exit 2; }

: "${DATABASE_URL:?set DATABASE_URL (the postgresql:// form, not +asyncpg)}"
: "${QDRANT_URL:?set QDRANT_URL}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"

database="$(python - "$DATABASE_URL" <<'PY'
import sys, urllib.parse
print(urllib.parse.urlsplit(sys.argv[1]).path.lstrip("/"))
PY
)"

if [[ "${RESTORE_CONFIRM:-}" != "${database}" ]]; then
  echo "refusing: set RESTORE_CONFIRM=${database} to confirm the target database" >&2
  exit 2
fi

echo "restoring ${SRC} into ${database}"
jq -r '"  taken at \(.taken_at), schema \(.alembic_revision)"' "${SRC}/manifest.json" 2>/dev/null || true

# --- postgres --------------------------------------------------------------
# `--clean --if-exists` rather than dropping the database itself: the role, the extensions
# and any grants outside the dump survive, and the application's own objects are replaced.
# `--exit-on-error` because a restore that half worked is worse than one that failed.
echo "  postgres..."
pg_restore \
  --dbname="${DATABASE_URL}" \
  --clean --if-exists \
  --no-owner --no-privileges \
  --exit-on-error \
  --jobs=4 \
  "${SRC}/postgres.dump"

# Partitions come back with the dump, but the newest ones were created by last night's job
# rather than by a migration — so the runway is however many days it had when the backup
# was taken, minus however long ago that was. Run the partition pass now rather than
# discovering it at midnight.
echo "  partitions (bringing the runway forward)..."
python -m app.cli --help >/dev/null 2>&1 && echo "    run: POST /api/v1/platform/maintenance/partitions once the API is up"

# --- qdrant ----------------------------------------------------------------
echo "  qdrant..."
auth=()
[[ -n "${QDRANT_API_KEY}" ]] && auth=(-H "api-key: ${QDRANT_API_KEY}")

shopt -s nullglob
for snapshot in "${SRC}"/qdrant/*__*; do
  file="$(basename "${snapshot}")"
  collection="${file%%__*}"
  echo "    ${collection}"
  curl -sf "${auth[@]}" -X POST \
    -H 'Content-Type: multipart/form-data' \
    -F "snapshot=@${snapshot}" \
    "${QDRANT_URL}/collections/${collection}/snapshots/upload?priority=snapshot" >/dev/null
done

# --- aliases ---------------------------------------------------------------
# Last, and separately, because this is what makes the collections readable. Since task 17
# every read goes through `org_{id}_docs`, which is an alias for `org_{id}_docs_v{n}`; a
# restore that stops before this point is one where every search returns nothing and
# everything else looks fine.
if [[ -f "${SRC}/qdrant/aliases.json" ]]; then
  echo "  aliases..."
  actions="$(jq '{actions: [.result.aliases[] | {create_alias: {collection_name: .collection_name, alias_name: .alias_name}}]}' "${SRC}/qdrant/aliases.json")"
  curl -sf "${auth[@]}" -X POST \
    -H 'Content-Type: application/json' \
    -d "${actions}" \
    "${QDRANT_URL}/collections/aliases" >/dev/null
fi

echo
echo "restored. Now verify it rather than assuming it:"
echo "  ./deploy/ops/verify-restore.sh ${SRC}"

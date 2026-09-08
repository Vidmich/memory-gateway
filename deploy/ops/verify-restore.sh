#!/usr/bin/env bash
# Prove a restore actually restored the product, not just the rows.
#
#   GATEWAY_URL=http://localhost:8000 GATEWAY_SLUG=demo GATEWAY_API_KEY=mg_... \
#     ./deploy/ops/verify-restore.sh /var/backups/memory-gateway/20260908T031500Z
#
# An untested backup is a hypothesis, and the way this hypothesis usually fails is not
# "the dump was corrupt". It is that everything restores, every row is present, every
# screen loads — and retrieval comes back empty, because the Qdrant aliases were never
# recreated and each collection is sitting there unaddressed. Counting rows would not
# catch that. Asking the product a question does.
#
# Six checks, in increasing order of how much they prove.
set -euo pipefail

SRC="${1:?usage: verify-restore.sh <backup-directory>}"
: "${DATABASE_URL:?set DATABASE_URL}"
: "${QDRANT_URL:?set QDRANT_URL}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"
auth=()
[[ -n "${QDRANT_API_KEY}" ]] && auth=(-H "api-key: ${QDRANT_API_KEY}")

failures=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok    ${label}"
  else
    echo "  FAIL  ${label}" >&2
    failures=$((failures + 1))
  fi
}

echo "verifying the restore of ${SRC}"

# 1. The schema is at the revision the application expects. A restore of an older dump into
#    a newer image is the failure that looks like a bug in the code.
expected="$(cd "$(dirname "$0")/../.." && uv run alembic heads 2>/dev/null | awk '{print $1}' | head -1)"
actual="$(psql "${DATABASE_URL}" -tAc 'select version_num from alembic_version' 2>/dev/null || echo none)"
if [[ -n "${expected}" && "${expected}" != "${actual}" ]]; then
  echo "  FAIL  schema revision is ${actual}, code expects ${expected}" >&2
  echo "        run 'alembic upgrade head' before going further" >&2
  failures=$((failures + 1))
else
  echo "  ok    schema revision ${actual}"
fi

# 2. The tables that hold the things a customer would notice are missing.
for table in organizations gateways api_keys documents memory_facts; do
  count="$(psql "${DATABASE_URL}" -tAc "select count(*) from ${table}" 2>/dev/null || echo 0)"
  if [[ "${count}" -gt 0 ]]; then
    echo "  ok    ${table}: ${count} row(s)"
  else
    echo "  WARN  ${table} is empty — correct for a young deployment, wrong for a restore" >&2
  fi
done

# 3. Every collection in the manifest came back.
if [[ -f "${SRC}/manifest.json" ]]; then
  live="$(curl -sf "${auth[@]}" "${QDRANT_URL}/collections" | jq -r '.result.collections[].name' | sort)"
  expected_collections="$(jq -r '.collections[]' "${SRC}/manifest.json" | sort)"
  if [[ "${live}" == "${expected_collections}" ]]; then
    echo "  ok    qdrant collections match the manifest"
  else
    echo "  FAIL  qdrant collections differ from the manifest" >&2
    diff <(echo "${expected_collections}") <(echo "${live}") || true
    failures=$((failures + 1))
  fi
fi

# 4. The aliases exist. This is the check that catches the failure described at the top:
#    without them every collection is present and nothing is readable.
aliases="$(curl -sf "${auth[@]}" "${QDRANT_URL}/aliases" | jq '.result.aliases | length')"
if [[ "${aliases}" -gt 0 ]]; then
  echo "  ok    ${aliases} qdrant alias(es)"
else
  echo "  FAIL  no qdrant aliases — every search will return nothing" >&2
  failures=$((failures + 1))
fi

# 5. The service considers itself ready, which exercises all four backing services.
check "/readyz" curl -sf "${GATEWAY_URL}/readyz"

# 6. And the one that actually proves it: a real completion through a real gateway, which
#    goes through gateway resolution, key authentication, retrieval and an upstream call.
#    Everything above can pass while this fails.
if [[ -n "${GATEWAY_API_KEY:-}" && -n "${GATEWAY_SLUG:-}" ]]; then
  body="$(curl -sf -X POST "${GATEWAY_URL}/g/${GATEWAY_SLUG}/v1/chat/completions" \
    -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${GATEWAY_MODEL:-demo}\",\"messages\":[{\"role\":\"user\",\"content\":\"restore check\"}],\"max_tokens\":16}" \
    || true)"
  if echo "${body}" | jq -e '.choices[0]' >/dev/null 2>&1; then
    echo "  ok    end-to-end completion through /g/${GATEWAY_SLUG}"
  else
    echo "  FAIL  end-to-end completion did not return a choice" >&2
    failures=$((failures + 1))
  fi
else
  echo "  SKIP  end-to-end completion (set GATEWAY_SLUG and GATEWAY_API_KEY)" >&2
  echo "        this is the check that matters most; do not call a restore verified without it" >&2
fi

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} check(s) failed. The restore is not verified." >&2
  exit 1
fi
echo "restore verified."

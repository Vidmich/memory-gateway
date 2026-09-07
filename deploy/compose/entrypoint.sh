#!/usr/bin/env bash
# Container entrypoint.
#
# RUN_MIGRATIONS=1 applies migrations before starting. That is a *development*
# convenience: in production migrations run as a Helm pre-upgrade hook (task 18), because
# several replicas racing `alembic upgrade` is a good way to corrupt a schema.
set -euo pipefail

if [[ "${RUN_MIGRATIONS:-0}" == "1" ]]; then
  echo "running migrations..."
  alembic upgrade head
fi

exec "$@"

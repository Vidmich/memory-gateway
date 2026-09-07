# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 01 complete** — a running, healthy service with its infrastructure
stack. No domain features yet; task 02 adds the pass-through proxy.

## Quick start (Docker)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
curl localhost:8000/healthz   # {"status":"ok","version":"0.1.0"}
curl localhost:8000/readyz    # {"postgres":"ok","redis":"ok","qdrant":"ok","storage":"ok"}
```

`make up` is the same thing. The stack brings up Postgres, Redis, Qdrant, MinIO (with its
bucket created), and a placeholder web container that task 03 replaces with the SPA.

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/) and the backing services reachable at the
addresses in `.env`.

```bash
cp .env.example .env
make install
make migrate
make dev
```

## Development

```bash
make check      # lint + typecheck + tests, the same three gates CI runs
make test       # pytest
make lint       # ruff check + format --check
make typecheck  # mypy, strict
make format     # apply fixes
```

`make help` lists every target. Without `make` installed (common on Windows) every target
is a one-liner you can run directly — e.g. `uv run pytest`, `uv run mypy`,
`uv run ruff check .`.

### Tests and the database

Tests marked `db` build a throwaway database by running the **migrations** — not
`metadata.create_all` — so a broken migration fails the suite rather than passing against
a schema no deployment will ever have. With no PostgreSQL reachable they skip, so the
suite still runs with the stack down. CI sets `REQUIRE_DB_TESTS=1`, which turns that skip
into a failure.

## Layout

```
app/
  api/        routers — health now, proxy/ and control/ from task 02
  core/       config, logging, errors, ids, metrics, middleware, clients
  db/         engine, session, declarative base, models
  services/   business logic
  workers/    background jobs (task 09)
  adapters/   upstream dialects (task 02)
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the SPA (task 03)
```

## Configuration

Everything is an environment variable, validated at import time: a missing or malformed
value stops the process immediately and names the variable, rather than surfacing on the
first request. See [.env.example](.env.example) for the full list.

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Checks nothing else — a dependency outage must not get the pod restarted into the same outage. |
| `GET /readyz` | Readiness. Probes Postgres, Redis, Qdrant, and object storage concurrently; 503 names what is broken. |
| `GET /metrics` | Prometheus. Request counts and latency labelled by route template. |

Every log line is one JSON object carrying `request_id`, which is also returned as
`X-Gateway-Request-Id` and honoured on the way in for cross-system correlation.

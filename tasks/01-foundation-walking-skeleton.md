# Task 01 — Project foundation & walking skeleton

**Slice:** a running, healthy service with its whole infrastructure stack up.
**Depends on:** nothing.
**Spec:** §4.3, §15.1, §15.3, §10.5
**Size:** M

---

## Why this slice

Everything downstream needs a place to live, a database to migrate, and a way to start the
stack. This task deliberately builds *no features* — it builds the ability to add features
without re-plumbing anything. All five backing services come up now, even though most are unused
until later tasks, so no future task has to touch Compose.

## Demo at the end of this task

```bash
docker compose up -d
curl localhost:8000/healthz     # {"status":"ok","version":"0.1.0"}
curl localhost:8000/readyz      # {"postgres":"ok","redis":"ok","qdrant":"ok","storage":"ok"}
make test lint typecheck        # all green
```

Killing the Postgres container makes `/readyz` return 503 with the failing dependency named,
while `/healthz` stays 200.

## In scope

- Repository layout, dependency management, tooling, CI.
- FastAPI application factory, configuration, logging, error handling.
- Database connectivity and migration framework (no domain tables yet).
- Docker Compose with all backing services.

## Out of scope

- Any domain model, any endpoint beyond health checks, any UI (task 03), any auth (task 03).

## Work items

### Repository & tooling
- [ ] Layout:
      ```
      app/            # Python package
        api/          # routers: proxy/, control/
        core/         # config, logging, errors, security primitives
        db/           # session, base, models/
        services/     # business logic
        workers/      # background jobs (populated in task 09)
        adapters/     # upstream dialects (populated in task 02)
      migrations/     # alembic
      tests/
      web/            # React SPA (populated in task 03)
      deploy/         # compose, helm (populated in task 18)
      ```
- [ ] `pyproject.toml` with `uv`; Python 3.12 pinned via `.python-version`.
- [ ] `ruff` (lint + format), `mypy` in strict mode for `app/`, `pytest` + `pytest-asyncio`.
- [ ] `Makefile`: `dev`, `test`, `lint`, `typecheck`, `migrate`, `revision`, `seed`, `clean`.
- [ ] Pre-commit hooks running ruff and mypy on staged files.
- [ ] CI (GitHub Actions): lint → typecheck → test with a Postgres service container. Fails the
      build on any of the three.

### Application core
- [ ] `create_app()` factory so tests build isolated app instances.
- [ ] `Settings` (Pydantic Settings) reading env vars, **validated at import time** — a missing
      `DATABASE_URL` must crash at startup with a readable message, not at first request.
      Required: `DATABASE_URL`, `REDIS_URL`, `QDRANT_URL`, `S3_ENDPOINT`/`S3_BUCKET`/creds,
      `ENCRYPTION_MASTER_KEY`, `JWT_SIGNING_KEY`, `PUBLIC_BASE_URL`.
- [ ] Structured JSON logging with a `request_id` bound per request via contextvar, emitted on
      every log line and returned as `X-Gateway-Request-Id`.
- [ ] Request-id middleware: accept an inbound id or generate a UUIDv7.
- [ ] Global exception handlers producing a consistent error envelope. Note: the proxy routes
      need the *OpenAI* error shape instead — task 02 registers its own handler for that subtree.
- [ ] `AppError` hierarchy (`NotFound`, `Conflict`, `Forbidden`, `Unauthorized`, `Validation`,
      `UpstreamError`) mapped to status codes in one place.

### Database
- [ ] Async SQLAlchemy 2.0 engine + session factory; `get_session` FastAPI dependency with
      per-request transaction and rollback on exception.
- [ ] Declarative `Base` with shared mixins: `id` (UUIDv7 primary key), `created_at`,
      `updated_at`.
- [ ] Alembic configured for async, with an empty baseline revision.
- [ ] Test fixtures: a per-test-session database created from migrations (not `create_all`, so
      migrations are exercised) and a transaction-rollback fixture per test.

### Health
- [ ] `GET /healthz` — liveness only, no dependency checks, always cheap.
- [ ] `GET /readyz` — checks Postgres (`SELECT 1`), Redis (`PING`), Qdrant (collections list),
      and object storage (bucket head). Returns 503 with a per-dependency status map on failure.
- [ ] `GET /metrics` — Prometheus endpoint with default process and HTTP metrics registered.

### Compose
- [ ] `deploy/compose/docker-compose.yml` with `api`, `postgres` (16), `redis` (7),
      `qdrant`, `minio` (+ a one-shot bucket-create job), and a placeholder `web` service.
- [ ] Healthchecks on every service; `api` `depends_on` them with `condition: service_healthy`.
- [ ] `api` runs migrations on start in dev (an entrypoint flag, never in prod — see task 18).
- [ ] Hot reload of `app/` via a bind mount.
- [ ] `.env.example` documenting every variable, with dev-safe defaults.

## Acceptance criteria

- [ ] A clean clone reaches a passing demo with only `docker compose up`.
- [ ] `make test lint typecheck` passes with zero warnings suppressed.
- [ ] Startup with a missing required env var exits non-zero within 2 seconds and names the
      variable.
- [ ] Every log line is valid JSON and carries `request_id`.
- [ ] `/readyz` correctly reports a downed dependency by name.
- [ ] CI runs the full suite on pull requests.

## Tests

- Settings validation: missing and malformed values raise at construction.
- Health endpoints: liveness, readiness happy path, readiness with a mocked-down dependency.
- Error handler mapping for each `AppError` subclass.
- Migration round-trip: `upgrade head` then `downgrade base` runs clean.

## Notes

- Use **UUIDv7** for primary keys: index locality of sequential ids without exposing row counts,
  and it sorts by creation time, which matters for the log tables in task 07.
- Do not add a domain model here even if it feels convenient. The next task owns the first tables
  and will define the FK topology.

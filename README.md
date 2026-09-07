# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 03 complete** — the OpenAI-compatible proxy works end to end, and
there is a web UI you can sign into. Configuration screens start in task 05; until then
gateways are created with `make seed`.

## Quick start (Docker)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
curl localhost:8000/healthz   # {"status":"ok","version":"0.1.0"}
curl localhost:8000/readyz    # {"postgres":"ok","redis":"ok","qdrant":"ok","storage":"ok"}
```

`make up` is the same thing. The stack brings up Postgres, Redis, Qdrant, MinIO (with its
bucket created), the API on :8000, and the Vite dev server on :5173.

## Sign in

`make seed` creates a platform superadmin and prints a generated password **once**.

```bash
make seed
open http://localhost:5173
```

The UI is a React SPA. In development Vite serves it and proxies `/api` to the API, so
the browser only ever sees one origin; in production the API serves the built assets
itself, which keeps the refresh cookie same-origin and puts one thing on the deploy path
instead of two that can drift apart.

Sessions are a short-lived access token held **in memory** — never `localStorage`, which
any injected script can read — plus a rotating refresh token in an httpOnly cookie.
Replaying a refresh token that has already been spent revokes the whole session family,
on the assumption that a token used twice has been copied.

## Try the proxy

With `OPENAI_API_KEY` set, `make seed` also creates a demo organization, upstream model,
gateway and API key. The provider key is encrypted with `ENCRYPTION_MASTER_KEY` before it
is stored, and the gateway key's plaintext is printed once and never again.

```bash
OPENAI_API_KEY=sk-... make seed
```

Then point any OpenAI client at it, changing only `base_url`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/g/demo/v1", api_key="mg_...")

print(client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)

for chunk in client.chat.completions.create(
        model="demo", messages=[{"role": "user", "content": "count to 5"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

`model` is the gateway's slug, not the provider's model name: that indirection is the
point, so the endpoint can be repointed at a different provider without the client
changing. `make seed --no-auth`-style local providers (Ollama, vLLM) work by setting
`OPENAI_BASE_URL` and passing `--no-auth` to `python -m app.cli seed`.

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
make check      # everything CI runs, backend and frontend
make test       # pytest
make lint       # ruff check + format --check
make typecheck  # mypy, strict
make format     # apply fixes

make check-web  # eslint + tsc + vitest + openapi drift + production build
make web        # Vite dev server on :5173, proxying /api to :8000
make openapi    # regenerate the typed API client from the live FastAPI schema
```

`make help` lists every target. Without `make` installed (common on Windows) every target
is a one-liner you can run directly — e.g. `uv run pytest`, `uv run mypy`,
`uv run ruff check .`, `npm --prefix web run test`.

### Tests

Tests marked `live` call a real provider and are skipped unless `OPENAI_API_KEY` is set;
run them deliberately with `uv run pytest -m live`. They cost a few tokens.

The frontend has unit tests (`make test-web`) that run anywhere, and a Playwright suite
(`make e2e`) covering log in → reload → log out in a real browser. The latter needs the
stack up and the seeded password:

```bash
make up && make seed
E2E_PASSWORD=<the printed password> make e2e
```

### Tests and the database

Tests marked `db` build a throwaway database by running the **migrations** — not
`metadata.create_all` — so a broken migration fails the suite rather than passing against
a schema no deployment will ever have. With no PostgreSQL reachable they skip, so the
suite still runs with the stack down. CI sets `REQUIRE_DB_TESTS=1`, which turns that skip
into a failure.

### The typed API client

`web/src/api/schema.d.ts` is generated from the server's OpenAPI schema, and the types the
app actually uses are aliased from it in `web/src/api/types.ts`. Rename a field on the
server and the frontend stops compiling, rather than rendering `undefined`. CI regenerates
and fails if the committed copy has drifted; `make openapi` updates it.

## Layout

```
app/
  api/        routers — health, proxy/ (data plane), control/ (the UI's API),
              spa.py (serves the built SPA in production)
  adapters/   upstream dialects — openai now, anthropic in task 16
  core/       config, logging, errors, ids, metrics, middleware, clients,
              crypto (envelope encryption), keys (API key format), passwords
              (Argon2id), tokens (JWT + refresh), background
  db/         engine, session, declarative base, models
  schemas/    the OpenAI wire format, control-plane request/response bodies
  services/   gateway resolution, API-key auth, prompt assembly, forwarding, SSE,
              control-plane auth (auth, auth_provider, auth_store, login_throttle)
  workers/    background jobs (task 09)
  cli.py      operator commands — `python -m app.cli seed | openapi`
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the React SPA
  src/api/      the fetch client and the generated schema types
  src/auth/     auth context, reducer, protected routes
  src/components/  DataTable, Form, ConfirmDialog, EmptyState, StatusBadge, CopyButton
  src/layout/   the app shell — sidebar, user menu, breadcrumb slot
  src/pages/    login, dashboard
  e2e/          Playwright
```

## Configuration

Everything is an environment variable, validated at import time: a missing or malformed
value stops the process immediately and names the variable, rather than surfacing on the
first request. See [.env.example](.env.example) for the full list.

## Operations

| Endpoint | Purpose |
|---|---|
| `POST /g/{slug}/v1/chat/completions` | The data plane. OpenAI schema, `stream: true` or `false`. |
| `GET /g/{slug}/v1/models` | The virtual models this gateway exposes, in OpenAI list format. |
| `POST /api/v1/auth/login` | Email and password. Returns an access token; sets the refresh cookie. |
| `POST /api/v1/auth/refresh` | Rotates the refresh token. Replaying a spent one revokes the session family. |
| `POST /api/v1/auth/logout` | Revokes the session and clears the cookie. Idempotent. |
| `GET /api/v1/auth/me` | The current user, role, and organization. |
| `POST /api/v1/auth/password` | Change your own password; signs every other session out. |
| `GET /healthz` | Liveness. Checks nothing else — a dependency outage must not get the pod restarted into the same outage. |
| `GET /readyz` | Readiness. Probes Postgres, Redis, Qdrant, and object storage concurrently; 503 names what is broken. |
| `GET /metrics` | Prometheus. Request counts and latency labelled by route template. |

Data-plane errors use the **OpenAI** error envelope so client SDKs raise a useful typed
exception; control-plane errors use the gateway's own envelope, which carries the request
id. Unsupported fields (`tools`, `tool_choice`, `functions`, `function_call`, `logprobs`)
are refused with a 400 naming the field rather than silently dropped.

Every log line is one JSON object carrying `request_id`, which is also returned as
`X-Gateway-Request-Id` and honoured on the way in for cross-system correlation.

Control-plane routes are **authenticated by default**: they hang off a router that
carries the `CurrentUser` dependency, so an endpoint added by a later task is protected
unless somebody deliberately registers it on the public router. A test asserts that every
`/api/v1` operation outside a short allow-list answers 401 without a token.

Login is throttled per IP and per email, in Redis so the limit holds across replicas. If
Redis is unreachable the throttle **fails open** and logs a warning: failing closed would
lock every operator out of the UI during a Redis outage, and `/readyz` already pulls such
an instance out of the load balancer.

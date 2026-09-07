# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 05 complete** — the OpenAI-compatible proxy works end to end,
there is a web UI you can sign into, it holds multiple isolated customer organizations
with their own members and roles, and upstream models are configured from that UI with
their credentials encrypted and a **Test connection** button that reports the real
upstream error. Gateways still come from `make seed` until task 06.

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

As the superadmin you can create organizations under **Platform → Organizations**, and
invite people into one from **Settings → Members**. There is no email delivery in v1, so
an invitation produces a link you copy and send yourself. The link works once, expires
after seven days, and is shown exactly once — only its hash is stored, so "resend" mints
a new one and invalidates the old.

## Tenancy

Every organization is isolated, and the isolation is structural rather than a check
repeated per endpoint.

- The **scope** comes from the session (`app/core/tenancy.py`), never from a path, query
  or body parameter. The one way to widen it is `TenantScope.assume`, which only a
  superadmin can call and which writes a record — that is what the "Open as" action and
  its persistent banner are doing.
- Every read of a tenant-keyed table goes through a **`ScopedRepository`**
  (`app/db/repositories.py`), which injects `WHERE organization_id = :scope` and stamps
  the same value on every write.
- A **guard** watches ORM execution and refuses any statement that touches a table with
  an `organization_id` column without either coming from a scoped repository or calling
  `app.db.scoping.unscoped("why")`. Some queries genuinely must span tenants — resolving
  a gateway by slug happens before anyone is authenticated — and `grep -r "unscoped("`
  is the complete list of them.
- Cross-tenant access answers **404, not 403**. A 403 confirms the id exists, which turns
  any endpoint that takes one into an oracle. `tests/test_cross_tenant.py` asserts this
  for every scoped endpoint and fails if a later task adds one it does not cover.

Roles are org-wide (SPEC §5.2) and defined once as data in `app/services/permissions.py`:

| Capability | superadmin | org_admin | org_member | org_viewer |
|---|---|---|---|---|
| View org resources | ✓ | ✓ | ✓ | ✓ |
| Create/edit connectors, gateways, models | ✓ | ✓ | ✓ | — |
| Reveal/create/revoke API keys | ✓ | ✓ | — | — |
| Members, roles, invitations, org profile | ✓ | ✓ | — | — |
| Organizations, global catalog, platform settings | ✓ | — | — | — |

`GET /api/v1/auth/me` returns the resolved set, so the UI hides and disables controls
from one source of truth. That is presentation only — the API refuses the same call
whether or not the button was rendered.

## Models

**Models** is where completions actually go: a base URL, a dialect, the provider's own
model id, and a credential. A gateway points at one of these (task 06 makes that
editable; until then `make seed` wires it).

Two tabs, over one endpoint — `?scope=` is a filter, so a model cannot show up in one
view and be missing from the other:

- **Our models** — this organization's own. Full CRUD for `resources:write`.
- **Global catalog** — models the platform operator shares with every tenant. Org users
  read them and point gateways at them; only a superadmin can create or edit one.

**Test connection** sends a one-token completion through the *same adapter the proxy
uses* and reports `OK, 340 ms` or the provider's own words — `401 invalid_api_key`. It
works on an unsaved draft too, so a base URL can be checked before it is stored. Almost
every misconfiguration is a wrong base URL, which is what the provider presets (OpenAI,
Azure, Groq, Together, OpenRouter, vLLM, Ollama) exist to prevent.

Credentials are **write-only** (SPEC §5.4). They are encrypted with envelope encryption
before storage, and no endpoint returns one — not for any role, superadmin included.
Responses carry `{"configured": true, "hint": "sk-…4f2a"}`, and the hint is computed from
the plaintext at write time and stored, so rendering the list never touches the master
key. Rotation is replacement: `PATCH` with the credential omitted keeps what is there, an
explicit `null` clears it, and a string replaces it. An org user reading a *global* model
sees neither its hint nor its `extra_headers` — headers are applied last and can contain
an auth header, which is exactly what the credential field protects.

`default_params` is validated against the documented OpenAI parameters with the
providers' own bounds, so a mistyped `temprature` is a 422 on the form rather than a 400
from the provider on somebody else's request an hour later. At request time the layers
merge model → gateway → client, lowest precedence first.

Deleting a model a gateway points at is refused and names the gateways; the foreign key
is `ON DELETE RESTRICT`, so the database would refuse it regardless. Disabling is always
allowed and takes effect on the next request — the gateway then answers 503 saying which
model is switched off.

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
  api/        routers — health, proxy/ (data plane), control/ (the UI's API:
              auth, directory, models), spa.py (serves the built SPA in production)
  adapters/   upstream dialects — openai now, anthropic in task 16
  core/       config, logging, errors, ids, metrics, middleware, clients,
              crypto (envelope encryption), keys (API key format), passwords
              (Argon2id), tokens (JWT + refresh), tenancy (TenantScope), background
  db/         engine, session, declarative base, models, scoping (ScopedRepository
              and the unscoped-query guard), repositories
  schemas/    the OpenAI wire format, control-plane request/response bodies
  services/   gateway resolution, API-key auth, prompt assembly, forwarding, SSE,
              control-plane auth (auth, auth_provider, auth_store, login_throttle),
              tenancy (permissions, directory, directory_store, pagination),
              the model catalog (catalog, catalog_store, model_probe, params,
              rate_limit)
  workers/    background jobs (task 09)
  cli.py      operator commands — `python -m app.cli seed | openapi`
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the React SPA
  src/api/      the fetch client and the generated schema types
  src/auth/     auth context, reducer, protected routes
  src/components/  DataTable, Form, ConfirmDialog, EmptyState, StatusBadge, CopyButton
  src/layout/   the app shell — sidebar, user menu, support banner
  src/pages/    login, dashboard, organizations, members, org settings,
                invitation acceptance, models (list and editor)
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
| `GET /api/v1/auth/me` | The current user, role, organization, and capability set. |
| `POST /api/v1/auth/password` | Change your own password; signs every other session out. |
| `GET`/`POST /api/v1/organizations` | List (scope-aware) and create (superadmin). |
| `GET`/`PATCH /api/v1/organizations/{id}` | Read and edit. `status` is superadmin-only. |
| `GET /api/v1/organizations/{id}/members` | Members of one organization. |
| `PATCH`/`DELETE /api/v1/members/{id}` | Change a role or status; remove a member. |
| `POST /api/v1/organizations/{id}/invitations` | Invite someone. Returns the link, once. |
| `GET`/`DELETE /api/v1/invitations[/{id}]` | List pending invitations; revoke one. |
| `POST /api/v1/invitations/{id}/resend` | Mint a new link; the previous one stops working. |
| `GET`/`POST /api/v1/invitations/accept/{token}` | Public. Validate a link, then create the account. |
| `GET`/`POST /api/v1/models` | List (own + global catalog, filterable by `scope` and `enabled`) and create. |
| `GET`/`PATCH`/`DELETE /api/v1/models/{id}` | Read, edit, delete. Delete is refused while a gateway points at it. |
| `POST /api/v1/models/{id}/test` | Probe the stored configuration. One token, rate-limited per user. |
| `POST /api/v1/models/test` | Probe an unsaved draft, before storing a credential. |
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

Suspending an organization takes effect immediately, not at the next token expiry: its
members are refused at login, at refresh, and on every control-plane request. A
superadmin is unaffected, because somebody has to be able to un-suspend it.

"Test connection" is rate-limited per user (20 a minute by default,
`MODEL_TEST_MAX_ATTEMPTS`), because every press is an outbound call billed to whoever
owns the model. Like the login throttle it fails open when Redis is unreachable; the
exposure is bounded by what the action costs, which is one token.

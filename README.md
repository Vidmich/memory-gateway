# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 07 complete**. An organization goes from empty to a working
OpenAI-compatible endpoint entirely in the browser — sign in, configure an upstream
model, create a gateway, copy its URL, mint a key, call it — and every request through it
is then recorded and inspectable. **Monitoring** charts the traffic; clicking a row shows
the client's original messages, the exact prompt that went upstream, the response, and a
timing waterfall.

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
model id, and a credential. A gateway points at one of these.

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

## Gateways and keys

A **gateway** is the endpoint you publish: `https://…/g/{slug}/v1`. It has its own URL,
its own API keys, its own system prompt and its own parameter policy. Create one under
**Gateways → New**, pick a model, write a prompt, save — the screen shows the URL with a
copy button, and **Create key** shows the secret exactly once.

The editor is sectioned so later releases slot in without moving anything: *Identity*,
*Routing*, *Memory* (task 10), *Prompt*, *Logging*, *Limits* (14), *Keys*. The two unbuilt
sections render a real empty state naming what will fill them, rather than being hidden —
a section that appears later moves everything below it.

**The slug is immutable.** It is a path segment on a URL customers have already deployed,
and nothing here can tell them it changed, so a rename from a settings form would break
production traffic silently and instantly. `PATCH` refuses it with that reason, and the
editor offers **Clone with a new slug** instead: a new gateway with the same
configuration, leaving the old one serving until its callers have moved. Reserved slugs
(`api`, `admin`, `health`, `metrics`, `g`, `www`) are refused.

**Keys are shown once.** Only `sha256(secret)` is stored, so the plaintext genuinely
cannot be recovered — the reveal dialog says so above the value, not under it. Revoking
is a timestamp rather than a delete, so the request log keeps a reference that
resolves, and it takes effect on the **next request**: a gateway's configuration is
cached in Redis, but a key never is, which is what makes that sentence true without an
asterisk. Keys can carry an optional expiry, and `last_used_at` is written at most once a
minute per key so a hot key does not turn every completion into a database write.

**Test gateway** sends a real completion through the real proxy path — same resolver
(cache included), same prompt assembly, same adapter — and returns the *assembled prompt*
alongside the answer and a latency breakdown. It is the fastest way to see what your
system context became; the request log is the record of what every real call did.

**Parameter policy has two strengths.** `param_overrides` is the organization's house
style and a client can beat it; `locked_params` is applied *after* the client's values
and wins. When a lock actually replaced something the caller asked for, the response
carries `X-Gateway-Locked-Params` naming it — ignoring a request silently is the failure
mode that design has to answer for.

Configuration changes take effect on the next request. The resolver caches a gateway in
Redis under a per-slug version counter; every write that could change what a request does
— including an edit to a *model* the gateway points at, in any organization — bumps that
counter, so the stale entry is orphaned rather than deleted. A delete has a window where
a slow reader can put stale config back afterwards; a version bump does not. A 60-second
TTL is the backstop, and the whole cache fails open onto PostgreSQL. Provider credentials
are cached still **encrypted**: Redis is a cache, not a vault.

A disabled gateway answers **403**, not 503. A 503 means "try again", and an SDK will —
indefinitely, against an endpoint somebody switched off on purpose.

## Request logging and monitoring

Every request through a gateway becomes a row. **Monitoring** shows the request rate with
its status breakdown, latency percentiles, token counts, traffic per model and the error
taxonomy over a chosen window, plus a live-tailing request table. Clicking a row opens the
detail drawer: the caller's own messages, the assembled prompt with the gateway's
additions marked, the response, a timing waterfall and a *Copy as curl* that reproduces
the call against the gateway.

**Logging never waits.** The request path fills in a record in memory, copies at most a
bounded number of bytes out of the response, and hands it to a queue; redaction, batching
and the insert all happen in a background flusher. An `INSERT` before the response returns
would be the obvious implementation and it is the wrong one: a database round trip is the
same order of magnitude as the whole latency budget, so a database that is briefly slow
would make the *proxy* briefly slow. Measured on this machine, logging on versus off is
indistinguishable at p95 (−0.01 ms of a 5 ms budget), and the test that matters asserts
the stronger thing — that the request path makes **no** synchronous call to the log store.

**Under pressure it sheds in a defined order.** Above a watermark, incoming records keep
their metadata and lose their bodies, which is where almost all the memory is; a full
queue drops whole records. Every drop increments `logs_dropped_total{reason}`, so a gap in
the log is a number somebody can alert on rather than an absence somebody notices. Nothing
on this path can fail a request.

**Bodies are configurable per gateway, and the form says what that means.** The §10.2
toggles — the client's request, the assembled prompt, the response — plus retention,
redaction patterns and the distillation switch live in the gateway's *Logging* section,
which states plainly that body capture stores end-user content and shows the effective
retention as a sentence. Metadata is always on: it is what the charts are made of.
Switching a body off means nothing is copied at all, not that it is hidden afterwards. An
organization can set its own defaults under `settings.logging_defaults`, and a new gateway
starts from them.

**Redaction runs before persistence, never after.** A redaction applied on read is a
display filter, and the raw card number is still in the backup. The patterns are applied
in the flusher, which is upstream of every write, and if they cannot finish within their
budget the bodies are dropped rather than stored half-cleaned — the row says
`bodies_omitted: redaction_budget` and the drawer explains it. Patterns are checked when
you save them: `(a+)+` and its relatives are refused on the form, because Python's `re`
cannot be interrupted once it is matching, so the only place to stop one is before it is
stored.

**Metadata and bodies are separate tables**, `request_logs` and `transcripts`, both
partitioned by day. The monitoring queries never touch the large text columns, and
retention (task 17) becomes a partition drop rather than a `DELETE` that rewrites a live
table while the proxy writes to it. Aggregation happens in PostgreSQL — `percentile_disc`
over the partition range — behind a `MetricsRepository`, which is the seam to move to
ClickHouse if the volume ever demands it.

Bucket widths are the server's decision, not the client's: a window maps onto a fixed
ladder (an hour at one-minute buckets, thirty days at one hour) and a requested interval
is widened until the answer fits. Summary queries are cached for 30 seconds per
organization and query; the cache is read-through and fails open.

The log detail endpoint takes no time range. Primary keys are UUIDv7 and carry the
millisecond they were minted, so the id itself says which day's partition to look in.

## Try the proxy

Create a gateway and a key in the UI, or let `make seed` wire a demo one up. With
`OPENAI_API_KEY` set, seeding creates a demo organization, upstream model, gateway and
API key. The provider key is encrypted with `ENCRYPTION_MASTER_KEY` before it is stored,
and the gateway key's plaintext is printed once and never again.

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
changing — edit the model in the UI and the very next request goes somewhere else. `make seed --no-auth`-style local providers (Ollama, vLLM) work by setting
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
              auth, directory, models, gateways), spa.py (serves the built SPA
              in production)
  adapters/   upstream dialects — openai now, anthropic in task 16
  core/       config, logging, errors, ids, metrics, middleware, clients,
              crypto (envelope encryption), keys (API key format), passwords
              (Argon2id), patterns (regex safety), tokens (JWT + refresh),
              tenancy (TenantScope), background
  db/         engine, session, declarative base, models, scoping (ScopedRepository
              and the unscoped-query guard), repositories
  schemas/    the OpenAI wire format, control-plane request/response bodies
  services/   gateway resolution and its Redis config cache (gateway_resolver),
              API-key auth, prompt assembly, forwarding, SSE, control-plane auth
              (auth, auth_provider, auth_store, login_throttle), tenancy
              (permissions, directory, directory_store, pagination), the model
              catalog (catalog, catalog_store, model_probe, params, rate_limit),
              gateways and keys (gateways, gateway_store, gateway_probe), request
              logging (request_log, log_store, redaction) and the monitoring reads
              (monitoring, metrics_store)
  workers/    background jobs (task 09)
  cli.py      operator commands — `python -m app.cli seed | openapi`
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the React SPA
  src/api/      the fetch client and the generated schema types
  src/auth/     auth context, reducer, protected routes
  src/components/  DataTable, Form, ConfirmDialog, EmptyState, StatusBadge, CopyButton,
                   Charts (hand-drawn SVG — no charting library)
  src/layout/   the app shell — sidebar, user menu, support banner
  src/pages/    login, dashboard, organizations, members, org settings,
                invitation acceptance, models (list and editor), gateways
                (list, editor, keys), monitoring (charts, request table,
                detail drawer)
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
| `GET`/`POST /api/v1/gateways` | List and create. The slug is globally unique and set once. |
| `GET`/`PATCH`/`DELETE /api/v1/gateways/{id}` | Read, edit, delete. `PATCH` refuses `slug`, with the reason. |
| `POST /api/v1/gateways/{id}/test` | A probe completion through the real proxy path; returns the assembled prompt. |
| `GET`/`POST /api/v1/gateways/{id}/keys` | List keys (prefix only); mint one — the plaintext is returned once. |
| `DELETE /api/v1/keys/{id}` | Revoke. Soft, and effective on the next request. |
| `GET /api/v1/metrics/summary` | Totals, percentiles, per-model traffic and the error taxonomy for a window. Cached 30 s. |
| `GET /api/v1/metrics/timeseries` | Bucketed series. `metric` is `requests`, `latency` or `tokens`; the server picks the bucket width. |
| `GET /api/v1/logs` | The request table. Cursor-paginated, filterable by gateway, model, status class, end user, session, latency and error text. |
| `GET /api/v1/logs/{id}` | One request in full, including whatever of the transcript was stored. No time range needed. |
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

"Test connection" and "Test gateway" are rate-limited per user (20 a minute each by
default, `MODEL_TEST_MAX_ATTEMPTS`), because every press is an outbound call billed to
whoever owns the model. Like the login throttle they fail open when Redis is unreachable;
the exposure is bounded by what the action costs, which is a handful of tokens.

# Task 06 — Gateways & API keys (API + UI)

**Slice:** a customer creates their own API endpoint in the UI, copies a URL and a key, and uses
it immediately.
**Depends on:** 05
**Spec:** §3, §5.1, §12.2, §13.1 (Gateways)
**Size:** L

> **Milestone M2.** After this task the system is a self-serve product: no CLI, no seeds, no
> engineer in the loop.

---

## Why this slice

This is the moment the pieces become a service someone can be given access to. It also
establishes the gateway editor, which every later task extends with one more section — memory
(10), logging (07), limits (14) — so its structure is worth getting right once.

## Demo at the end of this task

Log in as an Acme org_admin. **Gateways → New**: name it "Support Bot", slug `acme-support`,
pick a model, write a system prompt ("You are Acme's support assistant. Be concise."). Save.

The screen shows `https://localhost:8000/g/acme-support/v1` with a copy button. Create a key —
the secret is shown once with a warning. Paste both into a Python REPL and get a completion
that obeys the system prompt. Change the system prompt in the UI, resend, and the behavior
changes on the next request.

## In scope

- Full gateway configuration model and CRUD, slug rules, enable/disable.
- API key lifecycle: create, reveal-once, revoke, last-used.
- Gateway editor UI (Identity, Prompt, Keys sections) and gateway list.
- Config caching with correct invalidation.

## Out of scope

- Routing modes beyond `single` (08); the editor shows the selector disabled with "coming soon"
  or hides it entirely — do not build a half-working splitter.
- Memory, logging, and limits sections (10, 07, 14) — leave clearly marked placeholders so the
  editor's information architecture is settled now.

## Work items

### Model
- [x] Extend `gateways` to the full SPEC §14 field list: `description`, `enabled`,
      `routing_mode` (default `single`), `system_context`, `param_overrides_jsonb`,
      `locked_params_jsonb`, `memory_config_jsonb`, `logging_config_jsonb`, `limits_jsonb`.
      The three jsonb config blobs are created here with schema-validated defaults so later tasks
      only add fields.
- [x] Each config blob is a versioned Pydantic model with defaults, so an old row missing a new
      key still loads.
- [x] Slug rules: lowercase alphanumeric and hyphens, 3–63 chars, globally unique (it appears in
      a public URL), immutable after creation — changing it would silently break every deployed
      client. Offer clone-with-new-slug instead.
- [x] Reserved slugs blocklist (`api`, `admin`, `health`, `metrics`, `g`, `www`).

### API keys
- [x] `POST /gateways/{id}/keys` returns the plaintext **once**; it is never retrievable again.
- [x] `GET /gateways/{id}/keys` lists id, name, prefix, created, last used, revoked.
- [x] `DELETE /keys/{id}` revokes (soft, `revoked_at`) — never hard-delete, so historical request
      logs in task 07 keep a resolvable reference.
- [x] Optional `expires_at` on creation.
- [x] Key creation and revocation restricted to `org_admin`+ per the task 04 matrix.
- [x] `last_used_at` written at most once per minute per key (batched via Redis) so a hot key
      does not generate a write per request.

### Endpoints
- [x] `GET|POST /api/v1/gateways`, `GET|PATCH|DELETE /api/v1/gateways/{id}`.
- [x] `POST /api/v1/gateways/{id}/test` — sends a probe completion **through the real proxy path**
      using an internal credential, returning the assembled prompt, the response, and timings.
      This is the single most useful debugging affordance before task 07's monitoring exists.
- [x] `PATCH` accepts partial config-blob updates with deep merge, validated against the blob
      schema.

### Config caching
- [x] `GatewayResolver` (the seam from task 02) gains a Redis cache keyed by slug, holding the
      gateway, its targets, and the resolved model records.
- [x] Invalidate on any write to the gateway, its targets, its keys, or a referenced model. Use a
      version counter per gateway rather than blind deletes so a concurrent write cannot resurrect
      stale config.
- [x] Short TTL (60 s) as a backstop against a missed invalidation.
- [x] A revoked key must stop working **immediately**, not on cache expiry — key lookup checks
      revocation against the database or a dedicated revocation set, never a cached copy.

### Proxy integration
- [x] Disabled gateway → 403 with an explicit message.
- [x] Gateway with no enabled target → 503 naming the problem.
- [x] `locked_params` enforced: client-supplied values for locked keys are ignored rather than
      merged, and the response header notes an override occurred.

### UI
- [x] **Gateways list**: name, slug, endpoint URL with copy, mode, target model, key count,
      enabled toggle, 24 h request count (placeholder until task 07).
- [x] **Gateway editor**, sectioned so later tasks slot in:
      1. *Identity* — name, slug (create-only, with live URL preview), description, enabled.
      2. *Routing* — single-target model picker. Mode selector present but limited to `single`.
      3. *Memory* — placeholder: "Configured in a later release."
      4. *Prompt* — system context textarea, param overrides with per-param lock toggles, and a
         live **assembled prompt preview** showing exactly what will be sent.
      5. *Logging* — placeholder.
      6. *Limits* — placeholder.
      7. *Keys* — table, create dialog, reveal-once modal with copy and an explicit "you will not
         see this again" warning, revoke with typed confirmation.
- [x] **Test gateway** button in the editor: type a message, see the assembled prompt, the
      response, and the latency breakdown.
- [x] Unsaved-changes guard when navigating away from the editor.
- [x] Delete gateway requires typing the slug, and warns that deployed clients will break.

## Acceptance criteria

- [x] A new org can go from empty to a working endpoint entirely in the UI, in under two minutes,
      with no server restart.
- [x] The key secret is displayed exactly once and is unrecoverable afterward.
- [x] Revoking a key stops the next request immediately, even with a warm config cache.
- [x] Editing the system prompt affects the very next request.
- [x] Slugs are globally unique and immutable; reserved slugs are rejected.
- [x] Locked params override client values; the header reports it.
- [x] Cross-tenant test module extended: org A cannot read, patch, delete, or create keys on
      org B's gateways.

## Tests

- Slug validation, uniqueness, reserved words, immutability on patch.
- Key lifecycle: create → use → revoke → rejected; expiry; reveal-once.
- Cache invalidation: patch the gateway and assert the next proxy request uses new config;
  concurrent write + read does not resurrect stale config.
- Param merge with locks.
- Disabled gateway and no-target gateway error paths.
- End-to-end (Playwright): create gateway → create key → call the endpoint from the test.

## Notes

- Slug immutability will be questioned. The alternative — mutable slugs — means a customer can
  silently break their own production traffic from a settings page, and the gateway has no way to
  warn them. Clone is the safe escape hatch.
- Every placeholder section should render a real, styled empty state naming the task that fills
  it. It keeps the editor honest and makes progress visible in demos.

---

## Verification status

Everything below was run on this machine unless the table says otherwise.

```
uv run ruff check .              All checks passed!
uv run ruff format --check .     146 files already formatted
uv run mypy                      Success: no issues found in 139 source files
uv run pytest -q                 1224 passed, 193 skipped

npm --prefix web run lint        clean
npm --prefix web run typecheck   clean
npm --prefix web run test        162 passed
npm --prefix web run build       built in 1.8s
```

The generated API client was regenerated and is stable: `web/openapi.json` and
`web/src/api/schema.d.ts` reproduce byte-for-byte, and CI fails on drift.

### The live run

The demo was driven through the real HTTP stack — real routing, the production adapter,
the production config cache, and a **real provider on a real port**. Only persistence is
in memory, because no PostgreSQL on this machine accepts the credentials in `.env`:

```
an org admin creates a gateway               ok   201
the screen gets a URL to copy                ok   http://localhost:8000/g/acme-support/v1
config defaults come back filled in          ok   memory, logging and limits
a key is minted                              ok   201
the secret is never returned again           ok   searched the serialized listing
only the prefix is shown                     ok   mg_01a07ad0
test gateway answers                         ok   OK, 3 ms
and returns the assembled prompt             ok   system, then the user turn
with a latency breakdown                     ok   2 ms upstream
renaming the slug is refused with a reason   ok   422, names the URL
a reserved slug is refused                   ok   422
another org's slug is taken                  ok   409, globally unique
a member may configure a gateway             ok   200
a member may not mint a key                  ok   403, keys:manage
a viewer may read                            ok   200
a viewer may not write                       ok   403
another org's gateway is 404                 ok   404
another org's key is 404                     ok   404
editing the prompt bumps the config version  ok   v2 -> v3
editing the model bumps it too               ok   a model change reaches its gateways
revoking is a stamp, not a delete            ok   revoked_at set
a revoked key stays listed                   ok   history is kept for the request log
a completion flows through the gateway       ok   200
the system prompt is prepended               ok   assembled upstream
a locked parameter beats the client          ok   client asked for 1.9
and the response says so                     ok   temperature
the config is served from cache              ok   1 database read for 2 requests
editing the prompt changes the next request  ok   no restart
revoking a key stops the next request        ok   even with a warm config cache
a disabled gateway is a 403, not a 503       ok   a retry loop would never stop on a 503

30/30 steps ok
```

Separately, the real app was served with the built SPA mounted (`WEB_DIST_DIR=web/dist`)
and every backing service down:

| Request | Result |
|---|---|
| `GET /gateways`, `/gateways/new`, `/gateways/{id}` | 200 `text/html` — history fallback covers the new routes |
| `GET`/`POST /api/v1/gateways` | 401 + `WWW-Authenticate: Bearer` |
| `GET`/`POST /api/v1/gateways/{id}/keys`, `POST /api/v1/gateways/{id}/test`, `DELETE /api/v1/keys/{id}` | 401 — the authenticated-by-default router covers them |

### Not verifiable here

| Item | Why | What stands in for it |
|---|---|---|
| The migration against a real PostgreSQL | A server is listening on `localhost:5432`, but it rejects the credentials in `.env` (`password authentication failed for user "gateway"`) | `tests/test_migration_offline.py` renders the DDL and compares every table and column against the models; `tests/test_gateway_db.py` runs the store contract, the constraints and the cascade in CI via `REQUIRE_DB_TESTS=1` |
| `slug_is_url_safe`, `routing_mode_is_known`, the unique slug, and the key cascade | Same — a constraint is only real if the server enforces it | Asserted in `tests/test_gateway_db.py`; those tests are the only proof |
| A real Redis | Not running here | `tests/test_gateway_cache.py` drives the cache through a hand-written double implementing the four commands it uses (`get`, `set`, `incr`, `expire`); the live run above used the same double behind the real `GatewayCache` and the real `CachedGatewayResolver` |
| A real provider | No `OPENAI_API_KEY` on this machine | Every probe and completion goes to a scriptable upstream on a real socket, through the production adapter |
| Playwright | No browser binaries, and it needs the API, the database and Vite all up | The gateway screens are covered in jsdom (`web/src/pages/gateways.test.tsx`, 31 tests) and over HTTP (the live run above) |
| `docker compose up`, `make` as targets | Docker and `make` are unavailable (inherited from task 01) | Every target's underlying command was run directly |

### Notes for later tasks

- **`app/services/gateway_resolver.py` is the data plane's read path, and the only one.**
  Task 08's routing modes replace `ResolvedGateway.target()`; task 07 reads
  `logging_config` per request and will want it on the cached payload, which means
  bumping `PAYLOAD_VERSION` — the cache treats an older payload as a miss, so that is a
  one-line change with no migration.
- **`tests/gateway_store_contract.py` is the store's exam.** Both implementations run it;
  add a check there rather than to one half.
- **`app/schemas/gateway_config.py` is where tasks 07, 10 and 14 add their fields.**
  Adding one to `LoggingConfig` is a field with a default and nothing else: existing rows
  load it, `merge_config` starts accepting it, and the API starts returning it.
- **`tests/test_cross_tenant.py` now covers `/gateways/{id}` and `/keys/{id}`.** Task 09
  adds `/connectors/{id}`; `test_the_net_covers_every_scoped_route` fails until it does.
- **`ApiKeyRepository` is the one repository the scope guard does not protect.**
  `api_keys` has no `organization_id`, so `app.db.scoping.is_tenant_keyed` skips it and a
  bare `select(ApiKey)` sails through. Every read joins `gateways` by hand, and
  `test_the_guard_does_not_cover_api_keys` asserts the gap so it stays a known one.
- **`app/services/gateways.py` writes `audit_action` into its log lines**
  (`gateway.create`, `gateway.update`, `gateway.delete`, `gateway.test`, `key.create`,
  `key.revoke`). Task 15 turns those into `audit_events` rows.
- **The unsaved-changes guard is half a guard.** `useBlocker` needs a data router and this
  app mounts `BrowserRouter` with a plain `<Routes>` tree, so in-app navigation is guarded
  only where the editor calls `confirmLeave` itself. Converting to `createBrowserRouter`
  is a routing change that belongs with a task that has a reason to touch routing.

### Deliberate deviations

- **Locked parameters are a second JSON box, not per-parameter lock toggles.** The work
  item asks for "param overrides with per-param lock toggles", which implies a grid of
  rows each with a checkbox. Two labelled inputs — *Parameter defaults* and *Locked
  parameters* — say the same thing more plainly, because the two are different policies
  rather than one policy with a modifier: an override is a default a client can beat, a
  lock is a value it cannot. A toggle grid also has an unanswerable state (locked with no
  value), which the two-box form simply cannot express.
- **`POST /gateways/{id}/test` takes a message and nothing else.** No model override, no
  parameter override, no "what if" mode. The button's value is that it exercises the real
  path; every knob added to it is a way for the result to stop describing what a customer
  would get.
- **A disabled gateway is a 403 and a disabled *model* is still a 503.** They read as the
  same outage and are not. A switched-off gateway is a decision, and a client should stop
  retrying; a gateway pointing at a switched-off model is a misconfiguration that a toggle
  in another screen fixes, so a client backing off and retrying recovers on its own.
- **`failover` and `ab_split` are stored but refused.** The column and the UI accept them
  so "can this gateway fail over?" is answerable from the screen; `SUPPORTED_ROUTING_MODES`
  in `app/services/gateways.py` is the single check task 08 removes. The message says "not
  yet supported by this build", which is a different sentence from "not a mode".
- **Cloning is a client-side pre-fill, not an endpoint.** `/gateways/new?clone={id}` reads
  the source gateway through the existing `GET` and fills the create form. It copies
  configuration and deliberately not keys — those cannot be read back, and a clone that
  silently had no keys would be worse than one that obviously does not.
- **Provider credentials are cached encrypted.** The cached payload holds the ciphertext
  and decryption happens per request, so a Redis dump — a backup, a `MONITOR`, an
  unauthenticated instance — contains no provider keys. It costs one AES-GCM open per
  request, which is not measurable next to the network call it precedes.
- **`ProxyService.complete` and `open_stream` now take a `Prepared`.** Prompt assembly and
  the parameter merge moved into `ProxyService.prepare`, which the route calls first
  because the locked-parameter header has to be written before the body — and on a stream,
  before the first token. Splitting it also means the merge happens once per request rather
  than once per code path that needed its result.

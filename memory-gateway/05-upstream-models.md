# Task 05 — Upstream models (API + UI)

**Slice:** configure upstream models through the UI, with credentials safely stored and
connectivity verifiable before anything depends on them.
**Depends on:** 04
**Spec:** §5.4, §8.3, §8.4, §13.1 (Models)
**Size:** M

---

## Why this slice

Task 02 seeded a model from an environment variable. This turns that into real configuration
owned by real tenants, with the dual-scope rule (global catalog + org-owned) that the rest of
the product assumes. The **Test connection** button matters more than it looks: without it,
every misconfiguration surfaces later as a confusing proxy error.

## Demo at the end of this task

In the UI, open **Models → Our models → New**. Enter a base URL, model id, and API key. Press
**Test connection** — it reports "OK, 340 ms" or shows the exact upstream error
("401 invalid_api_key"). Save it, point the seeded gateway at it, and completions flow through
the new model with no restart.

Switch to the **Global catalog** tab as an org user: you see the operator's shared models with
their names and dialects, and no way to view or extract their credentials.

## In scope

- Full `upstream_models` fields, credential encryption, scope and visibility rules.
- CRUD API, test-connection endpoint, Models UI.
- Proxy consumes the full model configuration.

## Out of scope

- The `anthropic` dialect implementation (16) — the field accepts the value and the UI offers it,
  but selecting it is rejected with "not yet supported" until then.
- Routing across several models (08); a gateway still has exactly one target.
- Cost/pricing metadata (SPEC §16.2).

## Work items

### Model & security
- [x] Extend `upstream_models` to the full SPEC §8.4 field list.
- [x] `scope` ∈ {`global`, `org`}; `organization_id` NULL iff `scope = global`, enforced by check
      constraint.
- [x] Credentials stored via the envelope-encryption helper from task 02.
- [x] **Write-only credential API.** Responses return `{"configured": true, "hint": "sk-…4f2a"}`.
      There is no endpoint, for any role including superadmin, that returns a stored credential.
      Rotation is replacement, not reveal.
- [x] `PATCH` with credential omitted keeps the existing one; an explicit `null` clears it.
- [x] `default_params` validated against a known parameter allowlist with sane bounds, so a typo
      does not become a 400 from the provider at request time.

### Visibility rules
- [x] Org users list: all enabled `global` models (metadata only) + their own org models (full,
      minus credentials).
- [x] Only superadmins create, edit, or delete `global` models.
- [x] Deleting a model referenced by a gateway target is blocked with a message naming the
      gateways. Disabling it is allowed and takes effect immediately.

### Endpoints
- [x] `GET /api/v1/models` — scope-aware, filterable by `scope` and `enabled`, cursor-paginated.
- [x] `POST /api/v1/models`, `GET|PATCH|DELETE /api/v1/models/{id}`.
- [x] `POST /api/v1/models/{id}/test` — sends a minimal completion
      (`max_tokens: 1`, a trivial prompt) and returns
      `{ok, latency_ms, upstream_status, error_message, model_echo}`.
      *(Uses the stored configuration with no overrides — see Deliberate deviations.)*
- [x] `POST /api/v1/models/test` — same, on an unsaved draft, so configuration can be validated
      before it is stored.
- [x] Test connection is rate-limited per user and its cost is negligible by construction.

### Proxy integration
- [x] The proxy resolves the full model record: `base_url`, `dialect`, `upstream_model_id`,
      credentials, `extra_headers`, `system_context`, `default_params`, `timeout_seconds`.
- [x] Parameter merge implemented as an explicit, tested function:
      `model.default_params` → `gateway.param_overrides` → client request.
- [x] A disabled or deleted model makes its gateway return a clear 503 naming the misconfiguration
      rather than a generic failure.

### UI
- [x] **Models** screen with two tabs, *Global catalog* and *Our models*.
- [x] Model form: name, description, base URL, dialect, upstream model id, auth type, credential
      (masked, write-only, showing the hint when already set), extra headers (key/value rows),
      system context (textarea with a character count), default params, timeout, enabled.
- [x] Provider presets that prefill base URL and auth type for common targets (OpenAI, Azure
      OpenAI, Groq, Together, OpenRouter, vLLM, Ollama) — most misconfiguration is a wrong base
      URL.
- [x] **Test connection** inline: spinner → green with latency, or red with the verbatim upstream
      error.
- [x] Delete confirmation requiring the typed model name; blocked-delete shows the referencing
      gateways as links.

## Acceptance criteria

- [x] No API response, log line, or error message contains a stored credential — verified by a
      test that greps serialized responses for the known secret.
- [x] An org user cannot create, edit, or delete a global model, nor read its credential hint.
- [x] Test connection reports the real upstream error text for a bad key, a bad URL, and a
      timeout.
- [x] Editing a model changes proxy behavior on the next request with no restart.
- [x] Deleting a referenced model is blocked and names the referencing gateways.
- [x] Cross-tenant test module extended: org A cannot see or touch org B's models.

## Tests

- Credential round-trip: encrypt, store, decrypt for use, never serialize.
- Scope and visibility matrix across all four roles.
- Parameter merge precedence, including partial overrides and unknown keys.
- Test-connection against mocked success, 401, 404, and timeout.
- Referenced-model deletion is blocked; disabling propagates.

## Notes

- Keep the dialect registry a simple mapping from string to adapter class. Task 16 registers
  `anthropic`; nothing else should need to change.
- The credential hint is derived from the plaintext at write time and stored alongside the
  ciphertext, so rendering the list never requires decryption.

---

## Verification status

Everything below was run on this machine unless the table says otherwise.

```
uv run ruff check .              All checks passed!
uv run ruff format --check .     130 files already formatted
uv run mypy                      Success: no issues found in 124 source files
uv run pytest -q                 978 passed, 152 skipped

npm --prefix web run lint        clean
npm --prefix web run typecheck   clean
npm --prefix web run test        130 passed
npm --prefix web run build       built in 1.6s
```

The generated API client was regenerated and is stable: `web/openapi.json` and
`web/src/api/schema.d.ts` reproduce byte-for-byte, and CI fails on drift.

### The live run

The whole demo was driven through the real HTTP stack — real routing, the production
adapter, and a **real provider on a real port**, so "Test connection" makes an actual
outbound call. Only persistence is in memory, because no PostgreSQL on this machine
accepts the credentials in `.env`:

```
a superadmin publishes a shared model            ok   201
an org user sees it in the catalog               ok   operator-gpt-4o / openai
with no way to extract the credential            ok   {'configured': True, 'hint': None}
and no credential anywhere in the body           ok   searched the serialized response
marked read-only for them                        ok   editable=false
editing it is 404, not 403                       ok   404
publishing one is 403                            ok   403
a bad key reports the upstream verbatim          ok   invalid_api_key Incorrect API key provided.
an unreachable host says which error it was      ok   ConnectError: All connection attempts failed
a working configuration reports a latency        ok   OK, 1 ms, echoed gpt-4o-mini-2024
the probe cost one token                         ok   max_tokens=1, one message
saving it returns 201                            ok   201
the response carries a hint, never the key       ok   {'configured': True, 'hint': 'sk-...form'}
a patch without a credential keeps it            ok   hint unchanged
a new one replaces it                            ok   hint rotated
an explicit null clears it                       ok   configured=false
the anthropic dialect is refused with a reason   ok   422, names the dialect
a mistyped parameter names itself                ok   'temprature' is not a generation parameter
a viewer can read the catalog                    ok   200
a viewer cannot change one                       ok   403
deleting a referenced model is blocked           ok   names the gateway 'Acme Chat'
disabling it instead is allowed                  ok   enabled=false
another org's model is 404                       ok   404
a completion flows through the configured model  ok   upstream-model
editing the model changes the next request       ok   no restart
a disabled model is a 503 naming it              ok   "points at the disabled upstream model"

26/26 steps ok
```

Separately, the real app was served with the built SPA mounted (`WEB_DIST_DIR=web/dist`)
and every backing service down:

| Request | Result |
|---|---|
| `GET /models`, `/models/new`, `/models/{id}` | 200 `text/html` — history fallback covers the new routes |
| `GET /api/v1/models` | 401 + `WWW-Authenticate: Bearer` |
| `POST /api/v1/models/test`, `PATCH /api/v1/models/{id}` | 401 — the authenticated-by-default router covers them |

### Not verifiable here

| Item | Why | What stands in for it |
|---|---|---|
| The migration against a real PostgreSQL | A server is listening on `localhost:5432`, but it rejects the credentials in `.env` (`password authentication failed for user "gateway"`) | `tests/test_migration_offline.py` renders the DDL and compares every table and column against the models; `tests/test_catalog_db.py` runs the store contract, the constraints and the scope guard in CI via `REQUIRE_DB_TESTS=1` |
| `scope_matches_organization`, the partial unique index on global names, and `ON DELETE RESTRICT` | Same — a constraint is only real if the server enforces it | Asserted in `tests/test_catalog_db.py`; those tests are the only proof |
| A real provider | No `OPENAI_API_KEY` on this machine | The probe runs against a scriptable upstream on a real socket (`tests/test_model_probe.py`), through the production adapter. Tests marked `live` still exist for the real thing |
| Playwright | No browser binaries, and it needs the API, the database and Vite all up | The Models screens are covered in jsdom (`web/src/pages/models.test.tsx`) and over HTTP (the live run above) |
| `docker compose up`, `make` as targets | Docker and `make` are unavailable (inherited from task 01) | Every target's underlying command was run directly |

### Notes for later tasks

- **`tests/test_cross_tenant.py` now covers `/models/{id}`.** It gained a second table,
  `GLOBAL_MODEL_ENDPOINTS`, for the routes that must 404 on a model everyone can *read*
  but only the platform may write. Task 06 adds `/gateways/{id}` and `/keys/{id}` rows.
- **`tests/catalog_store_contract.py` is the store's exam.** Both implementations run it;
  add a check there rather than to one half.
- **The dialect registry is the source of truth for what is supported.** Task 16
  registers `anthropic` in `app/adapters/__init__.py`, and
  `CatalogService._check_dialect` starts accepting it with no other change — the
  "not yet supported" message is derived from `known_dialects()`, not from a list.
- **Gateway config caching (task 06) must preserve "no restart".** `GatewayResolver`
  reads the row per request today, and `test_editing_a_model_changes_the_next_request`
  in `tests/test_proxy_chat.py` is what a cache has to keep green.
- **`app/services/params.py` owns the parameter allowlist.** Task 06's `locked_params`
  validates against the same table; `test_the_allowlist_matches_the_wire_format` fails if
  it drifts from `app/schemas/openai.py`.
- **`FixedWindowLimiter` (`app/services/rate_limit.py`) is the small one.** Task 14's real
  request-rate limiter is a different thing; this exists for control-plane actions that
  cost somebody else money, and raises the shared `RateLimited` error.
- **Task 05 added a ninth named `unscoped(...)`** — the global model-name uniqueness
  check in `UpstreamModelRepository.global_name_taken`, which has to see one namespace
  rather than one tenant. Task 04's note says eight; `grep -r "unscoped(" app/` is still
  the complete audit, and is the thing to trust.
- **`app/services/catalog.py` writes `audit_action` into its log lines**
  (`model.create`, `model.update`, `model.delete`, `model.test`). Task 15 turns those into
  `audit_events` rows.

### Deliberate deviations

- **`POST /models/{id}/test` takes no overrides.** The obvious convenience — edit the base
  URL, keep the stored key, press Test — is a credential-reveal endpoint wearing a
  different hat: it would let anyone with `resources:write` aim a stored secret at a
  server they control. So the saved probe uses the saved configuration, the draft probe
  uses only what the caller supplied, and the UI says which of the two it just ran. (An
  org admin can still redirect their *own* model's credential by saving a new base URL
  first — that is inherent in being allowed to edit it, and it is a recorded change. The
  boundary that matters holds: only a superadmin can edit a global model, so an operator's
  credentials are out of a tenant's reach.)
- **An org user is not shown a global model's `extra_headers`**, not just its credential
  hint. Headers are applied last and can therefore *contain* an auth header
  (`app/adapters/openai.py` says so explicitly), so showing an operator's would hand over
  exactly what the credential field protects.
- **A disabled global model disappears from the catalog for tenants**, rather than showing
  as disabled. It cannot serve a request, so listing it only invites someone to point a
  gateway at something switched off. The platform still sees it, because the platform is
  who switches it back on.
- **Testing a model requires being able to edit it.** A probe spends the owner's tokens,
  and the owner of a global model is the platform — so an org user gets the same 404 there
  as for every other write. One rule (`owned_model`), not a second condition.
- **`auth_type: "none"` with a credential is refused**, rather than storing a secret that
  is never sent or silently dropping it. Both silent options produce a screen that
  disagrees with what goes on the wire.
- **The blocked-delete dialog lists the gateways as text, not links.** There is no gateway
  screen to link to until task 06; a link to a route that does not exist is worse than the
  name and slug.
- **`error.param` is now on the control-plane envelope.** Task 03's shape carried
  `{code, message, request_id, details}`, so a rule only the server knows — a name already
  taken, a parameter out of range — could only surface as a banner. It is an additive
  optional field, and `web/src/api/client.ts` puts the message on the field it names.

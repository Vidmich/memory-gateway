# Task 04 — Organizations, users, roles & tenancy

**Slice:** multiple isolated customer organizations, each with its own members and roles.
**Depends on:** 03
**Spec:** §5.2, §5.3, §13.1 (Settings, Platform)
**Size:** L

---

## Why this slice

Tenant isolation is the one thing that cannot be retrofitted safely. Every table added from
task 05 onward carries an `organization_id`, and every query must be scoped. Establishing the
enforcement mechanism *before* those tables exist means isolation is structural rather than a
checklist item repeated in forty endpoints.

## Demo at the end of this task

As the superadmin: create organizations "Acme" and "Globex", invite an admin to each, and see
both listed in the Platform area.

Open the invitation link for Acme in a private window, set a password, log in — you see only
Acme, with no route, filter, or API call that reveals Globex. Attempting to `GET` a Globex
resource id directly returns 404 (not 403 — existence itself is not disclosed).

## In scope

- Full organization and membership model, roles, invitations.
- Org-scoped repository enforcement and role guards, backend and frontend.
- Platform (superadmin) organizations screen; org Settings → Members screen.

## Out of scope

- Email delivery — invitations produce a link shown in the UI and returned by the API.
- Per-resource ACLs; roles are org-wide.
- SSO (SPEC §16.7).

## Work items

### Model
- [x] Extend `organizations` with `settings_jsonb` (org-level defaults consumed by later tasks)
      and `status` (`active` / `suspended`).
- [x] `users.organization_id` is `NULL` only for `superadmin`; enforce with a check constraint.
- [x] `invitations(id, organization_id, email, role, token_hash, invited_by, expires_at,
      accepted_at, created_at)`.
- [x] Roles: `superadmin`, `org_admin`, `org_member`, `org_viewer` (SPEC §5.2).

### Isolation enforcement — the core of this task
- [x] `TenantScope` request-scoped object derived **only** from the authenticated session, never
      from a path, query, or body parameter.
- [x] `ScopedRepository` base class that injects `WHERE organization_id = :scope` into every
      read, and sets it on every write. Domain repositories inherit from it; a repository that
      needs to bypass scoping must do so through an explicit, named, audit-logged method.
- [x] A test-time guard that fails the suite if a model with an `organization_id` column is
      queried through an unscoped session — cheap insurance against a future regression.
- [x] Cross-tenant access returns **404**, not 403.
- [x] Superadmin cross-org access goes through an explicit `assume_organization(org_id)` call
      that records the access (the record is written to structured logs now; task 15 routes it
      into the audit table).

### Role guards
- [x] `require_capability(...)` dependency, applied per route. *(A capability rather
      than a list of roles — see Deliberate deviations.)*
- [x] Permission matrix, defined once as data rather than scattered conditionals:

      | Capability | superadmin | org_admin | org_member | org_viewer |
      |---|---|---|---|---|
      | View org resources | ✓ | ✓ | ✓ | ✓ |
      | Create/edit connectors, gateways, models | ✓ | ✓ | ✓ | — |
      | Reveal/create/revoke API keys | ✓ | ✓ | — | — |
      | Manage members, roles, invitations | ✓ | ✓ | — | — |
      | Manage organizations, global catalog, platform settings | ✓ | — | — | — |

- [x] `GET /api/v1/auth/me` returns the resolved capability set so the UI can hide and disable
      controls from one source of truth.

### Endpoints
- [x] `GET/POST /organizations`, `GET/PATCH /organizations/{id}` (superadmin; org users may read
      and patch only their own).
- [x] `GET /organizations/{id}/members`, `PATCH /members/{id}`, `DELETE /members/{id}`.
- [x] `POST /organizations/{id}/invitations`, `GET /invitations`, `DELETE /invitations/{id}`.
- [x] `GET /invitations/accept/{token}` (public, validates), `POST /invitations/accept/{token}`
      (public, creates the user with a password).
- [x] Guards: an org must keep at least one active `org_admin`; a user cannot remove or demote
      themselves out of the last admin slot.

### UI
- [x] **Platform → Organizations** (superadmin): list with member and gateway counts, create,
      suspend, and an "open as" action for support that surfaces a persistent banner naming the
      organization being viewed.
- [x] **Settings → Members**: table with role editing, remove, pending invitations with the
      copyable link, and resend/revoke.
- [x] **Settings → Organization**: name, slug, org-level defaults.
- [x] Invitation acceptance page: token validation, password creation, auto-login.
- [x] Capability-driven UI: navigation entries and buttons render from `me.capabilities`.

## Acceptance criteria

- [x] A user in org A cannot read, modify, or discover any resource in org B through any endpoint,
      including by guessing ids.
- [x] Every role's permission matrix row is verified by an automated test, not by inspection.
- [x] The last org_admin cannot be removed or demoted.
- [x] Invitations expire (default 7 days) and are single-use.
- [x] Superadmin "open as" shows a persistent banner and writes an access record.
      *(The record is a structured log line carrying `audit_action`; task 15 routes
      the same event into `audit_events`.)*
- [x] The UI hides controls the current role cannot use, and the API still rejects them if called
      directly.

## Tests

- **A dedicated cross-tenant test module** that, for every org-scoped endpoint, asserts a
  foreign-org id returns 404. Add to it in every subsequent task — this is the regression net.
- Role matrix: each capability × each role.
- Invitation lifecycle: create, accept, expire, reuse, revoke.
- Last-admin protection.
- The unscoped-query guard fires when scoping is deliberately bypassed in a test.

## Notes

- Row-level security in Postgres was considered as a second layer. It is not required for v1, but
  keep `organization_id` as the first column of every composite index so adding RLS later is
  mechanical.
- Do not let any later task introduce an endpoint that takes `organization_id` as a parameter for
  an org user. That is the shape every multi-tenant leak takes.

---

## Verification status

Everything below was run on this machine unless the table says otherwise.

```
uv run ruff check .              All checks passed!
uv run ruff format --check .     113 files already formatted
uv run mypy                      Success: no issues found in 108 source files
uv run pytest -q                 764 passed, 109 skipped

npm --prefix web run lint        clean
npm --prefix web run typecheck   clean
npm --prefix web run test        106 passed
npm --prefix web run build       built in 1.70s
```

The generated API client was regenerated and is stable: `web/openapi.json` and
`web/src/api/schema.d.ts` reproduce byte-for-byte, and CI fails on drift.

### The live run

The whole demo was driven through the real HTTP stack — real routing, real cookies, real
tokens, real exception handlers — with only persistence in memory, because no PostgreSQL
on this machine accepts the credentials in `.env`:

```
superadmin /auth/me carries capabilities        ok   platform:administer present
superadmin creates an organization              ok   201
superadmin sees every organization              ok   [acme, globex, initech]
open as narrows to one organization             ok   only Globex's members
superadmin invites an admin                     ok   201
the link points at the UI                       ok   .../invitations/accept/<token>
an anonymous visitor can preview the link       ok   200
the preview leaks no organization id            ok   [email, expires_at, organization_name, role]
accepting creates the account                   ok   200
and signs them in                               ok   mg_refresh cookie set
with the right role and capabilities            ok   org_admin
the link is single-use                          ok   404 on replay
the new admin sees only their organization      ok   [initech]
a foreign id is 404, not 403                    ok   404
and identical to an id that does not exist      ok   same code and message
the assume header does nothing for an org user  ok   [initech]
a viewer can read the member list               ok   200
a viewer cannot change a member                 ok   403
nor create an organization                      ok   403
the last admin cannot demote themselves         ok   409, "appoint another one first"
a superadmin suspends an organization           ok   200
an open session stops working at once           ok   401 organization_suspended
and they cannot sign in again                   ok   401
the superadmin is unaffected                    ok   200

24/24 steps ok
```

Separately, the real app was served with the built SPA mounted (`WEB_DIST_DIR=web/dist`)
and every backing service down:

| Request | Result |
|---|---|
| `GET /settings/members`, `/platform/organizations` | 200 `text/html` — history fallback covers the new routes |
| `GET /invitations/accept/<token>` | 200 `text/html` — the acceptance page is a real SPA route |
| `GET /assets/missing-deadbeef.js` | 404 JSON |
| `GET /api/v1/organizations`, `/api/v1/invitations` | 401 + `WWW-Authenticate: Bearer` |
| `PATCH /api/v1/members/{id}` | 401 — the authenticated-by-default router covers the new routes |
| `GET /healthz` / `/readyz` | 200 / 503 |

The two 500s in that run (`/auth/login`, `/invitations/accept/...`) both trace to
`asyncpg.InvalidPasswordError` — the database, not this code. No `UnscopedQuery` escaped
to a response, which is what a missing scope declaration would have looked like.

### Not verifiable here

| Item | Why | What stands in for it |
|---|---|---|
| The migration against a real PostgreSQL | A server is listening on `localhost:5432`, but it rejects the credentials in `.env` (`password authentication failed for user "gateway"`) | `tests/test_migration_offline.py` renders the DDL and compares every table, column and constraint against the models; `tests/test_directory_db.py` runs the store contract, the constraints and the live scope guard in CI via `REQUIRE_DB_TESTS=1` |
| The partial unique index and the CHECK constraints | Same | Asserted in `tests/test_directory_db.py`; a CHECK is only real if the server enforces it, so those tests are the only proof |
| `docker compose up` | Docker is unavailable (inherited from task 01) | The compose file and Dockerfile parse and are internally consistent; the image build runs in CI |
| Playwright | No browser binaries, and it needs the API, the database and Vite all up | The invitation flow is covered in jsdom (`web/src/pages/AcceptInvitationPage.test.tsx`) and over HTTP (the live run above) |
| `make` targets as targets | `make` is not installed on this machine | Every target's underlying command was run directly |

### Notes for later tasks

- **`tests/test_cross_tenant.py` is the regression net.** Add a `ScopedEndpoint` row for
  every org-scoped endpoint a later task introduces.
  `test_the_net_covers_every_scoped_route` fails if you forget: it compares the table
  against the OpenAPI schema.
- **New tables carrying `organization_id` are guarded automatically** — membership is
  derived from the schema, not from a list. Give them a repository in
  `app/db/repositories.py` and they inherit isolation.
- **A query that must span tenants** calls `app.db.scoping.unscoped("reason")`. There are
  eight today — login, the access-token lookup, gateway resolution on the data plane, the
  global email and slug uniqueness checks, invitation acceptance, the grouped counts, and
  the seeding CLI. `grep -r "unscoped(" app/` is the complete audit.
- **`organization_id` leads every composite index**, so adding PostgreSQL row-level
  security later stays mechanical (task 04 Notes).
- **New capabilities** go in `app/services/permissions.py` and get a row in
  `tests/test_permissions.py`, which is transcribed from SPEC §5.2 rather than derived
  from the code it tests.
- **`app/services/directory.py` writes `audit_action` into its log lines.** Task 15 turns
  those into `audit_events` rows; the fields are already there.

### Deliberate deviations

- **`require_capability(...)` instead of `require_role(*roles)`.** The task asks for both
  a role dependency and a matrix "defined once as data rather than scattered
  conditionals", and the two pull in opposite directions: a route naming roles has to be
  edited whenever a role is added, which is the scattering the second bullet forbids. So
  there is one mechanism, and a route names the capability it needs.
- **The org profile rides on the "manage members" capability** rather than getting a
  sixth matrix row. Settings → Organization and Settings → Members are one screen area
  with one audience, and a row the matrix table does not contain is a row nobody tests.
- **"Resend" rotates the token instead of re-showing the link.** Only `sha256(token)` is
  stored, so the original is genuinely unrecoverable — and rotating is the safer reading
  of resending anyway: if the first link went to the wrong address, this takes it away.
  The UI therefore has a "New link" action rather than a copy button on every row.
- **A superadmin narrows scope with an `X-Assume-Organization` header**, read in one
  dependency, so no endpoint signature ever takes an organization id it might trust. It
  is *ignored* for everyone else rather than refused: a 403 would tell an org admin the
  header exists and is worth attacking.
- **Organizations are suspended, not deleted.** `DELETE /organizations/{id}` is not
  implemented — it would cascade through every table a later task adds, and none of the
  acceptance criteria need it.

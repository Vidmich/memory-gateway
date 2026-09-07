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
- [ ] Extend `organizations` with `settings_jsonb` (org-level defaults consumed by later tasks)
      and `status` (`active` / `suspended`).
- [ ] `users.organization_id` is `NULL` only for `superadmin`; enforce with a check constraint.
- [ ] `invitations(id, organization_id, email, role, token_hash, invited_by, expires_at,
      accepted_at, created_at)`.
- [ ] Roles: `superadmin`, `org_admin`, `org_member`, `org_viewer` (SPEC §5.2).

### Isolation enforcement — the core of this task
- [ ] `TenantScope` request-scoped object derived **only** from the authenticated session, never
      from a path, query, or body parameter.
- [ ] `ScopedRepository` base class that injects `WHERE organization_id = :scope` into every
      read, and sets it on every write. Domain repositories inherit from it; a repository that
      needs to bypass scoping must do so through an explicit, named, audit-logged method.
- [ ] A test-time guard that fails the suite if a model with an `organization_id` column is
      queried through an unscoped session — cheap insurance against a future regression.
- [ ] Cross-tenant access returns **404**, not 403.
- [ ] Superadmin cross-org access goes through an explicit `assume_organization(org_id)` call
      that records the access (the record is written to structured logs now; task 15 routes it
      into the audit table).

### Role guards
- [ ] `require_role(*roles)` dependency, applied per router.
- [ ] Permission matrix, defined once as data rather than scattered conditionals:

      | Capability | superadmin | org_admin | org_member | org_viewer |
      |---|---|---|---|---|
      | View org resources | ✓ | ✓ | ✓ | ✓ |
      | Create/edit connectors, gateways, models | ✓ | ✓ | ✓ | — |
      | Reveal/create/revoke API keys | ✓ | ✓ | — | — |
      | Manage members, roles, invitations | ✓ | ✓ | — | — |
      | Manage organizations, global catalog, platform settings | ✓ | — | — | — |

- [ ] `GET /api/v1/auth/me` returns the resolved capability set so the UI can hide and disable
      controls from one source of truth.

### Endpoints
- [ ] `GET/POST /organizations`, `GET/PATCH /organizations/{id}` (superadmin; org users may read
      and patch only their own).
- [ ] `GET /organizations/{id}/members`, `PATCH /members/{id}`, `DELETE /members/{id}`.
- [ ] `POST /organizations/{id}/invitations`, `GET /invitations`, `DELETE /invitations/{id}`.
- [ ] `GET /invitations/accept/{token}` (public, validates), `POST /invitations/accept/{token}`
      (public, creates the user with a password).
- [ ] Guards: an org must keep at least one active `org_admin`; a user cannot remove or demote
      themselves out of the last admin slot.

### UI
- [ ] **Platform → Organizations** (superadmin): list with member and gateway counts, create,
      suspend, and an "open as" action for support that surfaces a persistent banner naming the
      organization being viewed.
- [ ] **Settings → Members**: table with role editing, remove, pending invitations with the
      copyable link, and resend/revoke.
- [ ] **Settings → Organization**: name, slug, org-level defaults.
- [ ] Invitation acceptance page: token validation, password creation, auto-login.
- [ ] Capability-driven UI: navigation entries and buttons render from `me.capabilities`.

## Acceptance criteria

- [ ] A user in org A cannot read, modify, or discover any resource in org B through any endpoint,
      including by guessing ids.
- [ ] Every role's permission matrix row is verified by an automated test, not by inspection.
- [ ] The last org_admin cannot be removed or demoted.
- [ ] Invitations expire (default 7 days) and are single-use.
- [ ] Superadmin "open as" shows a persistent banner and writes an access record.
- [ ] The UI hides controls the current role cannot use, and the API still rejects them if called
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

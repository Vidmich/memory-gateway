# Task 15 — Audit log

**Slice:** every configuration change is attributable to a person, with a before/after diff.
**Depends on:** 06 (ideally after 05 and 14 so their mutations are covered from the start)
**Spec:** §10.4, §13.1 (Audit log)
**Size:** S

---

## Why this slice

The system holds provider credentials for multiple customers and lets support staff view any
organization. "Who changed this and when" stops being a nice-to-have the first time a gateway
misbehaves in production or a customer asks how their key was used. It is small, self-contained,
and worth doing before the first external user.

## Demo at the end of this task

Change a gateway's system prompt, rotate a model credential, revoke a key, and change a member's
role. Open **Audit log**: four entries, each with actor, timestamp, target, and an expandable
diff. The credential change shows `credential: "***" → "***"` — that it changed, never what it
changed to.

As superadmin, use "open as" on another organization. An entry records the cross-org access,
visible to that organization's own admins.

## In scope

- `audit_events` table, recording service, and hooks on every control-plane mutation.
- Secret-safe diffing, the audit UI, and CSV export.

## Out of scope

- Data-plane request logging (that is task 07).
- Tamper-proofing (hash chaining, external log shipping) — deferred to task 18's hardening.
- Alerting on suspicious events.

## Work items

### Model
- [x] `audit_events(id, organization_id, actor_user_id, actor_type, action, target_type,
      target_id, target_label, diff_jsonb, ip, user_agent, request_id, created_at)`.
- [x] `actor_type` ∈ {`user`, `system`, `superadmin_impersonation`} — background jobs mutate
      configuration too (task 17's reindex, task 09's connector deletion) and those need
      attribution as well.
- [x] `target_label` denormalizes the human name at the time of the event, so the log stays
      readable after the target is deleted.
- [x] Append-only: no update or delete path in the repository, enforced by a database grant in
      production rather than by convention.
- [x] Indexes on `(organization_id, created_at DESC)`, `(actor_user_id, created_at DESC)`, and
      `(target_type, target_id)`.

### Recording
- [x] `AuditService.record(...)` called from the service layer, not from routers — a router-level
      hook would miss mutations made by jobs.
- [x] Cover: organizations, users and roles, invitations, upstream models, connectors and their
      chunking config, documents (delete/reindex), gateways and every config blob, gateway
      targets, API keys (create/revoke), memory facts (manual edit/delete/purge), platform
      settings, and superadmin impersonation.
- [x] Diffing computes changed fields only, comparing the Pydantic models rather than raw rows so
      config-blob changes render as readable paths (`memory_config.doc_top_k: 6 → 10`).
- [x] **Secret redaction is structural, not pattern-based**: fields marked as secret in the schema
      are replaced with `"***"` on both sides before the diff is stored. Never rely on scanning
      values for things that look like keys.
- [x] Recording failures must never fail the mutation — log the failure and increment a counter.
      But write the event **in the same transaction** as the mutation where possible, so a
      committed change cannot lack its record.
- [x] Bulk operations record one event with a count and a sample, not N events.

### API & UI
- [x] `GET /api/v1/audit-events` — cursor-paginated, filterable by actor, action, target type,
      target id, and time range. Org-scoped; superadmins may query across orgs explicitly.
- [x] `GET /api/v1/audit-events/export` — streaming CSV over the current filter, with a row cap
      and a rate limit.
- [x] **Audit log** screen: table (time, actor, action, target) with an expandable per-row diff
      rendered as a field-level before/after list.
- [x] Contextual history: an "Audit" tab on the gateway, model, and connector detail screens,
      pre-filtered to that object — this is where the log actually gets read.
- [x] Impersonation events are visually distinct so a customer reviewing their log can see support
      access clearly.

## Acceptance criteria

- [x] Every mutating control-plane endpoint produces exactly one audit event — verified by a test
      that enumerates mutating routes and asserts coverage, so a future endpoint added without an
      audit hook fails CI.
- [x] No secret value appears in any diff, for any field, verified by a test that mutates every
      secret-marked field and greps the stored payload.
- [x] Events are immutable: no API path updates or deletes one.
- [x] Deleting a target leaves its events readable via `target_label`.
- [x] Superadmin impersonation is recorded and visible to the impersonated organization.
- [x] Background-job mutations are attributed to `actor_type: system`.
- [x] Cross-tenant test module extended: org A cannot read org B's audit events.

## Tests

- Route-coverage test enumerating mutating endpoints against recorded actions.
- Diff generation: added, removed, changed, and nested config-blob fields.
- Secret redaction across every secret-marked field.
- Immutability: repository exposes no mutation path; database grant blocks it.
- CSV export correctness and cap enforcement.
- Job-originated events carry the right actor type.

## Notes

- The route-coverage test is the point of this task. Without it, audit coverage decays the moment
  someone adds an endpoint — and an audit log with gaps is worse than none, because it is trusted.
- Hash-chaining events for tamper evidence is deliberately deferred. It only matters once the log
  is shipped somewhere the operator cannot rewrite, which belongs with task 18's deployment work.


---

## Verification status

Everything above is implemented and covered. What follows is the reasoning worth carrying
forward, then the numbers.

### Divergences from the task text, and why

**Recording is a method on the store transaction, not on `AuditService`.** The work items
ask for `AuditService.record(...)` called from the service layer, and for the event to be
written in the same transaction as the mutation. Those two pull apart the moment you try
to write them: a service object holding its own session cannot join a unit of work it does
not own, so "same transaction" would become "same-ish transaction, usually". Instead the
recorder is a mixin on every store transaction, so a call site reads
`transaction.audit(actor, "gateway.update", before=…, after=…)` on the line after the
change and commits with it. `AuditService` is the read half — the screen, the filters and
the CSV. The task's intent survives intact: recording happens in the *service* layer, never
in a router, so a mutation made by a background job is recorded on exactly the same path as
one made by a person.

**Append-only is a trigger, not a grant.** The work item names a database grant. A grant
needs a role name the migration does not have and cannot invent, and it is undone by the
next `GRANT ALL` somebody runs in a hurry. `0014_audit_log` installs a `BEFORE UPDATE OR
DELETE` trigger instead, which holds for the ORM, for `psql`, and for a migration written
at two in the morning — and needs nothing configured for it to be true. The grant is the
right *deployment* half and it is written down in the README as task 18's, next to the
hash-chaining this task explicitly defers.

**Contextual history is a panel, not a tab.** The gateway, model and connector screens are
single scrolling forms with sections, not tabbed layouts — a tab bar for one extra view
would be a second navigation idiom on three screens that do not have one. The panel sits at
the bottom, collapsed, and fetches nothing until it is opened.

**Platform settings are listed as a target type and have no hook.** They do not exist yet:
`platform_settings` is task 17's table, and there is no endpoint to record. The type is
reserved in `TARGET_TYPES` so the hook that task adds is a call site rather than a
migration, and the coverage test will demand one the moment the route appears.

### Decisions worth keeping

**Redaction is structural, and the plaintext never enters a snapshot.** Not a scan for
values that look like credentials — a scan is a guess, and the one it misses is the one
that ends up in an immutable table. A secret is wrapped in `Sensitive`, which carries a
digest used only for *comparison* and renders as `"***"`. The log can therefore say a
credential was replaced and can never say what it was replaced with, which is what SPEC
§5.4 says about credentials everywhere else. The same marker covers a password hash, an
invitation token, and — this is the part worth arguing about — the **values** of
`extra_headers` and a connector's `config`. Those are free-form maps that already have
somewhere for an `api-key` to go, and an append-only table is the wrong place to discover
somebody used them for one. Keys stay visible, so `extra_headers.api-key: "***" → "***"`
still says which header changed.

**End-user content stays out; the end user's id does not.** A memory fact's text is a
sentence about a person, and SPEC §6.5 gives that person the right to have it erased —
which is the one thing this table cannot do. So a manual edit records the *shape* of the
change (kind, confidence, expiry, supersession) and not the sentence. The external id is
the opposite call and for the opposite reason: an erasure request is *made* with it, and a
log that cannot say whose memory was purged cannot be used to show that it was.

**"Missing" and "null" are different, and the diff keeps them apart.** A change omits the
`before` key entirely when the field did not exist, and carries `"before": null` when it
existed and held nothing. That distinction is not pedantry here: `null → "***"` is a
credential being set on a model that had none, and `(absent) → "***"` is a header being
added. The API turns it into one `kind` field so a client switches on a value instead of
inspecting which keys the JSON happens to have.

**Impersonation is derived, not passed.** A person acting at *platform scope* carries no
organization of their own, so an event of theirs that lands in an organization's log is, by
construction, a platform administrator inside a customer. That one rule covers all three
routes to it — the `X-Assume-Organization` header, `DirectoryService._narrow`, and a direct
call with neither — including the one a call site would forget. What a customer wants to
find in their own log is "somebody from the vendor was in here", and a filter that depends
on every hook remembering to say so is a filter with holes in it.

**A support session is one event, not forty.** Opening an organization and reading three
screens is dozens of requests carrying the same header. Recording each would produce a log
nobody can read and a table growing at the rate of somebody scrolling, so the first assumed
request in a fifteen-minute window writes and the rest are the same visit — debounced
through the Redis counter the login throttle already uses, so two replicas serving one visit
still write one event. It fails **open**: duplicate records of a support access are noise, a
missing one is the failure this exists to prevent.

**Building an event never fails a mutation; writing one shares its fate.** `audit()` catches
everything a snapshot or a diff can raise, logs it, and increments
`audit_event_failures_total` — a defect in a snapshot function must not be able to stop
somebody saving a gateway. The *write* is deliberately the other way round: the row is added
to the same session, so it commits with the change or not at all. Those two sentences are
the whole failure model, and the counter is what keeps the first of them from being silent.
An audit log with gaps is worse than none, because it is trusted.

**Bulk operations are one event with a count and a sample.** A resync that touched four
hundred documents is one thing that happened; four hundred rows would bury every other event
on the screen, and the documents are on the connector's own tab. The same for an upload of
forty files, and for a purge.

**The log outlives its subjects — including the organization.** There is not a foreign key
on this table. Removing a member deletes their `users` row, and an audit trail whose actor
silently becomes `NULL` is worth very little in the investigation it exists for, so
`actor_label` and `target_label` hold the email and the name *as they were*. The same
applies one level up: a cascade from `organizations` would also be a `DELETE` the trigger
refuses, so the choice was between a table that cannot be tidied up and a customer record
that disappears with the customer. Neither is what a log is for.

**Reading it is `org:read`.** The same decision monitoring made, and it lands the same way:
SPEC §5.2 makes `org_viewer` read-only across the organization, and this table is strictly
*less* sensitive than the request detail view they can already open — one holds
configuration changes, the other holds end-user prompt bodies. Gating it higher would mean
the person answering "who turned this off yesterday" needs the permission to turn it back
on.

### Bugs and near-misses this task found

- **`current_actor` ran in a worker thread.** It was a synchronous FastAPI dependency, so
  the first thing that tried to spawn background work from it — the support-access recorder
  — hit `no running event loop`. It is `async` now although it awaits nothing, which is a
  thing worth knowing about every other sync dependency in this codebase.
- **`MemoryAuthTransaction` called its rows `_state` while every other memory transaction
  calls them `_db`.** Harmless until a mixin depended on the name; renamed, so the six
  in-memory transactions now agree.
- **The migration spelled a check constraint's full name.** `test_constraint_names_match_the
  _models` caught it immediately: the naming convention expands `actor_type_is_known` into
  `ck_audit_events_actor_type_is_known`, and spelling the expanded form makes it expand
  twice. That test was written for exactly this and earned its keep.
- **This repo has no `prettier` config, and `eslint` is the only formatter.** Reaching for
  `npx prettier` on the new files reformatted eleven of them — including eight that already
  existed — into a different quote and semicolon style before that was noticed. Reverted
  from `HEAD` and re-applied by hand. The tools that touch this repo are the ones in the
  `Makefile`, and nothing else.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   299 files already formatted
uv run mypy                    Success: no issues found in 284 source files
uv run pytest -q               2854 passed, 411 skipped

npx eslint . / npx tsc         clean
npx vitest run                 560 passed (25 files)
npm run build                  453.33 kB JS (132.74 kB gzipped)
```

Task 15 adds **142 backend checks** across five new modules plus three rows on the
cross-tenant net, and **40 web checks**. Twenty of the backend ones are
`tests/test_audit_db.py` and skip without PostgreSQL — see below.

`make openapi` regenerates `web/openapi.json` and `web/src/api/schema.d.ts`; both are
committed and byte-stable.

### Not verifiable on this machine

- **No PostgreSQL.** `tests/test_audit_db.py` skips, and with it the two checks that matter
  most here: that the database itself refuses an `UPDATE` and a `DELETE` on `audit_events`.
  The in-memory store runs the same contract and the same filters, but it cannot refuse a
  write nobody asks it to make — the trigger is the only thing that proves append-only, and
  it exists only on a real server. That is the gap this task most wants closed in CI.
- **No Redis.** The support-access debounce runs against `MemoryThrottleStore` in tests, so
  what is checked is that a second request inside the window writes nothing. That two
  *replicas* agree is a property of `RedisThrottleStore`, which task 03 covers where Redis is
  available.
- **The database grant is a deployment fact, not a test.** `SELECT, INSERT` and nothing else
  for the application role belongs with task 18's hardened setup, and until then the trigger
  is what holds.

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
- [ ] `audit_events(id, organization_id, actor_user_id, actor_type, action, target_type,
      target_id, target_label, diff_jsonb, ip, user_agent, request_id, created_at)`.
- [ ] `actor_type` ∈ {`user`, `system`, `superadmin_impersonation`} — background jobs mutate
      configuration too (task 17's reindex, task 09's connector deletion) and those need
      attribution as well.
- [ ] `target_label` denormalizes the human name at the time of the event, so the log stays
      readable after the target is deleted.
- [ ] Append-only: no update or delete path in the repository, enforced by a database grant in
      production rather than by convention.
- [ ] Indexes on `(organization_id, created_at DESC)`, `(actor_user_id, created_at DESC)`, and
      `(target_type, target_id)`.

### Recording
- [ ] `AuditService.record(...)` called from the service layer, not from routers — a router-level
      hook would miss mutations made by jobs.
- [ ] Cover: organizations, users and roles, invitations, upstream models, connectors and their
      chunking config, documents (delete/reindex), gateways and every config blob, gateway
      targets, API keys (create/revoke), memory facts (manual edit/delete/purge), platform
      settings, and superadmin impersonation.
- [ ] Diffing computes changed fields only, comparing the Pydantic models rather than raw rows so
      config-blob changes render as readable paths (`memory_config.doc_top_k: 6 → 10`).
- [ ] **Secret redaction is structural, not pattern-based**: fields marked as secret in the schema
      are replaced with `"***"` on both sides before the diff is stored. Never rely on scanning
      values for things that look like keys.
- [ ] Recording failures must never fail the mutation — log the failure and increment a counter.
      But write the event **in the same transaction** as the mutation where possible, so a
      committed change cannot lack its record.
- [ ] Bulk operations record one event with a count and a sample, not N events.

### API & UI
- [ ] `GET /api/v1/audit-events` — cursor-paginated, filterable by actor, action, target type,
      target id, and time range. Org-scoped; superadmins may query across orgs explicitly.
- [ ] `GET /api/v1/audit-events/export` — streaming CSV over the current filter, with a row cap
      and a rate limit.
- [ ] **Audit log** screen: table (time, actor, action, target) with an expandable per-row diff
      rendered as a field-level before/after list.
- [ ] Contextual history: an "Audit" tab on the gateway, model, and connector detail screens,
      pre-filtered to that object — this is where the log actually gets read.
- [ ] Impersonation events are visually distinct so a customer reviewing their log can see support
      access clearly.

## Acceptance criteria

- [ ] Every mutating control-plane endpoint produces exactly one audit event — verified by a test
      that enumerates mutating routes and asserts coverage, so a future endpoint added without an
      audit hook fails CI.
- [ ] No secret value appears in any diff, for any field, verified by a test that mutates every
      secret-marked field and greps the stored payload.
- [ ] Events are immutable: no API path updates or deletes one.
- [ ] Deleting a target leaves its events readable via `target_label`.
- [ ] Superadmin impersonation is recorded and visible to the impersonated organization.
- [ ] Background-job mutations are attributed to `actor_type: system`.
- [ ] Cross-tenant test module extended: org A cannot read org B's audit events.

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

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
- [ ] Extend `gateways` to the full SPEC §14 field list: `description`, `enabled`,
      `routing_mode` (default `single`), `system_context`, `param_overrides_jsonb`,
      `locked_params_jsonb`, `memory_config_jsonb`, `logging_config_jsonb`, `limits_jsonb`.
      The three jsonb config blobs are created here with schema-validated defaults so later tasks
      only add fields.
- [ ] Each config blob is a versioned Pydantic model with defaults, so an old row missing a new
      key still loads.
- [ ] Slug rules: lowercase alphanumeric and hyphens, 3–63 chars, globally unique (it appears in
      a public URL), immutable after creation — changing it would silently break every deployed
      client. Offer clone-with-new-slug instead.
- [ ] Reserved slugs blocklist (`api`, `admin`, `health`, `metrics`, `g`, `www`).

### API keys
- [ ] `POST /gateways/{id}/keys` returns the plaintext **once**; it is never retrievable again.
- [ ] `GET /gateways/{id}/keys` lists id, name, prefix, created, last used, revoked.
- [ ] `DELETE /keys/{id}` revokes (soft, `revoked_at`) — never hard-delete, so historical request
      logs in task 07 keep a resolvable reference.
- [ ] Optional `expires_at` on creation.
- [ ] Key creation and revocation restricted to `org_admin`+ per the task 04 matrix.
- [ ] `last_used_at` written at most once per minute per key (batched via Redis) so a hot key
      does not generate a write per request.

### Endpoints
- [ ] `GET|POST /api/v1/gateways`, `GET|PATCH|DELETE /api/v1/gateways/{id}`.
- [ ] `POST /api/v1/gateways/{id}/test` — sends a probe completion **through the real proxy path**
      using an internal credential, returning the assembled prompt, the response, and timings.
      This is the single most useful debugging affordance before task 07's monitoring exists.
- [ ] `PATCH` accepts partial config-blob updates with deep merge, validated against the blob
      schema.

### Config caching
- [ ] `GatewayResolver` (the seam from task 02) gains a Redis cache keyed by slug, holding the
      gateway, its targets, and the resolved model records.
- [ ] Invalidate on any write to the gateway, its targets, its keys, or a referenced model. Use a
      version counter per gateway rather than blind deletes so a concurrent write cannot resurrect
      stale config.
- [ ] Short TTL (60 s) as a backstop against a missed invalidation.
- [ ] A revoked key must stop working **immediately**, not on cache expiry — key lookup checks
      revocation against the database or a dedicated revocation set, never a cached copy.

### Proxy integration
- [ ] Disabled gateway → 403 with an explicit message.
- [ ] Gateway with no enabled target → 503 naming the problem.
- [ ] `locked_params` enforced: client-supplied values for locked keys are ignored rather than
      merged, and the response header notes an override occurred.

### UI
- [ ] **Gateways list**: name, slug, endpoint URL with copy, mode, target model, key count,
      enabled toggle, 24 h request count (placeholder until task 07).
- [ ] **Gateway editor**, sectioned so later tasks slot in:
      1. *Identity* — name, slug (create-only, with live URL preview), description, enabled.
      2. *Routing* — single-target model picker. Mode selector present but limited to `single`.
      3. *Memory* — placeholder: "Configured in a later release."
      4. *Prompt* — system context textarea, param overrides with per-param lock toggles, and a
         live **assembled prompt preview** showing exactly what will be sent.
      5. *Logging* — placeholder.
      6. *Limits* — placeholder.
      7. *Keys* — table, create dialog, reveal-once modal with copy and an explicit "you will not
         see this again" warning, revoke with typed confirmation.
- [ ] **Test gateway** button in the editor: type a message, see the assembled prompt, the
      response, and the latency breakdown.
- [ ] Unsaved-changes guard when navigating away from the editor.
- [ ] Delete gateway requires typing the slug, and warns that deployed clients will break.

## Acceptance criteria

- [ ] A new org can go from empty to a working endpoint entirely in the UI, in under two minutes,
      with no server restart.
- [ ] The key secret is displayed exactly once and is unrecoverable afterward.
- [ ] Revoking a key stops the next request immediately, even with a warm config cache.
- [ ] Editing the system prompt affects the very next request.
- [ ] Slugs are globally unique and immutable; reserved slugs are rejected.
- [ ] Locked params override client values; the header reports it.
- [ ] Cross-tenant test module extended: org A cannot read, patch, delete, or create keys on
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

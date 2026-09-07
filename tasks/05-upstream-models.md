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
- [ ] Extend `upstream_models` to the full SPEC §8.4 field list.
- [ ] `scope` ∈ {`global`, `org`}; `organization_id` NULL iff `scope = global`, enforced by check
      constraint.
- [ ] Credentials stored via the envelope-encryption helper from task 02.
- [ ] **Write-only credential API.** Responses return `{"configured": true, "hint": "sk-…4f2a"}`.
      There is no endpoint, for any role including superadmin, that returns a stored credential.
      Rotation is replacement, not reveal.
- [ ] `PATCH` with credential omitted keeps the existing one; an explicit `null` clears it.
- [ ] `default_params` validated against a known parameter allowlist with sane bounds, so a typo
      does not become a 400 from the provider at request time.

### Visibility rules
- [ ] Org users list: all enabled `global` models (metadata only) + their own org models (full,
      minus credentials).
- [ ] Only superadmins create, edit, or delete `global` models.
- [ ] Deleting a model referenced by a gateway target is blocked with a message naming the
      gateways. Disabling it is allowed and takes effect immediately.

### Endpoints
- [ ] `GET /api/v1/models` — scope-aware, filterable by `scope` and `enabled`, cursor-paginated.
- [ ] `POST /api/v1/models`, `GET|PATCH|DELETE /api/v1/models/{id}`.
- [ ] `POST /api/v1/models/{id}/test` — sends a minimal completion
      (`max_tokens: 1`, a trivial prompt) and returns
      `{ok, latency_ms, upstream_status, error_message, model_echo}`.
- [ ] `POST /api/v1/models/test` — same, on an unsaved draft, so configuration can be validated
      before it is stored.
- [ ] Test connection is rate-limited per user and its cost is negligible by construction.

### Proxy integration
- [ ] The proxy resolves the full model record: `base_url`, `dialect`, `upstream_model_id`,
      credentials, `extra_headers`, `system_context`, `default_params`, `timeout_seconds`.
- [ ] Parameter merge implemented as an explicit, tested function:
      `model.default_params` → `gateway.param_overrides` → client request.
- [ ] A disabled or deleted model makes its gateway return a clear 503 naming the misconfiguration
      rather than a generic failure.

### UI
- [ ] **Models** screen with two tabs, *Global catalog* and *Our models*.
- [ ] Model form: name, description, base URL, dialect, upstream model id, auth type, credential
      (masked, write-only, showing the hint when already set), extra headers (key/value rows),
      system context (textarea with a character count), default params, timeout, enabled.
- [ ] Provider presets that prefill base URL and auth type for common targets (OpenAI, Azure
      OpenAI, Groq, Together, OpenRouter, vLLM, Ollama) — most misconfiguration is a wrong base
      URL.
- [ ] **Test connection** inline: spinner → green with latency, or red with the verbatim upstream
      error.
- [ ] Delete confirmation requiring the typed model name; blocked-delete shows the referencing
      gateways as links.

## Acceptance criteria

- [ ] No API response, log line, or error message contains a stored credential — verified by a
      test that greps serialized responses for the known secret.
- [ ] An org user cannot create, edit, or delete a global model, nor read its credential hint.
- [ ] Test connection reports the real upstream error text for a bad key, a bad URL, and a
      timeout.
- [ ] Editing a model changes proxy behavior on the next request with no restart.
- [ ] Deleting a referenced model is blocked and names the referencing gateways.
- [ ] Cross-tenant test module extended: org A cannot see or touch org B's models.

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

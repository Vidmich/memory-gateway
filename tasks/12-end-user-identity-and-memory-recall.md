# Task 12 — End-user identity & conversation-memory recall

**Slice:** the gateway knows *who* is asking, and injects durable facts about that person.
**Depends on:** 10
**Spec:** §6.1 (B), §6.2, §6.5, §7 (layer 4), §13.1 (Memory browser)
**Size:** M

---

## Why this slice

Conversation memory has two halves: recalling facts and writing them. This task builds recall
and ships with **manual fact entry**, so the whole feature is demonstrable and testable before
the distillation worker (13) exists. Splitting it this way means the injection path is already
proven when the harder, less deterministic half arrives.

## Demo at the end of this task

In the **Memory browser**, add two facts for end user `alice`: "Prefers Python" and "Works in the
EU, needs GDPR-compliant answers."

Call the gateway with `user: "alice"` and ask "how should I store customer emails?" — the answer
reflects GDPR. Call with `user: "bob"` and ask the same thing — it does not. The request detail
drawer shows exactly which facts were recalled and their scores.

`X-Gateway-Memory: off` suppresses both documents and facts, proving the difference.

## In scope

- End-user identity and session resolution.
- `memory_facts` storage plus the per-org memory vector collection.
- Recall path, injection as prompt layer 4, per-gateway memory settings.
- Manual fact CRUD, end-users list, memory browser, and the erasure endpoint.

## Out of scope

- Automatic distillation (13). Facts are created manually or by API here.
- Agentic memory tools (SPEC §16.5).
- Cross-organization or cross-gateway memory sharing — memory is scoped to
  `(organization, end_user)`.

## Work items

### Identity resolution
- [ ] Resolve the end user in the order given by SPEC §6.2:
      1. `X-Gateway-User` header
      2. the `user` field in the request body
      3. `anon:{sha256(api_key_id + client_ip)[:16]}` when `allow_anonymous_memory` is on,
         otherwise no end user and conversation memory is skipped
- [ ] Session resolution:
      1. `X-Gateway-Session` header
      2. `sha256` of the serialized message list **minus the final turn**, so a growing thread
         hashes stably across turns
- [ ] `end_users(id, organization_id, external_id, label, first_seen_at, last_seen_at,
      request_count)`, unique on `(organization_id, external_id)`.
- [ ] Upsert on first sight, without blocking the request (fire-and-forget, batched counters).
- [ ] Validate and cap `external_id` length; treat it as untrusted input and never interpolate it
      into a query or a prompt instruction position.
- [ ] Replace the task 08 helper stub with this service; sticky A/B routing now uses the properly
      resolved id.

### Storage
- [ ] `memory_facts(id, organization_id, end_user_id, text, kind, confidence, source_log_id,
      superseded_at, expires_at, created_at, last_seen_at)`; `kind` ∈ {`preference`, `fact`,
      `goal`, `constraint`}.
- [ ] Qdrant collection `org_{org_id}_memory` with payload `{org_id, end_user_id, kind,
      confidence, created_at}` and payload indexes on `end_user_id`.
- [ ] Deleting an end user cascades to facts and vectors.

### Recall
- [ ] `memory_config` gains: `memory_enabled` (true), `memory_top_k` (8),
      `memory_max_tokens` (600), `memory_min_score` (0.3), `allow_anonymous_memory` (false),
      `max_facts_per_user` (500).
- [ ] Search filtered by `end_user_id` **and** `org_id`, excluding rows with `superseded_at` set
      or `expires_at` in the past.
- [ ] Rank by `similarity × confidence × recency_decay`; the decay half-life is a constant, tuned
      once and documented.
- [ ] Always include the N most recent high-confidence facts regardless of similarity — some
      facts ("uses metric units", "is a minor") must apply to every turn, and pure similarity
      search will miss them.
- [ ] **Run concurrently with document retrieval** via the `asyncio.gather` structure prepared in
      task 10, under an independent timeout and the same `on_retrieval_error` policy.

### Injection
- [ ] Fill prompt layer 4 per SPEC §7, rendered as the `## What you know about this user` block
      with one bullet per fact.
- [ ] Enforce `memory_max_tokens`; drop lowest-ranked facts first and record the drops.
- [ ] Truncation order across layers stays as specified: documents first, then memory.
- [ ] Omit the block entirely when there are no facts — never emit an empty heading.

### API & UI
- [ ] `GET /api/v1/end-users` — list with request counts, fact counts, last seen; searchable by
      `external_id`.
- [ ] `GET /api/v1/end-users/{id}` and `GET /api/v1/end-users/{id}/memory`.
- [ ] `POST /api/v1/end-users/{id}/memory` — manual fact creation (this is what makes the slice
      demoable).
- [ ] `PATCH|DELETE /api/v1/memory-facts/{id}`.
- [ ] `DELETE /api/v1/end-users/{id}/memory` — purge all facts and vectors, with an optional
      `include_transcripts` flag. This is the right-to-erasure path (SPEC §6.5) and must be
      complete: Postgres rows, Qdrant points, and optionally transcripts.
- [ ] **Memory browser** UI: end-users table → detail view listing facts with kind, confidence,
      created/last-seen, and superseded state; add, edit, delete; purge with typed confirmation.
- [ ] Semantic search box over a user's facts, so a large memory is navigable.
- [ ] Gateway editor Memory section extended with the conversation-memory settings and a warning
      when memory is enabled but no identity source is configured on the caller's side.
- [ ] Request detail drawer: recalled facts with scores, and dropped-fact notes.

## Acceptance criteria

- [ ] Facts recalled for `alice` are never recalled for `bob`, verified end to end.
- [ ] Anonymous callers get no conversation memory unless `allow_anonymous_memory` is enabled.
- [ ] Memory recall runs concurrently with document retrieval — total retrieval latency is close
      to the slower of the two, not their sum (assert on measured timings).
- [ ] `memory_max_tokens` is never exceeded.
- [ ] Superseded and expired facts are never injected.
- [ ] Always-include facts survive a query with low similarity to them.
- [ ] Purge removes Postgres rows and Qdrant points; a subsequent recall returns nothing.
- [ ] Cross-tenant test module extended: end users, facts, and the memory collection are
      org-isolated.

## Tests

- Identity resolution across all three fallbacks, including precedence and a missing/blank
  `user` field.
- Session hashing stability as a conversation grows, and divergence across different threads.
- Ranking function: similarity, confidence, and recency contributions, plus always-include
  behavior.
- Token budgeting and drop ordering across layers 3 and 4.
- Concurrent retrieval timing.
- Erasure completeness: assert zero rows and zero Qdrant points afterward.
- Adversarial `external_id` values (very long, unicode, prompt-injection-shaped) do not affect
  prompt structure — a fact block is data, and an `external_id` must never become an instruction.

## Notes

- Injected memory is untrusted content: it originates from end-user conversations. Render it
  inside a clearly delimited block and never interpolate it where it could read as a system
  instruction. This matters more once task 13 writes facts automatically.
- The default `allow_anonymous_memory = false` is deliberate. IP-derived identity is a coarse,
  surprising default for anything that persists personal facts.

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
- [x] Resolve the end user in the order given by SPEC §6.2:
      1. `X-Gateway-User` header
      2. the `user` field in the request body
      3. `anon:{sha256(api_key_id + client_ip)[:16]}` when `allow_anonymous_memory` is on,
         otherwise no end user and conversation memory is skipped
- [x] Session resolution:
      1. `X-Gateway-Session` header
      2. `sha256` of the serialized message list **minus the final turn**, so a growing thread
         hashes stably across turns
- [x] `end_users(id, organization_id, external_id, label, first_seen_at, last_seen_at,
      request_count)`, unique on `(organization_id, external_id)`.
- [x] Upsert on first sight, without blocking the request (fire-and-forget, batched counters).
- [x] Validate and cap `external_id` length; treat it as untrusted input and never interpolate it
      into a query or a prompt instruction position.
- [x] Replace the task 08 helper stub with this service; sticky A/B routing now uses the properly
      resolved id.

### Storage
- [x] `memory_facts(id, organization_id, end_user_id, text, kind, confidence, source_log_id,
      superseded_at, expires_at, created_at, last_seen_at)`; `kind` ∈ {`preference`, `fact`,
      `goal`, `constraint`}.
- [x] Qdrant collection `org_{org_id}_memory` with payload `{org_id, end_user_id, kind,
      confidence, created_at}` and payload indexes on `end_user_id`.
- [x] Deleting an end user cascades to facts and vectors.

### Recall
- [x] `memory_config` gains: `memory_enabled` (true), `memory_top_k` (8),
      `memory_max_tokens` (600), `memory_min_score` (0.3), `allow_anonymous_memory` (false),
      `max_facts_per_user` (500).
- [x] Search filtered by `end_user_id` **and** `org_id`, excluding rows with `superseded_at` set
      or `expires_at` in the past.
- [x] Rank by `similarity × confidence × recency_decay`; the decay half-life is a constant, tuned
      once and documented.
- [x] Always include the N most recent high-confidence facts regardless of similarity — some
      facts ("uses metric units", "is a minor") must apply to every turn, and pure similarity
      search will miss them.
- [x] **Run concurrently with document retrieval** via the `asyncio.gather` structure prepared in
      task 10, under an independent timeout and the same `on_retrieval_error` policy.

### Injection
- [x] Fill prompt layer 4 per SPEC §7, rendered as the `## What you know about this user` block
      with one bullet per fact.
- [x] Enforce `memory_max_tokens`; drop lowest-ranked facts first and record the drops.
- [x] Truncation order across layers stays as specified: documents first, then memory.
- [x] Omit the block entirely when there are no facts — never emit an empty heading.

### API & UI
- [x] `GET /api/v1/end-users` — list with request counts, fact counts, last seen; searchable by
      `external_id`.
- [x] `GET /api/v1/end-users/{id}` and `GET /api/v1/end-users/{id}/memory`.
- [x] `POST /api/v1/end-users/{id}/memory` — manual fact creation (this is what makes the slice
      demoable).
- [x] `PATCH|DELETE /api/v1/memory-facts/{id}`.
- [x] `DELETE /api/v1/end-users/{id}/memory` — purge all facts and vectors, with an optional
      `include_transcripts` flag. This is the right-to-erasure path (SPEC §6.5) and must be
      complete: Postgres rows, Qdrant points, and optionally transcripts.
- [x] **Memory browser** UI: end-users table → detail view listing facts with kind, confidence,
      created/last-seen, and superseded state; add, edit, delete; purge with typed confirmation.
- [x] Semantic search box over a user's facts, so a large memory is navigable.
- [x] Gateway editor Memory section extended with the conversation-memory settings and a warning
      when memory is enabled but no identity source is configured on the caller's side.
- [x] Request detail drawer: recalled facts with scores, and dropped-fact notes.

## Acceptance criteria

- [x] Facts recalled for `alice` are never recalled for `bob`, verified end to end.
- [x] Anonymous callers get no conversation memory unless `allow_anonymous_memory` is enabled.
- [x] Memory recall runs concurrently with document retrieval — total retrieval latency is close
      to the slower of the two, not their sum (assert on measured timings).
- [x] `memory_max_tokens` is never exceeded.
- [x] Superseded and expired facts are never injected.
- [x] Always-include facts survive a query with low similarity to them.
- [x] Purge removes Postgres rows and Qdrant points; a subsequent recall returns nothing.
- [x] Cross-tenant test module extended: end users, facts, and the memory collection are
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


---

## Verification status

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   249 files already formatted
uv run mypy                    Success: no issues found in 236 source files
uv run pytest -q               2409 passed, 355 skipped

npx eslint . / npx tsc         clean
npx vitest run                 440 passed (19 files)
npm run build                  419.21 kB JS (123.14 kB gzipped)
```

OpenAPI regenerated; `web/openapi.json` and `web/src/api/schema.d.ts` are in step with the
server.

### The demo, verified

Walked over a real socket — one app on a real port under uvicorn, a real HTTP client, a
scriptable provider on a second socket, and the **control plane and data plane sharing one
end-user store and one fact index**, which is the join the test suite cannot prove on its
own because there the two halves live in two harnesses. **37/37 checks**:

* `alice` and `bob` both appear in the end-users list the first time a request identifies
  them, neither flagged anonymous;
* two facts added for `alice` through the memory browser API come back with confidence
  1.0, and the browser's semantic search finds the GDPR one;
* the same question — "how should I store customer emails?" — sent as `alice` carries
  `Works in the EU, needs GDPR-compliant answers.` into the prompt, and sent as `bob`
  carries none of it and his own fact instead;
* the OpenAI `user` body field identifies exactly as the header does;
* `X-Gateway-Memory: off` carries no facts, no memory block at all, and no
  `X-Gateway-Memory-Facts` header;
* an unidentified caller gets no conversation memory;
* retracting a fact stops it being injected on the very next request while the fact beside
  it still is, and the retracted row stays visible on the screen that explains last
  month's answer while dropping out of the live-only view;
* every request is attributed to an end user and a session on its log row, with the
  recalled facts recorded by **text** as well as by id, and with their scores;
* a purge reports what it removed, leaves zero vectors, leaves the end-user row, and a
  recall immediately afterwards finds nothing — while `bob` is untouched.

### What is different from the plan, and why

* **SPEC §6.2's session fallback is not implemented literally, because as written it does
  not do what it says.** The SPEC asks for "the serialized message list minus the final
  turn, so a growing conversation hashes to a stable thread id across turns", and those
  two clauses describe different things: turn 3 sends `[u1, a1, u2, a2, u3]` where turn 2
  sent `[u1, a1, u2]`, so dropping the last message leaves `[u1, a1, u2, a2]` against
  `[u1, a1]` — a different hash every turn, and therefore not a thread id at all. What
  *is* byte-identical on every request of one thread is its **opening**: the messages up
  to and including the first user turn. That is what is hashed, salted with the end user
  so two people opening with "hi" are two threads. The stated property is kept; the stated
  mechanism is not. Task 13 debounces on this value, so an id that changed every turn
  would have defeated the coalescing it exists for.
* **Recall is two reads, and the second one is the reason it works.** SPEC §6.3 asks for
  the always-include set in one line; it is half the feature. A dense search for "how
  should I store customer emails?" finds "prefers Python" long before "works in the EU",
  because the first shares a topic word and the second shares almost nothing — and the
  second is the fact that changes the answer. Similarity alone would ship a feature whose
  most important facts are the ones it never recalls.
* **PostgreSQL is the record; Qdrant is an index.** The vector search returns ids and
  nothing else; text, confidence, supersession and expiry come from `memory_facts` with
  the liveness predicate applied in SQL. That makes "a retracted fact is never injected" a
  property of one `WHERE` clause rather than of a payload staying in step with a row it
  cannot see — and a payload claiming `superseded: false` about a row that says otherwise
  is exactly the leak this feature must not have. Supersession *also* deletes the vector,
  so the rule holds twice, independently.
* **The point id is the fact id.** No UUIDv5 derivation, unlike the document store: a fact
  is one point, so its own id is already unique and already deterministic, and deleting
  one becomes a delete-by-id rather than a delete-by-filter.
* **The recency half-life is a constant, not a setting.** Ninety days, decaying on
  `last_seen_at` rather than `created_at`: a preference stated two years ago and restated
  last week is current, and reading the creation date would bury it under something newer
  and less true. It is a statement about how fast people change rather than about any one
  deployment, and a per-gateway knob would be a number nobody has the data to set.
* **The score is a product, not a weighted sum.** Each factor is a fraction of "how much
  should this count", and a fact that fails one badly should not be rescued by the other
  two — a very recent, very confident fact about something else must not outrank the one
  that answers the question.
* **Identity resolution and memory are separate services.** A gateway with conversation
  memory switched off still attributes its traffic: the end-users screen is how a customer
  sees who is using their assistant, and folding identity into memory would make one
  checkbox quietly stop the reporting as well.
* **The identity lookup is cached per process and the counters are batched.** A resolved
  `(organization, external_id)` maps to one id for the life of that end user, so a hot
  caller costs a dictionary lookup; a miss costs one `ON CONFLICT ... RETURNING` upsert,
  which creates the row and returns its id whichever branch it took. `request_count` and
  `last_seen_at` accumulate in memory and flush on a timer, because writing them per
  request is how an innocuous column becomes a lock convoy. Losing a few counts to a
  killed process is the correct trade and is written down where somebody would otherwise
  mistake the number for exact.
* **Nothing in that path raises into a request.** A database that is unreachable costs
  this request its conversation memory and its attribution, not its completion.
  `on_retrieval_error` is about the *knowledge base* being unreachable; turning "PostgreSQL
  blinked" into a 503 on a fail-closed gateway would be a much larger outage than the one
  the setting asks for.
* **One embedding, not two.** Both halves search for the same question at the same time,
  so `QueryCache` gained a single-flight `embed`: whichever branch arrives first starts the
  task and the other joins it. It is `asyncio.shield`ed, because the two branches run under
  *independent* timeouts and without the shield whichever gave up first would cancel the
  shared task and take the other one down — turning one timeout into two, which is exactly
  what running them concurrently was supposed to prevent.
* **`memory_max_tokens` truncates a prefix, and the prefix is chosen by recall.** Facts
  arrive always-include first, and the assembler drops from the tail — which is what makes
  "these apply to every turn" hold under a tight budget rather than only when there is
  room. A fact is never partially injected: a truncated sentence about somebody says
  something they never said.
* **`allow_anonymous_memory` defaults to false**, as the task asks, and the anonymous
  branch refuses to run on a *partial* input. An identity hashed from a known API key and
  an empty address is the same identity for every caller behind that key, which pools
  strangers' facts into one profile — the failure the default exists to prevent, arriving
  by a different route.
* **Sticky A/B routing uses the explicit identity only.** An IP-derived id moves when
  somebody changes network, and a caller sliding from A to B mid-experiment is the one
  thing sticky routing exists to prevent, so `end_user_key` deliberately does not fall back
  to the anonymous form.
* **A purge does not delete the end user.** SPEC §6.5's erasure is about *memory*; the
  `end_users` row is what makes an existing request log say who a request belonged to, and
  removing it would rewrite the record of things that happened rather than forget what was
  learned from them. The confirmation dialog says so before it is pressed, because the
  thing people expect a "purge" to do is the other one.
* **Deletes go vectors-first, writes go row-first.** A crash between the two halves of a
  write leaves a fact that is visible, editable and still reachable through the
  always-include set — degraded, and obviously so. A crash between the two halves of a
  delete leaves a row with no vector rather than a point with no row: the first is a fact
  an operator can see and delete again, the second is an orphan nothing in the UI can
  reach. Erasure is a promise, and the half that must not be left behind is the half that
  is not on screen.
* **`include_transcripts` is a query parameter, not a body.** A DELETE with a body is legal
  and unevenly supported — intermediaries drop it — and a flag silently lost in transit on
  an erasure endpoint is the wrong thing to be clever about.
* **`max_facts_per_user` is enforced here by refusing, not by evicting.** SPEC §6.4's
  eviction belongs to distillation (task 13), which is the thing that will produce facts
  faster than a person can. Manual entry hitting the cap gets a 409 naming it, which is the
  honest answer when the alternative is silently deleting something somebody typed.
* **Erasing transcripts leaves the metadata rows.** They carry no bodies — a status code, a
  latency, a token count — and deleting them would silently rewrite the traffic charts for
  a period that did happen. Purging a person's memory is not the same act as denying that
  their requests occurred.

### Bugs and near-misses the tests found

* **The cross-tenant net caught all seven new routes** the moment they were registered,
  which is exactly what that test exists to do. Two of them are the worst rows in the
  table: `POST /end-users/{id}/memory` on a foreign id would be writing a fact into another
  tenant's memory — a prompt-injection primitive that survives every future request that
  person makes — and `GET .../memory` returns the most personal thing the control plane
  holds.
* **The two store implementations disagreed about where a fact's tenant comes from.** The
  PostgreSQL half went through `ScopedRepository.add`, which stamps the *caller's*
  organization; the memory half used the *end user's*. Both are right when the caller is an
  org admin and they differ for a superadmin. Unified on the end user's, through one shared
  constructor, because a fact must belong to the same tenant as the person it is about and
  deriving it from the row makes a mismatch impossible rather than merely unlikely.
* **The always-include set made two ranking tests pass for the wrong reason.** Both facts
  in them were confident and recent, so both arrived through the always-include path and
  were ordered by recency — not by the similarity the tests claimed to be asserting. Fixed
  by writing them below the confidence threshold, and the fix is the more interesting test:
  it now also asserts that neither fact was always-included.
* **A toast timer outliving its provider** surfaced as an unhandled
  `ReferenceError: window is not defined` attributed to whichever test file happened to be
  running when it fired — a pre-existing leak in `Toast.tsx` that the extra screens made
  reachable often enough to notice. The auto-dismiss timers are now cancelled on unmount.
* **A fact with a newline in it could have opened a new section of the system message.**
  The cheapest injection there is against a bulleted block, and it becomes reachable the
  moment task 13 writes facts from whatever an end user typed. Fixed twice, in the two
  places it can arrive: flattened on the way in by the service, and flattened again by the
  renderer.

### Not verifiable here

Unchanged from tasks 04-11. PostgreSQL on this machine rejects the credentials in `.env`,
so migration `0012_end_user_memory` is covered by `tests/test_migration_offline.py` — which
renders it to SQL and asserts every mapped column, index and constraint is created — but
the DDL has not run against a server, and `tests/test_end_user_db.py` (29 checks, including
the store contract, the four CHECK constraints, and the `ON DELETE CASCADE` that takes a
person's facts with them) skips.

There is no Qdrant, so `tests/test_fact_vector_store.py` runs its eleven checks against the
memory store and skips the `qdrant` half. That half matters here as much as it did for the
document store and for a sharper reason: every one of those checks is about the
`end_user_id` filter, and the consequence of the two implementations disagreeing is one
person's durable facts reaching another person's prompt.

Retrieval quality is exercised with the local hashing embedder, so the ranking tests assert
*relative* order and membership rather than absolute scores. The two properties that
matter — a standing constraint survives a query with no similarity to it, and one person's
facts never reach another's — hold for reasons that do not depend on the embedder, which is
why they are asserted as identity rather than as a score. No Playwright, no `make`, no
Docker.

### Notes for later tasks

* **Task 13** is the other half of this one. Everything it needs is in place:
  `request_logs.end_user_id` and `session_id` are populated, `transcripts.distilled_at` is
  the column it marks, `memory_facts.source_log_id` is the breadcrumb back, `superseded_at`
  is how it retracts rather than overwrites, and `MemoryConfig.max_facts_per_user` is the
  bound it evicts against. `EndUserService.create_fact` is the write path it can reuse, and
  the only thing it needs beyond this task is the distillation model and the reconcile step.
* **The editor's Try-retrieval box shows documents only.** A fact preview would need an end
  user to preview *as*, which is a second input on that screen; the memory browser's own
  search box answers the same question for a named person and shares the implementation.
  Worth revisiting if the two-column comparison turns out to be what people want.
* **`memory_facts` has no vector-dimension guard of its own.** Recall checks the collection
  width against the embedder and refuses to search a mismatched index, exactly as document
  retrieval does — but task 17's reindex will need to rebuild `org_{id}_memory` alongside
  `org_{id}_docs`, and the fact text is in PostgreSQL, so that rebuild needs no re-reading
  of anything.
* **Audit (task 15)** has one obvious first customer here: the purge already logs an
  `audit_action` of `end_user.memory.purge` with the counts, ready to be routed into
  `audit_events` rather than only into the structured log.

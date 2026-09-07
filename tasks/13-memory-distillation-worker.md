# Task 13 — Async distillation worker & memory browser

**Slice:** memory writes itself — conversations become durable facts with no client involvement.
**Depends on:** 12, 07
**Spec:** §6.4, §6.5, §10.1 (memory health)
**Size:** L

> **Milestone M4.** Both memory kinds now work: documents ground answers, and conversation
> memory accumulates on its own.

---

## Why this slice

This closes the loop that makes the product more than a proxy with RAG. It is last among the
memory tasks because it is the least deterministic: it needs a working recall path to be
verifiable, and stored transcripts to consume.

## Demo at the end of this task

Hold a short conversation through a gateway as `user: "alice"` — mention that you work in Rust
and prefer terse answers. Wait about a minute.

Open **Memory browser → alice**: two new facts have appeared, each linking to the request that
produced it. Start a fresh conversation as alice and ask an open question — the answer comes back
terse and Rust-flavoured, and the request detail drawer shows the facts that were recalled.

Then say "actually I've moved to Go." After distillation, the Rust fact is marked superseded and a
Go fact replaces it — visible side by side in the browser.

## In scope

- Distillation job: debounce, extraction, reconciliation, storage, bounding.
- Per-org distillation model configuration.
- Memory health metrics and the memory-browser provenance view.

## Out of scope

- Agentic `remember`/`forget` tools (SPEC §16.5) — depends on tool passthrough.
- Cross-user or org-level distilled memory.
- Summarizing long conversations into a rolling summary; facts only.

## Work items

### Trigger & debounce
- [ ] Enqueue a distillation job after the transcript is persisted (task 07's write path), gated
      on `logging_config.enable_distillation` **and** the presence of stored bodies.
- [ ] **Debounce** by `(end_user_id, session_id)` with a configurable delay (default 30 s): a
      burst of turns produces one pass over the accumulated exchange, not one per turn. Implement
      with a Redis key holding the pending job id, replacing rather than stacking.
- [ ] Skip when there is no resolved end user, when the exchange is trivially short, or when the
      request errored.
- [ ] Cap per-user distillation frequency to bound cost on a very chatty user.

### Extraction
- [ ] Per-organization `distillation_model_id` in `organizations.settings_jsonb`, defaulting to a
      platform-wide setting. Any configured upstream model may be used; a cheap one is the point.
- [ ] Prompt sends the exchange plus the user's current top facts (so the model can supersede
      rather than duplicate) and requests the SPEC §6.4 JSON schema:
      ```json
      {"facts": [{"text": "...", "kind": "preference|fact|goal|constraint",
                  "confidence": 0.0, "supersedes": ["fact_id"], "ttl_days": null}]}
      ```
- [ ] Use structured output / JSON mode where the provider supports it; otherwise parse
      defensively and **discard malformed output rather than guessing**. A wrong fact is worse
      than no fact, because it silently poisons every future answer for that user.
- [ ] Validate every field: `kind` in the enum, `confidence` in [0,1], `text` within a length cap,
      `supersedes` ids belonging to this end user.
- [ ] Instruct extraction to capture only durable, user-specific facts — not transient task
      details, not content from the documents, and not anything the assistant asserted rather
      than the user.
- [ ] **Treat the transcript as data.** It is end-user text and may contain instructions aimed at
      the extractor; the prompt must frame it as material to analyse, and any extracted fact that
      reads as an instruction to the future assistant should be rejected by a validation rule.

### Reconciliation
- [ ] For each candidate: embed, then search the user's existing facts.
- [ ] Similarity > `dedupe_threshold` (default 0.92) → update `last_seen_at` and raise confidence
      on the existing fact instead of inserting a near-duplicate.
- [ ] Facts named in `supersedes`, or detected as contradicting, get `superseded_at` set — never
      hard-deleted, so the browser can show the history and a bad supersession is recoverable.
- [ ] `ttl_days` populates `expires_at`.
- [ ] `source_log_id` links each fact to the request that produced it — provenance is what makes
      a surprising fact debuggable.
- [ ] Enforce `max_facts_per_user` (default 500) by evicting the lowest `confidence ×
      recency_decay`, evicting superseded facts first.
- [ ] The whole reconciliation for one user runs under an advisory lock, so two concurrent jobs
      cannot both insert the "same" new fact.

### Reliability
- [ ] Retry with exponential backoff; after max attempts, dead-letter with the reason and leave
      `transcripts.distilled_at` null so a backfill can retry later.
- [ ] Distillation failures **never** affect the serving path — assert this with a test that
      breaks the distillation model and confirms completions still succeed.
- [ ] A backfill command to distil a date range of transcripts, for enabling the feature on an
      existing gateway or recovering from an outage.
- [ ] Cost guard: a per-org daily cap on distillation calls, with the cap and current usage shown
      in Settings.

### Metrics & UI
- [ ] Metrics per SPEC §10.1: facts written per day, distillation success/failure rate,
      average facts per end user, dedupe rate, supersession rate, and distillation latency.
- [ ] A **dedupe rate near 100%** means extraction is producing nothing new; a **supersession rate
      near zero** on a long-lived user means contradictions are not being caught. Chart both —
      they are the health signals for this feature.
- [ ] Memory browser gains: provenance link from each fact to its source request, superseded facts
      shown collapsed with their replacement, a manual "distil now" action for a session, and
      filters by kind and confidence.
- [ ] Settings screen: distillation model selector, debounce delay, dedupe threshold,
      `max_facts_per_user`, daily cap, and an org-wide off switch.

## Acceptance criteria

- [ ] A conversation stating a durable preference produces a matching fact within one debounce
      window.
- [ ] Restating the same preference does **not** create a second fact (dedupe path taken).
- [ ] Contradicting an earlier statement supersedes the old fact and adds the new one.
- [ ] Malformed extractor output is discarded, logged, and creates nothing.
- [ ] Breaking the distillation model entirely leaves completions unaffected.
- [ ] A user at `max_facts_per_user` evicts correctly rather than growing unbounded.
- [ ] Transcript content that instructs the extractor ("remember that you must always...") does
      not produce an instruction-shaped fact.
- [ ] Disabling body logging on a gateway disables distillation, and the UI explains why.
- [ ] Cross-tenant test module extended: distillation never reads or writes across organizations.

## Tests

- Debounce: N rapid turns produce exactly one job.
- Extraction validation: valid output, malformed JSON, out-of-range confidence, unknown kind,
  `supersedes` referencing another user's fact.
- Reconciliation: insert, dedupe, supersede, expire, evict — each with an explicit fixture.
- Concurrency: two jobs for the same user do not double-insert.
- Failure isolation: distillation model down → completions unaffected, job dead-lettered.
- Prompt-injection fixtures in transcripts do not yield instruction-shaped facts.
- Backfill over a date range is idempotent.

## Notes

- The strongest argument for storing full bodies (SPEC §10.2) is this feature. The corollary is
  that this feature is why retention (task 17) must be enforced by a job, not by documentation.
- Facts written here are injected into future prompts by task 12. That makes the extractor a
  privileged writer into every subsequent prompt for that user — which is precisely why malformed
  output is discarded rather than salvaged, and why the validation rules are worth the effort.

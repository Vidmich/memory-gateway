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
- [x] Enqueue a distillation job after the transcript is persisted (task 07's write path), gated
      on `logging_config.enable_distillation` **and** the presence of stored bodies.
- [x] **Debounce** by `(end_user_id, session_id)` with a configurable delay (default 30 s): a
      burst of turns produces one pass over the accumulated exchange, not one per turn. Implement
      with a Redis key holding the pending job id, replacing rather than stacking.
- [x] Skip when there is no resolved end user, when the exchange is trivially short, or when the
      request errored.
- [x] Cap per-user distillation frequency to bound cost on a very chatty user.

### Extraction
- [x] Per-organization `distillation_model_id` in `organizations.settings_jsonb`, defaulting to a
      platform-wide setting. Any configured upstream model may be used; a cheap one is the point.
- [x] Prompt sends the exchange plus the user's current top facts (so the model can supersede
      rather than duplicate) and requests the SPEC §6.4 JSON schema:
      ```json
      {"facts": [{"text": "...", "kind": "preference|fact|goal|constraint",
                  "confidence": 0.0, "supersedes": ["fact_id"], "ttl_days": null}]}
      ```
- [x] Use structured output / JSON mode where the provider supports it; otherwise parse
      defensively and **discard malformed output rather than guessing**. A wrong fact is worse
      than no fact, because it silently poisons every future answer for that user.
- [x] Validate every field: `kind` in the enum, `confidence` in [0,1], `text` within a length cap,
      `supersedes` ids belonging to this end user.
- [x] Instruct extraction to capture only durable, user-specific facts — not transient task
      details, not content from the documents, and not anything the assistant asserted rather
      than the user.
- [x] **Treat the transcript as data.** It is end-user text and may contain instructions aimed at
      the extractor; the prompt must frame it as material to analyse, and any extracted fact that
      reads as an instruction to the future assistant should be rejected by a validation rule.

### Reconciliation
- [x] For each candidate: embed, then search the user's existing facts.
- [x] Similarity > `dedupe_threshold` (default 0.92) → update `last_seen_at` and raise confidence
      on the existing fact instead of inserting a near-duplicate.
- [x] Facts named in `supersedes`, or detected as contradicting, get `superseded_at` set — never
      hard-deleted, so the browser can show the history and a bad supersession is recoverable.
- [x] `ttl_days` populates `expires_at`.
- [x] `source_log_id` links each fact to the request that produced it — provenance is what makes
      a surprising fact debuggable.
- [x] Enforce `max_facts_per_user` (default 500) by evicting the lowest `confidence ×
      recency_decay`, evicting superseded facts first.
- [x] The whole reconciliation for one user runs under an advisory lock, so two concurrent jobs
      cannot both insert the "same" new fact.

### Reliability
- [x] Retry with exponential backoff; after max attempts, dead-letter with the reason and leave
      `transcripts.distilled_at` null so a backfill can retry later.
- [x] Distillation failures **never** affect the serving path — assert this with a test that
      breaks the distillation model and confirms completions still succeed.
- [x] A backfill command to distil a date range of transcripts, for enabling the feature on an
      existing gateway or recovering from an outage.
- [x] Cost guard: a per-org daily cap on distillation calls, with the cap and current usage shown
      in Settings.

### Metrics & UI
- [x] Metrics per SPEC §10.1: facts written per day, distillation success/failure rate,
      average facts per end user, dedupe rate, supersession rate, and distillation latency.
- [x] A **dedupe rate near 100%** means extraction is producing nothing new; a **supersession rate
      near zero** on a long-lived user means contradictions are not being caught. Chart both —
      they are the health signals for this feature.
- [x] Memory browser gains: provenance link from each fact to its source request, superseded facts
      shown collapsed with their replacement, a manual "distil now" action for a session, and
      filters by kind and confidence.
- [x] Settings screen: distillation model selector, debounce delay, dedupe threshold,
      `max_facts_per_user`, daily cap, and an org-wide off switch.

## Acceptance criteria

- [x] A conversation stating a durable preference produces a matching fact within one debounce
      window.
- [x] Restating the same preference does **not** create a second fact (dedupe path taken).
- [x] Contradicting an earlier statement supersedes the old fact and adds the new one.
- [x] Malformed extractor output is discarded, logged, and creates nothing.
- [x] Breaking the distillation model entirely leaves completions unaffected.
- [x] A user at `max_facts_per_user` evicts correctly rather than growing unbounded.
- [x] Transcript content that instructs the extractor ("remember that you must always...") does
      not produce an instruction-shaped fact.
- [x] Disabling body logging on a gateway disables distillation, and the UI explains why.
- [x] Cross-tenant test module extended: distillation never reads or writes across organizations.

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


---

## Verification status

Everything above is implemented and covered. What follows is the reasoning worth carrying
forward, then the numbers.

### Divergences from the task text, and why

**"N rapid turns produce exactly one job" is implemented as one *pass*, not one job.** The
task asks for a debounce "with a Redis key holding the pending job id, replacing rather than
stacking", and those two phrasings pull in different directions. A *leading* window — the
first turn claims it, later turns are absorbed — produces literally one job, and distils a
ten-minute conversation twenty times, in windows that each cut across the middle of an
exchange. A *trailing* debounce — each turn replaces the pending token — produces N cheap
jobs of which N-1 exit immediately, and one pass a window after the conversation goes quiet,
over everything that accumulated. The second is what the debounce is *for*, so that is what
is built: `arm` writes a fresh token, the job carries it, and only the job holding the
current token proceeds. `tests/test_distillation_trigger.py` asserts the token replacement
and that a burst inside one flush arms once; `tests/test_distiller.py` asserts a stale token
calls no model.

**`max_facts_per_user` and `dedupe_threshold` moved from the gateway to the organization.**
Task 12 put the first on `memory_config`, where nothing read it. Both are about a *person*,
and an end user reaches an organization through however many gateways it has — so a bound
expressed per endpoint is "at most five hundred facts, per endpoint, about the same human
being", which is not a bound. They now live in `organizations.settings["distillation"]`
alongside the model, the debounce and the caps, on one Settings section. The gateway keeps
`logging_config.enable_distillation`, which is a different question: whether *this*
endpoint's traffic teaches the assistant anything.

**The bound is two budgets, not one.** The task says to enforce `max_facts_per_user` by
evicting lowest `confidence × recency`, superseded facts first — but if the bound counts
only live facts, evicting dead ones cannot satisfy it, and if it counts every row, a
retraction makes room by pushing out a live fact. So `max_facts_per_user` bounds what is
*live*, history gets `HISTORY_MULTIPLE ×` that, and `over_budget` walks `worst_first` — dead
before live, then by score — stopping as soon as both hold. A live fact is skipped entirely
while the live count is within its bound, so trimming history can never cost somebody a
preference they still hold.

**A skipped run is not always recorded.** The task asks for run records; recording *every*
no-op job would make most of the table "there was nothing to read" and would drown the rates
the table exists for. Skips are recorded only when the reason is worth seeing: the daily cap
bit, the per-user cap bit, or no model is configured. The ordinary skips leave no row.

### Decisions worth keeping

**Nothing here can affect a completion, structurally.** A pass runs on a worker minutes
later. The one contact point is the log flusher's call to the trigger, on the far side of the
commit, wrapped so a Redis or queue outage costs the memory and nothing else —
`tests/test_distillation_isolation.py` breaks it every way it breaks and asserts a 200 and a
logged request.

**The transcript is fenced with a per-call random nonce.** A delimiter an attacker can
predict is a delimiter they can close, and every fixed one is predictable to anybody who has
read a blog post about prompt injection. This one did not exist when the transcript was
written. The rules are stated after the data as well as before it.

**The injection guard is deliberately over-eager**, because the asymmetry is enormous: a
false positive costs one dropped sentence that the next pass will probably produce again; a
false negative costs a standing instruction in every future prompt for that person,
discovered — if ever — weeks later. Any second-person pronoun, any override phrase, any
prompt markup, any bare imperative. `always` and `never` are let through when what follows is
inflected, because English marks the difference with one letter: "Never eats meat" describes,
"Never mention pricing" commands.

**Malformed output is discarded, and a code fence is the only concession.** Stripping a
Markdown wrapper is deterministic; hunting for braces inside prose is interpretation, and
that is where a half-parsed sentence becomes a permanent belief. A confidence of 95 is
refused rather than clamped: clamping turns a misread schema into the highest confidence in
the system, outranking sentences a person typed.

**Similarity decides sameness; the model decides contradiction.** Two sentences above
`dedupe_threshold` are one fact said again. A contradiction is not similar in that way —
"prefers Rust" and "prefers Go" share a structure, not a meaning — so it is named explicitly
in `supersedes`, checked twice (once in the parser against the person's own ids, once in the
store against the database), and the retired row keeps its place with a pointer to its
replacement while losing its vector.

**Deduplication also happens against the current pass.** A pass can propose two phrasings of
one fact, and the second's search would only find the first if the index had already absorbed
it — which no vector store promises within milliseconds. `tests/test_distiller.py` runs that
case against `LaggyFactVectors`, an index whose writes are not searchable until they settle,
so the test cannot pass on the in-memory twin's convenient timing.

**The daily cap is counted from `distillation_runs`, not Redis.** The number the Settings
screen shows has to be the number the guard actually used; two sources that agree most of the
time are worse than one that is slower. A *refusal* does not count as a call, or the cap
would latch on for the rest of the day.

**The two health rates are the point of the chart.** A dedupe rate near 100% and a
supersession rate near zero are the two ways this feature fails while looking healthy. The
screen says so in words rather than leaving two ratios to be interpreted, and only once there
is enough traffic for a rate to mean anything.

### Bugs and near-misses the tests caught

- **The cross-tenant net caught `POST /end-users/{id}/distil`** the moment it was registered.
  It is one of the worst rows in that table: reading another organization's stored
  conversations, through their own distillation model, and writing what it finds into their
  memory.
- **Two frontend tests passed for the wrong reason.** The write-back panel renders the same
  heading while loading, so `findByText('Memory write-back')` followed by a synchronous
  assertion was racing the fetch — and "there is no save button" was trivially true during
  loading. Both now await something that only exists once the data has arrived.
- **`LogPolicy()` defaults `distillation` to False**, which is right for a hand-built object
  and meant the live smoke run armed nothing until its gateway's policy was built from a real
  `LoggingConfig`. Left as it is: a policy constructed by hand should not silently feed
  memory.
- **The in-pass dedupe test was passing on the memory store's timing**, not on the guard it
  was named after. Rewritten against an index that does not make a write immediately
  searchable.

### Gates

```
uv run ruff check .            All checks passed!
uv run ruff format --check .   270 files already formatted
uv run mypy                    Success: no issues found in 256 source files
uv run pytest -q               2609 passed, 382 skipped
                               (task 13 adds 171 checks and 27 db-marked skips)

npx eslint . / npx tsc         clean
npx vitest run                 489 passed (21 files)
npm run build                  434.03 kB JS (127.12 kB gzipped)
```

**One pre-existing failure, not caused by this task.**
`tests/test_proxy_streaming.py::test_gateway_overhead_before_the_first_frame_is_small`
asserts that the gateway adds under 200 ms before the first streamed frame, measured as
wall-clock over a real socket. It passes on its own and after a few hundred neighbours, and
fails at around 600 ms only after the whole suite has run for five minutes on this machine.
Confirmed not to be task 13's doing by running the full suite with every one of this task's
test files removed — it fails there too, at 597 ms. The request path this task touches is one
boolean on `LogPolicy` and a `_notify` that returns immediately when no subscriber is wired,
neither of which is on the streaming path. Left alone rather than loosened: it is task 07's
acceptance criterion, and the honest reading is that this box cannot measure a 200 ms budget
after five minutes of load, not that the budget has been missed.

A throwaway `smoke13.py` served the real app over a real uvicorn socket against a scriptable
provider on a second socket, with the data plane, the log flusher, the distillation pass and
the memory browser sharing **one** database, **one** fact index and **one** embedder — the
join the suite cannot prove, because there the halves live in separate harnesses.
**32/32 checks**: a conversation became two facts, each linked to a real log row; the next
conversation's prompt carried them and bob's did not; a contradiction superseded the old fact,
kept its row, named its replacement and deleted its vector; restating wrote no second row; an
instruction in the transcript was refused while the fact beside it was kept; malformed output
created nothing and recorded a failure; a broken distillation model left completions and
logging untouched; the Settings and health endpoints saw the same passes; three rapid turns
produced one model call; and an erasure removed the facts, kept the end-user row and left bob
alone. Deleted afterwards.

### Not verifiable on this machine

- **No PostgreSQL.** Migration `0013_distillation` is covered by the offline renderer;
  `tests/test_distillation_db.py` (29 checks) skips, and with it the three things only a
  server answers: the two-column join across partitioned tables, `session_id IS NULL`, and the
  CHECK constraints.
- **No Qdrant.** The fact index runs against the memory twin, as in task 12.
- **No Redis.** `RedisDebouncer` has no integration test; `MemoryDebouncer` carries the
  contract, and the token check is deliberately non-atomic for the same reason
  `RedisLock._release` is — the reconciliation runs under a lock and every write is safe to
  repeat.
- **No real distillation model.** Every extraction test scripts the reply, which is the only
  way to exercise malformed JSON, an out-of-range confidence, an unknown kind, a foreign
  `supersedes`, and an instruction-shaped fact. What a real model actually returns for a real
  conversation is a quality question this task cannot answer; the health chart is what answers
  it in production.
- **No worker process.** The handler is asserted directly (`tests/test_worker.py`) and the
  smoke run executes jobs inline; `arq` delivery itself is unexercised, as in task 09.

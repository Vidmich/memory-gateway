# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 11 complete**. An organization goes from empty to a working
OpenAI-compatible endpoint entirely in the browser — sign in, configure an upstream model,
create a gateway, copy its URL, mint a key, call it — and every request through it is
recorded and inspectable. **Connectors** ingest the documents customers actually have —
PDF, Word, PowerPoint and Excel alongside Markdown, HTML, CSV and code — and each file
moves from `pending` to `indexed` while you watch, citing the page or slide it came from.
**Memory** then attaches those connectors to a gateway, and the endpoint starts answering
from them: the same question with `X-Gateway-Memory: off` cannot answer it, which is the
whole feature in one A/B. **Monitoring** charts the traffic, including how often retrieval
comes back with nothing; clicking a row shows the client's original messages, the exact
prompt that went upstream with the injected regions marked, which chunks were retrieved at
what score, and a timing waterfall. A gateway can also route over several models: a
failover chain that survives an upstream outage, or a weighted A/B split whose result you
read off the same charts.

## Quick start (Docker)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
curl localhost:8000/healthz   # {"status":"ok","version":"0.1.0"}
curl localhost:8000/readyz    # {"postgres":"ok","redis":"ok","qdrant":"ok","storage":"ok","jobs":"ok"}
```

`make up` is the same thing. The stack brings up Postgres, Redis, Qdrant, MinIO (with its
bucket created), the API on :8000, the ingestion worker, and the Vite dev server on :5173.
The API and the worker are the same image with a different command.

## Sign in

`make seed` creates a platform superadmin and prints a generated password **once**.

```bash
make seed
open http://localhost:5173
```

The UI is a React SPA. In development Vite serves it and proxies `/api` to the API, so
the browser only ever sees one origin; in production the API serves the built assets
itself, which keeps the refresh cookie same-origin and puts one thing on the deploy path
instead of two that can drift apart.

Sessions are a short-lived access token held **in memory** — never `localStorage`, which
any injected script can read — plus a rotating refresh token in an httpOnly cookie.
Replaying a refresh token that has already been spent revokes the whole session family,
on the assumption that a token used twice has been copied.

As the superadmin you can create organizations under **Platform → Organizations**, and
invite people into one from **Settings → Members**. There is no email delivery in v1, so
an invitation produces a link you copy and send yourself. The link works once, expires
after seven days, and is shown exactly once — only its hash is stored, so "resend" mints
a new one and invalidates the old.

## Tenancy

Every organization is isolated, and the isolation is structural rather than a check
repeated per endpoint.

- The **scope** comes from the session (`app/core/tenancy.py`), never from a path, query
  or body parameter. The one way to widen it is `TenantScope.assume`, which only a
  superadmin can call and which writes a record — that is what the "Open as" action and
  its persistent banner are doing.
- Every read of a tenant-keyed table goes through a **`ScopedRepository`**
  (`app/db/repositories.py`), which injects `WHERE organization_id = :scope` and stamps
  the same value on every write.
- A **guard** watches ORM execution and refuses any statement that touches a table with
  an `organization_id` column without either coming from a scoped repository or calling
  `app.db.scoping.unscoped("why")`. Some queries genuinely must span tenants — resolving
  a gateway by slug happens before anyone is authenticated — and `grep -r "unscoped("`
  is the complete list of them.
- Cross-tenant access answers **404, not 403**. A 403 confirms the id exists, which turns
  any endpoint that takes one into an oracle. `tests/test_cross_tenant.py` asserts this
  for every scoped endpoint and fails if a later task adds one it does not cover.

Roles are org-wide (SPEC §5.2) and defined once as data in `app/services/permissions.py`:

| Capability | superadmin | org_admin | org_member | org_viewer |
|---|---|---|---|---|
| View org resources | ✓ | ✓ | ✓ | ✓ |
| Create/edit connectors, gateways, models | ✓ | ✓ | ✓ | — |
| Reveal/create/revoke API keys | ✓ | ✓ | — | — |
| Members, roles, invitations, org profile | ✓ | ✓ | — | — |
| Organizations, global catalog, platform settings | ✓ | — | — | — |

`GET /api/v1/auth/me` returns the resolved set, so the UI hides and disables controls
from one source of truth. That is presentation only — the API refuses the same call
whether or not the button was rendered.

## Models

**Models** is where completions actually go: a base URL, a dialect, the provider's own
model id, and a credential. A gateway points at one of these.

Two tabs, over one endpoint — `?scope=` is a filter, so a model cannot show up in one
view and be missing from the other:

- **Our models** — this organization's own. Full CRUD for `resources:write`.
- **Global catalog** — models the platform operator shares with every tenant. Org users
  read them and point gateways at them; only a superadmin can create or edit one.

**Test connection** sends a one-token completion through the *same adapter the proxy
uses* and reports `OK, 340 ms` or the provider's own words — `401 invalid_api_key`. It
works on an unsaved draft too, so a base URL can be checked before it is stored. Almost
every misconfiguration is a wrong base URL, which is what the provider presets (OpenAI,
Azure, Groq, Together, OpenRouter, vLLM, Ollama) exist to prevent.

Credentials are **write-only** (SPEC §5.4). They are encrypted with envelope encryption
before storage, and no endpoint returns one — not for any role, superadmin included.
Responses carry `{"configured": true, "hint": "sk-…4f2a"}`, and the hint is computed from
the plaintext at write time and stored, so rendering the list never touches the master
key. Rotation is replacement: `PATCH` with the credential omitted keeps what is there, an
explicit `null` clears it, and a string replaces it. An org user reading a *global* model
sees neither its hint nor its `extra_headers` — headers are applied last and can contain
an auth header, which is exactly what the credential field protects.

`default_params` is validated against the documented OpenAI parameters with the
providers' own bounds, so a mistyped `temprature` is a 422 on the form rather than a 400
from the provider on somebody else's request an hour later. At request time the layers
merge model → gateway → client, lowest precedence first.

Deleting a model a gateway points at is refused and names the gateways; the foreign key
is `ON DELETE RESTRICT`, so the database would refuse it regardless. Disabling is always
allowed and takes effect on the next request — the gateway then answers 503 saying which
model is switched off.

## Gateways and keys

A **gateway** is the endpoint you publish: `https://…/g/{slug}/v1`. It has its own URL,
its own API keys, its own system prompt and its own parameter policy. Create one under
**Gateways → New**, pick a model, write a prompt, save — the screen shows the URL with a
copy button, and **Create key** shows the secret exactly once.

The editor is sectioned so later releases slot in without moving anything: *Identity*,
*Routing*, *Memory* (task 10), *Prompt*, *Logging*, *Limits* (14), *Keys*. The two unbuilt
sections render a real empty state naming what will fill them, rather than being hidden —
a section that appears later moves everything below it.

**The slug is immutable.** It is a path segment on a URL customers have already deployed,
and nothing here can tell them it changed, so a rename from a settings form would break
production traffic silently and instantly. `PATCH` refuses it with that reason, and the
editor offers **Clone with a new slug** instead: a new gateway with the same
configuration, leaving the old one serving until its callers have moved. Reserved slugs
(`api`, `admin`, `health`, `metrics`, `g`, `www`) are refused.

**Keys are shown once.** Only `sha256(secret)` is stored, so the plaintext genuinely
cannot be recovered — the reveal dialog says so above the value, not under it. Revoking
is a timestamp rather than a delete, so the request log keeps a reference that
resolves, and it takes effect on the **next request**: a gateway's configuration is
cached in Redis, but a key never is, which is what makes that sentence true without an
asterisk. Keys can carry an optional expiry, and `last_used_at` is written at most once a
minute per key so a hot key does not turn every completion into a database write.

**Test gateway** sends a real completion through the real proxy path — same resolver
(cache included), same prompt assembly, same adapter — and returns the *assembled prompt*
alongside the answer and a latency breakdown. It is the fastest way to see what your
system context became; the request log is the record of what every real call did.

**Parameter policy has two strengths.** `param_overrides` is the organization's house
style and a client can beat it; `locked_params` is applied *after* the client's values
and wins. When a lock actually replaced something the caller asked for, the response
carries `X-Gateway-Locked-Params` naming it — ignoring a request silently is the failure
mode that design has to answer for.

Configuration changes take effect on the next request. The resolver caches a gateway in
Redis under a per-slug version counter; every write that could change what a request does
— including an edit to a *model* the gateway points at, in any organization — bumps that
counter, so the stale entry is orphaned rather than deleted. A delete has a window where
a slow reader can put stale config back afterwards; a version bump does not. A 60-second
TTL is the backstop, and the whole cache fails open onto PostgreSQL. Provider credentials
are cached still **encrypted**: Redis is a cache, not a vault.

A disabled gateway answers **403**, not 503. A 503 means "try again", and an SDK will —
indefinitely, against an endpoint somebody switched off on purpose.

## Routing

A gateway routes over an ordered list of targets in one of three modes, chosen in the
editor's *Routing* section. Each is described there by what happens when it **fails**,
because that is the only thing that separates them.

| Mode | On failure |
|---|---|
| **Single model** | One target. The caller gets the error. |
| **Failover chain** | Targets are tried in priority order until one answers. |
| **A/B split** | One target per request, chosen by weight. No retry. |

**Retry classification is a table, not a judgement call.** A connect failure, a DNS
failure, a read timeout, 408, 429, 500, 502, 503 and 504 move to the next target; 400,
401, 403, 404 and 422 are returned immediately, because the next target would reject them
identically and trying it turns one bad request into two. Anything unrecognised is treated
as final — an unclassified failure is not evidence that retrying will help. Transport
errors never reach the classifier as exceptions: the proxy has already turned a refused
connection into a 502 and a read timeout into a 504, so the table is complete by
construction.

**Each attempt carries its model's own `timeout_seconds`, and the chain carries a
deadline.** Three targets at sixty seconds each is three minutes and no client waits that
long, so every attempt runs inside the remaining budget (`ROUTING_DEADLINE_SECONDS`,
default 120 s) — a chain cannot outlive it even when one target is slower than the whole
allowance. Between attempts there is a 50–150 ms jittered pause. That does nothing for one
caller; it exists so a fleet that all meets a provider's 503 in the same instant does not
all retry in the same instant.

**Streaming responses cannot fail over once output has begun.** Up to the first token a
failed target is replaced silently — `open_stream` sends the request and checks the status
*before* yielding anything, so a provider 503 on a streamed request is still an ordinary
HTTP error. After it, the 200 is on the wire and cannot be taken back: the stream ends with
an SSE error event and the row records `failed_after_stream_start`. Nothing is buffered to
widen that window, because buffering to make failover more likely would trade away the
point of streaming. The editor says all of this next to the mode selector.

**A/B assignment is sticky when it can be.** With an end-user id — `X-Gateway-User`, or
`user` on the request body — the target is `crc32(f"{user}:{gateway_id}")` against the
cumulative weight bands, so one person sees one variant and the comparison is between
models rather than between coin flips. The gateway id is in the hash so that somebody
unlucky enough to land in the bottom band is not in the bottom band of every experiment in
the account. Without an id, selection is uniform. Changing the weights re-buckets everyone:
that is documented and accepted, and a stable-assignment table is deliberately not here.

**Weights are percentages that must total exactly 100, checked when you save.** They are
never normalised on your behalf — 70/20 quietly stored as 78/22 would change the result of
whatever is being measured and tell nobody. The editor shows a running total and the split
it would actually produce, and blocks the save until they agree.

**There is no retry in A/B mode**, and that is a data-integrity rule rather than a
performance trade: a retried request would land on the other arm and bias the experiment
the mode exists to run. It is enforced by the plan having length one, not by a flag.

Circuit breaking and health checks are deliberately not here (SPEC §16.9). Plain failover
delivers most of the availability benefit; a breaker adds shared state and flapping
behaviour that needs this task's metrics to tune.

When more than one target was involved, the row records every attempt —
`{target_id, model_name, status, error_code, latency_ms, retryable}` — and the detail
drawer draws it as a timeline. One clean attempt records nothing, because the row's own
model, status and latency columns already say it. `routing_attempts_total{mode, model,
outcome}`, `routing_failovers_total{model, error_code}` and `routing_chain_attempts`
carry the same picture to Prometheus.

## Connectors and ingestion

A **connector** is where content comes from. The v1 type is a managed file drop: the
platform provisions `orgs/{org}/connectors/{id}/` in its own object store, and files
arrive either by dragging them onto the connector page or by `PUT` to a short-lived
presigned URL.

Each file becomes a **document**, and each document walks SPEC §9.5's pipeline —
`pending → extracting → chunking → embedding → indexed` — with the status written at every
step rather than inferred at the end. That is what makes the table on the connector page
move while you watch it, and what makes a stuck document say *which* step it is stuck on.

Two failures are handled in opposite directions, and that distinction is the shape of the
whole pipeline:

| The file is bad | The world is bad |
|---|---|
| Will not decode, unsupported format, empty. | Provider rate-limiting, Qdrant restarting, storage timeout. |
| Marked `failed` or `skipped` with a sentence a customer can act on. The job returns. | The document is untouched. The job **raises**, and the runner backs off and retries. |
| The retry that matters is the button in the UI, after the file has been fixed. | Nobody has to do anything. |

Getting that backwards either way is the most expensive mistake available here: one way a
provider blip permanently fails a thousand documents, the other way a corrupt file is
retried until it dead-letters and the customer is told nothing useful.

**What is read.** SPEC §9.2's formats in full; the four binary ones have a section of
their own below. The renderings that matter more than they look: CSV, TSV, spreadsheet
rows and every table in a Word file or a slide become named records — `name: Ada` /
`role: Engineer`, not `Ada,Engineer` — because a row of commas embeds to a vector about
commas. JSON becomes key paths, `user.roles.0: admin`.
HTML is stripped to text with its headings kept and its `<script>` dropped. A format
nothing can read yet is *recognised* and skipped with a note rather than failed, so a
roadmap gap does not read as a broken product.

**What a file is** is decided from its first bytes, never from its name. A JPEG called
`notes.txt` is skipped as an image; a `.mov` is recognised from its `ftyp` box. Binary
files are never read past the sniff window, so a folder of videos costs 8 KB each rather
than their size.

**Chunking** is per connector (SPEC §9.3): `recursive`, `fixed` or `by_heading`, with a
token size and an overlap. Sizes are counted with the embedding model's own tokenizer, and
a cut is walked back to a paragraph, then a sentence, then a word boundary — never
mid-word. Changing any of it invalidates the chunks already stored, so the editor says so
at the moment of the change, and only when there is an index to invalidate.

**The index** is one Qdrant collection per organization, `org_{org_id}_docs`, cosine
distance, with payload indexes on `connector_id` and `document_id`. Point ids are
deterministic over `(document_id, chunk_index)`, so re-ingesting a document overwrites its
points rather than doubling them — and re-ingestion *also* deletes by `document_id` first,
because a document that shrank from forty chunks to thirty leaves ten behind that still
match queries for text the file no longer contains.

**Resync** reconciles the connector against what storage actually holds and reports
`{added, updated, deleted, unchanged, skipped}`. It only ever deletes a document in a
terminal state: a row that says `pending` might be an upload whose object has not landed
yet, and a listing is a snapshot taken before any lock could have helped.

**The debug search** (`POST /api/v1/connectors/{id}/search`) returns scored chunks with
their source and section. It answers "is my file actually in there" without a gateway in
the way, which makes it the first thing to check when a gateway's answers look wrong: if
the search finds nothing either, the problem is ingestion rather than retrieval.

### Documents: PDF, Word, PowerPoint, Excel

Real corpora are PDFs and Office files, not tidy Markdown. Each of the four keeps the
structure it already has, and the label a chunk carries is what a citation will show:
`manual.pdf (p. 147)`, `Slide 3: Roadmap`, `Security > Access Control`, `Prices`.

**PDF extraction is where RAG quality quietly dies**, so four things are undone before
anything is embedded. Running headers and footers are stripped — detected by position and
repetition together, with digit runs masked so `Page 4 of 200` and `Page 5 of 200` are
recognised as the same footer — because otherwise the confidentiality notice is in all 200
chunks and similarity scores compress until nothing discriminates. Words broken across a
line break are rejoined. Two-column pages are read down rather than across, found with a
projection profile that tolerates a title lying across the gutter. And a **chunk never
crosses a page break**, whatever the connector's chunking strategy says, because a chunk
drawn from pages 144 to 147 can cite at most one of them truthfully — a reader who turns
to the page and does not find the sentence stops believing every citation after it.

The reader is [`pypdfium2`](https://pypi.org/project/pypdfium2/): PDFium, the engine in
Chrome's PDF viewer, BSD-3-Clause. The plan named PyMuPDF or pdfplumber; the first is
AGPL-3.0, which is a live question for a hosted service, and the second is pdfminer
underneath — 25 seconds on a 200-page document where PDFium takes 0.9.

**A scan is refused, not indexed.** Below a floor of extracted characters per page the
document lands as `skipped: needs_ocr` with an explanation and a next step on the
connector page, rather than as `indexed` with a handful of empty chunks — which is worse
than a failure, because nothing looks wrong. A password-protected PDF says so; one
carrying only an *owner* password ("you may read this but not print it") is read, because
that is most of the corporate documents anybody actually has.

**Word gives the accepted text, not the marked-up text.** Reading a paragraph's direct
children silently drops edits made with track changes on, so a policy document that has
been through review indexes as its pre-review draft with a plausible chunk count and
nothing looking wrong. Insertions are kept and deletions discarded. Headings become a
path, lists keep their markers, footnotes are appended to the paragraph that references
them, and headers and footers are ignored. There is no page count: pagination is a
rendering decision, so the column is empty rather than invented.

**PowerPoint includes the speaker notes**, labelled. A slide says "Q3 priorities" over
three bullets of four words each; the sentence that says what was actually decided is in
the notes. Slides do not join into one chunk: a slide is a unit somebody authored, and
gluing two of them together produces a chunk about two subjects.

**Excel rows are records**, by the same function CSV uses, with header detection, empty
columns dropped, formulas falling back to their own text where no cached value was saved,
and a 50 000-row cap per sheet that marks itself in the text — a spreadsheet is usually a
database export, and indexing all of it produces near-identical chunks that crowd every
prose document out of the index.

**Heavy extraction runs in a subprocess pool.** These are large C and C++ libraries
reading binary formats designed in the nineties, driven by files that arrive from the
internet: a crafted PDF can loop inside the parser, a workbook can allocate until the
machine swaps, and a corrupt font table can segfault a library that has no idea Python
exists. A thread cannot be interrupted and a segfault does not care whose thread it was,
so those four extractors run in children with a wall clock the parent enforces by killing
them and an address-space ceiling the kernel enforces. They also get a **queue of their
own**, so a folder of notes dropped alongside a 300-page manual does not sit at `pending`
behind it: `arq app.workers.main.HeavyWorkerSettings` reads it, and running one is
optional — without it those jobs simply wait, which is a visible backlog rather than a
silent loss.

**Reading it back.** The document table shows pages, slides or sheets beside the chunk
count, `needs_ocr` and `password_protected` render as explained states with a way out of
them rather than as red rows, and **Chunks** on any indexed row lists what the file
actually became with the page or section each chunk came from. That is the fastest way to
tell a healthy document from one that reports `indexed` and answers badly — they look
identical everywhere else on the screen. `extraction_duration_seconds{format}` and
`extractions_total{format, outcome}` are the same question in Prometheus: extraction
degrades one format at a time, and an unlabelled failure rate averages that into
invisibility.

### The worker

Ingestion runs in a separate process — `arq app.workers.main.WorkerSettings`, the same
image as the API. arq is used for exactly two things, durable delivery and deferral;
retries, backoff, the attempt ceiling and the dead-letter record are
[`app/services/jobs.py`](app/services/jobs.py), so the policy is one thing in one place
and testable without Redis.

Jobs are enqueued **after** the transaction that justified them commits — a job naming a
row a rollback removed is a worker failure nobody can explain from the evidence. Every
enqueue carries an idempotency key, and every job body is *also* safe to run twice,
because the key's reservation expires and races.

`jobs_started_total`, `jobs_completed_total{job, outcome}`, `job_duration_seconds`,
`jobs_dead_lettered_total` and `jobs_queue_depth` go to Prometheus; `/readyz` reports the
queue separately from Redis, because a queue that cannot be written to leaves the API
serving traffic and silently dropping ingestion.

### Embeddings

One model for the whole platform (SPEC §9.4) — a collection's vectors must all come from
one, and mixing them silently degrades retrieval. Configured by environment for now; task
17 moves it into `platform_settings` with a reindex-and-swap flow.

The development default is `EMBEDDING_PROVIDER=hash`: a local hashing-trick bag-of-words
embedder that needs no key and no network. It is genuinely lexical — shared words score
higher — which is enough to demonstrate the whole ingest-and-search path on a laptop, and
it knows nothing about meaning. The service **refuses to start** with it when
`ENVIRONMENT=prod`.

## Memory: retrieval and prompt assembly

Attach connectors to a gateway under **Memory**, and every request through it is answered
with the organization's own documents in front of the model.

**What happens per request.** The last user message (or the last N user turns) is embedded
and searched against the gateway's connectors, filtered by a similarity floor, capped by a
token budget, and rendered into the system message as a numbered reference block. The
whole of it runs inside `retrieval_timeout_ms`, and a gateway with no connectors attached
skips the embedding and the vector call outright, so the feature costs nothing until it is
switched on.

**Prompt assembly is SPEC §7 in full**, as a pure function over
`(messages, contexts, chunks, facts, limits)` in
[`app/services/prompt.py`](app/services/prompt.py):

```
[1] model.system_context      [2] gateway.system_context
[3] retrieved documents       [4] end-user memory (task 12)
[5] the client's own system message(s), verbatim and in order
```

Any empty layer is dropped along with its delimiter — never a `## Reference material`
heading with nothing under it, which would invite the model to cite excerpts that do not
exist. Being a pure function is what makes the editor's prompt preview the *same code* as
the request path rather than a second implementation that drifts.

**The token budget is over the whole rendered block**, boilerplate included, so
`doc_max_tokens` is a number a customer can verify by counting what reached the provider.
Chunks are dropped from the tail — lowest score first — and each drop is recorded on the
request log with its reason, so "why did it ignore the pricing page" has an answer rather
than a theory.

**Failure is a policy, not an exception.** Retrieval never raises; it returns an outcome,
and the gateway's `on_retrieval_error` decides what that means. `fail_open` serves an
ungrounded answer, which is right for a support bot that is better than nothing;
`fail_closed` returns 503, which is right for an assistant whose whole value is that it
only answers from the handbook. Neither ever hangs past the configured timeout.

**Two guards that only matter when they fire.** An index built by a *different* embedding
model has vectors of the wrong width; retrieval refuses to search it and logs at `error`,
because under `fail_open` the alternative is every request quietly losing its documents
with nothing anywhere saying why. And if the client's own messages already fill the
model's context window, nothing is injected and a warning flag is set — but only when the
model has a `context_window` set, because `NULL` there means *unknown*, not unlimited, and
a guessed window would withhold memory from requests a provider would have served.

**Tuning is a loop you can run.** The editor's **Try retrieval** box sends the question
with the *unsaved* form values, and returns the exact chunks that would be injected — with
scores, sources, per-chunk token costs, and a marker on the ones that fall outside the
budget. Nothing is saved, so tuning a score floor does not change what live callers are
getting between attempts. **Show the whole prompt** does the same and assembles it, layer
by layer, against the model's context window.

**Reading it back.** Responses carry `X-Gateway-Memory-Chunks` and `X-Gateway-Retrieval-Ms`
whenever retrieval ran — their *absence* means it did not, which is itself the answer to
"why did it not use my documents". `X-Gateway-Memory: off` on a request skips augmentation
entirely, which is the honest way to measure what the gateway contributes. The request
drawer shows every retrieved chunk with its score and what became of it, and
**empty-retrieval rate** is on the monitoring screen as a card and a chart: a gateway
retrieving nothing most of the time looks perfectly healthy on every other number and is
answering from nowhere.

The known limitation is stated in the editor rather than in a release note: retrieval is
dense search over the user's own words, so a conversational follow-up — "what about the
second one?" — retrieves poorly. SPEC §17.4 leaves query rewriting open, and the
empty-retrieval rate is there so that decision can be made from data.

## Request logging and monitoring

Every request through a gateway becomes a row. **Monitoring** shows the request rate with
its status breakdown, latency percentiles, token counts, traffic per model and the error
taxonomy over a chosen window, plus a live-tailing request table. Clicking a row opens the
detail drawer: the caller's own messages, the assembled prompt with the gateway's
additions marked, the response, a timing waterfall and a *Copy as curl* that reproduces
the call against the gateway.

**Logging never waits.** The request path fills in a record in memory, copies at most a
bounded number of bytes out of the response, and hands it to a queue; redaction, batching
and the insert all happen in a background flusher. An `INSERT` before the response returns
would be the obvious implementation and it is the wrong one: a database round trip is the
same order of magnitude as the whole latency budget, so a database that is briefly slow
would make the *proxy* briefly slow. Measured on this machine, logging on versus off is
indistinguishable at p95 (−0.01 ms of a 5 ms budget), and the test that matters asserts
the stronger thing — that the request path makes **no** synchronous call to the log store.

**Under pressure it sheds in a defined order.** Above a watermark, incoming records keep
their metadata and lose their bodies, which is where almost all the memory is; a full
queue drops whole records. Every drop increments `logs_dropped_total{reason}`, so a gap in
the log is a number somebody can alert on rather than an absence somebody notices. Nothing
on this path can fail a request.

**Bodies are configurable per gateway, and the form says what that means.** The §10.2
toggles — the client's request, the assembled prompt, the response — plus retention,
redaction patterns and the distillation switch live in the gateway's *Logging* section,
which states plainly that body capture stores end-user content and shows the effective
retention as a sentence. Metadata is always on: it is what the charts are made of.
Switching a body off means nothing is copied at all, not that it is hidden afterwards. An
organization can set its own defaults under `settings.logging_defaults`, and a new gateway
starts from them.

**Redaction runs before persistence, never after.** A redaction applied on read is a
display filter, and the raw card number is still in the backup. The patterns are applied
in the flusher, which is upstream of every write, and if they cannot finish within their
budget the bodies are dropped rather than stored half-cleaned — the row says
`bodies_omitted: redaction_budget` and the drawer explains it. Patterns are checked when
you save them: `(a+)+` and its relatives are refused on the form, because Python's `re`
cannot be interrupted once it is matching, so the only place to stop one is before it is
stored.

When a gateway is filtered to on **Monitoring** and it is running an A/B split, the
traffic-by-model chart marks each bar with the weight it was configured for, so drift
between an intended 70/30 and an actual 68/32 is a glance rather than a division.

**Metadata and bodies are separate tables**, `request_logs` and `transcripts`, both
partitioned by day. The monitoring queries never touch the large text columns, and
retention (task 17) becomes a partition drop rather than a `DELETE` that rewrites a live
table while the proxy writes to it. Aggregation happens in PostgreSQL — `percentile_disc`
over the partition range — behind a `MetricsRepository`, which is the seam to move to
ClickHouse if the volume ever demands it.

Bucket widths are the server's decision, not the client's: a window maps onto a fixed
ladder (an hour at one-minute buckets, thirty days at one hour) and a requested interval
is widened until the answer fits. Summary queries are cached for 30 seconds per
organization and query; the cache is read-through and fails open.

The log detail endpoint takes no time range. Primary keys are UUIDv7 and carry the
millisecond they were minted, so the id itself says which day's partition to look in.

## Try the proxy

Create a gateway and a key in the UI, or let `make seed` wire a demo one up. With
`OPENAI_API_KEY` set, seeding creates a demo organization, upstream model, gateway and
API key. The provider key is encrypted with `ENCRYPTION_MASTER_KEY` before it is stored,
and the gateway key's plaintext is printed once and never again.

```bash
OPENAI_API_KEY=sk-... make seed
```

Then point any OpenAI client at it, changing only `base_url`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/g/demo/v1", api_key="mg_...")

print(client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)

for chunk in client.chat.completions.create(
        model="demo", messages=[{"role": "user", "content": "count to 5"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

`model` is the gateway's slug, not the provider's model name: that indirection is the
point, so the endpoint can be repointed at a different provider without the client
changing — edit the model in the UI and the very next request goes somewhere else.

To see what memory contributes, ask the same question twice:

```python
grounded = client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "what is our refund window?"}])

bare = client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "what is our refund window?"}],
    extra_headers={"X-Gateway-Memory": "off"})
```

The first answers from your documents and the response carries
`X-Gateway-Memory-Chunks`; the second says it does not know. `make seed --no-auth`-style local providers (Ollama, vLLM) work by setting
`OPENAI_BASE_URL` and passing `--no-auth` to `python -m app.cli seed`.

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/) and the backing services reachable at the
addresses in `.env`.

```bash
cp .env.example .env
make install
make migrate
make dev       # the API
make worker    # in another terminal — nothing ingests without it
```

## Development

```bash
make check      # everything CI runs, backend and frontend
make test       # pytest
make lint       # ruff check + format --check
make typecheck  # mypy, strict
make format     # apply fixes
make worker     # the ingestion worker, locally

make check-web  # eslint + tsc + vitest + openapi drift + production build
make web        # Vite dev server on :5173, proxying /api to :8000
make openapi    # regenerate the typed API client from the live FastAPI schema
```

`make help` lists every target. Without `make` installed (common on Windows) every target
is a one-liner you can run directly — e.g. `uv run pytest`, `uv run mypy`,
`uv run ruff check .`, `npm --prefix web run test`.

### Tests

Tests marked `live` call a real provider and are skipped unless `OPENAI_API_KEY` is set;
run them deliberately with `uv run pytest -m live`. They cost a few tokens.

The frontend has unit tests (`make test-web`) that run anywhere, and a Playwright suite
(`make e2e`) covering log in → reload → log out in a real browser. The latter needs the
stack up and the seeded password:

```bash
make up && make seed
E2E_PASSWORD=<the printed password> make e2e
```

### Tests and the backing services

Tests marked `db` build a throwaway database by running the **migrations** — not
`metadata.create_all` — so a broken migration fails the suite rather than passing against
a schema no deployment will ever have. With no PostgreSQL reachable they skip, so the
suite still runs with the stack down. CI sets `REQUIRE_DB_TESTS=1`, which turns that skip
into a failure.

`s3` and `qdrant` do the same for MinIO and Qdrant. Both run the *same* checks that the
in-memory implementations run — `tests/object_store_contract.py` and
`tests/vector_store_contract.py` — which is the whole reason the fast half is worth
trusting. The Qdrant one is not optional in CI: payload filters and delete-by-filter are
exactly where a hand-written double agrees with itself and disagrees with the real thing.

```bash
make up                    # the services the marked tests need
uv run pytest -m "s3 or qdrant"
```

### The typed API client

`web/src/api/schema.d.ts` is generated from the server's OpenAPI schema, and the types the
app actually uses are aliased from it in `web/src/api/types.ts`. Rename a field on the
server and the frontend stops compiling, rather than rendering `undefined`. CI regenerates
and fails if the committed copy has drifted; `make openapi` updates it.

## Layout

```
app/
  api/        routers — health, proxy/ (data plane), control/ (the UI's API:
              auth, directory, models, gateways), spa.py (serves the built SPA
              in production)
  adapters/   upstream dialects — openai now, anthropic in task 16
  core/       config, logging, errors, ids, metrics, middleware, clients,
              crypto (envelope encryption), keys (API key format), passwords
              (Argon2id), patterns (regex safety), tokens (JWT + refresh),
              tenancy (TenantScope), background
  db/         engine, session, declarative base, models, scoping (ScopedRepository
              and the unscoped-query guard), repositories
  schemas/    the OpenAI wire format, control-plane request/response bodies
  services/   gateway resolution and its Redis config cache (gateway_resolver),
              API-key auth, prompt assembly, forwarding, SSE, control-plane auth
              (auth, auth_provider, auth_store, login_throttle), tenancy
              (permissions, directory, directory_store, pagination), the model
              catalog (catalog, catalog_store, model_probe, params, rate_limit),
              gateways and keys (gateways, gateway_store, gateway_probe), upstream
              routing (routing, end_user), request logging (request_log, log_store,
              redaction), the monitoring reads (monitoring, metrics_store), and
              ingestion — its ports (object_store, vector_store, embeddings,
              tokenizer, locks, jobs, job_queue), its pipeline (extraction and
              the readers it registers — pdf, office — plus extraction_pool for
              the ones that need a subprocess, then chunking, ingestion) and its
              control plane (connectors, connector_store, connector_source) — and
              the read side of the same index: retrieval (search, timeouts, the
              failure policy) and memory_preview (the editor's Try retrieval and
              prompt preview)
  workers/    the ingestion workers — `arq app.workers.main.WorkerSettings` and
              `HeavyWorkerSettings` for the PDF and Office queue — and the
              composition root they and the API both build their stack from
  cli.py      operator commands — `python -m app.cli seed | openapi`
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the React SPA
  src/api/      the fetch client and the generated schema types
  src/auth/     auth context, reducer, protected routes
  src/components/  DataTable, Form, ConfirmDialog, EmptyState, StatusBadge, CopyButton,
                   Charts (hand-drawn SVG — no charting library)
  src/layout/   the app shell — sidebar, user menu, support banner
  src/pages/    login, dashboard, organizations, members, org settings,
                invitation acceptance, models (list and editor), gateways
                (list, editor, routing and memory sections with Try retrieval,
                keys), connectors (list, and a detail screen with the upload
                zone, document table, chunk inspector, chunking panel and debug
                search),
                monitoring (charts, request table, detail drawer with the
                attempts timeline and the retrieved chunks)
  e2e/          Playwright
```

## Configuration

Everything is an environment variable, validated at import time: a missing or malformed
value stops the process immediately and names the variable, rather than surfacing on the
first request. See [.env.example](.env.example) for the full list.

## Operations

| Endpoint | Purpose |
|---|---|
| `POST /g/{slug}/v1/chat/completions` | The data plane. OpenAI schema, `stream: true` or `false`. |
| `GET /g/{slug}/v1/models` | The virtual models this gateway exposes, in OpenAI list format. |
| `POST /api/v1/auth/login` | Email and password. Returns an access token; sets the refresh cookie. |
| `POST /api/v1/auth/refresh` | Rotates the refresh token. Replaying a spent one revokes the session family. |
| `POST /api/v1/auth/logout` | Revokes the session and clears the cookie. Idempotent. |
| `GET /api/v1/auth/me` | The current user, role, organization, and capability set. |
| `POST /api/v1/auth/password` | Change your own password; signs every other session out. |
| `GET`/`POST /api/v1/organizations` | List (scope-aware) and create (superadmin). |
| `GET`/`PATCH /api/v1/organizations/{id}` | Read and edit. `status` is superadmin-only. |
| `GET /api/v1/organizations/{id}/members` | Members of one organization. |
| `PATCH`/`DELETE /api/v1/members/{id}` | Change a role or status; remove a member. |
| `POST /api/v1/organizations/{id}/invitations` | Invite someone. Returns the link, once. |
| `GET`/`DELETE /api/v1/invitations[/{id}]` | List pending invitations; revoke one. |
| `POST /api/v1/invitations/{id}/resend` | Mint a new link; the previous one stops working. |
| `GET`/`POST /api/v1/invitations/accept/{token}` | Public. Validate a link, then create the account. |
| `GET`/`POST /api/v1/models` | List (own + global catalog, filterable by `scope` and `enabled`) and create. |
| `GET`/`PATCH`/`DELETE /api/v1/models/{id}` | Read, edit, delete. Delete is refused while a gateway points at it. |
| `POST /api/v1/models/{id}/test` | Probe the stored configuration. One token, rate-limited per user. |
| `POST /api/v1/models/test` | Probe an unsaved draft, before storing a credential. |
| `GET`/`POST /api/v1/gateways` | List and create. The slug is globally unique and set once. `targets` is the routing chain; `model_id` is the one-target shorthand. |
| `GET`/`PATCH`/`DELETE /api/v1/gateways/{id}` | Read, edit, delete. `PATCH` refuses `slug`, with the reason. |
| `POST /api/v1/gateways/{id}/test` | A probe completion through the real proxy path; returns the assembled prompt. |
| `POST /api/v1/gateways/{id}/try-retrieval` | The chunks a question would inject, with scores and a budget marker. Accepts unsaved settings. |
| `POST /api/v1/gateways/{id}/prompt-preview` | The fully assembled system message for a question, layer by layer, with token counts. |
| `GET`/`POST /api/v1/gateways/{id}/keys` | List keys (prefix only); mint one — the plaintext is returned once. |
| `DELETE /api/v1/keys/{id}` | Revoke. Soft, and effective on the next request. |
| `GET /api/v1/metrics/summary` | Totals, percentiles, per-model traffic and the error taxonomy for a window. Cached 30 s. |
| `GET /api/v1/metrics/timeseries` | Bucketed series. `metric` is `requests`, `latency`, `tokens` or `retrieval`; the server picks the bucket width. |
| `GET /api/v1/logs` | The request table. Cursor-paginated, filterable by gateway, model, status class, end user, session, latency and error text. |
| `GET /api/v1/logs/{id}` | One request in full, including whatever of the transcript was stored. No time range needed. |
| `GET`/`POST /api/v1/connectors` | List (with per-status document counts) and create. |
| `GET`/`PATCH`/`DELETE /api/v1/connectors/{id}` | Read, edit chunking, delete. Delete is a 202: the objects and vectors go in a job. |
| `GET /api/v1/connectors/{id}/documents` | The document table. Cursor-paginated, filterable by `status`. |
| `POST /api/v1/connectors/{id}/upload` | Multipart, many files at once, streamed to object storage. Always 200, with a per-file outcome. |
| `POST /api/v1/connectors/{id}/upload-url` | A short-lived presigned `PUT`, for scripted uploads. Picked up by the next resync. |
| `POST /api/v1/connectors/{id}/resync` | Reconcile against storage; reports `{added, updated, deleted, unchanged, skipped}`. |
| `POST /api/v1/connectors/{id}/search` | Debug-only semantic search over one connector's chunks, with scores. |
| `POST /api/v1/documents/{id}/reindex` | The retry button. Resets the row to `pending` and enqueues it. |
| `GET /api/v1/documents/{id}/chunks` | The chunk inspector: what one document became, with each chunk's page or section. |
| `DELETE /api/v1/documents/{id}` | The document, its object and its vectors. |
| `GET /healthz` | Liveness. Checks nothing else — a dependency outage must not get the pod restarted into the same outage. |
| `GET /readyz` | Readiness. Probes Postgres, Redis, Qdrant, and object storage concurrently; 503 names what is broken. |
| `GET /metrics` | Prometheus. Request counts and latency labelled by route template. |

Data-plane errors use the **OpenAI** error envelope so client SDKs raise a useful typed
exception; control-plane errors use the gateway's own envelope, which carries the request
id. Unsupported fields (`tools`, `tool_choice`, `functions`, `function_call`, `logprobs`)
are refused with a 400 naming the field rather than silently dropped.

Every log line is one JSON object carrying `request_id`, which is also returned as
`X-Gateway-Request-Id` and honoured on the way in for cross-system correlation.

Control-plane routes are **authenticated by default**: they hang off a router that
carries the `CurrentUser` dependency, so an endpoint added by a later task is protected
unless somebody deliberately registers it on the public router. A test asserts that every
`/api/v1` operation outside a short allow-list answers 401 without a token.

Login is throttled per IP and per email, in Redis so the limit holds across replicas. If
Redis is unreachable the throttle **fails open** and logs a warning: failing closed would
lock every operator out of the UI during a Redis outage, and `/readyz` already pulls such
an instance out of the load balancer.

Suspending an organization takes effect immediately, not at the next token expiry: its
members are refused at login, at refresh, and on every control-plane request. A
superadmin is unaffected, because somebody has to be able to un-suspend it.

"Test connection" and "Test gateway" are rate-limited per user (20 a minute each by
default, `MODEL_TEST_MAX_ATTEMPTS`), because every press is an outbound call billed to
whoever owns the model. Like the login throttle they fail open when Redis is unreachable;
the exposure is bounded by what the action costs, which is a handful of tokens.

# Memory Gateway — Project Specification

**Status:** Draft v1.0
**Date:** 2026-09-06

---

## 1. Overview

Memory Gateway is a multi-tenant Python service that sits between AI clients and upstream
LLM providers. It presents an **OpenAI-compatible API**, and on every request it **augments
the prompt with memory** — retrieved organizational documents plus durable facts learned
about the end user — before forwarding to a configured upstream model.

Operators run one deployment serving many customer organizations. Each organization
self-serves through a web UI: it configures connectors (data sources), gateways (public API
endpoints), and upstream routing, and watches traffic on a monitoring page.

### 1.1 Value proposition

A customer changes one line in their existing code:

```python
client = OpenAI(base_url="https://gw.example.com/g/acme-support/v1", api_key="mg_...")
```

and every completion from that point on is grounded in their document corpus and personalized
by what the platform has learned about the calling end user — with no change to their prompts,
their SDK, or their application logic.

### 1.2 Primary use cases

1. **Grounded assistant** — a customer support bot whose answers are backed by the customer's
   own uploaded manuals and policies.
2. **Personalized assistant** — an app where the model remembers each end user's preferences,
   context, and prior decisions across sessions without the app implementing memory itself.
3. **Traffic control** — an operator running A/B evaluations across models, or a failover
   chain for availability, without touching client code.

---

## 2. Goals and non-goals

### 2.1 Goals

- Drop-in OpenAI compatibility, including SSE streaming.
- Hybrid memory: document RAG **and** per-end-user conversation memory.
- Multi-tenant isolation at the organization boundary, enforced in data, storage, and UI.
- Configuration entirely through the UI/API — no restarts, no YAML edits, no redeploys.
- Full request/response capture (configurable per gateway) feeding both debugging and memory
  distillation.
- Stateless application processes so the same image runs under Compose and Kubernetes.

### 2.2 Non-goals for v1

Explicitly out of scope; see §16 for the deferred roadmap.

- Tool / function-call passthrough (`tools`, `tool_choice`, `tool_calls`).
- Usage-based cost accounting and billing.
- Non-chat OpenAI surfaces beyond `/v1/models` (no `/v1/embeddings`, `/v1/images`, Assistants).
- Connector types other than the managed file drop.
- Fine-tuning, evaluation harnesses, or prompt versioning/experiments UI.
- Anthropic-native (`/v1/messages`) inbound protocol.

---

## 3. Core concepts

| Concept | Definition |
|---|---|
| **Organization** | The tenant. Owns users, connectors, gateways, org-scoped models, and all telemetry. Isolation boundary for every query in the system. |
| **User** | A human logging into the UI. Belongs to exactly one organization (except platform superadmins). Has a role. |
| **Upstream Model** | A callable LLM endpoint: base URL, provider dialect, model id, credentials, default parameters, and an optional system-context portion. Either **global** (operator-owned) or **org-owned**. |
| **Connector** | A source of documents for memory. v1 ships one type: **managed file drop**. Org-scoped. |
| **Gateway** | A published API endpoint. Has a slug, its own API keys, an upstream routing policy, a memory policy (which connectors, retrieval knobs), a logging policy, and rate limits. |
| **End user** | The customer's user, identified per-request via the `user` field or headers. Never logs into this system. The subject of conversation memory. |
| **Document / Chunk** | An ingested file and its embedded fragments, stored in Qdrant with org/connector metadata. |
| **Memory Fact** | A durable, embedded statement about an end user, distilled asynchronously from logged transcripts. |

### 3.1 Entity relationships

```
Organization 1─* User
Organization 1─* Connector 1─* Document 1─* Chunk ──► Qdrant
Organization 1─* UpstreamModel (org-owned)          ┐
Platform     1─* UpstreamModel (global catalog)     ├─► referenced by
Organization 1─* Gateway *─* UpstreamModel (via GatewayTarget, weighted/ordered)
Gateway      *─* Connector (via GatewayConnector)
Gateway      1─* ApiKey
Gateway      1─* RequestLog 1─? Transcript ──► distillation ──► MemoryFact
Organization 1─* EndUser 1─* MemoryFact ──► Qdrant
Organization 1─* AuditEvent
```

---

## 4. Architecture

### 4.1 Components

| Component | Responsibility |
|---|---|
| **API service** (FastAPI, ASGI) | Serves three route groups: the OpenAI-compatible proxy (`/g/{slug}/v1/*`), the admin/control API (`/api/v1/*`), and static SPA assets. Stateless. |
| **Worker** (Celery or arq on Redis) | Async jobs: document ingestion, embedding, reindexing, transcript distillation, log retention pruning, rate-limit rollups. Stateless. |
| **Postgres** | Source of truth for orgs, users, models, connectors, gateways, keys, document metadata, request logs, transcripts, audit events. |
| **Vector store** | Per-organization collections for document chunks and memory facts. **Qdrant by default; Chroma optional** (task 19), chosen per organization from the set the deployment configures. |
| **S3-compatible object store** | Raw uploaded files for managed file-drop connectors. MinIO in dev, S3/GCS in prod. |
| **Redis** | Job queue broker, rate-limit token buckets, short-lived caches (gateway config, resolved keys). |
| **Web UI** (React + TypeScript SPA) | Login, configuration, monitoring. Talks only to the control API. |

### 4.2 Request path (happy path)

```
client
  │  POST /g/{slug}/v1/chat/completions   (OpenAI schema, Bearer mg_...)
  ▼
[1] Resolve gateway by slug        ── cached
[2] Authenticate API key           ── hash lookup, org scoping
[3] Enforce rate limit / quota     ── Redis token bucket → 429
[4] Resolve end-user identity      ── `user` body field + X-Gateway-* headers
[5] Build retrieval query          ── last user message (+ short window)
[6] Retrieve in parallel:
        · document chunks  ── Qdrant, filtered to gateway's connectors
        · memory facts     ── Qdrant, filtered to (org, end_user)
[7] Assemble prompt                ── layered prepend, §7
[8] Select upstream target         ── single | failover | weighted A/B, §8
[9] Forward (streaming or not), translating dialect if needed
[10] Return to client; tee response into the log buffer
  │
  └──► async: persist RequestLog (+ Transcript per logging policy)
       async: distillation worker → MemoryFact
```

**Latency budget target:** steps 1–8 add **< 150 ms p95** over the bare upstream call. Retrieval
(step 6) is the dominant cost and runs both queries concurrently.

### 4.3 Technology choices

- Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) + Alembic.
- `httpx` with connection pooling for upstream calls; SSE relayed without buffering.
- React 18 + TypeScript + Vite; TanStack Query; Tailwind; Recharts for the monitoring page.
- `uv` for dependency management; `ruff` + `mypy` for lint/type; `pytest` + `pytest-asyncio`.

---

## 5. Multi-tenancy and authentication

### 5.1 Two distinct auth planes

| Plane | Who | Mechanism |
|---|---|---|
| **Control plane** (`/api/v1/*`, the UI) | Humans | Email + password (Argon2id), short-lived access JWT + rotating refresh token in an httpOnly cookie. Auth is abstracted behind a provider interface so OIDC/SSO can be added without touching call sites. |
| **Data plane** (`/g/{slug}/v1/*`) | Customer applications | Gateway API key, `Authorization: Bearer mg_<id>_<secret>`. Stored as a hash; plaintext shown once at creation. Keys are gateway-scoped. |

### 5.2 Roles

- **superadmin** (platform): manages organizations, the global model catalog, platform settings.
  Can view any org for support purposes — every such access is audit-logged.
- **org_admin**: full control within their organization, including members, keys, and connectors.
- **org_member**: read/write on connectors and gateways, cannot manage members or reveal keys.
- **org_viewer**: read-only, including monitoring.

### 5.3 Isolation rules

- Every control-API query is scoped by `organization_id` derived from the session, never from
  a client-supplied parameter. Enforced in a repository base class, not per-endpoint.
- Vector collections are named `org_{org_id}_docs` and `org_{org_id}_memory`; every search
  additionally carries an `org_id` payload filter as defense in depth. The naming is a
  property of the port, not of any one backend, and holds for every backend.
- A vector backend's **address is deployment configuration** and never tenant input. An
  organization is bound to a backend *by name*, chosen from the set the deployment enabled;
  no API or screen accepts a connection string. Same rule as §5.4's upstream URLs.
- Object storage keys are prefixed `orgs/{org_id}/connectors/{connector_id}/...`.
- A gateway may reference only global models and models owned by its own organization.
  Credentials for global models are never exposed to org users in any API response.

### 5.4 Secret handling

Provider API keys and connector credentials are encrypted at rest with envelope encryption
(AES-GCM data keys wrapped by a KMS key, or a master key from the environment in dev). They are
write-only over the API: responses return `{"configured": true, "hint": "sk-...4f2a"}`, never
the value.

---

## 6. Memory subsystem

### 6.1 Two memory kinds

**A. Document memory (RAG)** — organizational knowledge from connectors. Shared across all end
users of a gateway. Retrieved by semantic similarity to the current turn.

Every retrievable point carries a `kind`: `source` for a chunk of a document, or `summary`
(task 102) for the one point per document a connector in `summary_chunk` mode adds — a short,
model-written description of the whole document, so that "do we have a policy on this at all?"
finds something. A summary is *rewritten* text the document does not contain, and it is
labelled everywhere it appears — the prompt heading (§7), the citation (§7.1), the chunk
inspector — so it can never be mistaken for a quote. It is always in addition to the source
chunks, never instead of them.

**B. Conversation memory** — durable facts about a specific end user, distilled asynchronously
from their logged transcripts. Private to `(organization, end_user_id)`.

### 6.2 End-user identity resolution

Resolved per request, first match wins:

1. `X-Gateway-User` header — explicit, preferred.
2. `user` field in the OpenAI request body — the standard OpenAI convention.
3. Fallback: `anon:{sha256(api_key_id + client_ip)[:16]}`, when the gateway allows anonymous
   memory; otherwise conversation memory is skipped entirely for that request.

Session/conversation scoping, used for recency weighting and transcript grouping:

1. `X-Gateway-Session` header — explicit.
2. Fallback: `sha256` of the serialized message list minus the final turn, so a growing
   conversation hashes to a stable thread id across turns.

An `EndUser` row is created on first sight and carries `external_id`, first/last seen, request
count, and an optional display label.

### 6.3 Retrieval

Per gateway, configurable:

| Setting | Default | Meaning |
|---|---|---|
| `connector_ids` | `[]` | Which connectors this gateway may read. Empty = no document memory. |
| `doc_top_k` | 6 | Chunks retrieved before filtering. |
| `doc_min_score` | 0.35 | Cosine similarity floor; below it, a chunk is dropped. |
| `doc_max_tokens` | 2000 | Hard cap on injected document text; chunks are dropped from the tail. |
| `memory_enabled` | true | Whether conversation memory is recalled. |
| `memory_top_k` | 8 | Facts retrieved. |
| `memory_max_tokens` | 600 | Hard cap on injected memory text. |
| `query_strategy` | `last_user_message` | Also: `last_n_turns` (concatenate the last N user turns). |
| `on_retrieval_error` | `fail_open` | `fail_open` proceeds without memory; `fail_closed` returns 503. |

Retrieval and memory recall run **concurrently** with `asyncio.gather`, each under an
independent timeout (default 800 ms). A timeout is handled per `on_retrieval_error`.

### 6.4 Conversation memory write-back (async distillation)

After a response completes and its transcript is persisted, a job is enqueued:

1. **Debounce** — coalesce by `(end_user_id, session_id)` with a short delay (default 30 s) so a
   burst of turns produces one distillation pass.
2. **Extract** — send the exchange plus the user's current top facts to the configured
   **distillation model** (a cheap model set per organization, defaulting to a platform-wide
   default) with a structured-output prompt returning:
   ```json
   {"facts": [{"text": "...", "kind": "preference|fact|goal|constraint",
               "confidence": 0.0, "supersedes": ["fact_id"], "ttl_days": null}]}
   ```
3. **Reconcile** — for each candidate: embed it, search existing facts for the same end user; if
   similarity > `dedupe_threshold` (default 0.92), update the existing fact's `last_seen` and
   confidence instead of inserting. Facts named in `supersedes`, or contradicted per the model's
   output, are marked `superseded_at` rather than deleted.
4. **Store** — insert into Postgres (`memory_facts`) and upsert the vector into
   `org_{org_id}_memory` with payload `{org_id, end_user_id, kind, confidence, created_at}`.
5. **Bound** — enforce `max_facts_per_user` (default 500) by evicting the lowest-scoring facts,
   ranked by `confidence × recency_decay`.

Distillation failures are retried with backoff and never affect the serving path. A gateway
whose logging policy stores no bodies (§10.2) cannot distill; the UI warns when memory write-back
is enabled without body capture.

### 6.5 End-user memory controls

- The UI exposes a per-end-user memory browser: list, search, edit, and delete facts.
- Control API supports `DELETE /api/v1/end-users/{id}/memory` (right-to-erasure), which purges
  facts, vectors, and — optionally — transcripts for that end user.

### 6.6 Retrieval evaluation (task 103)

Whether a gateway's retrieval finds the right chunks is a question with a numeric answer, and it
needs labelled questions. Organizations do not have those; they have a request log full of
questions, the record of which chunks each answer cited (§7.1), and a **Try retrieval** box
they already tune by hand. An **evaluation set** is those things given a table.

**Sets and items.** A set belongs to a gateway, because its labels only mean something against
the connectors that gateway reads. An item is a question with the chunks and/or documents that
answer it; a chunk label carries the chunk's **text at labelling time**, so a recut — which
gives every point a new id — does not kill the label: the run re-anchors it to the chunk that
now holds that text (or to both halves, when the recut split it) and reports how many labels
it could not place. An item with no label at all is a **negative**: a question the corpus
should answer with nothing, so precision has something to be wrong about and `doc_min_score`
something to defend. Every item records its **source** — `manual`, `citation` (imported from
the log with the chunks the answer cited), `log` (imported, uncited, unlabelled) or
`generated` (a model wrote the question from the chunk) — and whether a person has
**verified** it. Labels from citations are free and biased; labels from people are expensive
and few; labels from a model are cheap and circular. The set keeps the three apart and every
run reports the verified population beside the whole; a single blended number would be more
comfortable and would mean nothing.

**Runs.** A run sends every item's question through the **same** retrieval a request goes
through — `MemoryService`, with the gateway's saved configuration or the editor's unsaved
patch merged exactly as Try retrieval merges it — and scores what came back. Per item: the
chunks retrieved with scores, which were relevant, and the rank of the first relevant one. Per
run: **recall@k**, **precision@k** and **MRR** at chunk level, the **hit rate** at document
level, and the same numbers **after the budget** — over the chunks that survived `doc_min_score`
and `doc_max_tokens`, which is what the model would actually have seen; a relevant chunk
retrieved at rank five and dropped by the budget is not a recall. `k` is the gateway's
`doc_top_k`. Precision is over what was retrieved rather than over `k`, because the floor
returning three chunks when six were allowed is the setting doing its job. A run is a
**measurement of a known state**: it stores the effective configuration, the embedding model,
the tokenizer, and per connector the chunk fingerprints its documents were cut with, so two runs
can be diffed and the diff names what changed between them — a setting, a reindex, a model —
rather than only that the number moved. A run costs one embedding call per question, runs on the
worker with progress, and takes at most 500 items.

**What the numbers cannot mean.** They measure retrieval, not answers: whether the model used
the right chunk well is a different measurement with a different cost and is out of scope. A run
that includes generated items says so on its headline, because a question written *from* a
chunk finds that chunk more easily than a question a person asked. Nothing here writes to the
index; an audit or a run leaves the collection byte-identical.

**Index audits** are the same idea one layer down: whether a connector was chunked well and
whether its embeddings are sane, over the *whole* index rather than one document. A **chunking
audit** scrolls the live collection once and reports the chunk-size histogram, the five numbers
Compare shows (§9.3) over every point, and **findings** — chunks under a floor, single-chunk
documents, chunks at or far over the ceiling, mid-sentence starts on prose formats, exact
duplicates across documents, documents cut unlike their neighbours, a mix of chunk
fingerprints — each with a count, the documents behind it, and a link to the page that fixes it;
per format as well as overall, because a repository's Markdown and its lockfiles have different
healthy shapes. An **embedding audit** checks the stored vectors: their width against the
platform setting, zero and identical vectors (padding returned as embeddings), the norm
distribution, **intra-document agreement** (for a sample of chunks, is the nearest neighbour
from the same document — asked of the index itself, so the answer is the ranking a request would
get), and — the one check that spends, priced before it runs — **drift**: a sample re-embedded
with the current model and compared with what is stored, where a bimodal result means two models
are in the collection and a uniform offset means the provider changed something underneath the
same model id. Findings carry a severity; red ones are the dashboard's degraded state.

---

## 7. Prompt assembly

A single system message is constructed by **layered prepend in fixed order**. Any empty layer is
omitted along with its delimiter.

```
[1] model.system_context         — from the selected upstream model
[2] gateway.system_context       — org's instructions for this endpoint
[3] retrieved documents          — rendered as a delimited block
[4] end-user memory              — rendered as a delimited block
[5] client's own system message  — verbatim, from the incoming request
```

Rendered shape — the **default** of a per-gateway template set (task 105; see the end of
this section):

```
{model.system_context}

{gateway.system_context}

## Reference material
The following excerpts are retrieved from the organization's knowledge base. Cite them when
relevant. If they do not answer the question, say so rather than inventing an answer.

[1] source: handbook.pdf (p. 12)
<chunk text>

[2] source: pricing.md
<chunk text>

[3] summary of: handbook.pdf
<the document's summary>

## What you know about this user
- Prefers concise answers with code examples.
- Works primarily in Python and Terraform.

{client system message}
```

Rules:

- The client's remaining messages (user/assistant turns) are forwarded unchanged.
- If the client sent multiple system messages, they are concatenated in order into layer 5.
- Total injected memory (layers 3+4) is capped by the gateway's token limits; the assembler
  truncates the document block first, then the memory block, and records what was dropped on
  the request log.
- The fully assembled system message is stored on the transcript when body logging is enabled —
  this is the primary debugging surface for "why did it answer that?".

**Templates (task 105).** The text the gateway itself writes is nine templates on the gateway
(`template_config`), each defaulting to the exact string above so an unedited gateway renders
byte-identically. Four are plain text: `reference_heading`, `reference_instruction`,
`memory_heading` and, on the response side, `sources_heading`. Five take placeholders from a
closed vocabulary, substituted once by name and never evaluated (no attribute access, no
expressions; a literal brace is `{{`/`}}`; a substituted value is never rescanned):

| Template | Default | Placeholders |
|---|---|---|
| `excerpt` | `[{handle}] source: {source_name}{section}\n{text}` | `{handle}`, `{source_name}`, `{section}` (` (p. 12)` or empty), `{section_raw}`, `{text}`, `{score}` |
| `fact` | `- {text}` | `{text}` |
| `source_line` (§7.1) | `[{handle}] {label}` | `{handle}`, `{label}` (name plus section, linked when a URL exists), `{source_name}`, `{section}`, `{url}` |
| `answer_prefix` / `answer_suffix` (§7.1) | empty | `{cited_count}`, `{injected_count}`, `{gateway}`, `{model}` |

Two invariants are enforced at save time: the excerpt template **must contain `[{handle}]`**
(the model cites by it and §7.1 resolves by it), and the fact template is **one line** (each
fact is flattened to one so a fact cannot open a new section of the prompt). An unknown
placeholder is a 422 naming it and listing the ones allowed. A document summary (task 102) keeps
its fixed `summary of:` shape under any excerpt template: it is a description, not a quote.
The budget measures the rendered block, template included. The organization may set
`template_defaults` under its settings; a new gateway starts from them, at creation only.
Every request log row records the **template fingerprint** — a short hash of the nine
effective strings — so a change of wording is a filter on Monitoring and a named change
between two evaluation runs (task 103).

### 7.1 Citations on the way back

The numbered handles are honoured in the answer, not only in the prompt. When the model writes
`[2]` — or `[1, 3]`, `[2-4]`, `[^2]`, `[2][3]` — the gateway resolves each handle against the
chunks *this request's* prompt numbered (the assembler's numbering, after budget truncation, never
a recount) and records on the request log which injected chunks were **cited** (`cited_chunk_ids`)
beside which were **injected** (`retrieved_chunk_ids`), plus how many handles named no chunk
(`citations_unresolved`). Handles inside fenced code blocks, and a bracket glued to a word
(`items[0]`), are not citations. This record is kept for every request with documents injected,
whatever the gateway's delivery mode below — cited-versus-injected is the one relevance signal
that arrives free.

What the client sees is the gateway's `citations` setting (memory configuration, default `off`):

| Mode | Behaviour |
|---|---|
| `off` | The response is byte-identical to the upstream's. Nothing is added. |
| `metadata` | A `citations` array and a `citations_unresolved` list are added to the assistant message (`choices[n].message`); streaming, they arrive in the `delta` of one final extra chunk after the upstream's last frame and before `[DONE]`. The text is not changed. |
| `footer` | `\n\nSources:` and one line per cited chunk — `[2] handbook.pdf (p. 12)`, linked to the chunk inspector when the deployment has a UI address — are appended to the content, as a final content delta when streaming. Handles that resolved to nothing are removed from the text. The heading and the line are the gateway's `sources_heading` and `source_line` templates (§7). |

Handles are never renumbered: the `[3]` in the footer is the `[3]` the model wrote. The
upstream's `usage` is forwarded as received; the footer is not counted against it. Under
`footer`, streaming holds back only an unfinished trailing handle (`[`, `[2,`) until the next
frame decides what it is; every other frame is relayed as the provider sent it, so
time-to-first-token is unchanged.

**Answer prefix and suffix (task 105).** A gateway may wrap the answer in its own text:
`answer_prefix` is the first content delta of a stream (one frame, sent before the provider's
first word) and `answer_suffix` the last, after the footer and before `[DONE]`; non-streaming,
the message content is prefix, answer (footer included), suffix. Both apply whatever the
citation mode and whether or not anything was injected, are outside the provider's `usage`,
and — empty by default — add no frame at all, so the `off` path stays byte-identical. In a
stream the prefix is sent before the answer exists, so `{cited_count}` there renders `0`.

---

## 8. Upstream routing

### 8.1 Modes

A gateway has exactly one routing mode over an ordered list of **targets**, where each target
references an upstream model.

| Mode | Behavior |
|---|---|
| **single** | Exactly one target. All traffic goes to it. |
| **failover** | Targets form a priority chain. Try target 1; on a retryable failure, try target 2, and so on. Errors surface to the client only after the chain is exhausted. |
| **ab_split** | Each target carries a weight (percentages summing to 100). A target is chosen per request by weighted selection. The chosen target is recorded on the request log so results can be compared. No failover — a failure is returned to the client. |

`ab_split` uses **sticky assignment** when an end-user id is present: the target is chosen by
`hash(end_user_id + gateway_id) % 100` against the cumulative weight bands, so a given end user
consistently lands on the same variant. Without an end-user id, selection is random.

### 8.2 Retryable failures (failover mode)

Retry on: connection error, DNS failure, timeout (configurable, default 60 s to first byte),
HTTP 408/429/500/502/503/504. Do **not** retry on 400/401/403/404/422 — these indicate a
malformed or unauthorized request that the next target would also reject.

**Streaming constraint:** failover is only possible before the first byte reaches the client.
Once any SSE chunk is flushed, a mid-stream upstream failure is terminated with an error event;
it cannot be transparently retried. This is documented behavior, and the request log records
`failed_after_stream_start = true`.

### 8.3 Provider dialects

An upstream model declares a `dialect`:

- `openai` — passthrough (also covers Azure OpenAI, Together, Groq, vLLM, Ollama, OpenRouter,
  and anything else OpenAI-shaped; differences are handled by base URL and auth style).
- `anthropic` — translated: system message extracted to the top-level `system` parameter,
  messages mapped to Anthropic content blocks, `max_tokens` required and defaulted, streaming
  events translated back into OpenAI `chat.completion.chunk` frames.

The adapter interface is `UpstreamAdapter.prepare(request) -> HTTPRequest` and
`UpstreamAdapter.parse(response) -> OpenAIResponse | AsyncIterator[OpenAIChunk]`, so adding
`bedrock`, `vertex`, or `gemini` later means one new class.

### 8.4 Model configuration fields

```
id, scope (global|org), organization_id (null when global), name, description,
base_url, dialect, upstream_model_id, auth_type (bearer|api_key_header|azure|none),
credential_ref (encrypted), extra_headers, system_context,
default_params {temperature, top_p, max_tokens, ...},
timeout_seconds, context_window, tokenizer, enabled
```

Gateway-level `param_overrides` are applied last, so an org can pin `temperature` for an
endpoint regardless of what the client requests. A gateway may also declare params as `locked`,
in which case client-supplied values are ignored rather than merged.

**`tokenizer` is derived unless overridden (task 101).** Every count made on a model's behalf —
the document and memory budgets in §7, the prompt-token estimate in §11 — is measured with the
tokenizer of *that* model, chosen from a closed registry: the `tiktoken` encodings
(`cl100k_base`, `o200k_base`, `p50k_base`), `approximate` with a characters-per-token ratio,
and `words`. `null` means the platform derives it from the dialect and model id (`gpt-4o*` →
`o200k_base`, `gpt-4*` → `cl100k_base`, `claude*` → `approximate:3.5`, anything else →
`approximate:4`); an override names one explicitly. The API returns both the stored value and
the effective one with its origin, so a wrong derivation is diagnosable as "no override set".
Every request records our estimate and its tokenizer beside the provider's `prompt_tokens`;
the ratio of the two per model over a window is the **calibration**, shown on the model page
with a **Calibrate** action that stores the measured ratio as an `approximate` override.
Drift over 15% is a warning on the model and on every gateway routing to it, and the gauge
`tokenizer_drift_ratio{model}` carries it for anyone who wants an alert.

---

## 9. Connectors and ingestion

### 9.1 v1 connector type: managed file drop

Each connector provisions a prefix in the platform's object store:
`orgs/{org_id}/connectors/{connector_id}/`.

Files arrive by:
- **UI upload** — drag-and-drop, multipart to the control API, streamed to object storage.
- **Presigned URL** — `POST /api/v1/connectors/{id}/upload-url` returns a short-lived presigned
  PUT, so customers can script uploads without proxying bytes through the API.

Ingestion is triggered by the upload completing (the API enqueues the job directly; an
object-store event notification is an optional prod optimization). A manual **Resync** action
re-scans the prefix and reconciles: new objects ingested, changed objects (by ETag) re-ingested,
removed objects' documents and vectors deleted.

The connector abstraction is deliberately type-agnostic — `list_objects()`, `fetch(object)`,
`watch()` — so S3-external, SQL, and HTTP connectors slot in later without changing the
ingestion pipeline or the retrieval path.

### 9.2 Supported formats

| Group | Formats | Extraction |
|---|---|---|
| Text | `.txt`, `.md`, `.rst`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.yaml`, `.xml`, `.html` | Direct decode; HTML stripped to text; CSV/JSON rendered to readable records. |
| Code | `.py`, `.js`, `.ts`, `.go`, `.java`, `.rb`, `.rs`, `.sql`, `.sh`, and similar | Direct decode, chunked on structural boundaries where possible. |
| Documents | `.pdf` | Text-layer extraction with page numbers preserved as chunk metadata. Scanned/image-only PDFs are flagged `needs_ocr` and skipped in v1. |
| Office | `.docx`, `.pptx`, `.xlsx` | Paragraph/slide/sheet extraction with heading and slide-title metadata. |

Unsupported types are recorded as `skipped` with a reason and surfaced in the connector's
ingestion log — never silently dropped.

Per-file limits: default max 50 MB, configurable per deployment. Extraction runs in the worker
with a wall-clock cap so one pathological file cannot occupy a worker indefinitely.

### 9.3 Chunking (configurable per connector)

```
strategy:   recursive | fixed | by_heading | semantic | sentence_window | code
chunk_size: 1000        # tokens
overlap:    150         # tokens
respect_boundaries: true  # do not split mid-sentence / mid-code-block

breakpoint_percentile: 85   # semantic only
min_chunk_size:        200  # semantic only — the floor
window_sentences:      2    # sentence_window only — neighbours kept either side

overrides:                  # per format kind, each a partial of the above
  code: {strategy: code}
```

The first three cut on a token budget, adjusted for where punctuation happens to be.
`by_heading` uses Markdown headings, DOCX heading styles, and PPTX slide boundaries, falling
back to `recursive` for formats with no structure.

The last three cut on something a token budget cannot see:

* **`semantic`** splits into sentences, embeds each, and cuts where consecutive-sentence
  distance exceeds a percentile breakpoint over *that document's own* distribution. A
  percentile rather than an absolute threshold, because the distance scale is a property of
  the embedding model. `chunk_size` becomes a ceiling rather than a target, and
  `min_chunk_size` is the floor that stops a page of short declarative sentences becoming
  one chunk per sentence.
* **`sentence_window`** embeds one sentence and stores that sentence plus `window_sentences`
  neighbours as the chunk text. The two strings have different jobs: the sentence is what a
  query matches, the window is what goes into the prompt, and §7's `doc_max_tokens` is
  measured on the window.
* **`code`** splits on declarations, carrying the enclosing signature into each fragment of
  an oversized body. Structural for Python, JavaScript, TypeScript and Go; `recursive` for
  every other language and for any file that will not parse. A syntax error must cost a
  worse chunking of that file, never a failed document.

**A connector is a source, not a format.** `overrides` maps a format kind — the closed set
the extraction metrics are labelled by — to a partial configuration, so a repository can cut
its code structurally and its Markdown recursively. The effective configuration for a
document is the connector's with its format's override applied, it is what the connector
screen displays, and a change to one override invalidates only that format's chunks.

**Two strategies depend on the embedding model, and one of them changes what §9.4 costs.**
Under `semantic` the boundaries themselves came out of the model, so a platform model change
cannot re-embed those chunks — it has to **recut** them from object storage. The reindex
estimate counts those connectors separately, because it is a different kind of cost from the
token count beside it.

Every chunk carries payload metadata: `org_id, connector_id, document_id, source_name,
source_uri, page_or_section, chunk_index, ingested_at, content_hash, token_count,
chunk_strategy, chunk_fingerprint, index_fingerprint, format_kind, tokenizer` — the
fingerprints for the same reason §9.4 records the embedding model, and with per-format
overrides a stronger one: two documents in one connector can legitimately be cut differently
(`chunk_fingerprint` is task 20's digest, kept for one release; `index_fingerprint` is task
104's structured one, §9.4, and `format_kind` is what retrieval compares it under). `tokenizer` (task 101) is what
`chunk_size` was *measured* with, by the name the tokenizer gives itself — so a worker whose
BPE vocabulary failed to load records `words (cl100k_base unavailable)` rather than claiming
the BPE. It is part of the fingerprint for every strategy: a chunk sized in a different unit is
a different chunk. A `sentence_window` chunk also carries
`embedded_text` and `window_sentences`, which is what lets the chunk inspector highlight the
matched sentence and what tells retrieval how far its near-duplicate filter should reach.
Every point carries `kind` (`source` or `summary`, task 102), and a chunk embedded under
`contextual` summarization carries `context` — the summary that was prefixed to what it was
embedded as — with `embedded_because` naming which of the two differences applies (`window`,
`context`, or `window+context`), because the inspector highlights a matched sentence for one
and shows a prefix for the other.

**Summarization before chunking (task 102).** A connector may have a model summarize each
document as it is ingested, in a new phase `summarizing` between extraction and chunking. The
input is the extracted text's head, and its tail if it fits, under `max_input_tokens` measured
with the embedding tokenizer; the prompt is fixed and versioned in code, never configurable.
The summary is stored on the document — editable by an operator, which makes it `manual` — and
used in one of two ways per connector, or both:

- `summary_chunk` adds **one extra point** per document with `kind: summary`, `section:
  Summary`, embedded like any chunk and counted against `doc_max_tokens` like any chunk. The
  source chunks are byte-identical to what `off` produces, so switching it on recuts nothing.
- `contextual` prefixes the summary to every source chunk's *embedded* text — the summary, a
  blank line, the chunk — and leaves the returned text unchanged. The embedding now depends on
  the prefix, so `mode`, the model's identity and the prompt version are part of the chunk
  fingerprint, switching it on marks every document stale, and the connector says what the
  re-embedding will cost before it is accepted.

Failure is handled by mode. Under `summary_chunk` a refusal, an empty reply, a cap hit or a
missing model indexes the document without a summary; the row says `summary: failed (reason)`
and a **Summarize** action retries just that phase. Under `contextual` a failure **fails the
document** with reason `summarization` and a message naming the model — half a corpus embedded
with context and half without is two corpora that rank differently — and a cap hit *parks*
it: `pending` with reason `summarization_cap`, retried after midnight UTC, and counted on the
connector and the dashboard as waiting. A retryable provider error raises for the job's
backoff, as everywhere else in the pipeline. The daily cap is per connector, counted from
`summarization_runs` before the call. Per-format overrides reuse the chunking override shape:
a repository connector summarizes the Markdown and not the lockfiles.

**Chunking is reviewable before it is committed.** `POST /connectors/{id}/chunking/preview`
runs a set of candidate configurations over one document and returns, per candidate, the
chunks it produces, a token distribution, how many chunks the size limit decided rather than
the strategy, how many boundaries fell mid-sentence, and how many embedding calls one
ingestion would cost. It writes nothing. Nobody can pick a chunking strategy from a
description, and without a comparison every user picks by name. When the connector summarizes
this document's format the comparison shows the stored summary as the prefix each candidate's
chunks would be embedded behind, and its cost line includes the summarization call — a
comparison that hid half the embedding cost would be the thing this endpoint refused to be.

### 9.4 Embeddings

A **single embedding model is a platform-level setting**, configured by superadmins (provider,
model id, dimension). Rationale: a collection's vectors must all come from one model, and
mixing models across a tenant silently degrades retrieval.

- Changing the platform embedding model requires an explicit **reindex** operation, which
  rebuilds collections in the background and makes the new one live atomically — no retrieval
  downtime.
- The active embedding model and dimension are recorded on each collection's metadata and on
  every document row, so drift is detectable.
- **One index fingerprint, defined once (task 104).** Every document row and every stored
  point carries `index_fingerprint`: five readable segments digesting everything the stored
  points depend on — the effective chunking settings for the document's format, the embedding
  model, the tokenizer, the contextual-summarization identity (or none), and the version of
  the extractor for that format. Nothing else is in it. Comparing a row's fingerprint with
  the one ingestion would write now says whether the row is stale and *which* input moved;
  the row keeps the model, tokenizer and strategy in clear so the reason can name old and
  new. A platform reindex rewrites only the model segment of the rows and points it
  re-embedded, because that is the one input it changed. A row with no fingerprint —
  indexed before the column existed — is *unrecorded*: shown as such, reprocessable on
  request, and never counted as stale, because a blank is not known to be wrong.
- **The embedding tokenizer is part of the chunking configuration (task 101).** `chunk_size`
  is a promise about the embedding model's input window, so it is measured with that model's
  tokenizer — derived from the provider and model name (`text-embedding-3-*` → `cl100k_base`;
  a model no vocabulary ships for → `approximate`, with the screen saying chunk sizes are
  estimates) and overridable in the same section. Changing it, derived or overridden, is a
  **recut, not a re-embed**: no reindex run starts, but every indexed document's fingerprint
  stops matching and each connector's document list shows it as stale until reindexed.

**Backends (task 19).** Qdrant is the default; Chroma is optional. Which one an organization
uses is a per-tenant binding, and both backends satisfy the same port — cosine similarity in
`[-1, 1]` where higher is better, `limit` counted after any score floor, and a promotion that a
concurrent reader never observes as "no collection". How a backend makes a promotion atomic is
its own business: Qdrant uses an alias, Chroma a pointer this deployment keeps. Moving an
organization between backends is an operation that copies, verifies, promotes and then drops the
source after a grace period — the same procedure as a reindex, without the re-embedding.

### 9.5 Ingestion status model

Each document moves through `pending → extracting → summarizing → chunking → embedding →
indexed`, or lands in `failed` / `skipped` with a message. The UI shows per-connector counts, a
live job list, and a retry action for failed documents. `summarizing` (task 102) is present
only for a connector that summarizes; a document parked on the summarization cap under
`contextual` reads `pending` with reason `summarization_cap` until the cap resets.

**Two axes (task 104).** Ingestion status is one axis; **index status** is the other:
`current | stale | reprocessing`. `stale` means the row's index fingerprint is no longer the
one ingestion would write for its format — the chunking, the embedding model, the tokenizer,
the summarization or the extractor moved since it was indexed; `reprocessing` means a
reprocessing run owns it. A document is `indexed` and `stale` at once, and that is the
normal state after a change, not an error. The status is **stored on the row** — set by the
connector service on every configuration save (one `UPDATE` per affected format, comparing
fingerprints, so a reverted setting un-marks them), by ingestion when it finishes, and by
the platform reindex when it adopts a model — and reconciled from the fingerprints nightly,
which logs any row the stored status had wrong. The fingerprint is the truth; the status is
the index over it, so a connector's stale count is a count and every screen agrees: the
connector header and list badge, the document table (filterable on either axis, with the
reason per row), the gateways that read the connector, and the dashboard. The change is
applied by a **reprocessing run**: a tracked, scoped (stale documents by default; a format;
everything; the unrecorded rows) re-ingestion with exact counters, progress and an ETA, a
`partial` outcome that keeps the failures and a **Retry failed** over exactly those,
sources gone from object storage counted as `skipped` rather than failed, continuation
after a worker death from the counters rather than from the start, and a history per
connector with who, when, how long, and estimated versus spent tokens. Retrieval keeps
serving throughout: a stale chunk is still returned, labelled `stale` on the request log
and the citation, and a document mid-replacement — its new cut is written before its old
tail is deleted — never returns the same text twice.

`chunking` is a pure CPU step for every strategy but `semantic`, which embeds the document's
sentences to find its boundaries. So that step can now fail from the outside, and the two
kinds of failure are separated exactly as elsewhere in the pipeline: a provider refusal that
will not change on a retry marks the document `failed` with reason `chunking_embedding` and a
message naming the *embedding provider* — not a generic chunking error, which would send
somebody to read the splitter — while a rate limit or an outage raises, so the job's backoff
handles it and the document is left alone.

---

## 10. Observability and logging

### 10.1 Metrics (monitoring page)

Time-series over a selectable window (1h / 24h / 7d / 30d), filterable by gateway, model, and
status:

- Request rate, and success/error breakdown by status class.
- Latency: p50 / p95 / p99, split into **time-to-first-token**, **retrieval time**, and
  **total time** — retrieval is broken out because it is the latency the gateway itself adds.
- Token counts: prompt, completion, and **injected memory tokens** (documents vs facts).
- Traffic distribution across upstream targets — essential for verifying A/B weights are real.
- Error taxonomy: upstream errors, retrieval timeouts, rate-limit rejections, auth failures.
- Ingestion health: documents indexed, failed, and pending per connector.
- Memory health: facts written per day, distillation failures, average facts per end user.
- Summarization health (task 102): documents summarized per day, tokens spent per day by
  model, failure rate, cap hits, the connectors spending the most over the window, and the
  documents waiting on a cap. Drawn from `summarization_runs`, one row per attempt with the
  **provider's reported token usage** — or the estimate, flagged, when the provider reported
  none — because this is the first table that records what a background pass *cost*, and it
  is the shape usage-and-cost accounting (§16) will build on. Counters
  `summarization_runs_total{outcome}`, `summarization_tokens_total{direction, model}` and
  `summarization_duration_seconds` fire alerts; the rows draw the charts. The dashboard's
  degraded-state list includes "N documents waiting on the summarization cap".
- Validation (task 103, §6.6): a connector's latest chunking and embedding audit, its age and
  its worst finding, on the connector; a gateway's evaluation runs with recall@k, precision@k
  and MRR, in the gateway editor. Not time series — an audit is a report over the index as it
  is, and a run is a measurement of a known state — but a health signal all the same: the
  dashboard's degraded-state list includes every connector whose last audit raised a red
  finding, because an index that ranks wrong looks healthy on every traffic chart.
- Reprocessing (task 104, §9.5): `documents_stale{connector}` as a gauge, set by every save
  and by the nightly reconciliation; `reprocessing_runs_total{outcome}` and
  `reprocessing_duration_seconds`. An alert on documents stale for longer than a day, pointed
  at the chunking runbook; the dashboard's degraded-state list names the connector and the
  age.

### 10.2 Request logging (configurable per gateway)

The default is **full capture** — bodies are what make distillation possible — but every part is
switchable:

| Setting | Default | Meaning |
|---|---|---|
| `log_metadata` | true | Always-on row: timestamp, gateway, key id, end-user id, session id, model chosen, status, latencies, token counts, retrieved chunk ids, error. |
| `log_request_body` | true | The client's original messages. |
| `log_assembled_prompt` | true | The full prompt actually sent upstream, including injected memory. |
| `log_response_body` | true | The completion text (streamed responses are reassembled). |
| `retention_days` | 30 | Bodies are hard-deleted after this window; metadata rows are kept longer (`metadata_retention_days`, default 365). |
| `redaction_patterns` | `[]` | Regex list applied to bodies before persistence — for emails, card numbers, and similar. |
| `enable_distillation` | true | Whether transcripts feed conversation memory. Requires body logging. |

Metadata lives in `request_logs`; bodies live in a separate `transcripts` table so the hot
monitoring queries never touch large text columns, and retention pruning is a cheap partition
drop rather than a wide delete.

**Data-handling note:** capturing prompt bodies means storing customer end-user content. The UI
states this plainly on the gateway logging form, retention is enforced by a scheduled job (not
just documented), and each organization can set stricter defaults than the platform's.

### 10.3 Request detail view

Clicking a request in the monitoring page opens a drill-down showing: the original client
request, each retrieved chunk with its similarity score and source document — marked **cited**
when the answer's handles named it (§7.1), with any handles that named nothing listed — each
recalled memory fact, the fully assembled prompt, the routing decision (and any failover
attempts), the upstream response, and a timeline waterfall of the phases. This view is the
product's main debugging affordance — it answers "why did the model say that?" directly. The
request list can be filtered to requests that were given documents and cited none of them,
which is the query to run when a corpus is suspected of being irrelevant.

### 10.4 Audit log

Immutable, append-only record of every control-plane mutation: actor, organization, action,
target entity, before/after diff (secrets redacted), IP, user agent, timestamp. Covers model and
gateway changes, key creation and revocation, connector changes, member and role changes, and
any superadmin cross-org access. Viewable and filterable in the UI, exportable as CSV.

### 10.5 Operational telemetry

Structured JSON logs with a request id propagated end-to-end; OpenTelemetry traces spanning
auth → retrieval → assembly → upstream; Prometheus metrics at `/metrics`; `/healthz` (liveness)
and `/readyz` (checks Postgres, Qdrant, Redis, object store).

---

## 11. Rate limiting and quotas

Per gateway, and optionally per end-user id within a gateway:

```
requests_per_minute:   int | null
tokens_per_minute:     int | null   # prompt + completion
concurrent_requests:   int | null
requests_per_day:      int | null
```

Implemented as Redis token buckets keyed by `(gateway_id)` and `(gateway_id, end_user_id)`.
Exceeding a limit returns `429` with `Retry-After` and an OpenAI-shaped error body, so existing
client retry logic works unchanged. Rate-limit rejections are counted in metrics and are visible
per gateway, so an org can see when it is being throttled rather than guessing.

Token-based limits are enforced optimistically: prompt tokens are counted before dispatch,
completion tokens are settled after the response and carried into the next window.

The pre-dispatch count is made with the **target model's tokenizer** (§8.4), the same one the
prompt assembler budgeted with, so `tokens_per_minute` and `doc_max_tokens` are one unit
rather than two units with one name. It is an estimate; the provider's `prompt_tokens` is the
settlement. Both are recorded on the request log, and their ratio per model is the
calibration §8.4 describes — the measured error of the estimate, shown rather than guessed.

---

## 12. API surface

### 12.1 Data plane — `/g/{gateway_slug}/v1/*`

| Endpoint | Notes |
|---|---|
| `POST /chat/completions` | The core endpoint. Supports `stream: true` (SSE) and `stream: false`. Honors `model`, `temperature`, `top_p`, `max_tokens`, `stop`, `n`, `presence_penalty`, `frequency_penalty`, `seed`, `response_format`, `user`. |
| `GET /models` | Lists the virtual models this gateway exposes, in OpenAI list format. |

Custom request headers: `X-Gateway-User`, `X-Gateway-Session`, `X-Gateway-Memory: off`
(per-request opt-out of augmentation, useful for evaluation baselines).

Custom response headers: `X-Gateway-Request-Id`, `X-Gateway-Model` (the target actually used),
`X-Gateway-Memory-Chunks`, `X-Gateway-Memory-Facts`.

Unsupported OpenAI fields (`tools`, `tool_choice`, `functions`, `logprobs`) return a `400` with
an explicit message naming the unsupported field, rather than being silently dropped — a loud
failure is far cheaper to diagnose than a quietly degraded agent.

**Gateway extension — citations** (§7.1; only when the gateway's `citations` mode is not `off`,
and marked as an extension because no OpenAI client expects it). Under `metadata`, the assistant
message carries two extra fields:

```json
"message": {
  "role": "assistant",
  "content": "Expenses are reimbursed within thirty days [2].",
  "citations": [
    {
      "handle": 2,
      "chunk_id": "6f1c…:3",
      "document_id": "6f1c…",
      "document_name": "handbook.pdf",
      "connector_id": "a81e…",
      "section": "p. 12",
      "chunk_strategy": "recursive",
      "matched_text": null,
      "url": "https://gateway.example.com/connectors/a81e…?document=6f1c…&chunk=6f1c…%3A3"
    }
  ],
  "citations_unresolved": []
}
```

`citations` is in order of first citation, deduplicated; `matched_text` is the sentence that
matched under `sentence_window` chunking and `null` otherwise; `url` opens the chunk in the
control plane's inspector and is `null` when the deployment has no UI address. Streaming, the
same two fields appear in the `delta` of one extra chunk with no `content`, emitted after the
upstream's last frame and before `data: [DONE]`. Under `footer`, `content` ends with:

```
\n\nSources:\n[2] handbook.pdf (p. 12)\n[4] pricing.md
```

Every field here is information the model was already shown inside the prompt; nothing new is
disclosed to the client.

### 12.2 Control plane — `/api/v1/*`

```
POST   /auth/login            POST /auth/refresh        POST /auth/logout
GET    /auth/me               POST /auth/password

GET    /organizations         POST /organizations          # superadmin
GET    /organizations/{id}    PATCH /organizations/{id}
GET    /organizations/{id}/members     POST /organizations/{id}/invitations
PATCH  /members/{id}          DELETE /members/{id}

GET    /models                POST /models                 # scope-aware listing
GET    /models/{id}           PATCH /models/{id}           DELETE /models/{id}
POST   /models/{id}/test                                   # connectivity probe

GET    /connectors            POST /connectors
GET    /connectors/{id}       PATCH /connectors/{id}       DELETE /connectors/{id}
POST   /connectors/{id}/upload            POST /connectors/{id}/upload-url
POST   /connectors/{id}/resync
GET    /connectors/{id}/documents         DELETE /documents/{id}
POST   /documents/{id}/reindex
POST   /connectors/{id}/reprocess         GET  /connectors/{id}/reprocessing-runs   # task 104
GET    /reprocessing-runs/{id}            POST /reprocessing-runs/{id}/retry
POST   /connectors/{id}/stale-preview     GET  /reprocessing/alerts
POST   /connectors/{id}/reindex                             # alias of /reprocess, one release

GET    /gateways              POST /gateways
GET    /gateways/{id}         PATCH /gateways/{id}         DELETE /gateways/{id}
GET    /gateways/{id}/keys    POST /gateways/{id}/keys     DELETE /keys/{id}
POST   /gateways/{id}/test                                 # send a probe completion
POST   /gateways/{id}/try-retrieval   POST /gateways/{id}/prompt-preview   # accept unsaved memory_config and template_config

GET    /logs                  GET /logs/{id}               # metadata list, full detail
GET    /logs/templates                                     # template fingerprints in a window (task 105)
GET    /metrics/timeseries    GET /metrics/summary
GET    /end-users             GET /end-users/{id}/memory
PATCH  /memory-facts/{id}     DELETE /memory-facts/{id}
DELETE /end-users/{id}/memory

GET    /audit-events
GET    /platform/settings     PATCH /platform/settings      # superadmin
POST   /platform/reindex                                    # superadmin

GET    /connectors/{id}/audits            POST /connectors/{id}/audits/{kind}   # task 103
GET    /validation/alerts
GET    /gateways/{id}/evaluation-sets     POST /gateways/{id}/evaluation-sets
GET    /evaluation-sets/{id}  PATCH /evaluation-sets/{id}  DELETE /evaluation-sets/{id}
POST   /evaluation-sets/{id}/items        PATCH /evaluation-items/{id}  DELETE /evaluation-items/{id}
POST   /evaluation-sets/{id}/import       POST /evaluation-sets/{id}/generate
POST   /evaluation-sets/{id}/runs         GET /evaluation-sets/{id}/runs
GET    /evaluation-runs/{id}              GET /evaluation-runs/{id}/diff/{against}
```

All list endpoints are cursor-paginated and return `{items, next_cursor}`.

---

## 13. Web UI

Single React SPA, org-scoped after login, with a superadmin area gated by role.

### 13.1 Screens

**Login** — email/password, error states, password reset. OIDC buttons render when a provider is
configured (post-v1).

**Dashboard** — org-level snapshot: requests over the last 24 h, error rate, active gateways,
documents indexed, memory facts stored, and any degraded state (failed ingestion, unhealthy
upstream, near-quota gateways).

**Models** — two tabs: *Global catalog* (read-only for org users, showing name, dialect, and
availability but never credentials) and *Our models* (full CRUD). The form covers base URL,
dialect, model id, auth, system context, default params, and timeout, with a **Test connection**
button that sends a trivial completion and reports latency or the exact upstream error.

**Connectors** — list with per-connector status and document counts. Detail view has a
drag-and-drop upload zone, a document table (name, size, type, status, chunks, indexed-at,
summary status) with retry and delete, a chunking-configuration panel, a **Summarization**
panel (mode, model with the inherited fallback shown greyed, caps, and a cost line before
saving that includes the re-embedding under `contextual`), and a **Resync** action. Failed
documents show the extraction error inline; the document's summary is shown with **Edit** and
**Regenerate**, and the chunk inspector shows an embedded prefix above the returned text. A
**Validation** section (task 103) has two tabs, *Chunking* and *Embeddings*: the last report,
its age, a **Run** button (the embedding tab's drift check says what it will spend first), the
chunk-size histogram, the whole-index numbers per format, and the findings as a list where each
document opens Compare with that document preselected. The header (task 104) reads *"N of M
documents indexed under a previous configuration"* with the reasons, a **Reprocess** button
with a scope selector (stale only / these formats / everything / unrecorded) and the estimate,
a progress bar with an ETA and the failure count while a run is going, and a history drawer;
the document table has an index-status column with filter chips, the reason on hover and a
per-row **Reprocess**; the list has a stale badge with the count; every settings form says
how many documents saving will mark stale, before saving.

**Gateways** — the most substantial screen. A gateway editor with sections:

1. *Identity* — name, slug (which composes the live endpoint URL, shown with a copy button),
   description, enabled toggle.
2. *Routing* — mode selector (single / failover / A/B). Failover renders a drag-orderable
   priority list; A/B renders weight sliders that must total 100, with a live preview of the
   expected split.
3. *Memory* — connector multi-select, retrieval knobs, memory toggles, and a **Try retrieval**
   box where you type a question and immediately see which chunks and facts would be injected,
   with scores. This is the fastest way to tune a gateway. A notice (task 104) names each
   attached connector with stale or reprocessing documents — *answers may be drawn from two
   chunkings until it is reprocessed* — linking to the connector.
4. *Prompt* — the gateway system context, param overrides and locks, and a rendered preview of
   the assembled prompt for a sample question. A link at the bottom opens **Advanced**
   (task 105), its own route (`/gateways/{id}/advanced`): the nine §7 templates, each with a
   label saying where the text goes, its placeholder chips (click to insert), the default
   shown greyed when the value differs, a **Reset** per field, and the server's validation
   message under a field that fails. A preview box at the top takes one question and shows
   the assembled prompt and the citation examples rendered with the unsaved templates —
   nobody has to save to see. Two inline, non-blocking warnings: an empty instruction, and
   an excerpt that no longer prints `{source_name}`. Own save, own unsaved-changes guard.
5. *Logging* — the §10.2 toggles, retention, redaction patterns, and distillation switch.
6. *Limits* — rate limits and quotas.
7. *Keys* — create/revoke, last-used timestamps; the secret is revealed exactly once.
8. *Validation* (task 103) — evaluation sets: create, **import** last week's questions from
   the log (pre-labelled with what the answer cited, unverified), **generate** questions with
   a model (marked as such), edit items inline with a chunk picker that is Try retrieval, and
   verify them; runs, with the headline numbers per run, a diff between any two runs that names
   what changed, and a per-item drill-down with the retrieved chunks and the relevant ones
   marked. **Run** sends the Memory form above unsaved, the way Try retrieval does. Try
   retrieval itself gains **Add to evaluation set**.

**Monitoring** — time-range picker, filters, the §10.1 charts, and a request table that can be
live-tailed. Row click opens the §10.3 detail drawer, which shows the template fingerprint
beside the model name; the filter row gains **Template** when more than one fingerprint
appears in the window, listed with first-seen dates (task 105).

**Memory browser** — end users list with request counts and last-seen; drill into one to view,
search, edit, and delete their facts, or purge them entirely.

**Audit log** — filterable table with expandable before/after diffs.

**Settings** — org profile, members and roles, invitations, distillation-model selection, the
summarization-model default beside it (task 102; a connector with no model of its own uses
this, then the distillation model, then the platform default), org-level logging defaults,
and the organization's template defaults (task 105) — the same editor as Advanced minus the
preview and the response prefix/suffix; *new gateways start from these*.

**Platform (superadmin)** — organizations list and creation, global model catalog, embedding
model configuration and reindex, and platform-wide health.

### 13.2 UI principles

- Every destructive action (delete a gateway, revoke a key, purge memory) requires typed
  confirmation of the resource name.
- Configuration changes take effect without restart; the UI states when a change is live —
  and, for a change that reaches the index, *how far from live it is*: the stale count and
  the reprocessing progress are stored facts shown wherever the consequence is felt (task
  104), not a banner that only the saving tab ever saw.
- Empty states are instructional: a new org's connectors page explains what to upload and why.

---

## 14. Data model (Postgres, abbreviated)

```sql
organizations       (id, name, slug, status, settings_jsonb, created_at)
users               (id, organization_id NULL, email UNIQUE, password_hash, role,
                     name, status, last_login_at, created_at)
sessions            (id, user_id, refresh_token_hash, expires_at, ip, user_agent)

upstream_models     (id, scope, organization_id NULL, name, description, base_url, dialect,
                     upstream_model_id, auth_type, credential_ciphertext, extra_headers_jsonb,
                     system_context, default_params_jsonb, timeout_seconds, context_window,
                     tokenizer_jsonb NULL, enabled, created_at)

connectors          (id, organization_id, name, type, config_jsonb, chunking_jsonb,
                     summarization_jsonb, storage_prefix, status, last_synced_at, created_at)
documents           (id, connector_id, organization_id, source_uri, source_name, mime_type,
                     size_bytes, content_hash, status, error, chunk_count,
                     embedding_model, chunk_strategy, chunk_fingerprint, index_fingerprint,
                     index_status, reprocessing_run_id, tokenizer,
                     summary, summary_status, summary_error, summary_model, summary_model_id,
                     summary_prompt_version, summary_tokens_in, summary_tokens_out,
                     summarized_at, indexed_at, created_at)
reprocessing_runs   (id, organization_id, connector_id, trigger, scope, formats_jsonb,
                     requested_by, requested_by_label, reindex_run_id, status, total, done,
                     failed, skipped, estimated_tokens, spent_tokens, error, resumed,
                     report_jsonb, started_at, finished_at)
summarization_runs  (id, organization_id, connector_id, document_id, outcome, purpose, reason,
                     model_id, model_name, tokens_in, tokens_out, estimated, duration_ms,
                     created_at)
index_audits        (id, organization_id, connector_id, kind, status, created_by, drift_sample,
                     points, report_jsonb, severity, error, created_at, finished_at)
evaluation_sets     (id, organization_id, gateway_id, name, description, created_by, created_at,
                     updated_at)
evaluation_items    (id, organization_id, set_id, question, relevant_jsonb, relevant_document_ids,
                     source, verified, notes, created_at, updated_at)
evaluation_runs     (id, organization_id, set_id, gateway_id, status, created_by, patch_jsonb,
                     config_jsonb, snapshot_jsonb, metrics_jsonb, results_jsonb, total_items,
                     completed_items, error, created_at, started_at, finished_at)

gateways            (id, organization_id, slug UNIQUE, name, description, enabled,
                     routing_mode, system_context, param_overrides_jsonb, locked_params_jsonb,
                     memory_config_jsonb, logging_config_jsonb, limits_jsonb,
                     template_config_jsonb, created_at)
gateway_targets     (id, gateway_id, upstream_model_id, priority, weight)
gateway_connectors  (gateway_id, connector_id)
api_keys            (id, gateway_id, name, key_hash, prefix, last_used_at, revoked_at, created_at)

end_users           (id, organization_id, external_id, label, first_seen_at, last_seen_at,
                     request_count, UNIQUE(organization_id, external_id))
memory_facts        (id, organization_id, end_user_id, text, kind, confidence, source_log_id,
                     superseded_at, expires_at, created_at, last_seen_at)

request_logs        (id, organization_id, gateway_id, api_key_id, end_user_id, session_id,
                     upstream_model_id, status_code, error_code, streamed,
                     latency_total_ms, latency_retrieval_ms, latency_ttft_ms,
                     prompt_tokens, completion_tokens, memory_tokens,
                     retrieved_chunk_ids, retrieved_fact_ids, failover_attempts_jsonb,
                     cited_chunk_ids, citations_unresolved,
                     tokenizer, estimated_prompt_tokens, template_fingerprint,
                     created_at)                              -- partitioned by day
transcripts         (request_log_id PK, request_body, assembled_prompt, response_body,
                     distilled_at, created_at)                -- partitioned by day
audit_events        (id, organization_id, actor_user_id, action, target_type, target_id,
                     diff_jsonb, ip, user_agent, created_at)
platform_settings   (key PK, value_jsonb, updated_by, updated_at)
```

`request_logs` and `transcripts` are declaratively partitioned by day, making retention a
partition drop. Hot indexes: `(gateway_id, created_at DESC)`, `(organization_id, created_at DESC)`,
`(end_user_id, created_at DESC)`.

---

## 15. Deployment

The application is **stateless**: no local disk, no in-process caches that matter for
correctness, no sticky sessions. The same container image runs in both targets.

### 15.1 Development — Docker Compose

Services: `api`, `worker`, `postgres`, `qdrant`, `redis`, `minio`, `web` (Vite dev server).
`docker compose up` yields a working system seeded with a superadmin, a demo organization, a
demo connector with sample documents, and a demo gateway. A `make seed` target reproduces it.

### 15.2 Production — Kubernetes

Helm chart with separate `api` and `worker` Deployments (independently scalable), HPA on CPU and
request rate, a `PodDisruptionBudget`, and a migration Job gated as a Helm pre-upgrade hook.
Postgres, Redis, object storage, and Qdrant are external/managed. Secrets come from `Secret`
refs or an external secrets operator — never baked into the image or chart values.

Graceful shutdown drains in-flight streaming responses before exit
(`terminationGracePeriodSeconds` comfortably above the upstream timeout).

### 15.3 Configuration

Twelve-factor: everything by environment variable, validated at startup by a Pydantic `Settings`
model that fails loudly on missing or malformed values. Required: database URL, Qdrant URL,
Redis URL, object-store credentials and bucket, encryption master key, JWT signing key, public
base URL.

---

## 16. Roadmap beyond v1

Ordered by expected value, with the reasoning for deferral.

1. **Tool / function-call passthrough.** *Highest priority.* Without it, agentic clients
   (Cursor, Claude Code, LangChain/LlamaIndex agents) cannot use a gateway. v1 rejects such
   requests loudly (§12.1) rather than degrading silently, and the adapter layer is written so
   `tools` can be threaded through both dialects without restructuring.
2. **Usage and cost accounting.** Per-model pricing tables, token-priced request costing, and
   spend dashboards per org/gateway/end-user. Prerequisite for any commercial packaging.
3. **Additional connectors.** Customer-owned S3, SQL databases, HTTP/web crawl, and SaaS sources
   (Confluence, Notion, Google Drive) — the connector interface already accommodates them.
4. **Hybrid search and reranking.** BM25 alongside dense retrieval, plus a cross-encoder rerank
   pass. The single biggest retrieval-quality lever once real corpora are in play.
5. **Agentic memory tools.** Expose `search_memory` / `remember` / `forget` as tools the model
   can call, as an alternative to always-on injection. Depends on item 1.
6. **Anthropic-native inbound API** (`/v1/messages`), so Anthropic-SDK clients can point at a
   gateway without translation.
7. **OIDC / SAML SSO** for the control plane; the auth abstraction is already in place.
8. **Prompt and retrieval experiments.** Versioned gateway configurations with side-by-side
   comparison, building on the A/B routing already present.
9. **Circuit breaking and upstream health checks** layered onto failover.
10. **Caching.** Semantic response cache and prompt-prefix caching to cut cost and latency.

---

## 17. Open questions

1. **Embedding model choice.** Which provider/model is the platform default, and is the embedding
   call made to a third party (data-residency implications for connector content)?
2. **Distillation model default.** Which cheap model is the platform default, and does its cost
   fall on the operator or the organization?
3. **Global-model key economics.** When an org uses a global catalog model on the platform's key,
   how is that consumption bounded? Rate limits (§11) are the v1 answer, but the commercial
   policy is unresolved.
4. **Retrieval query construction.** Is the last user message sufficient, or does v1 need query
   rewriting (an extra LLM call) for conversational follow-ups like "and what about the second
   one?" — a known weakness of naive RAG.
5. **Data-processing terms.** Storing prompt bodies and distilled personal facts is likely to
   require a DPA and a documented retention/erasure position before the first real customer.

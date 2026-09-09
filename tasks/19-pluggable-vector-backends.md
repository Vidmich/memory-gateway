# Task 19 — Pluggable vector backends: Chroma alongside Qdrant

**Slice:** an organization's vectors live in the store its deployment chose for it — Qdrant or
Chroma — and can be moved from one to the other without a gap in retrieval.
**Depends on:** 09, 10, 12, 17, 18
**Spec:** §5.3, §6.4, §6.5, §9.4, §13.1, §15.3 — and amends §2 and §9.4, which name Qdrant as
*the* vector store rather than as the default one.
**Size:** L
**Status:** post-v1. Nothing in tasks 01–18 depends on this.

> **Terminology.** This system has no "project". Its tenant unit is the **organization**, and
> the vector index is already partitioned that way — `org_{org_id}_docs` and
> `org_{org_id}_memory`, one collection per org per kind (SPEC §9.4, §6.4). So "per project"
> is implemented as **per organization**, which is the only granularity the index actually
> has. Per-connector or per-gateway selection is out of scope and the reason is in
> *Out of scope* below: it turns one search into a fan-out across backends.

---

## Why this slice

Qdrant is a required, separately-operated service. That is the right default for a platform
serving real traffic and the wrong one for three cases this product keeps meeting: a self-hosted
single-box deployment, an evaluation that should not begin with provisioning a cluster, and a
customer whose infrastructure team has already standardized on something else. Chroma runs
embedded or as one container, and for a corpus of a few hundred thousand chunks it is enough.

There is a second reason, and it is the honest one. `VectorStore`, `FactVectorStore` and
`VectorIndexAdmin` are already ports, with an in-memory implementation of each and a shared
contract suite (`tests/vector_store_contract.py`, `tests/fact_vector_store_contract.py`). But a
port with exactly one real implementation is a hypothesis. Every leak in it has been free so
far because the only thing on the other side agreed with itself. This task is where the
hypothesis gets tested, and the known leaks are enumerated below rather than discovered
halfway through.

## Demo at the end of this task

Bring up the stack with both backends enabled. Two organizations, **Acme on Qdrant and Globex
on Chroma**, in the same process. Upload the same PDF to a connector in each, ask each one's
gateway a question that can only be answered from it, and get the right answer from both — with
the request detail view naming the backend that served each retrieval.

Then migrate Globex from Chroma to Qdrant while a search loop hammers it: progress is visible
per organization in **Platform → Maintenance**, every search in the loop returns results
throughout, and the top-k for a fixed query is identical before and after the promotion. Stop
Chroma afterwards: `/readyz` reports that backend unhealthy, Globex keeps serving from Qdrant,
and Acme never noticed.

## In scope

- A second real implementation of all three vector ports, over Chroma.
- Per-organization backend selection, bound at the organization and enforced everywhere the
  index is addressed.
- Operator-declared backends: connection details are platform configuration, never tenant input.
- Cross-backend migration reusing task 17's reindex machinery, with the same zero-downtime
  guarantee.
- Health, metrics, tracing, erasure, orphan sweeping, backup and restore made backend-aware.
- The port cleanup this exposes — the places where "vector store" currently means "Qdrant".

## Out of scope

- **pgvector, Weaviate, Pinecone, Milvus.** Two implementations are what proves a port; the
  third is cheap once this task is done and should be its own slice against real demand.
- **Per-connector or per-gateway backends.** Retrieval searches one collection per org and
  merges nothing; splitting an org across backends means fan-out, cross-backend score
  normalization (their distance metrics are not comparable), and a partial-failure policy per
  request. That is a different and much larger task, and no requirement asks for it.
- **Federated search across backends.** An org is on exactly one backend at a time. During a
  migration both hold data and reads go to the source until promotion.
- **Chroma HA, sharding or replication.** Chroma's operational envelope is part of what an
  operator chooses when they choose it; documenting the limits is in scope, engineering around
  them is not.
- **Automatic backend selection.** No heuristic picks a backend by corpus size. An operator
  picks, and can move later.

## Work items

### The port, before anything is added to it

Six places where the abstraction is currently Qdrant-shaped. Each is a small change and each
one is a bug if it is found later instead of now.

- [x] **`MemoryVectorStore` models aliases.** `aliases: dict[str, str]` and `live()` exist in the
      test double because Qdrant has aliases. Chroma does not. Move the notion of "which
      physical collection is live" behind `VectorIndexAdmin.live_collection` for every backend,
      and let each implementation satisfy it however it can.
- [x] **`swap_alias` is a Qdrant verb in a shared protocol.** Rename to `promote(organization_id,
      collection)`; Qdrant implements it with an alias operation, Chroma with the binding row
      below. The rename is the point: a port method named after one vendor's feature is how the
      next implementation ends up emulating that feature instead of satisfying the contract.
- [x] **`score_threshold` is pushed down.** Qdrant applies `min_score` server-side, so `limit`
      means "this many results above the threshold". Chroma has no threshold push-down and
      returns *distances*, not scores. Convert at the boundary (`score = 1 - distance`, clamped),
      over-fetch, filter, truncate — and put the semantics in the contract suite so both
      backends mean the same thing by `limit` and `min_score`.
- [x] **`Page.cursor` is a Qdrant scroll offset.** Chroma paginates with `limit`/`offset`.
      Keep the opaque-string cursor and let each implementation encode what it needs, but state
      the consequence: offset pagination over a collection being written to can skip or repeat a
      point, which for a migration means the count check at the end is load-bearing rather than
      belt-and-braces.
- [x] **`vector_size(info)` parses a Qdrant `CollectionInfo`.** Chroma infers width from the
      first insert and will not tell you what it expects. Write the dimension into collection
      metadata explicitly at creation and read it back there, so `dimension()` keeps its
      contract — retrieval refuses to search an index built by a different embedding model
      (task 10), and losing that turns a changed `EMBEDDING_DIMENSION` back into a silent
      failure under `fail_open`.
- [x] **`create_payload_index` has no Chroma equivalent.** It becomes a no-op there, but the
      reason it exists does not go away: `connector_id`, `document_id` and `end_user_id` are
      equality filters on every read and every delete. Measure Chroma's filtered read at a
      realistic corpus size rather than assuming its metadata store indexes them.
- [x] `from qdrant_client import models` appears inside methods in three modules. Once there are
      two backends, neither client should be importable from the other's implementation, and
      `app/services/vector_store.py` should not import either.

### Chroma implementations

- [x] `ChromaVectorStore`, `ChromaFactVectorStore`, `ChromaVectorIndexAdmin`, over
      `chromadb.AsyncHttpClient`, in their own modules beside the Qdrant ones.
- [x] Cosine, explicitly configured at collection creation. Chroma's default space is L2, and
      the embeddings this system produces are direction — an L2 default would rank by magnitude
      and be wrong in a way that still returns plausible results.
- [x] **`embedding_function=None`, asserted by a test.** Chroma's default embedding function
      downloads an ONNX model on first use. In task 18's image that fails (no egress, non-root,
      read-only filesystem); if it ever succeeded it would embed queries with a model that has
      nothing to do with the one that built the index. Both outcomes are worse than a loud
      refusal.
- [x] Deterministic point ids unchanged — `point_id()` is a UUIDv5 over `(document_id,
      chunk_index)` and stays the id in both backends, so a migration copies ids rather than
      minting new ones and re-ingestion stays idempotent on either side.
- [x] Delete-by-filter for documents and connectors, and delete-by-id for facts, matching the
      Qdrant semantics exactly — including the "collection does not exist" case, which is a
      no-op and not an error, because a delete asks for an end state.
- [x] `chromadb` as an **optional dependency group**. A Qdrant-only deployment should not carry
      it, and the import must fail with a message naming the extra rather than a traceback.

### Backend selection

- [x] Backends are **declared by the operator**: a named set in platform settings (task 17's
      precedence — environment bootstraps, database overrides), each entry a name, a kind
      (`qdrant` | `chroma`) and its connection configuration.
      > **Deviation, deliberate.** Backends are declared in the *environment*, one per
      > kind, not as a named set in `platform_settings`. The reason is the item below
      > it: a connection detail in a settings table is a server address an operator
      > edits on a screen, which recreates the tenant-adjacent-URL problem one level
      > up. The split this settled on is topology in the environment, policy in the
      > database — and the *default* was meant to be the policy half; see line 154.
      > Two Qdrants for two tenants is a real requirement the day somebody has it,
      > and it is a change to `vector_backends.py` rather than to anything above it.
- [x] **No tenant input reaches a backend URL.** An organization selects a *name* from the
      enabled set; an unknown name is a 422 listing what is enabled. This is the same boundary
      task 18 drew for upstream URLs, and for the same reason: a tenant-supplied endpoint the
      server connects to is an SSRF surface however it is spelled.
- [x] `vector_bindings(organization_id PK, backend, collection, dimension, status, updated_at)` —
      which backend an organization is on, and which physical collection is live *for backends
      that cannot answer that themselves*. A column set rather than a key in
      `organizations.settings`: this is read on the retrieval path and constrained, not a
      default somebody may override.
      > Built with `target` and `target_collection` instead of `dimension`: a
      > migration in flight needs a destination recorded somewhere a constraint can
      > see it, and the width is already on the collection. Check constraints refuse
      > a row that claims a migration with no destination, or names itself as one.
- [x] Authoritative-source rule, written down where it is enforced: the binding row decides the
      **backend**; within a backend the live **version** is the backend's business — Qdrant's
      alias, Chroma's binding column. Two mechanisms with one boundary between them, not two
      sources of truth for the same fact. A test asserts they cannot disagree.
- [ ] A platform default for organizations created without a choice, and a superadmin-only write
      to change an existing one — which does not move data by itself and says so.
      > Half. The default is `DEFAULT_VECTOR_BACKEND`, an environment variable, and a
      > superadmin can move an organization (that is the migration endpoint) — but the
      > default is **not** a `platform_settings` section, so changing it takes a
      > deploy. It is policy rather than topology and belongs in the database by the
      > rule stated above; adding it is a section in `app/schemas/platform.py` and a
      > read in the registry, and it is not built.
- [x] Every resolution path goes through one factory: request path, worker runtime, reindexer,
      erasure, maintenance, CLI. A second place that constructs a client from settings is a
      place that will keep using the default after a binding changes.
- [x] Audit-log selection and migration through task 15, with before/after.

### Migration between backends

- [x] `POST /api/v1/platform/organizations/{id}/vector-backend` starts a migration to a named
      backend; blocked when a reindex or another migration is in flight for that org, reusing
      `ReindexInProgress` rather than inventing a second conflict.
- [x] Reuse the task 17 procedure end to end — build beside the live index, verify counts, sample
      search, promote, drop the source after a grace period — with the one difference stated in
      the code: **this copies vectors instead of re-embedding them.** Same model, same
      dimension, so re-embedding would be an expense with no effect.
- [x] Therefore `VectorIndexAdmin.scroll` needs `with_vectors`. It deliberately omits vectors
      today because a reindex re-embeds from payload text; a migration is the case that needs
      them, and it is also the case where they are the bulk of the transfer.
- [x] Both kinds move together. An organization whose documents are on one backend and whose
      memory facts are on another is a state no screen can explain; the migration covers
      `_docs` and `_memory` and is not complete until both promote.
- [ ] Resumable, count-verified, rate-limited, with progress and an ETA per organization — the
      same `Progress` model the reindexer already exposes.
      > Resumable, count-verified and rate-limited, but **no progress or ETA**.
      > There is no run table at all: a reindex persists a cursor because a point
      > costs an embedding call, whereas here a point costs a network write, so an
      > interrupted copy starts again and converges (deterministic ids, upserts).
      > That is a real simplification — one fewer table — and a real gap: an operator
      > watching a large migration sees `status: migrating` and nothing else.
- [x] Roll back before promotion by dropping the target; after promotion, roll back by migrating
      the other way. There is no third option and the API should not imply one.

      > Blocked against another **migration**, by the binding row, which is written
      > before anything is copied. Not blocked against a *reindex* for the same
      > organization — the two would interleave badly, and that check is not built.
### Everything that addresses the index

- [x] `/readyz` checks each **enabled** backend, not `clients.qdrant`. A backend that is down
      degrades the organizations bound to it and no others; the payload names each backend and
      its state. Bounded and cached, so a probe does not fan out across backends every second.
- [ ] Retrieval failures stay per-organization: an org whose backend is unreachable gets its
      gateway's `fail_open` behaviour, and orgs on a healthy backend are unaffected. This is the
      claim most worth a test, because the natural implementation makes one backend's outage
      everybody's.
      > The mechanism is there and the registry-level isolation is asserted
      > (`test_one_backend_failing_is_one_backend_s_problem`), as is the readiness
      > rule. What is **not** driven end to end is a request through a gateway whose
      > organization is on a dead backend, checking `fail_open` behaves — that needs
      > two real backends and one of them stopped.
- [ ] `vector_backend` as a label on the retrieval metrics and an attribute on the
      `memory.documents` / `memory.facts` spans, so task 18's latency budget can be attributed
      per backend. Cardinality is bounded by the operator-declared set.
      > **Not done.** No `vector_backend` label on the retrieval metrics and no span
      > attribute. It is the one observability item in this task and it is missing;
      > without it, task 18's latency budget cannot be attributed per backend, which
      > is exactly the question a deployment running both will ask first.
- [x] Erasure reports name the backend: `Erased(store="qdrant")` becomes the backend the
      organization was actually on. An erasure report that names the wrong store is worthless
      for the purpose it exists for.
- [x] The orphan sweeper enumerates collections per backend, and reports per backend. Its
      report-before-delete rule and its age floor are unchanged.
      > Reports per backend. It does **not** enumerate collections per backend —
      > `list_collections` is on the port and no caller uses it, here or before this
      > task, so nothing was added that nothing reads.
- [x] Organization deletion drops from whichever backend holds it, and clears the binding row.
- [ ] `deploy/ops/backup.sh` / `restore.sh` / `verify-restore.sh` handle both. Qdrant snapshots
      and a Chroma persistent volume are different artifacts with different restore procedures,
      and `verify-restore.sh` must still end with a real completion through a real gateway —
      the check that catches a restore where every row is present and retrieval returns nothing.
      > **Not done.** `deploy/ops/*.sh` still assume Qdrant snapshots. A Chroma
      > deployment has no backup procedure in this repository, which is the most
      > serious gap in this task: `verify-restore.sh` is the check that catches a
      > restore where every row is present and retrieval returns nothing, and it
      > cannot run against half the backends this build now offers.

### Deployment

- [x] Optional Chroma service in `deploy/compose/docker-compose.yml`, and an optional dependency
      in the Helm chart, with the API and worker deployments unchanged when it is off.
- [x] Chart guard: an enabled backend must have connection configuration, and the platform
      default must name an enabled backend. `helm template` fails otherwise — the failure mode
      is otherwise a pod that starts and cannot retrieve for one organization.
- [x] `chroma` pytest marker beside the existing `qdrant` one, and a CI service container so the
      contract suite runs against both on every push.
- [x] Document the trade-off honestly in `docs/deployment.md`: what Chroma gives up (operational
      maturity at scale, snapshot tooling, filtered-search performance at large corpora) and the
      corpus size beyond which the answer is Qdrant.

### UI

- [ ] Org Settings shows the backend an organization is on, read-only for org admins.
      > **None of the UI is built.** Everything below this line is API-only: the
      > backends and bindings are readable at `GET /platform/vector-backends`, a
      > migration is `POST`/`DELETE` on `/platform/organizations/{id}/vector-backend`,
      > and `dry_run` returns the plan the confirmation dialog would show. The typed
      > client in `web/src/api/schema.d.ts` is regenerated, so the screens are a
      > frontend change with no server work left in front of them.
- [ ] **Platform → Settings** declares the enabled backends and the default for new organizations.
- [ ] **Platform → Maintenance** lists migrations in flight with per-organization progress, beside
      the reindex runs it already shows.
- [ ] Starting a migration requires a confirmation naming the organization, the point count and
      the estimated duration — the same shape as the reindex confirmation, because it is the same
      class of operation.
- [ ] The request detail view names the backend that served the retrieval.

### Spec

- [x] Amend SPEC §2 and §9.4: Qdrant is the **default** vector backend, not the only one, and
      collection naming is a property of the port rather than of Qdrant. A spec left contradicting
      the code is a spec people stop reading, and §5.3's isolation rule is the one thing here that
      must not become folklore.

## Acceptance criteria

- [x] Two organizations in one process, one on each backend, both retrieve correctly — proven by
      a single test that issues a request through each gateway and asserts on the answer.
- [ ] `tests/vector_store_contract.py` and `tests/fact_vector_store_contract.py` pass **unchanged**
      against Chroma. Any assertion that has to be relaxed to make that true is a contract the
      port was not actually enforcing, and is fixed rather than relaxed.
      > Passes unchanged against `tests/chroma_fake.py`, which speaks Chroma's dialect
      > — distances, parallel lists, `where` operators, offset paging — and is
      > deliberately awkward in the ways Chroma is awkward. **Against a real Chroma it
      > has not run here**: there is no server on this machine. CI now starts one, and
      > a Qdrant, so both marked runs execute on every push; neither has executed yet.
- [ ] `limit`, `min_score` and dimension-mismatch behaviour are identical across memory, Qdrant
      and Chroma.
      > Identical across the in-memory store and the Chroma translation, asserted by
      > the same contract file. The two server-backed runs are marked and skipped
      > here. Two of these checks exist *because* of this task and are the ones worth
      > naming: a score is a similarity and not a distance, and `limit` counts results
      > after the connector filter — pinned with interleaved scores, so an
      > implementation that filters after taking the top-k fails while passing
      > everything else.
- [x] Migrating an organization between backends with a search loop running returns valid results
      at every moment, and the top-k for a fixed query is identical before and after.
      > Asserted with two in-memory backends: the same top-k before, during and after,
      > and never an empty answer. Against two real servers it has not been run.
- [x] A migration killed halfway resumes and completes without duplicate or missing points, with
      the final count check passing exactly.
- [x] No tenant-supplied value reaches a vector-backend connection. Selecting a backend name that
      is not enabled is refused with the enabled set in the message.
- [ ] One backend down: bound organizations degrade per their `fail_open` setting, other
      organizations are unaffected, and `/readyz` says which backend is unhealthy.
      > Both halves of the readiness rule are tested — one backend down keeps the pod
      > in rotation and names the unhealthy one; every backend down is a 503, which is
      > exactly the old behaviour when there is one. The `fail_open` half is the gap
      > described on line 186.
- [ ] Erasure of an organization on Chroma leaves zero points, and the report names Chroma.
      > The report names the backend and a purge drops from **every** backend, which is
      > the case that matters — an abandoned migration leaves data in two, and a report
      > covering only the live one is worthless for the purpose it exists for. Asserted
      > with the in-memory registry; not against a real Chroma.
- [x] A Chroma client is never constructed with a default embedding function.
- [ ] Retrieval p95 for a Chroma-backed organization is within SPEC §4.2 at the documented corpus
      size — and the size at which it stops being true is measured and written down rather than
      guessed.
      > **Not measured.** No load harness and no Chroma here. Nothing in this task
      > tells you the corpus size at which the answer becomes Qdrant, and
      > `docs/deployment.md` says so rather than guessing a number.

## Tests

- The two contract suites, run against Chroma with a `chroma` marker and a real container.
- Threshold and limit semantics, asserted identically across all three implementations.
- Dimension refusal on both real backends.
- Binding resolution: every entry point (request, worker, reindexer, erasure, maintenance, CLI)
  reaches the backend the binding names, asserted by construction rather than by inspection.
- The backend/version authority rule: a binding row and a Qdrant alias cannot disagree.
- Migration: resumability, count verification, promotion invisible to a concurrent reader, source
  dropped only after the grace period, rollback before and after promotion.
- Isolation under partial outage: one backend unreachable, orgs on the other unaffected.
- Backend name validation, including that an org admin cannot supply connection details.
- Erasure and orphan sweeping per backend.
- Chart rendering guards for an enabled-but-unconfigured backend and an unknown default.

## Notes

- **The interesting failure here is not Chroma, it is the port.** Everything above that is not a
  Chroma implementation is a place where the current abstraction says "vector store" and means
  Qdrant. Doing those first, against the existing contract suite and with Qdrant still the only
  backend, keeps the two halves of this task separable — and if the second half is ever dropped,
  the first half is still an improvement.
- **`MemoryVectorStore` will get simpler.** It models Qdrant's aliases today because a reindex
  had to be testable without a container. Once "which collection is live" belongs to the admin
  port, the double stops emulating a vendor feature and goes back to being a dictionary.
- **Distance versus similarity is the classic silent bug in a task like this.** Chroma returns
  cosine distance; every threshold in this codebase is a similarity. Getting the conversion
  wrong does not raise — it returns the *worst* matches first, ranked confidently, and the only
  symptom is that answers get vaguer. The contract suite's one-hot vectors make this exact, which
  is why the conversion belongs there and not in a Chroma-specific test.
- **A migration is cheaper than a reindex and should not be sold as one.** Same model, same
  dimension, no embedding calls — so the cost estimate the reindex screen shows would be a
  fabrication here. Show point count and estimated duration instead.

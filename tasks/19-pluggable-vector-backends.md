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

- [ ] **`MemoryVectorStore` models aliases.** `aliases: dict[str, str]` and `live()` exist in the
      test double because Qdrant has aliases. Chroma does not. Move the notion of "which
      physical collection is live" behind `VectorIndexAdmin.live_collection` for every backend,
      and let each implementation satisfy it however it can.
- [ ] **`swap_alias` is a Qdrant verb in a shared protocol.** Rename to `promote(organization_id,
      collection)`; Qdrant implements it with an alias operation, Chroma with the binding row
      below. The rename is the point: a port method named after one vendor's feature is how the
      next implementation ends up emulating that feature instead of satisfying the contract.
- [ ] **`score_threshold` is pushed down.** Qdrant applies `min_score` server-side, so `limit`
      means "this many results above the threshold". Chroma has no threshold push-down and
      returns *distances*, not scores. Convert at the boundary (`score = 1 - distance`, clamped),
      over-fetch, filter, truncate — and put the semantics in the contract suite so both
      backends mean the same thing by `limit` and `min_score`.
- [ ] **`Page.cursor` is a Qdrant scroll offset.** Chroma paginates with `limit`/`offset`.
      Keep the opaque-string cursor and let each implementation encode what it needs, but state
      the consequence: offset pagination over a collection being written to can skip or repeat a
      point, which for a migration means the count check at the end is load-bearing rather than
      belt-and-braces.
- [ ] **`vector_size(info)` parses a Qdrant `CollectionInfo`.** Chroma infers width from the
      first insert and will not tell you what it expects. Write the dimension into collection
      metadata explicitly at creation and read it back there, so `dimension()` keeps its
      contract — retrieval refuses to search an index built by a different embedding model
      (task 10), and losing that turns a changed `EMBEDDING_DIMENSION` back into a silent
      failure under `fail_open`.
- [ ] **`create_payload_index` has no Chroma equivalent.** It becomes a no-op there, but the
      reason it exists does not go away: `connector_id`, `document_id` and `end_user_id` are
      equality filters on every read and every delete. Measure Chroma's filtered read at a
      realistic corpus size rather than assuming its metadata store indexes them.
- [ ] `from qdrant_client import models` appears inside methods in three modules. Once there are
      two backends, neither client should be importable from the other's implementation, and
      `app/services/vector_store.py` should not import either.

### Chroma implementations

- [ ] `ChromaVectorStore`, `ChromaFactVectorStore`, `ChromaVectorIndexAdmin`, over
      `chromadb.AsyncHttpClient`, in their own modules beside the Qdrant ones.
- [ ] Cosine, explicitly configured at collection creation. Chroma's default space is L2, and
      the embeddings this system produces are direction — an L2 default would rank by magnitude
      and be wrong in a way that still returns plausible results.
- [ ] **`embedding_function=None`, asserted by a test.** Chroma's default embedding function
      downloads an ONNX model on first use. In task 18's image that fails (no egress, non-root,
      read-only filesystem); if it ever succeeded it would embed queries with a model that has
      nothing to do with the one that built the index. Both outcomes are worse than a loud
      refusal.
- [ ] Deterministic point ids unchanged — `point_id()` is a UUIDv5 over `(document_id,
      chunk_index)` and stays the id in both backends, so a migration copies ids rather than
      minting new ones and re-ingestion stays idempotent on either side.
- [ ] Delete-by-filter for documents and connectors, and delete-by-id for facts, matching the
      Qdrant semantics exactly — including the "collection does not exist" case, which is a
      no-op and not an error, because a delete asks for an end state.
- [ ] `chromadb` as an **optional dependency group**. A Qdrant-only deployment should not carry
      it, and the import must fail with a message naming the extra rather than a traceback.

### Backend selection

- [ ] Backends are **declared by the operator**: a named set in platform settings (task 17's
      precedence — environment bootstraps, database overrides), each entry a name, a kind
      (`qdrant` | `chroma`) and its connection configuration.
- [ ] **No tenant input reaches a backend URL.** An organization selects a *name* from the
      enabled set; an unknown name is a 422 listing what is enabled. This is the same boundary
      task 18 drew for upstream URLs, and for the same reason: a tenant-supplied endpoint the
      server connects to is an SSRF surface however it is spelled.
- [ ] `vector_bindings(organization_id PK, backend, collection, dimension, status, updated_at)` —
      which backend an organization is on, and which physical collection is live *for backends
      that cannot answer that themselves*. A column set rather than a key in
      `organizations.settings`: this is read on the retrieval path and constrained, not a
      default somebody may override.
- [ ] Authoritative-source rule, written down where it is enforced: the binding row decides the
      **backend**; within a backend the live **version** is the backend's business — Qdrant's
      alias, Chroma's binding column. Two mechanisms with one boundary between them, not two
      sources of truth for the same fact. A test asserts they cannot disagree.
- [ ] A platform default for organizations created without a choice, and a superadmin-only write
      to change an existing one — which does not move data by itself and says so.
- [ ] Every resolution path goes through one factory: request path, worker runtime, reindexer,
      erasure, maintenance, CLI. A second place that constructs a client from settings is a
      place that will keep using the default after a binding changes.
- [ ] Audit-log selection and migration through task 15, with before/after.

### Migration between backends

- [ ] `POST /api/v1/platform/organizations/{id}/vector-backend` starts a migration to a named
      backend; blocked when a reindex or another migration is in flight for that org, reusing
      `ReindexInProgress` rather than inventing a second conflict.
- [ ] Reuse the task 17 procedure end to end — build beside the live index, verify counts, sample
      search, promote, drop the source after a grace period — with the one difference stated in
      the code: **this copies vectors instead of re-embedding them.** Same model, same
      dimension, so re-embedding would be an expense with no effect.
- [ ] Therefore `VectorIndexAdmin.scroll` needs `with_vectors`. It deliberately omits vectors
      today because a reindex re-embeds from payload text; a migration is the case that needs
      them, and it is also the case where they are the bulk of the transfer.
- [ ] Both kinds move together. An organization whose documents are on one backend and whose
      memory facts are on another is a state no screen can explain; the migration covers
      `_docs` and `_memory` and is not complete until both promote.
- [ ] Resumable, count-verified, rate-limited, with progress and an ETA per organization — the
      same `Progress` model the reindexer already exposes.
- [ ] Roll back before promotion by dropping the target; after promotion, roll back by migrating
      the other way. There is no third option and the API should not imply one.

### Everything that addresses the index

- [ ] `/readyz` checks each **enabled** backend, not `clients.qdrant`. A backend that is down
      degrades the organizations bound to it and no others; the payload names each backend and
      its state. Bounded and cached, so a probe does not fan out across backends every second.
- [ ] Retrieval failures stay per-organization: an org whose backend is unreachable gets its
      gateway's `fail_open` behaviour, and orgs on a healthy backend are unaffected. This is the
      claim most worth a test, because the natural implementation makes one backend's outage
      everybody's.
- [ ] `vector_backend` as a label on the retrieval metrics and an attribute on the
      `memory.documents` / `memory.facts` spans, so task 18's latency budget can be attributed
      per backend. Cardinality is bounded by the operator-declared set.
- [ ] Erasure reports name the backend: `Erased(store="qdrant")` becomes the backend the
      organization was actually on. An erasure report that names the wrong store is worthless
      for the purpose it exists for.
- [ ] The orphan sweeper enumerates collections per backend, and reports per backend. Its
      report-before-delete rule and its age floor are unchanged.
- [ ] Organization deletion drops from whichever backend holds it, and clears the binding row.
- [ ] `deploy/ops/backup.sh` / `restore.sh` / `verify-restore.sh` handle both. Qdrant snapshots
      and a Chroma persistent volume are different artifacts with different restore procedures,
      and `verify-restore.sh` must still end with a real completion through a real gateway —
      the check that catches a restore where every row is present and retrieval returns nothing.

### Deployment

- [ ] Optional Chroma service in `deploy/compose/docker-compose.yml`, and an optional dependency
      in the Helm chart, with the API and worker deployments unchanged when it is off.
- [ ] Chart guard: an enabled backend must have connection configuration, and the platform
      default must name an enabled backend. `helm template` fails otherwise — the failure mode
      is otherwise a pod that starts and cannot retrieve for one organization.
- [ ] `chroma` pytest marker beside the existing `qdrant` one, and a CI service container so the
      contract suite runs against both on every push.
- [ ] Document the trade-off honestly in `docs/deployment.md`: what Chroma gives up (operational
      maturity at scale, snapshot tooling, filtered-search performance at large corpora) and the
      corpus size beyond which the answer is Qdrant.

### UI

- [ ] Org Settings shows the backend an organization is on, read-only for org admins.
- [ ] **Platform → Settings** declares the enabled backends and the default for new organizations.
- [ ] **Platform → Maintenance** lists migrations in flight with per-organization progress, beside
      the reindex runs it already shows.
- [ ] Starting a migration requires a confirmation naming the organization, the point count and
      the estimated duration — the same shape as the reindex confirmation, because it is the same
      class of operation.
- [ ] The request detail view names the backend that served the retrieval.

### Spec

- [ ] Amend SPEC §2 and §9.4: Qdrant is the **default** vector backend, not the only one, and
      collection naming is a property of the port rather than of Qdrant. A spec left contradicting
      the code is a spec people stop reading, and §5.3's isolation rule is the one thing here that
      must not become folklore.

## Acceptance criteria

- [ ] Two organizations in one process, one on each backend, both retrieve correctly — proven by
      a single test that issues a request through each gateway and asserts on the answer.
- [ ] `tests/vector_store_contract.py` and `tests/fact_vector_store_contract.py` pass **unchanged**
      against Chroma. Any assertion that has to be relaxed to make that true is a contract the
      port was not actually enforcing, and is fixed rather than relaxed.
- [ ] `limit`, `min_score` and dimension-mismatch behaviour are identical across memory, Qdrant
      and Chroma.
- [ ] Migrating an organization between backends with a search loop running returns valid results
      at every moment, and the top-k for a fixed query is identical before and after.
- [ ] A migration killed halfway resumes and completes without duplicate or missing points, with
      the final count check passing exactly.
- [ ] No tenant-supplied value reaches a vector-backend connection. Selecting a backend name that
      is not enabled is refused with the enabled set in the message.
- [ ] One backend down: bound organizations degrade per their `fail_open` setting, other
      organizations are unaffected, and `/readyz` says which backend is unhealthy.
- [ ] Erasure of an organization on Chroma leaves zero points, and the report names Chroma.
- [ ] A Chroma client is never constructed with a default embedding function.
- [ ] Retrieval p95 for a Chroma-backed organization is within SPEC §4.2 at the documented corpus
      size — and the size at which it stops being true is measured and written down rather than
      guessed.

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

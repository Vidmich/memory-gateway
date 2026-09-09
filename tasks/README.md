# Task Plan

Implementation plan for [SPEC.md](../SPEC.md), sliced **vertically**: every task ends with
something you can run, click, or curl. No task is "build the data layer" or "build all the
models" — each one cuts through storage, API, and UI far enough to be demonstrated and reviewed.

## How to read a task file

Each file has the same shape:

- **Slice** — the user-visible capability this adds.
- **Demo at the end of this task** — the literal steps to prove it works. If you can't run
  these, the task isn't done.
- **In scope / Out of scope** — what to build now and what to deliberately leave to a later task.
- **Work items** — the checklist.
- **Acceptance criteria** — what a reviewer verifies.
- **Tests** — what must be covered.

Size guide: **S** ≈ 1–2 days · **M** ≈ 3–5 days · **L** ≈ 1–2 weeks (single developer).

## Task list

| # | Task | Size | What becomes demonstrable |
|---|---|---|---|
| [01](01-foundation-walking-skeleton.md) | Project foundation & walking skeleton | M | `docker compose up` → healthy service |
| [02](02-passthrough-proxy.md) | Pass-through proxy | L | **The OpenAI SDK talks to your gateway and gets a real completion** |
| [03](03-control-plane-auth-ui-shell.md) | Control-plane auth & UI shell | M | Log into a web UI |
| [04](04-organizations-users-tenancy.md) | Organizations, users, roles & tenancy | L | Multiple isolated customers |
| [05](05-upstream-models.md) | Upstream models (API + UI) | M | Configure models in the UI, test connectivity |
| [06](06-gateways-and-api-keys.md) | Gateways & API keys (API + UI) | L | **Self-serve: create an endpoint in the UI and call it** |
| [07](07-request-logging-and-monitoring.md) | Request logging & monitoring page | L | Watch traffic and inspect any request |
| [08](08-routing-modes.md) | Routing modes: failover & A/B | M | Traffic splits and survives an upstream outage |
| [09](09-connectors-and-ingestion.md) | Connectors & file-drop ingestion | L | Upload files, watch them get indexed |
| [10](10-rag-retrieval-and-prompt-assembly.md) | RAG retrieval & prompt assembly | L | **The model answers from your uploaded documents** |
| [11](11-pdf-and-office-extraction.md) | PDF & Office extraction | M | Real-world documents work |
| [12](12-end-user-identity-and-memory-recall.md) | End-user identity & memory recall | M | The model knows things about a specific end user |
| [13](13-memory-distillation-worker.md) | Async distillation worker & memory browser | L | **Memory writes itself from conversations** |
| [14](14-rate-limiting-and-quotas.md) | Rate limiting & quotas | M | Noisy tenants get throttled |
| [15](15-audit-log.md) | Audit log | S | Every config change is attributable |
| [16](16-anthropic-dialect-adapter.md) | Anthropic dialect adapter | M | Claude models usable as upstreams |
| [17](17-retention-reindex-platform-settings.md) | Retention, reindex & platform settings | M | Data lifecycle is enforced, not just documented |
| [18](18-production-deployment-hardening.md) | Production deployment & hardening | L | Runs on Kubernetes within the latency budget |

### Post-v1

| # | Task | Size | What becomes demonstrable |
|---|---|---|---|
| [19](19-pluggable-vector-backends.md) | Pluggable vector backends: Chroma alongside Qdrant | L | Two organizations on two vector stores, in one deployment |
| [20](20-chunking-strategies.md) | Chunking strategies: semantic, sentence-window & code-aware | L | Compare strategies on your own documents, then pick one |

## Dependency graph

```
01 foundation
 └─ 02 passthrough proxy ◄──────────── first demoable server
     ├─ 03 auth + UI shell
     │   └─ 04 orgs / users / tenancy
     │       └─ 05 upstream models
     │           └─ 06 gateways + keys ◄── self-serve product
     │               ├─ 07 logging + monitoring
     │               │   ├─ 08 routing modes
     │               │   └─ 13 distillation ──┐
     │               ├─ 09 connectors + ingestion
     │               │   ├─ 10 RAG injection ◄── core promise works
     │               │   │   └─ 12 end-user memory recall ─┘
     │               │   └─ 11 PDF + Office
     │               ├─ 14 rate limits
     │               ├─ 15 audit log
     │               └─ 16 anthropic adapter
     └─ 17 retention / reindex ── 18 production deployment
         │                         └─ 19 pluggable vector backends (post-v1)
         └─ 20 chunking strategies (post-v1)
```

## Milestones

| Milestone | After task | Meaning |
|---|---|---|
| **M1 — It proxies** | 02 | An OpenAI client gets completions through the gateway. Prove the core plumbing before building anything around it. |
| **M2 — It's a product** | 06 | A customer logs in, configures an endpoint, and uses it without an engineer. |
| **M3 — It's observable** | 08 | Traffic, routing, and prompts are visible and controllable. |
| **M4 — It has memory** | 13 | Both memory kinds work: documents ground answers, and conversation memory writes itself. |
| **M5 — It ships** | 18 | Multi-tenant limits, audit, lifecycle, and a k8s deployment inside the latency budget. |

## Sequencing notes

- **Tasks 01–02 come before any UI work on purpose.** The riskiest part of this system is the
  streaming proxy path; prove it with curl and the OpenAI SDK before investing in screens.
- **Task 02 creates a minimal `organizations` table** even though tenancy lands in task 04.
  Foreign keys are cheap to add up front and painful to retrofit across every table.
- **Task 07 (logging) precedes task 08 (routing)** so routing decisions are visible in the
  monitoring UI the moment they exist, instead of only in server logs.
- **Task 12 ships with manual fact entry** so end-user memory is demonstrable before the
  distillation worker in task 13 exists. Each slice stands alone.
- **Tasks 14–16 are independent** of each other and of 09–13; they can be parallelized or
  reordered against business priority.
- **Tool-call passthrough is not in this plan** (SPEC §16.1). It is the first post-v1 task.
  Task 02 must reject `tools` with an explicit 400 so the gap fails loudly.
- **Task 19 is post-v1 and nothing depends on it.** It splits cleanly in two: the first half
  removes Qdrant's fingerprints from a port that is supposed to be vendor-neutral and is worth
  doing on its own; the second half adds Chroma behind it. It comes after 18 because
  per-backend readiness, metrics and backup all have to fit what 18 built, and after 17 because
  moving an organization between backends reuses the reindex machinery rather than repeating it.
- **Task 20 comes after 17 for one specific reason.** Semantic chunking makes the embedding
  model part of the *chunking* configuration, so a platform embedding change stops being a
  re-embed and becomes a recut for those connectors. That is a change to the reindexer, and it
  can only be written against a reindexer that exists. 19 and 20 are independent of each other.

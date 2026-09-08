# Qdrant is down or slow

Not an alert — the effect depends on each gateway's own policy, so there is no single
threshold worth paging on. What follows is what happens and what to do about it.

## What each gateway does

Retrieval failure is per gateway, set by `on_retrieval_error` in the Memory section:

* **`continue`** (fail open) — the request is served with no retrieved context. The answer
  is worse and nothing says so. `retrieval_attempts_total{outcome="error"}` moves.
* **`fail`** (fail closed) — the request gets a 503. Loud, and correct for a gateway whose
  answers are worthless without its documents.

Conversation memory recall fails independently of document retrieval, and the same policy
covers both — they are two branches of one `asyncio.gather` with two outcomes.

Ingestion is affected differently: a job that cannot write vectors fails and retries, so
documents stay `pending` and the queue grows. Nothing is lost.

## Check

```bash
curl -sS "$QDRANT_URL/healthz"
curl -sS "$QDRANT_URL/collections" | jq '.result.collections | length'
curl -sS "$QDRANT_URL/aliases" | jq '.result.aliases | length'
```

The alias count matters. Since task 17 every read addresses `org_{id}_docs`, which is an
alias for `org_{id}_docs_v{n}`. Collections present with no aliases means every search
returns nothing while everything looks healthy — the signature of a restore that stopped
one step early.

## Fix

**Qdrant is down.** Restore it. Nothing here needs restarting.

**Qdrant is slow.** Retrieval carries its own timeout (default 800 ms) and gives up rather
than blowing the latency budget, so the symptom is `outcome="timeout"` rather than a slow
request. Check whether a reindex is running — **Platform → Maintenance** shows one in
flight, and a rebuild running beside the live collections is real load on the same node.

**Data is gone.** Vectors are derivable: every document and every fact can be re-embedded.
Under time pressure, the choice is:

1. **Restore the snapshot** — see [backup-restore.md](backup-restore.md). Minutes, and
   remember the aliases.
2. **Rebuild** — `POST /api/v1/platform/reindex`. Hours and an embedding bill, but it
   needs nothing but Postgres, which is the system of record.

Take option 1 unless the snapshot is missing.

## If it keeps happening

Set `on_retrieval_error` deliberately per gateway rather than leaving the default. It is
the one place where "answer without the documents" and "refuse" are both defensible, and
the right answer is a property of the customer's use case, not of the platform.

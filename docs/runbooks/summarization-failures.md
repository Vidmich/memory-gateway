# `SummarizationFailureRateHigh` — documents are indexing without their summaries

**Symptom.** A quarter of document summarization attempts are failing. Under `summary_chunk`
no document fails: it indexes without a summary and its row says `summary: failed`. Under
`contextual` the documents themselves fail, with reason `summarization` and a message naming
the model — half a corpus embedded with context and half without would be two corpora that
rank differently, so the pipeline refuses to produce that.

**What it means.** The phase between extraction and chunking reads the document's head and
tail, calls a cheap model, and stores one paragraph. It fails at the model call (most
common), or because the model returned nothing usable.

## Check

```promql
sum by (outcome) (rate(summarization_runs_total[30m]))
sum by (model, direction) (rate(summarization_tokens_total[30m]))
```

Then **Monitoring → Summarization**, or `GET /api/v1/summarization/health`, which reports
documents summarized per day, tokens by model, failure rate, cap hits, the connectors
spending the most, and the documents parked on a cap — for one organization, over the
page's window. A connector's own slice is on its detail screen.

The document row carries the reason: `summary_status`, `summary_error`, and `summary_model`.

```bash
curl -sS "$GW/api/v1/connectors/$CONNECTOR/documents?limit=200" -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | "\(.status)\t\(.summary_status // "-")\t\(.summary_error // "")"' | sort | uniq -c
```

## Fix

**No model configured.** Not really a failure: a connector whose chain — its own
`model_id`, the organization's summarization default, the distillation model, the platform
default — resolves to nothing. The ledger rows say `skipped` with reason
`no_summarization_model`. Set one on the connector, under **Settings → Document summaries**,
or as the platform default.

**The model is refusing.** A 4xx from the provider is recorded as `failed` with reason
`provider_refused` and is not retried; the same document to the same model produces the same
answer. It is an ordinary upstream model row, so [upstream-outage.md](upstream-outage.md)
applies. Once it answers, **Summarize** on the document (or **Retry**, under `contextual`)
runs just the phase again.

**The model is unwell.** 429, 5xx and timeouts raise for the job's backoff and appear as
`failed` rows *and* retries; nothing needs doing beyond what the upstream runbook says.

**A daily cap reached.** That is a `skipped` outcome with reason `daily_cap_reached`, and
it is the connector's cap working. Under `contextual` the documents are *parked* — `pending`
with reason `summarization_cap`, retried just after midnight UTC — and the dashboard counts
them. Raise `daily_document_cap` on the connector if the wait is not acceptable.

## If it keeps happening

Summaries are a per-document model call and, under `contextual`, a per-document re-embed
when anything about them changes. If the bill is the problem rather than the failures,
`summary_chunk` costs one point per document and answers a question the source chunks never
can, and per-format overrides let a connector summarize the Markdown and not the lockfiles.

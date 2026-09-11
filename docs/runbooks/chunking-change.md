# Retrieval got worse after a chunking change, or a reindex is taking far longer than its estimate

Not an alert, and it never will be. Chunking is the one knob in this product where a wrong
value is invisible: nothing fails, no error rate moves, no document goes red. Retrieval
still returns something — it is just worse, and the report arrives weeks later as "the
answers used to be better".

## What actually changed

| | Effect |
|---|---|
| `chunk_size`, `overlap`, `respect_boundaries`, `strategy` | Every stored chunk for the affected formats is now wrong. |
| An entry added to or removed from `overrides` | Only that format's chunks are wrong. |
| The platform embedding model | Chunks are still correct **unless** a connector is on `semantic`, where the boundaries came out of the old model. |
| The embedding **tokenizer** (Platform → Settings → Embedding, derived or overridden) | Every stored chunk everywhere is now sized in a different unit. No reindex run starts; every document reads `stale` until its connector is reindexed. |
| Summarization `mode` to or from `contextual`/`both`, or the summarization model while on one of them (task 102) | Every stored **vector** for the affected formats was built with a different prefix. The chunks' text is unchanged; every document reads `stale` until reindexed, and the reindex reuses each document's stored summary rather than paying for it again. |
| Summarization `mode` to or from `summary_chunk` | Nothing. One point per document is added on its next ingestion, or removed; the source chunks are byte-identical. |

The connector's PATCH response says which: `reindex_required` and `reindex_formats`. The
document row says what each file was actually cut with — `chunk_strategy`,
`chunk_fingerprint` and `tokenizer` — and the listing marks `stale` any row whose fingerprint
no longer matches what ingestion would write now. That is how you tell a connector that is
half reindexed from one that is not.

```bash
curl -sS "$GW/api/v1/connectors/$CONNECTOR/documents?limit=200" -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | "\(.chunk_strategy // "unrecorded")\t\(.tokenizer // "unrecorded")\t\(if .stale then "STALE" else "ok" end)\t\(.source_name)"' | sort | uniq -c
```

Two strategies or two tokenizers in that output means the connector holds two chunkings at
once. That is a normal transient during a reindex and a problem if it persists.

**On the tokenizer specifically (task 101).** Before it, every chunk on every deployment was
measured with `cl100k_base`, whatever the embedding model. After it, `chunk_size` is measured
with the embedding model's own tokenizer, so on a non-OpenAI embedding model the first deploy
marks every document stale. That is correct and it is a reindex bill: the chunks were sized in
the wrong unit and the fingerprint is now saying so. Reindex connectors at a pace the
embedding provider tolerates rather than all at once, and read `tokenizer` on a row that
looks odd — `words (cl100k_base unavailable)` means a worker could not load its vocabulary
and cut by word count, which is a network problem on the worker and not a chunking decision.

**On summarization specifically (task 102).** Two things look like a chunking problem and
are not. A document that is `indexed` with `summary_status: failed` is a summarization
failure under `summary_chunk` — the source chunks are fine, and **Summarize** on the row
retries just that phase. A document that is `pending` with reason `summarization_cap` is not
stuck: the connector's daily cap is spent and the job is queued for just after midnight UTC.
The Monitoring page's summarization panel and the dashboard both count them; raise
`daily_document_cap` on the connector if the wait is not acceptable. A `failed` document
whose reason is `summarization` is the `contextual` rule — a document that could not be
summarized is not embedded half-right — and the message names the model; fix the model or
the cap and **Retry**.

## Check

**Is anything actually stale?** A `NULL` `chunk_strategy` is a document indexed before this
was recorded, not a fault. It fills in on the next ingestion of that file.

**Look at the chunks.** `GET /api/v1/documents/{id}/chunks` is the fastest way to see
whether the cut is sensible. Under `sentence_window` the inspector highlights the sentence
that was embedded inside its window — a chunk whose text does not contain the words you
searched for is the strategy working, not a bug.

**Compare before deciding.** `POST /api/v1/connectors/{id}/chunking/preview` runs candidate
configurations over one document without writing anything, and returns four numbers per
candidate. Two of them are the ones to read first:

* **Cut by the size limit.** High under `semantic` means the ceiling is doing the cutting
  rather than the strategy — the boundaries it found are further apart than `chunk_size`.
  Raise the ceiling or lower the breakpoint percentile; as it stands you are paying for
  semantic chunking and getting recursive chunking.
* **Boundaries mid-sentence.** High on a prose corpus means `respect_boundaries` is off or
  the strategy is `fixed`.

It embeds the document, so it costs what one ingestion costs. That is deliberate: it is the
same code path ingestion uses, and a cheaper approximation would be a comparison nobody
should believe.

## Fix

**Apply a chunking change.** Save, then reindex — and reindex *the formats that changed*,
which the UI offers by name:

```bash
curl -X POST "$GW/api/v1/connectors/$CONNECTOR/reindex" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"formats": ["code"]}'
```

Omit `formats` for everything. This is a re-*ingestion*, not the platform reindex: it reads
the files again, so it costs extraction as well as embedding.

**A document `failed` with reason `chunking_embedding`.** Only possible under `semantic`,
which embeds every sentence to find its boundaries. The message names the embedding
provider, because the splitter is not what broke. Fix the provider and press **Retry**; a
rate limit or a 5xx never lands here at all — those raise and the job's own backoff handles
them.

**A platform reindex is far slower than the estimated token count suggested.** Check the
estimate's recut line:

```bash
curl -X POST "$GW/api/v1/platform/reindex" \
  -H "Authorization: Bearer $SUPERADMIN" -H 'Content-Type: application/json' \
  -d '{"dry_run": true}' | jq
```

```json
{"points": 412000, "tokens": 9100000, "recut_connectors": 3, "recut_documents": 1840}
```

`recut_connectors` are the ones on `semantic`. They are not re-embedded from the index —
they are re-read from object storage, extracted again, and cut again with the new model, so
they cost extraction and roughly twice the embedding calls of an ordinary connector. The
token figure above deliberately does **not** include them: nothing knows how many chunks a
recut will produce, and a number invented for it is the number an operator would anchor on.

**The worker has no recutter.** A reindex that fails with *"connectors here chunk with the
embedding model and have to be recut"* is a process built without an ingestion pipeline.
That is the API process's shape, not the worker's; the run belongs on the worker. It fails
rather than falling back to a plain copy on purpose — a fallback would re-embed the old
model's boundaries, report success, and leave a collection nobody could tell was wrong.

## If it keeps happening

Somebody is changing chunking without comparing first. The **Compare** panel exists so the
choice is made with evidence, and the four numbers are checked into the fixture corpus so a
change to a strategy shows up as a diff rather than as a feeling.

If it is `semantic` specifically: it is the expensive option twice over — every ingestion
embeds every sentence, and every future embedding-model change recuts the connector instead
of re-embedding it. The screen says so at the moment of choosing. If a corpus does not
measurably improve under it, move back to `recursive`, which costs nothing per sentence and
whose failure mode is a boundary in a slightly wrong place rather than a bill.

## Related

* [A vector-backend migration is stuck](vector-backend-migration.md) — a different
  operation with a confusingly similar shape.
* [Qdrant is down or slow](qdrant-outage.md) — where a rebuild's cost is discussed from the
  other direction.

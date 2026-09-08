# `WorkerBacklogGrowing` — ingestion is falling behind

**Symptom.** `jobs_queue_depth` above 500 for fifteen minutes. Uploads still succeed;
documents sit at `pending` and do not become searchable. A customer who has just uploaded a
folder sees a table that is not moving.

**What it means.** Jobs are being enqueued faster than the workers finish them. The queue
is durable, so nothing is lost — this is a latency problem, not a data one.

## Check

Which queue, and whether anything is consuming it:

```bash
kubectl -n <ns> get pods -l app.kubernetes.io/component=worker
kubectl -n <ns> get pods -l app.kubernetes.io/component=heavy-worker
kubectl -n <ns> logs -l app.kubernetes.io/component=worker --tail=50
```

Two very different situations look the same on the depth chart:

* **no worker is running** — the depth climbs linearly and `jobs_completed_total` is flat;
* **workers are running and losing** — the depth climbs and completions are non-zero.

If the depth is entirely PDFs and Office documents, the heavy worker is the one that is
short. It is a separate deployment for exactly this reason, and `heavyWorker.enabled:
false` is a supported configuration in which those jobs wait indefinitely.

## Fix

**Scale the workers.** They are a separate Deployment from the API and scale on this
signal:

```bash
kubectl -n <ns> scale deploy/<release>-memory-gateway-worker --replicas=6
```

With `worker.autoscaling.enabled` this happens on its own, provided a metrics adapter is
publishing `jobs_queue_depth`. Without one, this alert *is* the autoscaler.

**A poison job.** If completions are non-zero but one job keeps restarting, look for
repeated `jobs_started_total` with no matching completion for the same name. A job that
exhausts its retries is dead-lettered and stops consuming a slot; one that times out at 900
seconds and retries does not.

**An upstream dependency.** Extraction calls no network, but embedding does. If
`extraction_duration_seconds` is normal and jobs are still slow, the embedding provider is
the constraint — check `EMBEDDING_MAX_CONCURRENCY` and the provider's rate limit.

## If it keeps happening

Size the worker fleet from the sustained document rate rather than the peak, and let the
HPA absorb the peaks. The queue is the buffer; a backlog that drains within an hour is the
system working.

# `RequestLogsDropped` — the request log is shedding

**Symptom.** `logs_dropped_total` is moving. Customers notice nothing; their requests are
being served normally. What is lost is the record of them.

**What it means.** The log queue sheds rather than blocking a request — deliberately, since
task 07: a database that cannot keep up must not become a data-plane outage. The `reason`
label says which kind of shedding:

* `queue_pressure` — the flusher cannot write as fast as requests arrive, so whole records
  are dropped;
* `redaction_budget` — the record is kept, the *bodies* are not, because they were too
  large. The row lands with `bodies_omitted` set and the UI says so.

## Check

```promql
sum by (reason) (rate(logs_dropped_total[5m]))
logs_queue_depth
```

A depth that is high and flat means the flusher is keeping up at capacity. A depth pinned
at its maximum means it is not.

For `queue_pressure`, the flusher is bounded by Postgres:

```sql
select wait_event_type, wait_event, count(*)
from pg_stat_activity where datname = 'gateway' group by 1, 2 order by 3 desc;
```

## Fix

**`queue_pressure` during a traffic spike.** Legitimate, and self-correcting. Nothing to do
if the depth is falling.

**`queue_pressure` sustained.** Either the database is the constraint (check for a missing
partition — see [partition-runway.md](partition-runway.md), because an insert failing on a
missing partition looks exactly like a slow one from here) or the write rate genuinely
exceeds one connection's throughput. Raising `DB_POOL_SIZE` helps only if the pool is the
constraint; check `pg_stat_activity` first.

**`redaction_budget`.** Working as intended for a gateway logging full bodies of very large
prompts. If the transcripts matter for that gateway, lower what is logged (Gateway →
Logging) rather than raising the budget: a 200 KB prompt stored on every request is a
retention problem two weeks later.

## If it keeps happening

Consider whether every gateway needs `log_request_body`. It is per gateway precisely so it
can be on for the ones being debugged and off for the ones carrying volume.

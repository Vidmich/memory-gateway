# `PartitionRunwayLow` — request logging is about to stop

**Symptom.** Fewer than seven days of partitions exist ahead of today. Nothing is broken
*yet*.

**What it means.** `request_logs` and `transcripts` are partitioned by day. An insert into a
day with no partition **fails** — it is not slow, it errors — so when the runway reaches
zero the log queue starts dropping every record and the monitoring screens go blank. The
nightly job creates thirty days ahead, so a falling runway means that job is not running.

This is the one alert in this directory where the correct response is to act tonight.

## Check

```promql
min by (table) (partition_runway_days)
```

Then whether the nightly pass ran. **Platform → Maintenance** shows the last run of each
job with its report. Or:

```sql
select job, status, started_at, finished_at, error
from maintenance_runs order by started_at desc limit 10;
```

## Fix

Immediately, from the UI: **Platform → Maintenance → Run partitions**. It is synchronous,
idempotent, and takes a second — it creates whatever is missing up to thirty days ahead and
reports what it made.

Or over the API:

```bash
curl -sS -X POST https://<host>/api/v1/platform/maintenance/partitions \
  -H "Authorization: Bearer <superadmin token>" | jq
```

Then find out why the schedule did not fire. The cron entry runs at 03:05 UTC on exactly
one worker, so:

* **no worker was running at 03:05** — arq's cron does not catch up on a missed occurrence;
* **the job ran and failed** — the `error` column on `maintenance_runs` has the reason, and
  the pass is resumable, so running it by hand now is safe regardless;
* **the worker is running an older image** that predates the cron entry.

## If it keeps happening

Alert on the *runway*, not on the job. A job that runs and silently does nothing produces a
green job history and a falling runway, and the runway is the number that is actually true.
It counts **consecutive** days: today plus a partition next month is one day of runway, and
counting partitions would report the reassuring number until midnight.

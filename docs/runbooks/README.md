# Runbooks

One file per alert, plus the procedures nobody is paged for but everybody eventually needs.

Every alert in `deploy/helm/memory-gateway/templates/prometheusrule.yaml` carries a
`runbook_url` pointing here, and `tests/test_deploy_assets.py` fails if one points at a
file that does not exist. An alert without a runbook is a page that begins with the
responder reading source code at three in the morning.

## Alerts

| Alert | Runbook |
|---|---|
| `ApiDown` | [api-down.md](api-down.md) |
| `GatewayErrorRateHigh` · `UpstreamFailureRateHigh` | [upstream-outage.md](upstream-outage.md) |
| `GatewayOverheadBudgetBreached` | [latency-budget.md](latency-budget.md) |
| `RequestLogsDropped` | [log-queue-drops.md](log-queue-drops.md) |
| `WorkerBacklogGrowing` | [worker-backlog.md](worker-backlog.md) |
| `PartitionRunwayLow` | [partition-runway.md](partition-runway.md) |
| `RateLimiterUnavailable` | [redis-outage.md](redis-outage.md) |
| `DistillationFailureRateHigh` | [distillation-failures.md](distillation-failures.md) |

## Procedures

| When | Runbook |
|---|---|
| Qdrant is down or slow | [qdrant-outage.md](qdrant-outage.md) |
| A vector-backend migration is stuck | [vector-backend-migration.md](vector-backend-migration.md) |
| Retrieval got worse after a chunking change | [chunking-change.md](chunking-change.md) |
| Rotating `ENCRYPTION_MASTER_KEY` or a provider credential | [credential-rotation.md](credential-rotation.md) |
| An organization was scheduled for deletion by mistake | [restore-deleted-organization.md](restore-deleted-organization.md) |
| Restoring from backup | [backup-restore.md](backup-restore.md) |
| Someone is locked out of the control plane | [login-lockout.md](login-lockout.md) |
| Deploying while the load test runs | [rolling-deploy-under-load.md](rolling-deploy-under-load.md) |

## The shape these follow

**Symptom** — what fired and what a customer would notice, which are usually different.
**What it means** — the mechanism, not the metric.
**Check** — commands, in the order that narrows fastest.
**Fix** — including the option of doing nothing.
**If it keeps happening** — the change that stops it recurring.

They are written to be followed by somebody who did not build this and is not fully awake.

# Deployment

Kubernetes, one image, two Deployments. Postgres, Redis, Qdrant and object storage are
external and managed.

## What you need first

| | Notes |
|---|---|
| **PostgreSQL 16+** | The system of record. Partitioned tables need nothing special, but the role must be able to `CREATE TABLE` at runtime — the partition job creates them nightly. |
| **Redis 7+** | Rate limits, the gateway config cache, login throttling, the job queue. Give it its own instance or at least its own database; the limiter's keys have TTLs and must not be evicted by a cache policy. |
| **Qdrant 1.12+** | Vectors. One collection per organization, addressed through an alias. |
| **S3-compatible storage** | Uploaded files. **Enable versioning** — a mistaken resync is otherwise unrecoverable. |
| **An embedding provider** | Any OpenAI-compatible `/embeddings` endpoint. The service refuses to start in production with the local lexical embedder. |

## Install

Create the Secret. The chart never creates it, and never will: chart values are stored in
the release, printed by `helm get values`, and committed to whatever repository holds the
environment's overrides.

```bash
kubectl -n gateway create secret generic memory-gateway \
  --from-literal=DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/gateway' \
  --from-literal=REDIS_URL='redis://host:6379/0' \
  --from-literal=S3_ACCESS_KEY_ID='...' \
  --from-literal=S3_SECRET_ACCESS_KEY='...' \
  --from-literal=ENCRYPTION_MASTER_KEY="$(openssl rand -base64 32)" \
  --from-literal=JWT_SIGNING_KEY="$(openssl rand -hex 32)" \
  --from-literal=EMBEDDING_API_KEY='sk-...'
```

The Secret's **keys are environment-variable names**, mounted with `envFrom`, so adding a
variable to it needs no chart change. An external-secrets operator writing to the same name
works identically.

```bash
helm upgrade --install gateway deploy/helm/memory-gateway \
  -n gateway --create-namespace \
  -f deploy/helm/examples/production.yaml
```

Then the one thing the UI cannot do — create the first superadmin, because there is nobody
to sign in as yet:

```bash
kubectl -n gateway run mg-seed --rm -it --restart=Never \
  --image=ghcr.io/memory-gateway/memory-gateway:0.1.0 \
  --overrides='{"spec":{"containers":[{"name":"mg-seed","image":"ghcr.io/memory-gateway/memory-gateway:0.1.0","args":["python","-m","app.cli","seed"],"envFrom":[{"configMapRef":{"name":"gateway-memory-gateway"}},{"secretRef":{"name":"memory-gateway"}}]}]}}'
```

It prints a generated password once. Change it at first sign-in.

## The four numbers that have to be right

### 1. `terminationGracePeriodSeconds`

Must exceed `api.drainSeconds` + `config.upstream.routingDeadlineSeconds`. **The chart
refuses to render if it does not**, because getting it wrong truncates a customer's
completion on every single deploy and is invisible in any test that does not deploy under
load.

The sequence, from `app/core/lifecycle.py`:

1. `SIGTERM` → `/readyz` answers 503 **immediately**, so the load balancer stops sending
   new work;
2. the pod keeps serving for `drainSeconds` (10 by default), because endpoint removal is
   not instant and requests routed a moment ago are still arriving;
3. only then does uvicorn stop accepting and wait for in-flight requests — a 120-second
   streaming completion included;
4. the kubelet's grace period has to cover 2 and 3.

Verify it the only way it can be verified — a deploy during sustained load. See
[runbooks/rolling-deploy-under-load.md](runbooks/rolling-deploy-under-load.md).

### 2. Migrations are backward-compatible

They run as a `pre-upgrade` hook, so the new schema is live while the **old** release is
still serving. Expand in one release, contract in a later one. The rule and its table are
in [CONTRIBUTING.md](../CONTRIBUTING.md#migrations-must-be-backward-compatible--expand-then-contract).

Before a migration you are unsure about, rehearse the half that CI cannot: restore a
staging database, apply the new migration, and run the **previous** image against it.

### 3. The SSRF guard

`config.upstream.privateAddresses: block`. An organization user can point an upstream
model at any URL, which without this makes the gateway an authenticated request forwarder
with a position inside your VPC — a `base_url` of `http://169.254.169.254/...` turns "test
connection" into a credential read.

The chart refuses to render `allow` alongside `environment: prod`. For a legitimate
internal endpoint — a self-hosted vLLM — name it:

```yaml
config:
  upstream:
    allowedHosts: [vllm.models.svc.cluster.local]
```

Not a CIDR unless you mean the whole range, including whatever moves into it later.

### 4. The embedding model

`config.embedding.provider` must not be `hash` in production. The chart refuses it and so
does the application: the local embedder is lexical, knows nothing about meaning, and the
failure it produces is silent — retrieval simply gets worse.

Changing the model **after** there is data is a reindex, not a restart: **Platform →
Settings** starts a rebuild beside the live collections and swaps the aliases when it is
done. Retrieval keeps working throughout. See the README's "Reindex" section.

## Vector backends

Qdrant is required and is the default. Chroma is optional: set `config.chromaUrl` (and
install this package with the `chroma` extra) and it becomes a second backend an operator
may place organizations on, **one tenant at a time**.

```yaml
config:
  qdrantUrl: http://qdrant:6333
  chromaUrl: http://chroma:8000     # optional
  defaultVectorBackend: qdrant      # where a new organization starts
```

**Which backend a tenant uses is a binding, not a URL.** An organization is placed on a
backend *by name*, chosen from the set this deployment configured; nothing tenant-facing
ever carries an address. That is the same rule the SSRF guard applies to upstream models,
one layer down, and it is why there is no field for a connection string on any screen.

**Choosing between them.** Chroma is the right answer for a single-box deployment, an
evaluation that should not begin by provisioning a cluster, or a team that has already
standardized on it. What it gives up, honestly:

| | Qdrant | Chroma |
|---|---|---|
| Atomic swap of the live collection | Alias, server-side | A pointer row this deployment keeps |
| Score threshold pushed down | Yes | No — the gateway over-fetches and filters |
| Snapshot tooling for backup | Built in | A persistent volume; see the backup runbook |
| Operational envelope at scale | Sharding, replication | Single node |

The corpus size at which the answer becomes Qdrant is **not measured here**; see the
"Not verified" note in the task file. Treat Chroma as the small-deployment option until
somebody has run the load suite against it.

**Moving a tenant between backends** is an operation, not a config change:

```bash
curl -X POST "$GW/api/v1/platform/organizations/$ORG/vector-backend"   -H "Authorization: Bearer $SUPERADMIN"   -d '{"backend": "chroma", "dry_run": true}'   # what would move
```

Drop `dry_run` to start it. The copy runs in the worker; **reads keep going to the current
backend until it is verified and promoted**, so a migration that stalls or fails costs disk
and nothing else. The source is dropped 15 minutes after the promotion — long enough for
every replica to have re-read the binding, which they cache for 15 seconds. `DELETE` on the
same path cancels one in flight.

## Sizing

Start here and correct from the load test rather than from intuition:

| | Requests | Limits | Scale on |
|---|---|---|---|
| `api` | 500m / 512Mi | memory 1Gi | CPU 70%, and request rate if a metrics adapter is available |
| `worker` | 500m / 1Gi | memory 2Gi | queue depth |
| `heavy-worker` | 1 / 2Gi | memory 4Gi | by hand; it is PDF and Office extraction |

**No CPU limit** on the API, deliberately. Throttling a latency-sensitive process to smooth
a burst is how a p95 becomes a p99 nobody can explain; the HPA is what bounds cost.

The heavy worker's memory floor is `EXTRACTION_MEMORY_LIMIT_BYTES` × `EXTRACTION_WORKERS`
plus the parent — the subprocesses are where a large PDF's memory actually goes.

`DB_POOL_SIZE` is per replica. Twelve API pods at the default 10 plus 5 overflow is 180
connections before the workers ask for any; check it against the database's `max_connections`
before scaling up, because the symptom of getting it wrong is latency on every phase at
once and nothing that looks like a slow query.

## Observability

```yaml
serviceMonitor: {enabled: true, labels: {release: kube-prometheus-stack}}
prometheusRule: {enabled: true}
tracing: {enabled: true, endpoint: "http://otel-collector:4318/v1/traces"}
```

Import the four dashboards in `deploy/grafana/`. Every alert links to a runbook in
`docs/runbooks/`, and a test fails the build if one links to a file that does not exist.

Traces are head-sampled at 5% in the application and tail-sampled in the collector —
`deploy/otel/collector.yaml` keeps every trace carrying an error, because "sample what
failed" is a decision that can only be taken once the trace is complete, which is not
something the process emitting it can know.

The alert to make sure actually reaches somebody is **`PartitionRunwayLow`**. It is the one
where the failure is an `INSERT` that errors rather than a query that is slow.

## Backups

`deploy/ops/backup.sh` nightly, `deploy/ops/verify-restore.sh` quarterly into a scratch
namespace. Details and the reasoning in [runbooks/backup-restore.md](runbooks/backup-restore.md).

An untested backup is a hypothesis, and the way this one fails in practice is not a corrupt
dump — it is a restore where every row is present, every screen loads, and retrieval quietly
returns nothing because the Qdrant aliases were never recreated.

## Upgrading

```bash
helm upgrade gateway deploy/helm/memory-gateway -n gateway -f production.yaml --wait
```

The pre-upgrade Job migrates; the rollout replaces pods with `maxUnavailable: 0`, so a new
pod is ready before an old one is asked to leave. Watch:

```bash
kubectl -n gateway rollout status deploy/gateway-memory-gateway-api
kubectl -n gateway logs -l app.kubernetes.io/component=api --tail=20 -f
```

If the migration Job fails the release stops and the Job is **kept**, which is the whole
reason migrations are a hook: its logs are the only account of what went wrong.

## Rotating `ENCRYPTION_MASTER_KEY`

There is a window in which no credential can be read. The procedure, including the
resumable re-wrap command, is in
[runbooks/credential-rotation.md](runbooks/credential-rotation.md).

## Configuration reference

Every variable is in [.env.example](../.env.example) with a comment saying what it protects.
The chart surfaces the ones a deployment usually changes; anything else goes in
`config.extra` verbatim.

Settings that are **operator policy** rather than deployment topology — retention ceilings,
rate-limit ceilings, storage caps, the distillation default, the embedding model — live in
the database since task 17 and are edited on **Platform → Settings**, with an audit trail.
The environment values are the bootstrap a fresh database starts from.

Vector backends split along the same line and it is worth being explicit about which half
is which. A backend's **address** is topology and stays in the environment; there is no
API or screen that can change one, because a server address a tenant's data flows to must
not be reachable through a form. A tenant's **binding** and the default for new
organizations are policy, and both are changed through the platform API with an audit
trail.

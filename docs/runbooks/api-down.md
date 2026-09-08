# `ApiDown` — nothing is being scraped

**Symptom.** No API replica is answering Prometheus. Customers are getting connection
errors, or they are not — the two cases look identical from here, which is the first thing
to establish.

**What it means.** Either every replica is down, or the `ServiceMonitor` stopped matching
and the service is fine. A label change during a chart upgrade produces the second, and it
is the more common of the two.

## Check

```bash
kubectl -n <ns> get pods -l app.kubernetes.io/component=api
kubectl -n <ns> get endpoints <release>-memory-gateway
curl -sS https://<host>/healthz
```

If `/healthz` answers, this is a monitoring failure and not an outage. Compare the
`ServiceMonitor` selector against the Service labels and stop here.

## Fix

Pods `CrashLoopBackOff`:

```bash
kubectl -n <ns> logs -l app.kubernetes.io/component=api --tail=50 --previous
```

The first line of a configuration failure names the variable. Common causes, in order:

* a Secret key renamed or removed — every variable comes from `secrets.existingSecret`
  with `envFrom`, so a missing one is only discovered at startup;
* `EMBEDDING_PROVIDER=hash` with `ENVIRONMENT=prod` — refused on purpose (the local
  embedder is lexical, and the failure it would otherwise cause is silent);
* the database unreachable — the process starts anyway and `/readyz` says which dependency
  is at fault, so if pods are running and unready, read that first.

Pods `Running` but never `Ready`: `curl` the pod's `/readyz` directly. It names the broken
dependency rather than answering a bare 503.

```bash
kubectl -n <ns> port-forward <pod> 8000:8000 &
curl -sS localhost:8000/readyz | jq
```

Pods `Pending`: no capacity. If `api.antiAffinity: hard` and the cluster has fewer nodes
than replicas, the surplus will never schedule — that is the trade the setting makes.

## If it keeps happening

A crash loop after a deploy is usually the migration hook having failed while the release
went ahead anyway. Check `kubectl get jobs` for the migrate Job; it is kept on failure
precisely so its logs are still there.

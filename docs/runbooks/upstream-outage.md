# `UpstreamFailureRateHigh` / `GatewayErrorRateHigh` — a provider is failing

**Symptom.** One upstream model is failing a fifth of its attempts, or the overall 5xx
rate is above 5%. On a gateway with a failover chain, customers may notice nothing at all
— which is the design working, and still worth acting on before the last target goes too.

**What it means.** `routing_attempts_total{outcome="failed"}` counts attempts, not
requests. A failover chain turns two failed attempts and one success into a served
request, so the two alerts firing together means the chain is exhausted and the one firing
alone means it is absorbing.

## Check

Which model, and with what error:

```promql
sum by (model, error_code) (rate(routing_failovers_total[10m]))
```

Then the provider's own status page. Then, in the UI, **Monitoring → Requests**, filtered
to the model: the error taxonomy and one request's detail view give the upstream's verbatim
message, which is usually the whole answer (an expired key, a deprecated model name, a
quota).

## Fix

**A provider outage.** Nothing to fix here. If the gateway has a single target, add a
second and set the mode to `failover` — that is a UI change, effective on the next request,
and it is the reason routing modes exist.

**An expired or revoked credential.** Models → the model → rotate the credential. Test
connection confirms it before any customer traffic does. The old value is not recoverable
and does not need to be.

**A deprecated model name.** The provider's 404 arrives as `model_not_found`. Change
`upstream_model_id` on the model row; every gateway pointing at it picks the change up on
its next request, because the config cache is bumped by the write.

**A quota.** If this is a global catalog model on the platform's own credential, the
platform rate-limit ceilings (Platform → Settings) are what stop one tenant spending the
whole quota. Lower them before raising the provider's.

## If it keeps happening

Put the model behind a failover chain, and consider whether it should be a weighted split
across two providers instead. `routing_chain_attempts` rising is the early signal — it
moves before any request actually fails.

# `DistillationFailureRateHigh` — memory has stopped learning

**Symptom.** A quarter of distillation passes are failing. No customer request fails. What
stops is conversation memory getting better — the product quietly reverts to retrieval-only
and nothing on any screen says so, which is why this needs an alert rather than a complaint.

**What it means.** A pass reads a transcript, calls a cheap model, and writes facts. It can
fail at the model call (most common), at parsing the model's answer, or at reconciliation.

## Check

```promql
sum by (outcome) (rate(distillation_passes_total[30m]))
sum by (disposition) (rate(distillation_facts_total[30m]))
```

Then **Memory browser → any end user → the distillation health panel**, or
`GET /api/v1/distillation/health`, which reports facts written per day, failure rate,
dedupe rate and supersession rate for one organization.

Worker logs carry the reason per pass:

```bash
kubectl -n <ns> logs -l app.kubernetes.io/component=worker --tail=200 | grep distillation
```

## Fix

**No model configured.** The most common cause and not really a failure: an organization
with no distillation model and no platform default. Set one in **Platform → Settings →
Distillation**, which is where the platform default lives since task 17.

**The model is failing.** It is an ordinary upstream model row, so
[upstream-outage.md](upstream-outage.md) applies. A distillation model is usually the
cheapest one available, which is also the one a provider deprecates first.

**Answers that do not parse.** If the failures are at parsing rather than at the call, the
model was changed to one that does not follow the prompt's output format. Distillation
prompts are strict on purpose — a parser that guesses would write facts nobody said.

**A daily spend cap reached.** That is a `skipped` outcome, not a `failed` one, and it is
the system working. Check the cap on the organization's distillation settings before
treating it as an incident.

## If it keeps happening

A dedupe rate near 100% with a healthy pass rate is the other failure worth knowing about:
the pass runs, succeeds, and learns nothing new. That is on the same health panel and does
not fire an alert, because it is a product question rather than an operational one.

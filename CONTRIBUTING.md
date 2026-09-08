# Contributing

## Getting set up

```bash
make install     # sync the virtualenv, install web packages, install the pre-commit hook
make up          # postgres, redis, qdrant, minio, the API, the workers, the Vite server
make seed        # a superadmin to sign in as, and the demo gateway if a provider key is set
```

`make check` runs everything CI runs. Run it before opening a pull request; it is faster
than a round trip through Actions and it is the same set of gates.

## Migrations must be backward-compatible — expand, then contract

**This is the rule that breaks production if it is broken, and it breaks it silently.**

Migrations run as a Helm `pre-upgrade` hook, so the new schema is in place *before the
first new pod starts*. During the rollout that follows, the old release is still serving —
against the new schema. A migration that drops a column the running code still selects
takes every old replica down, one query at a time, while the deploy looks healthy.

So a change that removes or renames anything is **two releases**:

| | Release N (expand) | Release N+1 (contract) |
|---|---|---|
| Rename a column | add the new one, write both, read the old | read the new one; drop the old |
| Drop a column | stop writing it; leave it in place | drop it |
| Add a `NOT NULL` column | add it nullable with a default | backfill, then add the constraint |
| Change a type | add a new column, dual-write, backfill | read the new one; drop the old |
| Drop a table | stop using it | drop it |

Release N must be deployed, and its rollout finished, before release N+1 is merged. In
practice that means a rename is a pull request now and a follow-up pull request next
sprint, and the follow-up is the one people forget — so leave a `TODO(contract)` comment
naming the release that may remove it.

**Adding** things is always safe: a new nullable column, a new table, a new index
(`CONCURRENTLY` on a large table, which needs `autocommit_block()` in the migration).

`tests/test_migration_offline.py` checks that migrations render as SQL without a database
and that constraint names match the models. It cannot check this rule — nothing can,
short of running the previous release against the new schema, which the deployment guide
describes as a manual step before a risky migration.

## Tests

Written as prose: `test_a_suspended_organizations_members_cannot_sign_in`, not
`test_auth_3`. The name is what a failure prints, and it should say what is now untrue.

- Database-backed tests are marked `db` and **skip** when no PostgreSQL is reachable, so
  the suite runs on a laptop with the stack down. CI sets `REQUIRE_DB_TESTS=1`, where they
  must run.
- Chart tests skip without `helm`, for the same reason and with the same CI guarantee.
- Store contracts (`tests/*_store_contract.py`) run against both the in-memory and the
  PostgreSQL implementation, which is what keeps the two agreeing.

Prefer testing behaviour through a service or a route over testing a private method. The
in-memory stores exist so that most tests need no containers at all.

## Comments

Comment the **why**, never the what. A comment that restates the line above it is noise; a
comment explaining why the obvious approach was rejected is the most valuable thing in the
file. Several modules here open with a paragraph on the decision they embody — see
`app/core/ssrf.py` or `app/services/maintenance.py` — and that is the standard.

## Conventions

- **Type annotations everywhere.** `mypy --strict` covers `app` and `tests`.
- **Line length 100**, `ruff format` decides the rest.
- **No secret ever leaves the process.** SPEC §5.4: credentials are write-only over the
  API, and there is no reveal endpoint. Do not add one, do not log one, do not put one in
  a span attribute — a trace is exported to a third-party backend.
- **Errors are typed**: raise from `app.core.errors`, and let the handlers choose the
  envelope. The data plane's is OpenAI's, the control plane's is the gateway's own, and
  the choice is made in one place by path.
- **New control-plane routes are authenticated by default** — they hang off a router that
  carries the `CurrentUser` dependency. A test asserts every `/api/v1` operation outside a
  short allow-list answers 401 without a token, and another asserts every mutating route
  is declared as audited or explicitly not.

## The API client is generated

`web/src/api/schema.d.ts` and `web/openapi.json` come from the live schema. After changing
any response model:

```bash
make openapi
```

and commit both. CI fails if they have drifted; the frontend's types are only worth
anything if they are the server's.

## Adding a setting

1. A field on `Settings` in `app/core/config.py`, with a comment saying what it protects
   and why the default is what it is.
2. An entry in `.env.example`.
3. If a deployment should be able to change it: a value in
   `deploy/helm/memory-gateway/values.yaml` and a line in the ConfigMap template.
4. If it is operator policy rather than deployment topology, consider `platform_settings`
   instead — it is a row, editable on a screen, with an audit trail. Secrets and endpoints
   stay in the environment; see `app/services/platform_settings.py`.

## Adding a metric

Add it to `app/core/metrics.py`, in the dataclass its subsystem owns. `tests/test_deploy_assets.py`
checks every PromQL expression in the alerts and dashboards against the real registry, so a
renamed metric fails the build rather than silently disabling an alert.

If it is worth alerting on, add the alert to
`deploy/helm/memory-gateway/templates/prometheusrule.yaml` **and** a runbook in
`docs/runbooks/`. A test enforces that pairing: an alert whose `runbook_url` points at a
file nobody wrote is a page that starts with a 404.

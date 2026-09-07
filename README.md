# memory-gateway

AI model gateway that augments requests with memory, monitors traffic, and processes chat
history. Clients keep speaking plain OpenAI; the gateway adds retrieval, per-end-user
memory, routing, and observability behind that interface.

- **[SPEC.md](SPEC.md)** — the design: concepts, architecture, data model, API surface.
- **[tasks/](tasks/README.md)** — the implementation plan, sliced so each task ends with
  something you can run.

Current state: **task 02 complete** — the OpenAI-compatible proxy works end to end,
streaming and not. No UI or configuration API yet; gateways are created with `make seed`.

## Quick start (Docker)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
curl localhost:8000/healthz   # {"status":"ok","version":"0.1.0"}
curl localhost:8000/readyz    # {"postgres":"ok","redis":"ok","qdrant":"ok","storage":"ok"}
```

`make up` is the same thing. The stack brings up Postgres, Redis, Qdrant, MinIO (with its
bucket created), and a placeholder web container that task 03 replaces with the SPA.

## Try the proxy

Create a demo organization, upstream model, gateway and API key. The provider key is
encrypted with `ENCRYPTION_MASTER_KEY` before it is stored, and the gateway key's
plaintext is printed once and never again.

```bash
OPENAI_API_KEY=sk-... make seed
```

Then point any OpenAI client at it, changing only `base_url`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/g/demo/v1", api_key="mg_...")

print(client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)

for chunk in client.chat.completions.create(
        model="demo", messages=[{"role": "user", "content": "count to 5"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

`model` is the gateway's slug, not the provider's model name: that indirection is the
point, so the endpoint can be repointed at a different provider without the client
changing. `make seed --no-auth`-style local providers (Ollama, vLLM) work by setting
`OPENAI_BASE_URL` and passing `--no-auth` to `python -m app.cli seed`.

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/) and the backing services reachable at the
addresses in `.env`.

```bash
cp .env.example .env
make install
make migrate
make dev
```

## Development

```bash
make check      # lint + typecheck + tests, the same three gates CI runs
make test       # pytest
make lint       # ruff check + format --check
make typecheck  # mypy, strict
make format     # apply fixes
```

`make help` lists every target. Without `make` installed (common on Windows) every target
is a one-liner you can run directly — e.g. `uv run pytest`, `uv run mypy`,
`uv run ruff check .`.

Tests marked `live` call a real provider and are skipped unless `OPENAI_API_KEY` is set;
run them deliberately with `uv run pytest -m live`. They cost a few tokens.

### Tests and the database

Tests marked `db` build a throwaway database by running the **migrations** — not
`metadata.create_all` — so a broken migration fails the suite rather than passing against
a schema no deployment will ever have. With no PostgreSQL reachable they skip, so the
suite still runs with the stack down. CI sets `REQUIRE_DB_TESTS=1`, which turns that skip
into a failure.

## Layout

```
app/
  api/        routers — health, proxy/ (data plane); control/ from task 03
  adapters/   upstream dialects — openai now, anthropic in task 16
  core/       config, logging, errors, ids, metrics, middleware, clients,
              crypto (envelope encryption), keys (API key format), background
  db/         engine, session, declarative base, models
  schemas/    the OpenAI wire format
  services/   gateway resolution, authentication, prompt assembly, forwarding, SSE
  workers/    background jobs (task 09)
  cli.py      operator commands — `python -m app.cli seed`
migrations/   alembic
deploy/       compose now, helm from task 18
web/          the SPA (task 03)
```

## Configuration

Everything is an environment variable, validated at import time: a missing or malformed
value stops the process immediately and names the variable, rather than surfacing on the
first request. See [.env.example](.env.example) for the full list.

## Operations

| Endpoint | Purpose |
|---|---|
| `POST /g/{slug}/v1/chat/completions` | The data plane. OpenAI schema, `stream: true` or `false`. |
| `GET /g/{slug}/v1/models` | The virtual models this gateway exposes, in OpenAI list format. |
| `GET /healthz` | Liveness. Checks nothing else — a dependency outage must not get the pod restarted into the same outage. |
| `GET /readyz` | Readiness. Probes Postgres, Redis, Qdrant, and object storage concurrently; 503 names what is broken. |
| `GET /metrics` | Prometheus. Request counts and latency labelled by route template. |

Data-plane errors use the **OpenAI** error envelope so client SDKs raise a useful typed
exception; control-plane errors use the gateway's own envelope, which carries the request
id. Unsupported fields (`tools`, `tool_choice`, `functions`, `function_call`, `logprobs`)
are refused with a 400 naming the field rather than silently dropped.

Every log line is one JSON object carrying `request_id`, which is also returned as
`X-Gateway-Request-Id` and honoured on the way in for cross-system correlation.

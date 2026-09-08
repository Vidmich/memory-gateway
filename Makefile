.DEFAULT_GOAL := help
SHELL := /bin/sh

COMPOSE := docker compose -f deploy/compose/docker-compose.yml
# The four values the chart requires, so `helm lint` has something to render.
CHART_VALUES := --set config.publicBaseUrl=https://gw.example.com --set config.qdrantUrl=http://qdrant:6333 --set config.s3Endpoint=http://minio:9000 --set config.s3Bucket=bucket --set secrets.existingSecret=memory-gateway
UV := uv

NPM := npm --prefix web

.PHONY: help install install-web dev web test test-all test-web lint lint-web format \
        typecheck typecheck-web check check-web openapi openapi-check build-web \
        e2e migrate downgrade revision seed up down logs worker worker-logs shell clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: install-web ## Sync the virtualenv from uv.lock and install web packages
	$(UV) sync --all-groups
	$(UV) run pre-commit install

install-web: ## Install the frontend's packages from package-lock.json
	$(NPM) ci

dev: ## Run the API locally with reload (needs the backing services up)
	$(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker: ## Run the ingestion worker locally (needs Redis and the rest up)
	$(UV) run arq app.workers.main.WorkerSettings

web: ## Run the Vite dev server, proxying /api to the API on :8000
	$(NPM) run dev

test: ## Run the Python test suite
	$(UV) run pytest

test-web: ## Run the frontend unit tests
	$(NPM) run test

test-all: ## Run the test suite with coverage, including database-backed tests
	$(UV) run pytest --cov --cov-report=term-missing

lint: ## Lint and check formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

lint-web: ## Lint the frontend
	$(NPM) run lint

format: ## Apply formatting and safe lint fixes
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Type-check with mypy (strict)
	$(UV) run mypy

typecheck-web: ## Type-check the frontend
	$(NPM) run typecheck

build-web: ## Production build of the SPA into web/dist
	$(NPM) run build

openapi: ## Regenerate web/openapi.json and the typed API client from the live schema
	$(UV) run python -m app.cli openapi > web/openapi.json
	$(NPM) run api:generate

openapi-check: openapi ## Fail if the committed API client has drifted from the server
	@git diff --quiet -- web/openapi.json web/src/api/schema.d.ts || { \
		echo ""; \
		echo "The generated API client is out of date. Run 'make openapi' and commit"; \
		echo "web/openapi.json and web/src/api/schema.d.ts."; \
		git --no-pager diff --stat -- web/openapi.json web/src/api/schema.d.ts; \
		exit 1; \
	}

e2e: ## Playwright end-to-end tests (needs the stack up and E2E_PASSWORD set)
	$(NPM) run e2e

check: lint typecheck test check-web ## Everything CI runs

check-web: lint-web typecheck-web test-web openapi-check build-web ## Frontend CI

chart: ## Lint the Helm chart and render it for every example values file
	helm lint deploy/helm/memory-gateway $(CHART_VALUES)
	@for values in deploy/helm/examples/*.yaml; do echo "== $$values"; helm template release deploy/helm/memory-gateway -f $$values > /dev/null; done
	$(UV) run pytest tests/test_deploy_assets.py -q

mock-upstream: ## A stand-in provider on :9099, so the load suite costs nothing
	$(UV) run python deploy/loadtest/mock_upstream.py --port 9099

loadtest: ## Mixed streaming and non-streaming traffic (needs k6 and GATEWAY_API_KEY)
	k6 run --summary-export=results/chat.json deploy/loadtest/chat.js

overhead: ## The SPEC 4.2 measurement: the gateway against a direct-to-provider baseline
	@test -n "$(UPSTREAM_KEY)" || (echo "set UPSTREAM_URL and UPSTREAM_KEY; without the baseline this measures nothing" && exit 1)
	k6 run deploy/loadtest/overhead.js

migrate: ## Apply migrations up to head
	$(UV) run alembic upgrade head

downgrade: ## Roll back one migration
	$(UV) run alembic downgrade -1

revision: ## Autogenerate a migration: make revision m="add gateways"
	@test -n "$(m)" || (echo "usage: make revision m=\"message\"" && exit 1)
	$(UV) run alembic revision --autogenerate -m "$(m)"

seed: ## Create the demo org, upstream model, gateway and API key
	$(UV) run python -m app.cli seed

up: ## Start the full stack
	$(COMPOSE) up -d --build

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

logs: ## Tail the API logs
	$(COMPOSE) logs -f api

worker-logs: ## Tail the ingestion worker logs
	$(COMPOSE) logs -f worker

shell: ## Open a shell in the API container
	$(COMPOSE) exec api /bin/bash

clean: ## Remove the stack, its volumes, and local caches
	-$(COMPOSE) down -v
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

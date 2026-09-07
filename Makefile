.DEFAULT_GOAL := help
SHELL := /bin/sh

COMPOSE := docker compose -f deploy/compose/docker-compose.yml
UV := uv

.PHONY: help install dev test test-all lint format typecheck check migrate downgrade revision seed up down logs shell clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Sync the virtualenv from uv.lock
	$(UV) sync --all-groups
	$(UV) run pre-commit install

dev: ## Run the API locally with reload (needs the backing services up)
	$(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run the test suite
	$(UV) run pytest

test-all: ## Run the test suite with coverage, including database-backed tests
	$(UV) run pytest --cov --cov-report=term-missing

lint: ## Lint and check formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Apply formatting and safe lint fixes
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Type-check with mypy (strict)
	$(UV) run mypy

check: lint typecheck test ## Everything CI runs

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

shell: ## Open a shell in the API container
	$(COMPOSE) exec api /bin/bash

clean: ## Remove the stack, its volumes, and local caches
	-$(COMPOSE) down -v
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

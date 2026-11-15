.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
COMPOSE ?= docker compose
PY := $(UV) run python

# First-party packages use a flat layout (packages/aegis_core -> `import aegis_core`).
# PYTHONPATH is the only wiring needed; separator resolved per platform.
PY_SEP := $(shell $(UV) run python -c "import os; print(os.pathsep)")
export PYTHONPATH := packages$(PY_SEP)apps

.DEFAULT: help
.PHONY: help install install-hooks dev paper test test-unit test-integration cov \
        cov-core lint fmt typecheck check migrate migration seed compose-dev compose-paper \
        compose-down docker-build logs clean tree

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install all workspace deps (incl. dev)
	$(UV) sync --all-groups

dev: ## Run the API locally against configs/dev.yaml (hot reload)
	AEGIS_ENVIRONMENT=dev $(UV) run uvicorn api.main:app --reload --port 8000 --app-dir apps/api

paper: ## Run the full paper-trading stack in Docker (no live keys needed)
	$(COMPOSE) -f infra/compose/docker-compose.paper.yml up --build

test: ## Run the whole test suite
	$(UV) run pytest

test-unit: ## Run unit tests only
	$(UV) run pytest tests/unit -m "not integration"

test-integration: ## Run integration tests only (OrderIntent -> Risk -> Gateway -> Ledger)
	$(UV) run pytest tests/integration -m integration

cov: ## Full suite with coverage report
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

cov-core: ## Coverage gate for the money-path packages (must be >= 80%)
	$(UV) run pytest --cov=packages/aegis_risk --cov=packages/aegis_backtest \
		--cov=packages/aegis_execution --cov=packages/aegis_ledger \
		--cov-report=term-missing --cov-fail-under=80

lint: ## Ruff lint
	$(UV) run ruff check .

fmt: ## Ruff format + autofix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy (strict)
	$(UV) run mypy

check: lint typecheck test ## Everything CI runs locally

migrate: ## Apply DB migrations to the configured database
	$(UV) run alembic -c data/migrations/alembic.ini upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	@test -n "$(m)" || (echo 'usage: make migration m="message"' && exit 1)
	$(UV) run alembic -c data/migrations/alembic.ini revision --autogenerate -m "$(m)"

seed: ## Load CSV seed market data into the database
	$(PY) -m scripts.load_seed_data

seed-data: ## Regenerate the deterministic synthetic candle CSV (demo data)
	$(PY) scripts/generate_synthetic_data.py

backtest-demo: ## Run the EMA-cross demo backtest on synthetic data
	$(PY) scripts/run_demo_backtest.py

collector-once: ## One collector pass (paper: CSV seeds -> market.candles; needs Postgres)
	$(PY) -m market_collector --once

collector-run: ## Collector loop, 60s between passes (needs Postgres)
	$(PY) -m market_collector --interval 60

docker-build: ## Build the single AegisTrade image
	$(COMPOSE) -f infra/compose/docker-compose.dev.yml build api

compose-dev: ## Postgres + Redis + Prometheus + Grafana for local development
	$(COMPOSE) -f infra/compose/docker-compose.dev.yml up -d

compose-paper: paper ## Alias for paper

compose-down: ## Stop all compose stacks
	-$(COMPOSE) -f infra/compose/docker-compose.dev.yml down
	-$(COMPOSE) -f infra/compose/docker-compose.paper.yml down

logs: ## Tail the paper stack logs
	$(COMPOSE) -f infra/compose/docker-compose.paper.yml logs -f --tail=100

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

tree: ## Print the repository tree (depth 3)
	find . -maxdepth 3 -type d -not -path '*/.git*' -not -path '*/.venv*' \
		-not -path '*/__pycache__*' -not -path '*/.freebuff*' | sort

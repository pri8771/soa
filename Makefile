SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE_FILE := infrastructure/local/docker-compose.yml

# Commands implement the contract in docs/DELIVERY_PLAN.md §7.1.
# Targets whose backing capability is not yet implemented fail loudly with a
# pointer to the backlog task that delivers them — they never fake success.

.PHONY: help
help: ## Show available commands
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: bootstrap
bootstrap: ## Install toolchains/dependencies and copy env template
	uv sync --dev
	pnpm install
	@if [ -f .env.example ] && [ ! -f .env ]; then cp .env.example .env && echo "Copied .env.example -> .env"; fi
	@echo "Bootstrap complete."

.PHONY: local-up
local-up: ## Start PostgreSQL, storage, mail, scanner (and optional telemetry)
	@if [ ! -f $(COMPOSE_FILE) ]; then echo "ERROR: local services not implemented yet (FND-006)."; exit 1; fi
	docker compose -f $(COMPOSE_FILE) up -d --wait

.PHONY: local-down
local-down: ## Stop local services (data volumes are preserved)
	@if [ ! -f $(COMPOSE_FILE) ]; then echo "ERROR: local services not implemented yet (FND-006)."; exit 1; fi
	docker compose -f $(COMPOSE_FILE) down

.PHONY: migrate
migrate: ## Apply database migrations
	uv run alembic upgrade head

.PHONY: verify-artifacts
verify-artifacts: ## Reconcile artifact records against object storage (STO-005)
	uv run python -m soa_api.ops.verify_artifacts

.PHONY: seed
seed: ## Load deterministic demo tenant and sample data
	@echo "ERROR: soa-fixtures defines the demo tenant, but the database SeedSink is not built yet (arrives with the ING epic's intake fixtures)." && exit 1

.PHONY: dev
dev: ## Run web, API, and worker with reload
	@echo "ERROR: combined dev runner not built yet — run these in separate terminals for now:" && \
	 echo "  uv run uvicorn soa_api.main:app --reload" && \
	 echo "  uv run soa-worker" && \
	 echo "  pnpm --filter @soa/web run dev" && exit 1

.PHONY: lint
lint: ## Lint Python and TypeScript
	uv run ruff check .
	uv run ruff format --check .
	pnpm run lint
	pnpm run format:check

.PHONY: format
format: ## Format Python and TypeScript in place
	uv run ruff format .
	pnpm run format

.PHONY: typecheck
typecheck: ## Type-check Python (mypy) and TypeScript (tsc)
	uv run mypy
	pnpm run typecheck

.PHONY: test
test: ## Fast unit and component tests
	uv run pytest
	pnpm run test

.PHONY: test-integration
test-integration: ## PostgreSQL-only tests (SET SOA_TEST_POSTGRES_URL; make local-up first)
	@if [ -z "$$SOA_TEST_POSTGRES_URL" ]; then \
	 echo "ERROR: set SOA_TEST_POSTGRES_URL to a non-superuser PostgreSQL URL (see ci.yml migrations job)."; exit 1; fi
	uv run pytest -m postgres -v

.PHONY: test-e2e
test-e2e: ## End-to-end tests
	@echo "ERROR: e2e suite not implemented yet (REV/ING epics)." && exit 1

.PHONY: test-security
test-security: ## Security and cross-tenant tests (RLS tests skip without SOA_TEST_POSTGRES_URL)
	uv run pytest tests/security -v

.PHONY: eval
eval: ## Golden document evaluation
	@echo "ERROR: evaluation runner not implemented yet (AIO-016)." && exit 1

.PHONY: build
build: ## Build all apps
	pnpm run build

.PHONY: check-docs
check-docs: ## Validate documentation links and documented commands
	python3 scripts/check_docs.py

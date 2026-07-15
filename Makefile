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
seed: ## Load the demo tenant (org, published process, active stream) for local dev
	uv run python scripts/seed_local.py

.PHONY: dev
dev: ## Print the commands to run web, API, and worker locally (see docs/LOCAL_DEV.md)
	@echo "Run these in three terminals (no Docker needed — filesystem storage):" && \
	 echo "  SOA_API_STORAGE_BACKEND=filesystem SOA_API_CORS_ALLOWED_ORIGINS='[\"http://localhost:5173\"]' uv run uvicorn soa_api.main:app --port 8000" && \
	 echo "  SOA_WORKER_STORAGE_BACKEND=filesystem uv run soa-worker" && \
	 echo "  VITE_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @soa/web run dev" && \
	 echo "Then open http://localhost:5173/app/northstar/overview (run 'make seed' first). See docs/LOCAL_DEV.md."

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

.PHONY: perf
perf: ## Run the performance test suite (REL-009)
	uv run pytest tests/performance -o addopts=""

.PHONY: docker-build
docker-build: ## Build the API, worker, and web container images (REL-001)
	docker build -f apps/api/Dockerfile -t soa-api .
	docker build -f apps/worker/Dockerfile -t soa-worker .
	docker build -f apps/web/Dockerfile -t soa-web .

.PHONY: security-scan
security-scan: ## Dependency + secret scans and the supply-chain policy gate (SEC-011)
	@set -euo pipefail; \
	uv export --format requirements-txt --no-emit-project --no-emit-workspace > /tmp/soa-requirements.txt; \
	uvx pip-audit -r /tmp/soa-requirements.txt --format json --output /tmp/soa-pip-audit.json || true; \
	pnpm audit --json > /tmp/soa-pnpm-audit.json || true; \
	python3 scripts/supply_chain.py check-exceptions; \
	python3 scripts/supply_chain.py evaluate \
		--pip-audit /tmp/soa-pip-audit.json --pnpm-audit /tmp/soa-pnpm-audit.json

# ==========================================================================
# olympus — operator workflow
# Usage: make <target>   (see `make help`)
# ==========================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup doctor up down logs ps check check-commits check-compose factory-doctor factory-trigger

help: ## Show this help message
	@echo "olympus — operator workflow"
	@echo "Usage: make <target>"
	@grep -E '^[a-zA-Z_0-9./-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---- Bootstrap ------------------------------------------------------------

setup: ## Preflight, install guard hooks, generate .env secrets
	bash setup.sh

doctor: ## Audit readiness (factory doctor + trigger status)
	python3 factory/doctor.py || true
	python3 factory/trigger.py --status 2>/dev/null || echo "trigger: not configured (status unavailable until first lap)"

## ---- Factory (autonomy level 0 — manual) ---------------------------------

factory-doctor: ## Run factory doctor (readiness + blockers)
	python3 factory/doctor.py

factory-trigger: ## Show scheduler trigger status (expected: NOT_ARMED)
	python3 factory/trigger.py --status

## ---- Compose (Infisical profile) -----------------------------------------

up: ## Start supporting services where present (Infisical profile)
	docker compose -f compose.infisical.yml --profile infisical up -d 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical up -d 2>/dev/null || echo "no compose stack for this profile"

down: ## Stop supporting services (keeps volumes)
	docker compose -f compose.infisical.yml --profile infisical down 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical down 2>/dev/null || true

logs: ## Tail logs from supporting services
	docker compose -f compose.infisical.yml --profile infisical logs -f 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical logs -f 2>/dev/null || true

ps: ## List supporting service status
	docker compose -f compose.infisical.yml --profile infisical ps 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical ps 2>/dev/null || true

## ---- Conformity -----------------------------------------------------------

check: ## Run attribution guard + structure checks
	bash .githooks/commit-msg .git/COMMIT_EDITMSG 2>/dev/null || true
	python3 -m compileall -q factory harness 2>/dev/null || true

check-commits: ## Run the attribution guard over recent commit messages
	bash .githooks/commit-msg .git/COMMIT_EDITMSG 2>/dev/null || true
	git log --oneline -5 2>/dev/null | head -5

check-compose: ## Validate compose files against .env.example
	cp .env.example .env 2>/dev/null || true
	docker compose -f compose.infisical.yml config --quiet 2>/dev/null || docker compose -f docker-compose.infisical.yml config --quiet 2>/dev/null || echo "compose config: not applicable"
	rm -f .env 2>/dev/null || true

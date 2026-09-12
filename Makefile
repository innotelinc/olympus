# ==========================================================================
# olympus — operator workflow
# Usage: make <target>   (see `make help`)
# ==========================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup doctor up down logs ps check secret-scan secret-scan-history check-commits check-compose factory-doctor factory-trigger app new-request builds studio-install studio-dev studio-build studio studio-test studio-check docker-build docker-up docker-down docker-logs docker-ps docker-shell docker-app docker-clean

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

## ---- App manufacturing (local dev — mirrors olympus-app-builder.yml) ------

app: ## Manufacture app from SPEC (or most recent build-requests/*.md) → ./builds
	bash scripts/manufacture.sh $(if $(SPEC),$(SPEC),)

new-request: ## Scaffold build-requests/$(NAME).md from factory/APP_SPEC_TEMPLATE.md
	@if [ -z "$(NAME)" ]; then echo "usage: make new-request NAME=my-app" >&2; exit 2; fi
	@mkdir -p build-requests
	@if [ -f "build-requests/$(NAME).md" ]; then echo "already exists: build-requests/$(NAME).md" >&2; exit 1; fi
	@cp factory/APP_SPEC_TEMPLATE.md "build-requests/$(NAME).md"
	@echo "created build-requests/$(NAME).md — edit it, then run: make app SPEC=build-requests/$(NAME).md"

builds: ## List factory output (./builds, gitignored)
	@ls -la builds 2>/dev/null || echo "builds/: empty (run make app) — output is gitignored per .gitignore:builds/"

## ---- Compose (Infisical profile) -----------------------------------------

up: ## Start supporting services where present (Infisical profile)
	docker compose -f compose.infisical.yml --profile infisical up -d 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical up -d 2>/dev/null || echo "no compose stack for this profile"

down: ## Stop supporting services (keeps volumes)
	docker compose -f compose.infisical.yml --profile infisical down 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical down 2>/dev/null || true

logs: ## Tail logs from supporting services
	docker compose -f compose.infisical.yml --profile infisical logs -f 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical logs -f 2>/dev/null || true

ps: ## List supporting service status
	docker compose -f compose.infisical.yml --profile infisical ps 2>/dev/null || docker compose -f docker-compose.infisical.yml --profile infisical ps 2>/dev/null || true

## ---- Docker (container) ---------------------------------------------------

docker-build: ## Build the Olympus image (ghcr.io/innotelinc/olympus:local)
	docker build -t ghcr.io/innotelinc/olympus:local .

docker-up: ## Start the Olympus container (detached, builds → volume)
	docker compose up --build -d

docker-down: ## Stop the Olympus container (keeps builds volume)
	docker compose down

docker-logs: ## Tail Olympus container logs
	docker compose logs -f

docker-ps: ## List Olympus container status
	docker compose ps

docker-shell: ## Shell into the running Olympus container
	docker compose exec olympus bash

docker-app: ## Manufacture inside the container (SPEC= or newest build-request)
	docker compose exec olympus bash scripts/manufacture.sh $(if $(SPEC),$(SPEC),)

docker-clean: ## Remove container + builds volume (irreversible)
	docker compose down -v

## ---- Studio (vibe-coding web UI — web/studio) -----------------------------

studio-install: ## Install Studio dependencies (web/studio)
	cd web/studio && npm ci

studio-dev: ## Run the Studio dev server (default http://localhost:3001)
	cd web/studio && npm run dev

studio-build: ## Production build of Studio
	cd web/studio && npm run build

studio: ## Run the built Studio server
	cd web/studio && npm run start

studio-test: ## Run the Studio test suite (vitest — parser, gateway route, OIDC flow)
	cd web/studio && npm test

studio-check: ## Typecheck + test Studio (run make studio-install first)
	cd web/studio && npx tsc --noEmit && npm test

studio-e2e: ## Drive the real Authentik handshake (needs STUDIO_E2E_* vars; see web/studio/README.md)
	cd web/studio && npm run test:integration

## ---- Conformity -----------------------------------------------------------

check: ## Run attribution guard + credential scan + structure checks
	bash .githooks/commit-msg .git/COMMIT_EDITMSG 2>/dev/null || true
	python3 -m compileall -q factory harness 2>/dev/null || true
	python3 scripts/secret-scan.py

secret-scan: ## Fail on literal credentials in tracked files
	python3 scripts/secret-scan.py

secret-scan-history: ## Scan every blob in git history (post-purge verification)
	python3 scripts/secret-scan.py --history

check-commits: ## Run the attribution guard over recent commit messages
	bash .githooks/commit-msg .git/COMMIT_EDITMSG 2>/dev/null || true
	git log --oneline -5 2>/dev/null | head -5

check-compose: ## Validate compose files against .env.example
	cp .env.example .env 2>/dev/null || true
	docker compose -f compose.infisical.yml config --quiet 2>/dev/null || docker compose -f docker-compose.infisical.yml config --quiet 2>/dev/null || echo "compose config: not applicable"
	rm -f .env 2>/dev/null || true

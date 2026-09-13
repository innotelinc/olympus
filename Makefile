# ==========================================================================
# olympus — operator workflow
# Usage: make <target>   (see `make help`)
# ==========================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup doctor up down logs ps check secret-scan secret-scan-history check-commits check-compose factory-doctor factory-trigger app new-request builds build-runner-install build-runner-check build-runner-list test-runner studio-install studio-dev studio-build studio studio-test studio-check studio-e2e studio-oidc studio-oidc-check studio-token-check studio-token-rotate studio-export-dir studio-build-queue-dir docker-build docker-up docker-up-host docker-down docker-down-host docker-logs docker-ps docker-ps-host docker-shell docker-app docker-clean docker-studio vault-bootstrap vault-renew

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

## ---- Build runner (Studio's "Build it" executor) --------------------------

build-runner-install: ## Install the host build runner as a systemd service (needs root)
	bash scripts/install-build-runner.sh

build-runner-check: ## Check the build runner's environment without building anything
	python3 scripts/build-runner.py --check

build-runner-list: ## Show the build queue and where each job got to
	python3 scripts/build-runner.py --list

test-runner: ## Run the build runner's unit tests (validation, env allow-list, status)
	python3 -m unittest discover -s scripts/tests -t scripts/tests -v

## ---- Compose (Vault profile) ----------------------------------------------

up: ## Start supporting services where present (Vault profile)
	docker compose -f compose.vault.yml --profile vault up -d 2>/dev/null || docker compose -f docker-compose.vault.yml --profile vault up -d 2>/dev/null || echo "no compose stack for this profile"

down: ## Stop supporting services (keeps volumes)
	docker compose -f compose.vault.yml --profile vault down 2>/dev/null || docker compose -f docker-compose.vault.yml --profile vault down 2>/dev/null || true

logs: ## Tail logs from supporting services
	docker compose -f compose.vault.yml --profile vault logs -f 2>/dev/null || docker compose -f docker-compose.vault.yml --profile vault logs -f 2>/dev/null || true

ps: ## List supporting service status
	docker compose -f compose.vault.yml --profile vault ps 2>/dev/null || docker compose -f docker-compose.vault.yml --profile vault ps 2>/dev/null || true

## ---- Docker (container) ---------------------------------------------------

docker-build: ## Build the Olympus image (ghcr.io/innotelinc/olympus:local)
	docker build -t ghcr.io/innotelinc/olympus:local .

docker-up: ## Start the Olympus container (detached, builds → volume)
	docker compose up --build -d

# The gateway this stack talks to is published on 127.0.0.1 only, and a bridge
# container cannot reach a loopback-published port (verified: host.docker.internal,
# the host-gateway IP and the LAN address all refuse). Use this target when the
# gateway runs on this host; see the header of compose.host-gateway.yml.
docker-up-host: ## Start with host networking (gateway published on loopback here)
	docker compose -f docker-compose.yml -f compose.host-gateway.yml up -d --build

docker-down: ## Stop the Olympus container (keeps builds volume)
	docker compose down
docker-down-host: ## Stop the host-networked stack
	docker compose -f docker-compose.yml -f compose.host-gateway.yml down

docker-logs: ## Tail Olympus container logs
	docker compose logs -f

docker-ps: ## List Olympus container status
	docker compose ps
docker-ps-host: ## List the host-networked stack's status
	docker compose -f docker-compose.yml -f compose.host-gateway.yml ps

docker-shell: ## Shell into the running Olympus container
	docker compose exec olympus bash

docker-app: ## Manufacture inside the container (SPEC= or newest build-request)
	docker compose exec olympus bash scripts/manufacture.sh $(if $(SPEC),$(SPEC),)

docker-clean: ## Remove container + builds volume (irreversible)
	docker compose down -v

docker-studio: ## Build the Studio image (ghcr.io/innotelinc/olympus-studio:local)
	docker compose build studio

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

studio-export-dir: ## Make build-requests/ writable by the Studio container (Export to factory)
	bash scripts/studio-export-dir.sh

studio-build-queue-dir: ## Make .factory/build-queue/ writable by the Studio container (Build it)
	STUDIO_DIR_LABEL=build-queue bash scripts/studio-export-dir.sh .factory/build-queue

studio-check: ## Typecheck + test Studio (run make studio-install first)
	cd web/studio && npx tsc --noEmit && npm test

studio-e2e: ## Drive the real Authentik handshake (needs STUDIO_E2E_* vars; see web/studio/README.md)
	cd web/studio && npm run test:integration

# Registers Studio's OIDC application on Cerulean's Authentik and registers the
# local + public callbacks as redirect URIs. Idempotent: a re-run repairs an
# existing provider (grant_types, missing redirect URIs) instead of skipping it.
# Needs AUTHENTIK_URL/AUTHENTIK_TOKEN in .env — see `make studio-token-rotate`.
studio-oidc: ## Register/repair Studio's OIDC app in Cerulean Authentik (ARGS="--dry-run" to preview)
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi
	python3 scripts/authentik-studio-app.py $(ARGS)

studio-oidc-check: ## Confirm the issuer answers discovery, and the credential is not lapsing
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi; \
	issuer=$$(sed -n 's/^OIDC_ISSUER_URL=//p' .env | tail -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]'); \
	if [[ -z "$$issuer" ]]; then echo "OIDC_ISSUER_URL is not set in .env" >&2; exit 2; fi; \
	url="$${issuer%/}/.well-known/openid-configuration"; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$$url" || true); \
	if [[ "$$code" == "200" ]]; then echo "discovery: ok — $$url"; else echo "discovery: HTTP $$code from $$url" >&2; exit 1; fi
	@python3 scripts/authentik-studio-token.py --check $(ARGS)

studio-token-check: ## Report the registration credential's expiry (exit 2 once it is lapsing)
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi
	python3 scripts/authentik-studio-token.py --check $(ARGS)

# Mints the credential in the Authentik shell rather than through the REST API.
# Authentik 2026.8 forces `expires` to the tenant's default_token_duration for
# every api-intent token (minutes=30 on Cerulean), so a REST-created credential
# would die every half hour; and PATCHing a token re-parents it to the caller,
# which is how an administrator credential gets mistaken for this one. The host
# is a parameter because this repository stores neither its name nor a key; the
# key is the operator's own, in their ssh agent or the default identity, with
# AUTHENTIK_SSH_KEY to point somewhere else.
studio-token-rotate: ## Rotate the registration credential (needs AUTHENTIK_HOST=<host running cerulean-authentik>)
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi; \
	if [[ -z "$(AUTHENTIK_HOST)" ]]; then \
		echo "set AUTHENTIK_HOST=<host running cerulean-authentik>, e.g. make studio-token-rotate AUTHENTIK_HOST=10.0.0.5" >&2; \
		echo "the credential is rebuilt by running a program in that container's shell; see docs/stack.md" >&2; \
		exit 2; \
	fi
	python3 scripts/authentik-studio-token.py --snippet $(ARGS) \
		| ssh $(if $(AUTHENTIK_SSH_KEY),-i $(AUTHENTIK_SSH_KEY)) $${AUTHENTIK_SSH_USER:-root}@$(AUTHENTIK_HOST) 'docker exec -i cerulean-authentik ak shell' \
		| python3 scripts/authentik-studio-token.py --store-stdin $(ARGS)

vault-bootstrap: ## Store this stack's password in Cerulean Vault (AUTHENTIK_* are written too when set)
	python3 scripts/vault-bootstrap.py

vault-renew: ## Renew this stack's scoped Vault token so it cannot lapse (--check to report only)
	bash scripts/vault-renew.sh

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

check-compose: ## Validate every compose file (rendered against .env.example, never touching .env)
	@if ! command -v docker >/dev/null 2>&1; then echo "compose config: skipped (no docker)"; exit 0; fi; \
	tmp_env=$$(mktemp); \
	cp .env.example "$$tmp_env" 2>/dev/null || true; \
	status=0; \
	for f in docker-compose.yml compose.vault.yml; do \
		if docker compose --env-file "$$tmp_env" -f "$$f" config --quiet 2>/dev/null; then \
			echo "compose config: ok ($$f)"; \
		else \
			echo "compose config: FAILED ($$f)" >&2; status=1; \
		fi; \
	done; \
	if docker compose --env-file "$$tmp_env" -f docker-compose.yml -f compose.host-gateway.yml config --quiet 2>/dev/null; then \
		echo "compose config: ok (docker-compose.yml + compose.host-gateway.yml)"; \
	else \
		echo "compose config: FAILED (host-gateway overlay)" >&2; status=1; \
	fi; \
	rm -f "$$tmp_env"; \
	exit $$status

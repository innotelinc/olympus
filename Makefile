# ==========================================================================
# olympus — operator workflow
# Usage: make <target>   (see `make help`)
# ==========================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup env-sync env-sync-write doctor up down logs ps check secret-scan secret-scan-history check-commits check-compose factory-doctor factory-trigger app plan new-request builds prune build-runner-install build-runner-check build-runner-list test-runner studio-install studio-dev studio-build studio studio-test studio-check studio-e2e studio-oidc studio-oidc-check studio-token-check studio-token-rotate studio-export-dir studio-build-queue-dir tui docker-build docker-up docker-up-host docker-down docker-down-host docker-logs docker-ps docker-ps-host docker-shell docker-app docker-clean docker-studio vault-bootstrap vault-renew sites-up sites-down site-package site-publish site-unpublish sites-wildcard sites-list site-check app-package app-up app-down app-remove apps-list app-publish gateway-edge-check

help: ## Show this help message
	@echo "olympus — operator workflow"
	@echo "Usage: make <target>"
	@grep -E '^[a-zA-Z_0-9./-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---- Bootstrap ------------------------------------------------------------

setup: ## Preflight, install guard hooks, generate .env secrets
	bash setup.sh
	@# Last, so a failed preflight does not leave a half-seeded .env: bring the new
	@# file level with every key .env.example documents. `setup.sh` does not open
	@# .env at all, so an upgrade that adds a knob was invisible until somebody
	@# read the example and noticed — see the header of scripts/env-sync.py.
	@[ -f .env ] && python3 scripts/env-sync.py --write || true

# An absent key and a key set to the documented default behave identically — until
# the default changes, and then a deployment that never had the key moves with it
# while its operator believes nothing changed. `make check` reports; `env-sync-write`
# appends. Neither ever edits, reorders or removes a line already in `.env`.
env-sync: ## Report .env keys .env.example documents that .env has never mentioned
	@if [ ! -f .env ]; then echo "env-sync: no .env — nothing to compare (cp .env.example .env)"; exit 0; fi
	python3 scripts/env-sync.py $(ARGS)

env-sync-write: ## Append those keys to .env, carrying the example's own comments
	@if [ ! -f .env ]; then echo "env-sync: no .env — create it first (make setup)" >&2; exit 2; fi
	python3 scripts/env-sync.py --write $(ARGS)

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

# The step `make app` now runs first, runnable on its own. It answers one question —
# which stack is this spec going to be built in — and answering it costs one gateway
# turn, while getting it wrong costs the whole agent run. `--dry-run` prints the plan
# without writing plan.json, which is the way to look before committing to a build.
plan: ## Plan a SPEC's stack without building it (SPEC=build-requests/x.md, ARGS="--dry-run")
	@if [ -z "$(SPEC)" ]; then echo "usage: make plan SPEC=build-requests/<name>.md" >&2; exit 2; fi
	python3 scripts/project_plan.py --spec "$(SPEC)" $(ARGS)

## ---- Build runner (Studio's "Build it" executor) --------------------------

build-runner-install: ## Install the host build runner as a systemd service (needs root)
	bash scripts/install-build-runner.sh

build-runner-check: ## Check the build runner's environment without building anything
	@# As the account the unit actually runs as — which the installer probes for (root,
	@# on hosts that deny unprivileged user namespaces). Checking as the wrong account
	@# reports a PATH and a .env the runner cannot really see, which is the difference
	@# that makes a broken install look green.
	@UNIT_USER=$$(systemctl show olympus-build-runner -p User --value 2>/dev/null); \
	UNIT_HOME=$$(systemctl show olympus-build-runner -p Environment --value 2>/dev/null \
		| tr ' ' '\n' | sed -n 's/^HOME=//p' | head -1); \
	if [ "$$(id -u)" = "0" ] && [ -n "$$UNIT_USER" ] && [ "$$UNIT_USER" != "root" ] \
		&& command -v runuser >/dev/null 2>&1; then \
		runuser -u "$$UNIT_USER" -- env HOME="$$UNIT_HOME" USER="$$UNIT_USER" \
			PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin BUILD_EXTRA_PATH=/usr/local/bin \
			python3 scripts/build-runner.py --check; \
	else \
		python3 scripts/build-runner.py --check; \
	fi

prune: ## Report old builds + finished queue files (ARGS="--yes" to delete, "--older-than 0" for all, "--queue-only" to spare builds)
	python3 scripts/prune-builds.py $(ARGS)

## ---- Build model (the one thing that decides whether a build writes anything) --

build-model-check: ## Can the configured build model call a tool, and be asked twice? (exit 1 = builds write nothing)
	@# Exit 0 usable, 1 broken, 2 could not tell. A build fails silently when this
	@# exits 1: Codex exits 0, the app directory stays empty, nothing says why.
	python3 scripts/build-model-check.py $(ARGS)

build-model-alert: ## Run the check and alert (Telegram) when the chain cannot build
	bash scripts/build-model-alert.sh $(ARGS)

build-runner-list: ## Show the build queue and where each job got to
	python3 scripts/build-runner.py --list

tui: ## Build from the terminal UI (ARGS="--list"|"--once 'a weight tracker'")
	python3 scripts/olympus-tui.py $(ARGS)

test-runner: ## Run the script unit tests (runner, prune, model check, restore)
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

# The dashboard SSO proxy. Deliberately not folded into `docker-up`: it needs the
# GATEWAY_* credentials from `make gateway-oidc`, and without them oauth2-proxy
# would start and then refuse every login, which is the failure mode that looks
# like a working deployment. See compose.gateway-sso.yml and docs/gateway-sso.md.
# The gateway now lives in THIS repo's compose (profile `gateway`), published on
# loopback so the host-side build runner and the SSO proxy can reach it and
# nothing on the LAN can. It used to be a container from another project; moving
# it here is what gives it a config that can be recreated. `OMNIROUTE_BASE_URL`
# stays the loopback URL for that reason — host scripts read the same .env.
gateway-up: ## Start the in-repo gateway (profile: gateway) and wait for it
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi
	docker compose --profile gateway up -d omniroute
	@port=$$(sed -n 's/^OMNIROUTE_PORT=//p' .env 2>/dev/null | tail -1 | tr -d "'\" " ); port=$${port:-20128}; \
	for i in $$(seq 1 30); do \
		code=$$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$$port/healthz" || true); \
		if [[ "$$code" == "200" ]]; then echo "gateway: ok — live on 127.0.0.1:$$port"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "gateway: not answering /healthz on 127.0.0.1:$$port — check 'docker logs olympus-omniroute'" >&2; exit 1

gateway-down: ## Stop the in-repo gateway (keeps its data volume)
	docker compose --profile gateway rm -sf omniroute

# The gateway's state is ONE volume, and the key that decrypts its provider
# connections lives inside that same volume (`server.env`) — so the volume does
# not merely hold the gateway, it is the gateway. This copies both halves into
# Cerulean Vault, under this stack's own path, so losing the host stops mattering.
# It is safe to run any time: --check compares the backup with the live gateway
# and exits non-zero on drift, which is the version worth putting on a timer.
gateway-vault-backup: ## Back the gateway's keys + connections up to Cerulean Vault
	python3 scripts/omniroute-vault-backup.py $(ARGS)

gateway-vault-check: ## Fail when the Vault backup no longer matches the live gateway
	python3 scripts/omniroute-vault-backup.py --check

gateway-vault-restore: ## Put the stored keys + connections back (ARGS="--force" to overwrite server.env)
	python3 scripts/omniroute-vault-backup.py --restore $(ARGS)

gateway-sso-up: ## Put the gateway dashboard behind Cerulean Authentik (oauth2-proxy)
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi; \
	for k in GATEWAY_OIDC_CLIENT_SECRET GATEWAY_SSO_COOKIE_SECRET GATEWAY_PUBLIC_HOST; do \
		v=$$(sed -n "s/^$$k=//p" .env | tail -1); \
		if [[ -z "$$v" ]]; then echo "$$k is not set in .env — run 'make gateway-oidc ARGS=--rotate-secret' first" >&2; exit 2; fi; \
	done; \
	r=$$(sed -n 's/^GATEWAY_SSO_REDIS_PASSWORD=//p' .env | tail -1); \
	if [[ -z "$$r" ]]; then \
		echo "GATEWAY_SSO_REDIS_PASSWORD is not set in .env." >&2; \
		echo "The proxy keeps its sessions there rather than in a cookie — a cookie" >&2; \
		echo "session overflows on an identity with many groups and the edge answers" >&2; \
		echo "the login callback with 502. Add one with:" >&2; \
		echo "  printf 'GATEWAY_SSO_REDIS_PASSWORD=%s\\n' \$$(openssl rand -hex 32) >> .env" >&2; \
		exit 2; \
	fi; \
	docker compose -f docker-compose.yml -f compose.gateway-sso.yml up -d --no-deps --wait gateway-sso-sessions gateway-sso

gateway-sso-down: ## Stop the dashboard SSO proxy and its session store (the gateway keeps running)
	docker compose -f docker-compose.yml -f compose.gateway-sso.yml rm -sf gateway-sso gateway-sso-sessions

# Makes Authentik the only gate at the gateway: `requireLogin=false`, so there is
# one login instead of two and the one that could not authenticate anyone (the
# gateway's own) is gone. Idempotent, and it refuses if the gateway is reachable
# beyond this host — because reachability is the whole control once its own login
# is off. See scripts/gateway-auth-mode.py.
gateway-auth-mode: ## Make Cerulean Authentik the only gate at the gateway (ARGS="--dry-run"|"--verify")
	python3 scripts/gateway-auth-mode.py $(ARGS)

# Publishes GATEWAY_PUBLIC_HOST at the edge. The SSO proxy makes the dashboard
# safe to reach; this is what makes it REACHABLE — the CNAME, a certificate, and
# the NPM proxy host that forwards to the proxy rather than to the gateway.
# Idempotent, and it refuses to repoint a name that already answers somewhere
# else. Needs CERULEAN_* in .env; see docs/gateway-sso.md.
gateway-edge: ## Publish the gateway's public name through Cerulean + the NPM edge (ARGS="--dry-run")
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi
	python3 scripts/cerulean-edge.py $(ARGS)

# Asserts the two things that distinguish "wired up" from "running": the proxy
# answers liveness, and an unauthenticated request is handed to Authentik with
# OUR client id. A dashboard served directly would pass the first and fail the
# second — which is exactly the misconfiguration worth catching.
gateway-sso-check: ## Confirm the proxy redirects to Authentik instead of serving the dashboard
	@port=$$(sed -n 's/^GATEWAY_SSO_PORT=//p' .env 2>/dev/null | tail -1 | tr -d "'\" " ) ; port=$${port:-20129}; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$$port/ping" || true); \
	if [[ "$$code" != "200" ]]; then echo "proxy: not answering — HTTP $$code from http://127.0.0.1:$$port/ping" >&2; exit 1; fi; \
	echo "proxy: ok — live on 127.0.0.1:$$port"; \
	loc=$$(curl -s -o /dev/null -w '%{redirect_url}' -m 10 "http://127.0.0.1:$$port/" || true); \
	issuer=$$(sed -n 's/^GATEWAY_OIDC_ISSUER_URL=//p' .env 2>/dev/null | tail -1 | tr -d "'\" "); \
	client=$$(sed -n 's/^GATEWAY_OIDC_CLIENT_ID=//p' .env 2>/dev/null | tail -1 | tr -d "'\" "); \
	base=$${issuer%/}; base=$${base%/application/o/*}; \
	if [[ "$$loc" != "$$base/application/o/authorize/"* ]]; then \
		echo "sso: FAILED — / did not redirect to Authentik (got '$$loc')" >&2; exit 1; \
	fi; \
	if [[ -n "$$client" && "$$loc" != *"client_id=$$client"* ]]; then \
		echo "sso: FAILED — redirect names a different client than GATEWAY_OIDC_CLIENT_ID" >&2; exit 1; \
	fi; \
	echo "sso: ok — / redirects to $$base/application/o/authorize/ as client '$$client'"

# The step the two checks above cannot make: whether the PUBLIC name resolves and
# serves. They probe loopback ports, so they pass while DNS is broken, the
# certificate has lapsed or the edge is down — which is precisely the report that
# arrives as "it's not resolving". This walks the chain in order and names the
# first link that is broken. `monitor-gateway-edge` is the same check on a timer.
gateway-edge-check: ## Is gateway.olympus.innotel.us reachable, and if not, which link broke?
	python3 scripts/gateway-edge-check.py $(ARGS)

# ---- Published websites (Studio "Build & publish") ----------------------------
# A Studio *app* is finished when it is generated; a Studio *website* is finished
# when it has been built. These targets are that second step, in the order it has
# to happen: package (source → dist) then publish (staged tree → a name).
#
# The pieces are separate on purpose. Packaging is deterministic and testable and
# works with no configuration at all; publishing needs Cerulean and the edge, and
# failing there must not throw away a good build.

sites-up: ## Start the static site server for staged sites (compose profile `sites`)
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi
	docker compose --profile sites up -d sites

sites-down: ## Stop the static site server (staged sites are left on disk)
	docker compose --profile sites rm -sf sites

site-package: ## Build a Studio website into a servable dist/ and stage it (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make site-package SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/package-website.py $(SLUG) --publish

# ONE WILDCARD, THEN INSTANT PUBLISHES.
#
# Before this, every published name cost a DNS record and a certificate — a minute
# or more of Let's Encrypt issuance per site, which is not a button you can put in
# a UI. `sites-wildcard` runs the slow half once: `*.studio.olympus.innotel.us`
# plus one certificate covering it. After that, `site-publish` adds only a proxy
# host — seconds, no waiting, no per-name certificate.
#
# The name is always <slug>.<SITE_HOST_SUFFIX>; it is derived, never typed, so it
# cannot drift from the build directory the slug already names.
sites-wildcard: ## Create the wildcard name + certificate under which sites publish (once)
	python3 scripts/studio-sites.py --wildcard $(ARGS)

sites-list: ## List the sites published under the wildcard suffix
	python3 scripts/studio-sites.py --list

# The stage step is separate and it is what fills the site's directory; the publish
# step only points a name at the server. Running both here is the convenience, not
# the design — a republish of already-staged files is just `site-publish`.
site-publish: ## Stage a website and put it on <slug>.<SITE_HOST_SUFFIX> (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make site-publish SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/package-website.py "$(SLUG)" --publish || exit $$?
	python3 scripts/studio-sites.py --publish "$(SLUG)" $(ARGS)

site-unpublish: ## Take a published site off the edge (SLUG=<slug>, ARGS="--dry-run")
	@if [ -z "$(SLUG)" ]; then echo "usage: make site-unpublish SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/studio-sites.py --remove "$(SLUG)" $(ARGS)

# --- applications (Studio → one container per app) --------------------------
#
# An *app* is full-stack, so it has two more steps than a website and they are
# separate for the same reason packaging and publishing are: the build is
# deterministic and testable, and running a container is a deployment.
#
#   app-package   client build + the source archive (`dist/client`, `app.zip`)
#   app-up        build the image, run the container, write its nginx vhost
#   app-publish   app-up, then put <slug>.<SITE_HOST_SUFFIX> in front of it
#
# The vhost is what makes the edge generic: `olympus-sites` proxies the name to
# the app's loopback port, so `site-publish` never learns an app-specific port.

app-package: ## Build a Studio app's client and write its archive (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make app-package SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/package-app.py "$(SLUG)" $(ARGS)

app-up: ## Build and run a Studio app as its own container (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make app-up SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/app-runtime.py --up "$(SLUG)" --build $(ARGS)

app-down: ## Stop an app's container, keeping its database (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make app-down SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/app-runtime.py --down "$(SLUG)" $(ARGS)

app-remove: ## Stop an app and delete its container, database and image (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make app-remove SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/app-runtime.py --remove "$(SLUG)" $(ARGS)

apps-list: ## List the applications this host is running
	python3 scripts/app-runtime.py --list

app-publish: ## Package, run and publish an app on <slug>.<SITE_HOST_SUFFIX> (SLUG=<slug>)
	@if [ -z "$(SLUG)" ]; then echo "usage: make app-publish SLUG=<slug>" >&2; exit 2; fi
	python3 scripts/package-app.py "$(SLUG)" || exit $$?
	python3 scripts/app-runtime.py --up "$(SLUG)" --build || exit $$?
	python3 scripts/studio-sites.py --publish "$(SLUG)" $(ARGS)

# The assertion that publishing actually published: the name is served by the
# edge, from this host's staged tree, and the entry point is the built site
# rather than the edge's error page. A name that 502s or 404s passes "the record
# exists" and fails this, which is the difference worth checking.
site-check: ## Confirm a published site answers with its own page (HOST=<name>)
	@if [ -z "$(HOST)" ]; then echo "usage: make site-check HOST=<name>" >&2; exit 2; fi
	@if [ -z "$(HOST)" ]; then echo "usage: make site-check HOST=<name>" >&2; exit 2; fi; \
	code=$$(curl -s -o /tmp/site-check.html -w '%{http_code}' -m 15 "https://$(HOST)/" || true); \
	if [ "$$code" != "200" ]; then echo "site: FAILED — HTTP $$code from https://$(HOST)/" >&2; exit 1; fi; \
	if ! grep -qi '<div id="root"\|<script\|<html' /tmp/site-check.html; then \
		echo "site: FAILED — https://$(HOST)/ answered 200 but not with a page" >&2; exit 1; \
	fi; \
	echo "site: ok — https://$(HOST)/ serves a built page"

docker-shell: ## Shell into the running Olympus container
	docker compose exec olympus bash

docker-app: ## Manufacture inside the container (SPEC= or newest build-request)
	docker compose exec olympus bash scripts/manufacture.sh $(if $(SPEC),$(SPEC),)

docker-clean: ## Remove container + builds volume (irreversible)
	docker compose down -v

docker-studio: ## Build the Studio image (ghcr.io/innotelinc/olympus-studio:local)
	docker compose build studio

# Rebuild + restart Studio THE WAY THIS HOST RUNS IT. Not a convenience alias:
# `docker compose up -d studio` starts Studio on the bridge network, where its
# `OMNIROUTE_BASE_URL` (127.0.0.1 by default and in .env) resolves to Studio
# itself — every generation then fails ECONNREFUSED with nothing in the UI that
# says so. That is how this deployment broke once. Studio belongs in the same
# host-networked group as `olympus` and the SSO proxy whenever the gateway is
# published on loopback; see the header of compose.host-gateway.yml.
docker-studio-up: ## Rebuild + restart Studio with host networking (the gateway is loopback-published)
	docker compose -f docker-compose.yml -f compose.host-gateway.yml up -d --build studio
	@echo "--- reachability (Studio -> gateway) ---"; \
	docker exec olympus-studio node -e 'fetch(process.env.OMNIROUTE_BASE_URL.replace(/\/v1$$/,"")+"/healthz",{signal:AbortSignal.timeout(8000)}).then(r=>{console.log("gateway",r.status);process.exit(r.ok?0:1)}).catch(e=>{console.error("gateway unreachable:",e.cause?.code||e.name);process.exit(1)})'

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

# The gateway dashboard's OIDC client. Registered separately from Studio's
# because the issuer path IS the application slug, and the dashboard is a second
# application with its own callbacks. GATEWAY_PUBLIC_HOST is what the browser
# uses, so each callback is built from it and Authentik must have it on file.
#
# TWO callbacks are registered on purpose:
#   /oauth2/callback        the identity-aware proxy that is actually deployed
#   /api/auth/oidc/callback OmniRoute's own OIDC, which cannot validate an
#                           Authentik ID token today (docs/gateway-sso.md) but
#                           needs no re-registration if upstream fixes it
#
# ARGS="--rotate-secret" is the only way to obtain a client secret at all:
# Authentik stores them write-only, so an existing provider has no readable
# value. It prints the new secret once, and the previous one stops working.
gateway-oidc: ## Register/repair the gateway dashboard's OIDC client in Cerulean Authentik (ARGS="--dry-run")
	@if [[ ! -f .env ]]; then echo "no .env — cp .env.example .env first" >&2; exit 2; fi; \
	host=$$(sed -n 's/^GATEWAY_PUBLIC_HOST=//p' .env | tail -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]'); \
	if [[ -z "$$host" ]]; then \
		echo "set GATEWAY_PUBLIC_HOST in .env (e.g. gateway.olympus.innotel.us) — it is the redirect URI Authentik registers" >&2; \
		exit 2; \
	fi; \
	slug=$$(sed -n 's/^GATEWAY_SLUG=//p' .env | tail -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]'); \
	python3 scripts/authentik-studio-app.py \
		--slug "$${slug:-omniroute}" \
		--client-id "$${slug:-omniroute}" \
		--name "OmniRoute Gateway" \
		--env-prefix GATEWAY_OIDC_ \
		--redirect-uri "https://$$host/oauth2/callback" \
		--redirect-uri "https://$$host/api/auth/oidc/callback" $(ARGS)

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
	@# Only where there is a .env — CI and a fresh clone legitimately have none, and
	@# a missing file is not drift. On a deployment it is the whole point: the check
	@# is what turns "a knob was added" into a line of output instead of a surprise.
	@if [ -f .env ]; then python3 scripts/env-sync.py; fi

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
	for overlay in compose.host-gateway.yml compose.gateway-sso.yml; do \
		if docker compose --env-file "$$tmp_env" -f docker-compose.yml -f "$$overlay" config --quiet 2>/dev/null; then \
			echo "compose config: ok (docker-compose.yml + $$overlay)"; \
		else \
			echo "compose config: FAILED ($$overlay)" >&2; status=1; \
		fi; \
	done; \
	rm -f "$$tmp_env"; \
	exit $$status

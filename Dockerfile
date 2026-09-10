# Olympus — The AI Software Factory (FactoryOps)
# Runtime: Node 20 + Bun + Python 3.11 + git/curl. The factory itself (Archon +
# OmniRoute routing; Hermes 3 + Qwen via gateway) is zero-local-GPU by design —
# heavy inference is behind OMNIROUTE_BASE_URL (default http://omniroute:20128).
# This image runs the Telegram surface + the manufacture loop; builds/ is a
# bind-mount (gitignored) so generated apps survive restarts.

FROM node:20-bookworm-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# System deps: python3 + git for setup.sh cloning, curl for OmniRoute health.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      git curl ca-certificates tini bash \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && python3 -m pip install --no-cache-dir --upgrade pip --break-system-packages 2>/dev/null || python3 -m pip install --no-cache-dir --upgrade pip

# Bun (portable, matches local dev's bun check)
RUN curl -fsSL https://bun.sh/install | bash
ENV BUN_INSTALL=/root/.bun
ENV PATH=/root/.bun/bin:$PATH

WORKDIR /app

# App source (kept lean by .dockerignore — .env/.factory-env/builds excluded).
# Single COPY so the build doesn't fail when the repo has no root package.json
# (vendor deps live under core-modules/, populated by setup.sh at runtime).
COPY . .

# JS deps — root + each vendor module, only when a manifest exists.
RUN if [ -f package.json ]; then bun install 2>/dev/null || bun install --ignore-scripts 2>/dev/null || true; fi \
    && for d in core-modules/omniroute core-modules/archon core-modules/ai-software-factory; do \
         if [ -f "$d/package.json" ]; then (cd "$d" && bun install 2>/dev/null || bun install --ignore-scripts 2>/dev/null || true); fi; \
       done

# Optional Python deps at build time (gated — don't break image if absent)
RUN for req in core-modules/ai-software-factory/requirements.txt factory/requirements.txt requirements.txt; do \
      if [ -f "$req" ]; then python3 -m pip install --no-cache-dir -r "$req" 2>/dev/null || true; fi; \
    done

# Entrypoint helper that manufacture.sh / setup.sh already handle
RUN chmod +x scripts/*.sh setup.sh 2>/dev/null || true \
    && chmod +x scripts/manufacture.sh 2>/dev/null || true \
    && mkdir -p builds build-requests .archon/cache factory \
    && git config --global --add safe.directory /app 2>/dev/null || true

EXPOSE 20128

# Runtime volumes expected by compose: builds/ (factory output), .factory-env (token),
# and optionally build-requests for triggering manufactures without rebuild.
VOLUME ["/app/builds"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:20128/health >/dev/null 2>&1 || python3 factory/doctor.py 2>/dev/null | grep -qi "ready\|ok" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "scripts/docker-entrypoint.sh"]

# syntax=docker/dockerfile:1
# ═══════════════════════════════════════════════════════════════════════════
# Studio — vibe-coding web UI (Next.js, standalone output)
#
# Three stages: install deps, build, then ship only the traced runtime. The
# final image carries no toolchain, no source tree, and no dependency cache —
# just the server bundle and its node_modules.
#
# Build context is web/studio (see the `studio` service in docker-compose.yml).
# ═══════════════════════════════════════════════════════════════════════════

FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# STUDIO_STANDALONE=1 turns on `output: "standalone"` in next.config.ts. It is a
# build-only switch — `next start` does not support standalone output, so the
# flag stays out of the runtime environment.
RUN STUDIO_STANDALONE=1 npm run build

FROM node:22-bookworm-slim AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3001 \
    HOSTNAME=0.0.0.0

RUN groupadd --system --gid 1001 nodejs \
    && useradd --system --uid 1001 --gid nodejs nextjs

# Next's standalone output is a self-contained server bundle.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static

# The saved-app library (STUDIO_DATA_DIR). Created here, owned by the runtime
# user, because a fresh named volume inherits the ownership of the image path it
# mounts over — without this the volume is root-owned and every save answers
# 500. `docker run` without a volume still works; the data is just ephemeral.
RUN mkdir -p /app/data/studio && chown -R nextjs:nodejs /app/data

USER nextjs
EXPOSE 3001

# No curl in the slim image — probe with node itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3001)+'/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]

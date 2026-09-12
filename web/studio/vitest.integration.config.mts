import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));

/**
 * Integration run: drives a real Authentik (and optionally the real gateway).
 *
 * Kept apart from `vitest.config.mts` so the default `npm test` can never start
 * signing in to a live identity provider. Every test self-skips unless the
 * STUDIO_E2E_* opt-in variables are set.
 */
export default defineConfig({
  resolve: {
    alias: { "@": root.replace(/\/$/, "") },
  },
  test: {
    environment: "node",
    include: ["tests/integration/**/*.test.ts"],
    // A real sign-in plus an optional model round-trip is minutes, not seconds.
    testTimeout: 240_000,
    hookTimeout: 120_000,
    // These share one login, so they must not run concurrently.
    fileParallelism: false,
  },
});

import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  resolve: {
    alias: { "@": root.replace(/\/$/, "") },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    // Integration tests talk to a live Authentik and are opt-in; keep them out
    // of the default run so `npm test` never performs a real login.
    exclude: ["**/node_modules/**", "**/.next/**", "tests/integration/**"],
    // The auth flow tests stand up a local HTTP server and use real fetch.
    testTimeout: 20_000,
  },
});

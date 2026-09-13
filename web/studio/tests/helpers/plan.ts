import { parsePlanObject, type BuildPlan } from "@/lib/plan";

/**
 * A confirmed plan, for tests that exercise a route past the planning step.
 *
 * `/api/generate` requires a plan, because the whole point of the flow is that
 * nothing is generated before a stack has been confirmed. Tests that are about
 * something else — auth, rate limiting, streaming, header handling — still have to
 * get past that gate, so they send this one.
 */
export const TEST_PLAN: BuildPlan = parsePlanObject(
  {
    name: "Test App",
    summary: "A plan the route tests use to reach the generation turn.",
    runtime: { language: "node", frameworks: ["react"], database: "sqlite" },
    run: { install: "npm install", build: "npm run build", start: "npm start", port: 3000 },
    files: [{ path: "package.json", purpose: "Dependencies and scripts" }],
  },
  "app",
);

/**
 * A generate-route body with a plan merged in.
 *
 * Accepts the JSON *string* the existing tests pass around, because that is what
 * they assert on: a malformed body must still reach the handler as malformed, so
 * anything that is not parseable JSON is returned untouched.
 */
export function withPlan(payload: unknown): string {
  if (typeof payload !== "string") {
    return JSON.stringify({ plan: TEST_PLAN, ...(payload as Record<string, unknown>) });
  }

  try {
    const parsed = JSON.parse(payload) as unknown;
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return payload;
    return JSON.stringify({ plan: TEST_PLAN, ...(parsed as Record<string, unknown>) });
  } catch {
    return payload;
  }
}

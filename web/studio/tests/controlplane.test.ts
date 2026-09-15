import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ControlPlaneError,
  checkTurnQuota,
  controlPlaneEnabled,
  provisionIdentity,
  readControlPlaneConfig,
  recordAudit,
  reportTurnUsage,
} from "@/lib/controlplane";
import { resetCallerCache } from "@/lib/identities";
import { beginTurn, finishTurn } from "@/lib/tenancy";
import type { Gate } from "@/lib/identities";
import { startFakeControlPlane, type FakeControlPlane } from "./helpers/mock-control-plane";

/**
 * The tenancy layer is a conversation with another service, so these tests speak
 * to a real local HTTP server (tests/helpers/mock-control-plane.ts) rather than
 * stubbing `fetch`: what matters is the request Studio actually sends — which
 * header carries which credential.
 *
 * The behaviours pinned here are the ones with consequences:
 *   * the per-user key, never the shared `.env` key, is what a turn spends when a
 *     control plane is configured;
 *   * a quota denial is a 429 *before* the gateway is called;
 *   * a control plane that cannot answer does not stop a user from building
 *     (fail-open), while one that cannot *identify* them does (strict);
 *   * a gateway key never appears in a response.
 */

const GATE: Gate = { ok: true, session: { sub: "sub-1", email: "dev@example.com" } };

let plane: FakeControlPlane | null = null;

beforeEach(() => {
  resetCallerCache();
  // Empty rather than deleted: the repo env loader fills unset keys, so a
  // deleted one could come back from a developer's `.env`.
  process.env.CONTROL_PLANE_INTERNAL_URL = "";
  process.env.CONTROL_INTERNAL_TOKEN = "";
  process.env.OMNIROUTE_API_KEY = "";
});

afterEach(async () => {
  if (plane) await plane.close();
  plane = null;
  resetCallerCache();
});

async function enablePlane(): Promise<FakeControlPlane> {
  plane = await startFakeControlPlane();
  process.env.CONTROL_PLANE_INTERNAL_URL = plane.url;
  process.env.CONTROL_INTERNAL_TOKEN = "internal-token";
  return plane;
}

describe("configuration", () => {
  it("is inert with no URL or a placeholder token", () => {
    expect(readControlPlaneConfig()).toBeNull();
    expect(controlPlaneEnabled()).toBe(false);

    process.env.CONTROL_PLANE_INTERNAL_URL = "http://127.0.0.1:20140/";
    // `.env.example` ships a `change-me…` placeholder; reading it as a credential
    // would make a fresh checkout answer 401 to everyone.
    process.env.CONTROL_INTERNAL_TOKEN = "change-me-internal-token";
    expect(readControlPlaneConfig()).toBeNull();
  });

  it("trims the trailing slash off the URL", () => {
    process.env.CONTROL_PLANE_INTERNAL_URL = "http://127.0.0.1:20140/";
    process.env.CONTROL_INTERNAL_TOKEN = "internal-token";

    expect(readControlPlaneConfig()).toEqual({ url: "http://127.0.0.1:20140", token: "internal-token" });
  });
});

describe("control-plane calls", () => {
  it("provisions an identity and carries the service token", async () => {
    const fake = await enablePlane();
    const config = readControlPlaneConfig()!;

    const identity = await provisionIdentity(config, { sub: "sub-1", email: "Dev@Example.com" });

    expect(identity).toMatchObject({
      userId: "cp-user-1",
      sub: "sub-1",
      email: "dev@example.com",
      gatewayKey: "sk-per-user",
      created: true,
    });
    expect(identity.quota).toEqual({
      plan: "pro",
      requestsPerDay: 100,
      tokensPerDay: null,
      spendCapUsd: 5,
    });

    const call = fake.identityCalls[0];
    expect(call.headers["x-control-internal-token"]).toBe("internal-token");
    expect(call.body).toMatchObject({ sub: "sub-1", email: "Dev@Example.com" });
  });

  it("surfaces a conflict with the plane's own status", async () => {
    const fake = await enablePlane();
    fake.identityStatus = 409;
    fake.identityError = "this email is already linked to a different identity";

    const error = await provisionIdentity(readControlPlaneConfig()!, {
      sub: "sub-1",
      email: "dev@example.com",
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ControlPlaneError);
    expect((error as ControlPlaneError).status).toBe(409);
    expect((error as ControlPlaneError).message).toContain("already linked");
  });

  it("identifies the account by the user's gateway key", async () => {
    const fake = await enablePlane();

    const decision = await checkTurnQuota(readControlPlaneConfig()!, "sk-per-user");

    expect(decision).toEqual({ allowed: true, reasons: [] });
    expect(fake.quotaHeaders[0].authorization).toBe("Bearer sk-per-user");
  });

  it("swallows a failed usage report and a failed audit row", async () => {
    process.env.CONTROL_PLANE_INTERNAL_URL = "http://127.0.0.1:1";
    const config = readControlPlaneConfig()!;

    await expect(
      reportTurnUsage(config, "sk-per-user", { tokensIn: 1, tokensOut: 2, requests: 1 }),
    ).resolves.toBeUndefined();
    await expect(
      recordAudit(config, { action: "build.publish", sub: "sub-1" }),
    ).resolves.toBeUndefined();
  });
});

describe("beginTurn", () => {
  it("spends the shared key when no control plane is configured", async () => {
    process.env.OMNIROUTE_API_KEY = "sk-shared";

    const started = await beginTurn(GATE);

    expect(started.ok).toBe(true);
    if (!started.ok) return;
    expect(started.turn.caller).toBeNull();
    expect(started.turn.config.apiKey).toBe("sk-shared");
  });

  it("refuses a turn with a placeholder shared key and no control plane", async () => {
    process.env.OMNIROUTE_API_KEY = "change-me-omniroute-api-key";

    const started = await beginTurn(GATE);

    expect(started.ok).toBe(false);
    if (started.ok) return;
    expect(started.response.status).toBe(503);
  });

  it("spends the user's own key, never the shared one", async () => {
    await enablePlane();
    process.env.OMNIROUTE_API_KEY = "sk-shared";

    const started = await beginTurn(GATE);

    expect(started.ok).toBe(true);
    if (!started.ok) return;
    expect(started.turn.config.apiKey).toBe("sk-per-user");
    expect(started.turn.caller?.namespace).toMatch(/^u-[0-9a-f]{32}$/);
  });

  it("refuses the turn before the gateway when the quota is exhausted", async () => {
    const fake = await enablePlane();
    fake.quotaAllowed = false;
    fake.quotaReasons = ["daily token limit reached"];

    const started = await beginTurn(GATE);

    expect(started.ok).toBe(false);
    if (started.ok) return;
    expect(started.response.status).toBe(429);
    await expect(started.response.json()).resolves.toMatchObject({
      error: expect.stringContaining("daily token limit reached"),
    });
  });

  it("fails open when the quota decision cannot be read", async () => {
    const fake = await enablePlane();
    fake.quotaStatus = 500;

    const started = await beginTurn(GATE);

    // The gateway key's own hard cap is the backstop; a read-only hiccup on the
    // tenancy service must not become an outage for the builder.
    expect(started.ok).toBe(true);
  });

  it("fails strictly when the account cannot be resolved", async () => {
    const fake = await enablePlane();
    fake.identityStatus = 502;
    fake.identityError = "gateway unreachable: connect ECONNREFUSED";

    const started = await beginTurn(GATE);

    expect(started.ok).toBe(false);
    if (started.ok) return;
    expect(started.response.status).toBe(503);
  });

  it("refuses an unidentifiable caller when a plane is configured", async () => {
    await enablePlane();

    const started = await beginTurn({ ok: true, session: null });

    expect(started.ok).toBe(false);
    if (started.ok) return;
    expect(started.response.status).toBe(401);
  });

  it("never puts the gateway key in a refusal body", async () => {
    const fake = await enablePlane();
    fake.quotaAllowed = false;
    fake.quotaReasons = ["daily token limit reached"];

    const started = await beginTurn(GATE);
    if (started.ok) throw new Error("expected a refusal");

    const text = await started.response.text();

    // The refusal is the exploitable surface: it goes to the browser, and the
    // key it was made with must not ride along with it.
    expect(text).toContain("daily token limit reached");
    expect(text).not.toContain("sk-per-user");
  });
});

describe("finishTurn", () => {
  it("reports usage against the user's own key", async () => {
    const fake = await enablePlane();
    const started = await beginTurn(GATE);
    if (!started.ok) throw new Error("expected a turn");

    await finishTurn(started.turn, { tokensIn: 12, tokensOut: 34, requests: 1, model: "auto/coding" });

    expect(fake.usage).toHaveLength(1);
    expect(fake.usage[0].headers.authorization).toBe("Bearer sk-per-user");
    expect(fake.usage[0].body).toMatchObject({
      tokensIn: 12,
      tokensOut: 34,
      requests: 1,
      model: "auto/coding",
    });
  });

  it("reports nothing in single-operator mode", async () => {
    process.env.OMNIROUTE_API_KEY = "sk-shared";
    const started = await beginTurn(GATE);
    if (!started.ok) throw new Error("expected a turn");

    await finishTurn(started.turn, { tokensIn: 1, tokensOut: 1, requests: 1 });

    expect(plane).toBeNull();
  });
});

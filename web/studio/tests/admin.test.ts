import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { classifyGatewayUrl, collectStatus, listBuilds, worstState, type AdminCheck } from "@/lib/admin";
import { resetModelCache } from "@/lib/omniroute";

// The repo .env loader would otherwise put OIDC settings back after a test deletes
// them, on any machine whose checkout carries credentials. See models-route.test.ts.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const ROOT = join(process.cwd(), "tests", ".tmp", "admin");
const QUEUE = join(ROOT, "build-queue");
const BUILDS = join(ROOT, "builds");

const MANAGED = [
  "OMNIROUTE_API_KEY",
  "OMNIROUTE_BASE_URL",
  "OMNIROUTE_MODEL",
  "STUDIO_ACCESS_TOKEN",
  "STUDIO_BUILD_QUEUE_DIR",
  "STUDIO_BUILDS_DIR",
  "STUDIO_DATA_DIR",
  "SITE_HOST_SUFFIX",
  "CONTROL_PLANE_INTERNAL_URL",
  "CONTROL_INTERNAL_TOKEN",
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_ALLOWED_GROUPS",
  "OLYMPUS_ADMIN_GROUPS",
] as const;

const CATALOG = {
  data: [
    { id: "auto/coding", owned_by: "combo" },
    { id: "anthropic/claude-3-5", owned_by: "anthropic", context_length: 200000 },
    { id: "openai/gpt-4o", owned_by: "openai", context_length: 128000 },
  ],
};

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }

  rmSync(ROOT, { recursive: true, force: true });
  mkdirSync(QUEUE, { recursive: true });
  mkdirSync(BUILDS, { recursive: true });

  process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";
  process.env.OMNIROUTE_BASE_URL = "http://192.168.1.46:20129/v1";
  process.env.STUDIO_BUILD_QUEUE_DIR = QUEUE;
  process.env.STUDIO_BUILDS_DIR = BUILDS;

  resetModelCache();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(ROOT, { recursive: true, force: true });
  vi.unstubAllGlobals();
  resetModelCache();
});

function stubCatalog(payload: unknown = CATALOG, status = 200): ReturnType<typeof vi.fn> {
  const mock = vi.fn(async () => Response.json(payload, { status }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** The heartbeat the runner service writes. `ageSeconds` decides whether it is alive. */
function beat(ageSeconds = 3, extra: Record<string, unknown> = {}): void {
  writeFileSync(
    join(QUEUE, "runner.heartbeat.json"),
    JSON.stringify({
      v: 1,
      pid: 4242,
      host: "olympus-host",
      beat_at: new Date(Date.now() - ageSeconds * 1000).toISOString(),
      busy_with: null,
      ...extra,
    }),
  );
}

function status(job: string, fields: Record<string, unknown>): void {
  writeFileSync(
    join(QUEUE, `${job}.status.json`),
    JSON.stringify({ v: 1, job, updated_at: new Date().toISOString(), ...fields }),
  );
}

function build(slug: string, artefacts: string[] = []): void {
  const dir = join(BUILDS, slug);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "index.html"), "<!doctype html>");
  for (const name of artefacts) writeFileSync(join(dir, name), "x");
}

function check(checks: AdminCheck[], id: string): AdminCheck {
  const found = checks.find((entry) => entry.id === id);
  if (!found) throw new Error(`no check with id ${id}`);
  return found;
}

describe("which gateway address is the door", () => {
  it("accepts the SSO proxy in front of the gateway", () => {
    const { door, detail } = classifyGatewayUrl("http://192.168.1.46:20129/v1");
    expect(door).toBe(true);
    expect(detail).toContain("20129");
  });

  it("accepts the same proxy reached over loopback on the gateway's own host", () => {
    expect(classifyGatewayUrl("http://127.0.0.1:20129/v1").door).toBe(true);
  });

  it("refuses the gateway's own port, because nothing off that host can dial it", () => {
    const { door, detail } = classifyGatewayUrl("http://192.168.1.46:20128/v1");
    expect(door).toBe(false);
    expect(detail).toMatch(/loopback and bridge alone/i);
  });

  it("says out loud that loopback on the gateway's port is this container", () => {
    // The failure this panel exists to name: it compiles, it resolves, and the
    // gateway log stays empty because the request never left the container.
    const { door, detail } = classifyGatewayUrl("http://127.0.0.1:20128/v1");
    expect(door).toBe(false);
    expect(detail).toMatch(/this container's own loopback/i);
  });

  it("refuses anything that is neither port", () => {
    expect(classifyGatewayUrl("http://192.168.1.46:8080/v1").door).toBe(false);
    expect(classifyGatewayUrl("https://gateway.example.com/v1").door).toBe(false);
  });

  it("refuses a value a URL parser cannot read", () => {
    expect(classifyGatewayUrl("not a url").door).toBe(false);
    expect(classifyGatewayUrl("").door).toBe(false);
  });
});

describe("what is on disk", () => {
  it("lists build directories and ignores files and dot-directories", () => {
    build("weight-tracker", ["site.zip", "plan.json"]);
    build("rota", ["project.zip"]);
    build(".hidden");
    writeFileSync(join(BUILDS, "README.md"), "not a build");

    // Order-independent: two directories written in the same millisecond have no
    // meaningful "newest" between them, and this is not the assertion to flake on.
    const slugs = listBuilds(BUILDS)
      .map((entry) => entry.slug)
      .sort();
    expect(slugs).toEqual(["rota", "weight-tracker"]);
  });

  it("says which artefacts a build actually produced", () => {
    // The difference between "the agent wrote files" and "something can be served"
    // is the whole reason a build is worth looking at.
    build("weight-tracker", ["site.zip", "plan.json"]);
    build("rota", ["project.zip"]);
    build("half-done");

    const bySlug = new Map(listBuilds(BUILDS).map((entry) => [entry.slug, entry]));
    expect(bySlug.get("weight-tracker")).toMatchObject({ site: true, project: false, plan: true });
    expect(bySlug.get("rota")).toMatchObject({ site: false, project: true, plan: false });
    expect(bySlug.get("half-done")).toMatchObject({ site: false, project: false, plan: false });
  });

  it("reports the size of a build rather than asking the host", () => {
    build("weight-tracker", ["site.zip"]);
    expect(listBuilds(BUILDS)[0].bytes).toBeGreaterThan(0);
  });

  it("treats a directory it cannot read as no builds, not as an error", () => {
    expect(listBuilds(join(ROOT, "nope"))).toEqual([]);
  });
});

describe("the collected panel", () => {
  it("reports a deployment where the next build will work", async () => {
    stubCatalog();
    beat();
    status("a".repeat(16), { state: "running", action: "build", slug: "weight-tracker" });
    status("b".repeat(16), { state: "succeeded", action: "build", slug: "rota", published_url: "https://rota.example" });
    build("weight-tracker", ["site.zip"]);

    const state = await collectStatus(null);

    expect(state.gateway.door).toBe(true);
    expect(state.gateway.probe).toMatchObject({ ok: true, models: 3, providers: 3, combos: 1 });
    expect(state.gateway.resolvedModel).toBe("auto/coding");
    expect(state.runner).toMatchObject({ live: true, host: "olympus-host", pid: 4242 });
    expect(state.queue).toMatchObject({ running: 1, finished: 1 });
    expect(state.queue.recent[0].state).toBe("running");
    expect(state.builds.map((entry) => entry.slug)).toEqual(["weight-tracker"]);

    expect(check(state.checks, "gateway-door").state).toBe("ok");
    expect(check(state.checks, "gateway-reachable").state).toBe("ok");
    expect(check(state.checks, "gateway-key").state).toBe("ok");
    expect(check(state.checks, "runner").state).toBe("ok");
    expect(check(state.checks, "model").state).toBe("ok");
    expect(check(state.checks, "builds").state).toBe("ok");
    // The one thing left undone on an otherwise healthy deployment, and the panel
    // is expected to keep saying so until it is configured.
    expect(check(state.checks, "tenancy").state).toBe("warn");
    expect(worstState(state.checks)).toBe("warn");
  });

  it("fails the door check and names the container's own loopback", async () => {
    process.env.OMNIROUTE_BASE_URL = "http://127.0.0.1:20128/v1";
    stubCatalog();
    beat();

    const state = await collectStatus(null);

    expect(state.gateway.door).toBe(false);
    expect(check(state.checks, "gateway-door").state).toBe("fail");
    expect(check(state.checks, "gateway-door").hint).toMatch(/20129/);
    expect(worstState(state.checks)).toBe("fail");
  });

  it("reports a gateway that does not answer without claiming a key problem", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connect ECONNREFUSED 192.168.1.46:20129");
      }),
    );
    beat();

    const state = await collectStatus(null);

    expect(state.gateway.probe.ok).toBe(false);
    expect(state.gateway.probe.error).toMatch(/could not reach/i);
    expect(check(state.checks, "gateway-reachable").state).toBe("fail");
    expect(check(state.checks, "gateway-key").state).toBe("ok");
    // Nothing can be said about model linking while the catalogue is unreadable.
    expect(check(state.checks, "model").state).toBe("warn");
    expect(state.gateway.resolvedModel).toBeNull();
  });

  it("reports a rejected key as a credential problem", async () => {
    stubCatalog({ error: "unauthorized" }, 401);
    beat();

    const state = await collectStatus(null);

    expect(state.gateway.probe.ok).toBe(false);
    expect(check(state.checks, "gateway-reachable").detail).toMatch(/401|rejected/i);
  });

  it("does not even call the gateway when no key is configured", async () => {
    process.env.OMNIROUTE_API_KEY = "";
    const mock = stubCatalog();
    beat();

    const state = await collectStatus(null);

    expect(mock).not.toHaveBeenCalled();
    expect(check(state.checks, "gateway-key").state).toBe("fail");
    expect(check(state.checks, "gateway-reachable").detail).toMatch(/no gateway key/i);
  });

  it("treats a placeholder key as no key", async () => {
    process.env.OMNIROUTE_API_KEY = "change-me";
    const state = await collectStatus(null);
    expect(state.gateway.keyConfigured).toBe(false);
  });

  it("reports a stale runner with the command that starts one", async () => {
    stubCatalog();
    beat(10 * 60);

    const state = await collectStatus(null);

    expect(state.runner.live).toBe(false);
    expect(state.runner.ageSeconds).toBeGreaterThan(60);
    const runner = check(state.checks, "runner");
    expect(runner.state).toBe("warn");
    expect(runner.hint).toMatch(/install-build-runner/);
  });

  it("reports a runner that has never been seen", async () => {
    stubCatalog();
    const state = await collectStatus(null);
    expect(state.runner.ageSeconds).toBeNull();
    expect(check(state.checks, "runner").detail).toMatch(/no heartbeat/i);
  });

  it("warns when the configured model is no longer linked in", async () => {
    stubCatalog();
    process.env.OMNIROUTE_MODEL = "openai/gpt-5-that-was-unlinked";

    const state = await collectStatus(null);

    const model = check(state.checks, "model");
    expect(model.state).toBe("warn");
    expect(model.detail).toMatch(/no longer linked in/i);
    // The fallback is OmniRoute's own router, which survives a provider going away.
    expect(state.gateway.resolvedModel).toBe("auto/coding");
  });

  it("counts what the queue is doing rather than what it was asked to do", async () => {
    stubCatalog();
    beat();
    status("a".repeat(16), { state: "running", action: "build" });
    status("b".repeat(16), { state: "failed", action: "build" });
    status("c".repeat(16), { state: "cancelled", action: "publish" });

    const state = await collectStatus(null);

    expect(state.queue).toMatchObject({ running: 1, finished: 2 });
  });

  it("ignores a queue file that is not a job", async () => {
    stubCatalog();
    writeFileSync(join(QUEUE, "not-a-job.status.json"), JSON.stringify({ job: "nope" }));
    writeFileSync(join(QUEUE, "broken.status.json"), "{");

    await expect(collectStatus(null)).resolves.toBeDefined();
  });

  it("passes the session through as the viewer, and never as a secret", async () => {
    stubCatalog();

    const state = await collectStatus({ sub: "user-1", email: "operator@example.com" });

    expect(state.auth.viewer).toBe("operator@example.com");
    expect(JSON.stringify(state)).not.toContain("sk-valid-looking-key");
  });

  it("reports per-user keys as configured when a control plane is set up", async () => {
    stubCatalog();
    process.env.CONTROL_PLANE_INTERNAL_URL = "http://192.168.1.46:8300";
    process.env.CONTROL_INTERNAL_TOKEN = "internal-secret";

    const state = await collectStatus(null);

    expect(state.tenancy).toEqual({ controlPlane: true, internalToken: true });
    expect(check(state.checks, "tenancy").state).toBe("ok");
    expect(JSON.stringify(state)).not.toContain("internal-secret");
  });
});

describe("who may see the panel", () => {
  it("says so when OIDC is off and the panel is therefore open", async () => {
    stubCatalog();
    const state = await collectStatus(null);
    expect(state.auth).toMatchObject({ oidc: false, adminOpen: true });
    expect(check(state.checks, "admin-policy").state).toBe("warn");
  });

  it("says so when OIDC is on but no admin group is set", async () => {
    stubCatalog();
    process.env.OIDC_ISSUER_URL = "https://auth.cerulean.innotel.us/application/o/studio/";
    process.env.OIDC_CLIENT_ID = "studio";
    process.env.OIDC_CLIENT_SECRET = "not-a-placeholder";

    const state = await collectStatus(null);

    expect(state.auth).toMatchObject({ oidc: true, adminOpen: true, adminGroups: [] });
    expect(check(state.checks, "admin-policy").detail).toMatch(/OLYMPUS_ADMIN_GROUPS is empty/);
  });

  it("reports the groups when the panel is restricted", async () => {
    stubCatalog();
    process.env.OIDC_ISSUER_URL = "https://auth.cerulean.innotel.us/application/o/studio/";
    process.env.OIDC_CLIENT_ID = "studio";
    process.env.OIDC_CLIENT_SECRET = "not-a-placeholder";
    process.env.OLYMPUS_ADMIN_GROUPS = "authentik Admins, olympus-operators";

    const state = await collectStatus(null);

    expect(state.auth.adminGroups).toEqual(["authentik Admins", "olympus-operators"]);
    expect(check(state.checks, "admin-policy").state).toBe("ok");
  });
});

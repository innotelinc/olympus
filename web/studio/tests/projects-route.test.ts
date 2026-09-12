import { rmSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The routes reach the shared auth gate, which calls loadRepoEnv(). Left real, a
// configured checkout would turn OIDC on before the assertions run — the same
// trap route.test.ts documents. The store's location is overridden below.
vi.mock("@/lib/env", () => ({
  loadRepoEnv: () => {},
  repoRoot: () => process.cwd(),
}));

import { GET as listApps, POST as saveApp } from "@/app/api/projects/route";
import { DELETE as deleteApp, GET as getApp } from "@/app/api/projects/[id]/route";
import { SESSION_COOKIE, readAuthConfig, signSession } from "@/lib/auth";

const TMP = join(process.cwd(), "tests", ".tmp", "projects-route");

const MANAGED = [
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_ALLOWED_GROUPS",
  "OIDC_SCOPES",
  "STUDIO_SESSION_SECRET",
  "STUDIO_ACCESS_TOKEN",
  "STUDIO_DATA_DIR",
];

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  rmSync(TMP, { recursive: true, force: true });
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  process.env.STUDIO_DATA_DIR = TMP;
});

afterEach(() => {
  rmSync(TMP, { recursive: true, force: true });
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

function enableOidc(): void {
  process.env.OIDC_ISSUER_URL = "http://idp.test/application/o/studio/";
  process.env.OIDC_CLIENT_ID = "studio";
  process.env.OIDC_CLIENT_SECRET = "not-a-placeholder-secret";
}

/** A signed session cookie for a subject, as the callback route would issue. */
function cookieFor(sub: string): string {
  const config = readAuthConfig();
  if (!config) throw new Error("OIDC is not configured for this test");
  return `${SESSION_COOKIE}=${signSession({ sub }, config)}`;
}

function request(method: string, body?: unknown, headers: Record<string, string> = {}): Request {
  return new Request("http://studio.test/api/projects", {
    method,
    headers: {
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function context(id: string) {
  return { params: Promise.resolve({ id }) };
}

const app = (title: string) => ({
  title,
  prompt: `build ${title}`,
  files: [{ path: "index.html", contents: `<h1>${title}</h1>` }],
});

describe("access control", () => {
  it("requires a session when OIDC is configured", async () => {
    enableOidc();

    const listed = await listApps(request("GET"));
    expect(listed.status).toBe(401);
    expect((await listed.json()).error).toMatch(/sign in/i);

    const created = await saveApp(request("POST", app("blocked")));
    expect(created.status).toBe(401);
  });

  it("requires the access token when STUDIO_ACCESS_TOKEN is set", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";

    expect((await listApps(request("GET"))).status).toBe(401);
    expect((await listApps(request("GET", undefined, { "x-studio-token": "wrong" }))).status).toBe(401);

    const allowed = await listApps(request("GET", undefined, { "x-studio-token": "shared-secret" }));
    expect(allowed.status).toBe(200);
    expect((await allowed.json()).projects).toEqual([]);
  });

  it("keeps each identity's library separate", async () => {
    enableOidc();

    const created = await saveApp(
      request("POST", app("Mine"), { cookie: cookieFor("user-a") }),
    );
    expect(created.status).toBe(201);
    const { project } = await created.json();

    const theirs = await listApps(request("GET", undefined, { cookie: cookieFor("user-b") }));
    expect((await theirs.json()).projects).toEqual([]);

    const stolen = await getApp(
      request("GET", undefined, { cookie: cookieFor("user-b") }),
      context(project.id),
    );
    expect(stolen.status).toBe(404);

    const mine = await listApps(request("GET", undefined, { cookie: cookieFor("user-a") }));
    expect((await mine.json()).projects).toHaveLength(1);
  });
});

describe("library round-trip", () => {
  it("saves, lists, opens and deletes an app", async () => {
    const created = await saveApp(request("POST", app("Kanban")));
    expect(created.status).toBe(201);

    const saved = await created.json();
    expect(saved.created).toBe(true);
    expect(saved.project.title).toBe("Kanban");

    const listed = await listApps(request("GET"));
    const { projects } = await listed.json();
    expect(projects).toHaveLength(1);
    expect(projects[0]).toMatchObject({ id: saved.project.id, title: "Kanban", fileCount: 1 });

    const opened = await getApp(request("GET"), context(saved.project.id));
    expect(opened.status).toBe(200);
    const openedBody = await opened.json();
    expect(openedBody.project.files).toEqual([{ path: "index.html", contents: "<h1>Kanban</h1>" }]);
    expect(openedBody.project.prompt).toBe("build Kanban");

    const removed = await deleteApp(request("DELETE"), context(saved.project.id));
    expect(removed.status).toBe(200);

    expect((await getApp(request("GET"), context(saved.project.id))).status).toBe(404);
    expect(await (await listApps(request("GET"))).json()).toEqual({ projects: [] });
  });

  it("updates in place when the id is supplied", async () => {
    const first = await (await saveApp(request("POST", app("Draft")))).json();

    const revised = await saveApp(
      request("POST", {
        id: first.project.id,
        title: "Draft v2",
        files: [{ path: "index.html", contents: "v2" }],
      }),
    );

    expect(revised.status).toBe(200);
    const body = await revised.json();
    expect(body.created).toBe(false);
    expect(body.project.id).toBe(first.project.id);
    expect(body.project.title).toBe("Draft v2");
    expect((await (await listApps(request("GET"))).json()).projects).toHaveLength(1);
  });

  it("answers 404 for an unknown or hostile id", async () => {
    expect((await getApp(request("GET"), context("abcdefghij"))).status).toBe(404);
    expect((await deleteApp(request("DELETE"), context("abcdefghij"))).status).toBe(404);

    // A traversing id is not a valid id at all — it must never reach a path join.
    expect((await getApp(request("GET"), context("../../etc/passwd"))).status).toBe(404);
    expect((await deleteApp(request("DELETE"), context("../decoy"))).status).toBe(404);
  });
});

describe("validation", () => {
  it("rejects a malformed body", async () => {
    const response = await saveApp(
      new Request("http://studio.test/api/projects", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "not json",
      }),
    );

    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatch(/must be JSON/i);
  });

  it("rejects an app that is too large to save", async () => {
    const response = await saveApp(
      request("POST", {
        title: "Huge",
        files: [{ path: "index.html", contents: "x".repeat(400_000) }],
      }),
    );

    expect(response.status).toBe(413);
    expect((await response.json()).error).toMatch(/too large/i);
  });

  it("answers no-store so an edge never caches one identity's library", async () => {
    const listed = await listApps(request("GET"));
    expect(listed.headers.get("cache-control")).toContain("no-store");

    const created = await saveApp(request("POST", app("Cached?")));
    expect(created.headers.get("cache-control")).toContain("no-store");
  });
});

import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/projects/[id]/export/route";
import { ANONYMOUS_NAMESPACE, type Project, saveProject } from "@/lib/projects";

// Both routes reach for the repo .env through loadRepoEnv(); the loader is
// mocked so a real .env on this machine cannot decide an assertion, and the
// OIDC keys are cleared so the gate is the access token alone (session null →
// the single-operator library).
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const ROOT = join(process.cwd(), "tests", ".tmp", "export-route");
const DATA = join(ROOT, "data");
const REQUESTS = join(ROOT, "build-requests");
const NOT_A_DIR = join(ROOT, "not-a-directory");

const MANAGED = [
  "STUDIO_DATA_DIR",
  "STUDIO_FACTORY_REQUESTS_DIR",
  "STUDIO_ACCESS_TOKEN",
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_SCOPES",
  "OIDC_ALLOWED_GROUPS",
  "STUDIO_SESSION_SECRET",
] as const;

let saved: Record<string, string | undefined> = {};

const TOKEN = { "x-studio-token": "shared-secret" } as const;

function context(id: string): { params: Promise<{ id: string }> } {
  return { params: Promise.resolve({ id }) };
}

function seed(overrides: Record<string, unknown> = {}): Project {
  const { project } = saveProject(ANONYMOUS_NAMESPACE, {
    title: "Markdown Notes",
    prompt: "A markdown notes app with a live preview pane.",
    files: [
      { path: "index.html", contents: "<!doctype html>\n<h1>Notes</h1>\n" },
      { path: "app.js", contents: "console.log('notes');\n" },
    ],
    ...overrides,
  });
  return project;
}

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }

  rmSync(ROOT, { recursive: true, force: true });
  mkdirSync(DATA, { recursive: true });
  mkdirSync(REQUESTS, { recursive: true });

  process.env.STUDIO_DATA_DIR = DATA;
  process.env.STUDIO_FACTORY_REQUESTS_DIR = REQUESTS;
  process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(ROOT, { recursive: true, force: true });
});

/* ---- GET — the download fallback ---------------------------------------- */

describe("GET /api/projects/[id]/export", () => {
  it("returns the spec as a markdown attachment", async () => {
    const project = seed();
    const response = await GET(
      new Request(`http://studio.test/api/projects/${project.id}/export`, { headers: TOKEN }),
      context(project.id),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/markdown; charset=utf-8");
    expect(response.headers.get("content-disposition")).toBe(
      'attachment; filename="markdown-notes.md"',
    );
    expect(response.headers.get("cache-control")).toBe("no-store");

    const body = await response.text();
    expect(body.startsWith("# Application Specification: Markdown Notes")).toBe(true);
    for (const heading of [
      "## 🎯 Core Purpose",
      "## 🧰 Tech Stack",
      "## 🛠️ Key Features & Pages",
      "## 🚦 Verification Criteria",
    ]) {
      expect(body).toContain(heading);
    }
  });

  it("404s an app that is not in this library", async () => {
    const response = await GET(
      new Request("http://studio.test/api/projects/nosuchapp1234/export", { headers: TOKEN }),
      context("nosuchapp1234"),
    );
    expect(response.status).toBe(404);
  });

  it("is gated like every other route", async () => {
    const project = seed();

    const denied = await GET(
      new Request(`http://studio.test/api/projects/${project.id}/export`),
      context(project.id),
    );
    expect(denied.status).toBe(401);

    const allowed = await GET(
      new Request(`http://studio.test/api/projects/${project.id}/export`, { headers: TOKEN }),
      context(project.id),
    );
    expect(allowed.status).toBe(200);
  });

  it("still works where build-requests/ cannot be written", async () => {
    const project = seed();
    writeFileSync(NOT_A_DIR, "not a directory\n");
    process.env.STUDIO_FACTORY_REQUESTS_DIR = NOT_A_DIR;

    // The whole point of the download: the handoff survives a deployment that
    // never mounted the directory.
    const response = await GET(
      new Request(`http://studio.test/api/projects/${project.id}/export`, { headers: TOKEN }),
      context(project.id),
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("Markdown Notes");
  });
});

/* ---- POST — the write --------------------------------------------------- */

describe("POST /api/projects/[id]/export", () => {
  function post(id: string, body?: unknown, headers: Record<string, string> = TOKEN) {
    return POST(
      new Request(`http://studio.test/api/projects/${id}/export`, {
        method: "POST",
        headers: body === undefined ? { ...headers } : { "content-type": "application/json", ...headers },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      context(id),
    );
  }

  it("writes the spec into build-requests/ and says what to do next", async () => {
    const project = seed();
    const response = await post(project.id);

    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toBe("no-store");

    const payload = (await response.json()) as {
      filename: string;
      path: string;
      bytes: number;
      replaced: boolean;
      next: string;
    };

    expect(payload.filename).toBe("markdown-notes.md");
    expect(payload.replaced).toBe(false);
    expect(payload.next).toBe("make app SPEC=build-requests/markdown-notes.md");
    expect(readdirSync(REQUESTS)).toEqual(["markdown-notes.md"]);

    // What landed on disk is the spec, not a stub or a partial write.
    const written = readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8");
    expect(written).toContain("# Application Specification: Markdown Notes");
    expect(written).toContain("A markdown notes app with a live preview pane.");
    expect(payload.bytes).toBe(Buffer.byteLength(written, "utf8"));
  });

  it("takes no body at all, and treats a junk body as no body", async () => {
    const first = seed({ title: "First App" });
    expect((await post(first.id)).status).toBe(201);

    const second = seed({ title: "Second App" });
    const response = POST(
      new Request(`http://studio.test/api/projects/${second.id}/export`, {
        method: "POST",
        headers: { "content-type": "application/json", ...TOKEN },
        body: "not json",
      }),
      context(second.id),
    );
    expect((await response).status).toBe(201);
  });

  it("refuses to clobber an existing spec until asked", async () => {
    const project = seed();
    await post(project.id);

    writeFileSync(join(REQUESTS, "markdown-notes.md"), "# hand-edited\n");

    const conflict = await post(project.id);
    expect(conflict.status).toBe(409);
    expect(((await conflict.json()) as { error: string }).error).toContain("already exists");
    expect(readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8")).toBe("# hand-edited\n");

    const replaced = await post(project.id, { overwrite: true });
    expect(replaced.status).toBe(200);
    expect(((await replaced.json()) as { replaced: boolean }).replaced).toBe(true);
    expect(readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8")).toContain(
      "# Application Specification",
    );
  });

  it("keeps a hostile title inside the directory", async () => {
    const project = seed({ title: "../../../../etc/passwd" });
    const response = await post(project.id);

    expect(response.status).toBe(201);
    expect(((await response.json()) as { filename: string }).filename).toBe("etc-passwd.md");
    expect(readdirSync(REQUESTS)).toEqual(["etc-passwd.md"]);
  });

  it("reports where to write instead of failing obscurely", async () => {
    const project = seed();
    writeFileSync(NOT_A_DIR, "not a directory\n");
    process.env.STUDIO_FACTORY_REQUESTS_DIR = NOT_A_DIR;

    const response = await post(project.id);
    expect(response.status).toBe(503);
    expect(((await response.json()) as { error: string }).error).toContain("chown 1001:1001");
  });

  it("404s an app that is not in this library", async () => {
    const response = await post("nosuchapp1234");
    expect(response.status).toBe(404);
    expect(readdirSync(REQUESTS)).toEqual([]);
  });

  it("is gated like every other route", async () => {
    const project = seed();
    const response = await post(project.id, undefined, {});
    expect(response.status).toBe(401);
    expect(readdirSync(REQUESTS)).toEqual([]);
  });
});

import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/projects/[id]/build/route";
import {
  RUNNER_STALE_SECONDS,
  type BuildStatus,
  buildQueueDir,
  latestBuildStatus,
  readBuildStatus,
  readRunnerState,
} from "@/lib/build-queue";
import { specSlug } from "@/lib/factory-spec";
import { ANONYMOUS_NAMESPACE, type Project, saveProject } from "@/lib/projects";

// The repo .env loader is mocked for the same reason the export tests mock it:
// a real .env on this machine must not be able to decide an assertion.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const ROOT = join(process.cwd(), "tests", ".tmp", "build-route");
const DATA = join(ROOT, "data");
const REQUESTS = join(ROOT, "build-requests");
const QUEUE = join(ROOT, "build-queue");

const MANAGED = [
  "STUDIO_DATA_DIR",
  "STUDIO_FACTORY_REQUESTS_DIR",
  "STUDIO_BUILD_QUEUE_DIR",
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
    files: [{ path: "index.html", contents: "<!doctype html>\n<h1>Notes</h1>\n" }],
    ...overrides,
  });
  return project;
}

/** A runner that is alive: the heartbeat the service writes while idle. */
function beat(overrides: Record<string, unknown> = {}): void {
  writeFileSync(
    join(QUEUE, "runner.heartbeat.json"),
    JSON.stringify({
      v: 1,
      pid: 4242,
      host: "test-host",
      repo: ROOT,
      started_at: new Date().toISOString(),
      beat_at: new Date().toISOString(),
      busy_with: null,
      ...overrides,
    }),
  );
}

function statusFile(job: string, fields: Record<string, unknown>): void {
  writeFileSync(
    join(QUEUE, `${job}.status.json`),
    JSON.stringify({ v: 1, job, updated_at: new Date().toISOString(), ...fields }),
  );
}

function post(id: string, body?: unknown, headers: Record<string, string> = TOKEN) {
  return POST(
    new Request(`http://studio.test/api/projects/${id}/build`, {
      method: "POST",
      headers: body === undefined ? { ...headers } : { "content-type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    context(id),
  );
}

function get(id: string, query = "", headers: Record<string, string> = TOKEN) {
  return GET(new Request(`http://studio.test/api/projects/${id}/build${query}`, { headers }), context(id));
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
  mkdirSync(QUEUE, { recursive: true });

  process.env.STUDIO_DATA_DIR = DATA;
  process.env.STUDIO_FACTORY_REQUESTS_DIR = REQUESTS;
  process.env.STUDIO_BUILD_QUEUE_DIR = QUEUE;
  process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(ROOT, { recursive: true, force: true });
});

/* ---- runner presence ---------------------------------------------------- */

describe("readRunnerState", () => {
  it("is not live when nothing has ever run", () => {
    expect(readRunnerState()).toMatchObject({ live: false, ageSeconds: null });
  });

  it("is live on a fresh heartbeat", () => {
    beat({ busy_with: "markdown-notes" });
    const state = readRunnerState();
    expect(state.live).toBe(true);
    expect(state.pid).toBe(4242);
    expect(state.host).toBe("test-host");
    expect(state.busyWith).toBe("markdown-notes");
  });

  it("goes stale once the heartbeat is old", () => {
    // A runner that died leaves its last beat behind; queueing against it would
    // park the build forever.
    const old = new Date(Date.now() - (RUNNER_STALE_SECONDS + 30) * 1000).toISOString();
    beat({ beat_at: old });

    const state = readRunnerState();
    expect(state.live).toBe(false);
    expect(state.ageSeconds).toBeGreaterThan(RUNNER_STALE_SECONDS);
  });

  it("ignores a heartbeat it cannot parse", () => {
    writeFileSync(join(QUEUE, "runner.heartbeat.json"), "not json");
    expect(readRunnerState().live).toBe(false);
  });
});

/* ---- POST — queue a build ---------------------------------------------- */

describe("POST /api/projects/[id]/build", () => {
  it("is gated like every other route", async () => {
    const project = seed();
    beat();

    const denied = await post(project.id, undefined, {});
    expect(denied.status).toBe(401);
    expect(readdirSync(QUEUE)).not.toContainEqual(expect.stringContaining(".request.json"));
  });

  it("404s an app that is not in this library", async () => {
    beat();
    expect((await post("nosuchapp1234")).status).toBe(404);
  });

  it("refuses to build an app with no files", async () => {
    const project = seed({ files: [] });
    beat();

    const response = await post(project.id);
    expect(response.status).toBe(409);
    expect(((await response.json()) as { error: string }).error).toContain("no files");
  });

  it("refuses when no runner is alive, naming the fix", async () => {
    const project = seed();
    // No heartbeat at all — the service was never installed or has stopped.
    const response = await post(project.id);

    expect(response.status).toBe(503);
    const message = ((await response.json()) as { error: string }).error;
    expect(message).toContain("No build runner");
    expect(message).toContain("olympus-build-runner");
    // Nothing may be queued: a request nobody will claim looks like a hang.
    expect(readdirSync(QUEUE)).toEqual([]);
  });

  it("queues a request the runner can act on, and writes the spec", async () => {
    const project = seed();
    beat();

    const response = await post(project.id);
    expect(response.status).toBe(202);

    const payload = (await response.json()) as { job: string; slug: string; spec: string; replaced: boolean };
    expect(payload.slug).toBe("markdown-notes");
    expect(payload.spec).toBe("build-requests/markdown-notes.md");
    expect(payload.replaced).toBe(false);

    // The spec is on disk, so the runner's resolve_spec() will find it.
    expect(readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8")).toContain(
      "# Application Specification: Markdown Notes",
    );

    // Exactly one request, plus the heartbeat the runner itself writes — the
    // request is the only thing this route may add to the queue.
    const files = readdirSync(QUEUE);
    expect(files).toContain(`${payload.job}.request.json`);
    expect(files.filter((name) => name.endsWith(".request.json"))).toEqual([`${payload.job}.request.json`]);

    const request = JSON.parse(readFileSync(join(QUEUE, `${payload.job}.request.json`), "utf8")) as Record<
      string,
      unknown
    >;
    expect(request.v).toBe(1);
    expect(request.job).toBe(payload.job);
    expect(request.spec).toBe("build-requests/markdown-notes.md");
    expect(request.replace).toBe(false);
    // The runner refuses any request whose slug does not match the spec's own
    // name, so these two must be derived from the same value.
    expect(request.slug).toBe("markdown-notes");
    expect(`${request.slug}.md`).toBe("markdown-notes.md");
  });

  it("keeps a hostile title inside both directories", async () => {
    const project = seed({ title: "../../../../etc/passwd" });
    beat();

    const response = await post(project.id);
    expect(response.status).toBe(202);

    const payload = (await response.json()) as { slug: string; spec: string };
    expect(payload.slug).toBe("etc-passwd");
    expect(payload.spec).toBe("build-requests/etc-passwd.md");

    // Same value on both sides of the queue — the runner requires it.
    expect(specSlug("../../../../etc/passwd")).toBe(payload.slug);
    expect(readdirSync(REQUESTS)).toEqual(["etc-passwd.md"]);
  });

  it("refuses to replace an existing spec until asked", async () => {
    const project = seed();
    beat();

    expect((await post(project.id)).status).toBe(202);
    writeFileSync(join(REQUESTS, "markdown-notes.md"), "# hand-edited\n");

    const conflict = await post(project.id);
    expect(conflict.status).toBe(409);
    expect(((await conflict.json()) as { error: string }).error).toContain("already exists");
    // The operator's edit survives an unconfirmed build.
    expect(readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8")).toBe("# hand-edited\n");

    const replaced = await post(project.id, { replace: true });
    expect(replaced.status).toBe(202);
    expect(((await replaced.json()) as { replaced: boolean }).replaced).toBe(true);
    expect(readFileSync(join(REQUESTS, "markdown-notes.md"), "utf8")).toContain(
      "# Application Specification",
    );
  });

  it("passes replace through to the runner so a rebuild is explicit", async () => {
    const project = seed();
    beat();

    await post(project.id);
    const second = await post(project.id, { replace: true });
    const payload = (await second.json()) as { job: string };

    const request = JSON.parse(
      readFileSync(join(QUEUE, `${payload.job}.request.json`), "utf8"),
    ) as Record<string, unknown>;
    // Without this the runner keeps the previous build and the workflow refuses
    // to overwrite, which would look like the button doing nothing.
    expect(request.replace).toBe(true);
  });

  it("treats a junk body as no body", async () => {
    const project = seed();
    beat();

    const response = await POST(
      new Request(`http://studio.test/api/projects/${project.id}/build`, {
        method: "POST",
        headers: { "content-type": "application/json", ...TOKEN },
        body: "not json",
      }),
      context(project.id),
    );
    expect(response.status).toBe(202);
  });

  it("reports where to write instead of failing obscurely", async () => {
    const project = seed();
    beat();
    const notADir = join(ROOT, "not-a-directory");
    writeFileSync(notADir, "not a directory\n");
    process.env.STUDIO_BUILD_QUEUE_DIR = notADir;

    const response = await post(project.id);
    expect(response.status).toBe(503);
    expect(((await response.json()) as { error: string }).error).toContain("chown 1001:1001");
  });
});

/* ---- GET — where did it get to ----------------------------------------- */

describe("GET /api/projects/[id]/build", () => {
  it("is gated like every other route", async () => {
    const project = seed();
    expect((await get(project.id, "", {})).status).toBe(401);
  });

  it("404s an app that is not in this library", async () => {
    expect((await get("nosuchapp1234")).status).toBe(404);
  });

  it("reports no build and a dead runner for a fresh app", async () => {
    const project = seed();
    const payload = (await (await get(project.id)).json()) as {
      build: BuildStatus | null;
      runner: { live: boolean };
      next: string;
    };

    expect(payload.build).toBeNull();
    expect(payload.runner.live).toBe(false);
    expect(payload.next).toBe("make app SPEC=build-requests/markdown-notes.md");
  });

  it("returns one job by id", async () => {
    const project = seed();
    statusFile("0123456789abcdef", { state: "succeeded", slug: "markdown-notes", message: "Built." });

    const response = await get(project.id, "?job=0123456789abcdef");
    expect(response.status).toBe(200);

    const payload = (await response.json()) as { build: BuildStatus };
    expect(payload.build.state).toBe("succeeded");
    expect(payload.build.message).toBe("Built.");
  });

  it("404s a job id that is not a job id", async () => {
    const project = seed();
    for (const job of ["..%2Fetc%2Fpasswd", "nope", "ABCDEF0123456789"]) {
      const response = await get(project.id, `?job=${job}`);
      expect(response.status).toBe(404);
    }
  });

  it("returns the app's latest build when no job is named", async () => {
    const project = seed();
    statusFile("aaaaaaaaaaaaaaaa", {
      state: "failed",
      slug: "markdown-notes",
      message: "old",
      updated_at: "2026-01-01T00:00:00Z",
    });
    statusFile("bbbbbbbbbbbbbbbb", {
      state: "succeeded",
      slug: "markdown-notes",
      message: "new",
      updated_at: "2026-02-01T00:00:00Z",
    });

    const payload = (await (await get(project.id)).json()) as { build: BuildStatus };
    expect(payload.build.message).toBe("new");
  });
});

/* ---- status reading ----------------------------------------------------- */

describe("readBuildStatus", () => {
  it("normalises what the runner writes", () => {
    statusFile("0123456789abcdef", {
      state: "succeeded",
      slug: "markdown-notes",
      title: "Markdown Notes",
      spec: "build-requests/markdown-notes.md",
      started_at: "2026-09-13T00:00:00Z",
      finished_at: "2026-09-13T00:02:00Z",
      exit_code: 0,
      message: "Built.",
      artifact: { dir: "builds/markdown-notes", files: 3, bytes: 900, entry: "index.html" },
      log_tail: "manufacture: done",
    });

    const status = readBuildStatus("0123456789abcdef");
    expect(status).toMatchObject({
      job: "0123456789abcdef",
      state: "succeeded",
      exitCode: 0,
      message: "Built.",
      logTail: "manufacture: done",
    });
    expect(status?.artifact).toEqual({
      dir: "builds/markdown-notes",
      files: 3,
      bytes: 900,
      entry: "index.html",
    });
  });

  it("refuses a job id that is not one, rather than reading a path", () => {
    expect(readBuildStatus("../../etc/passwd")).toBeNull();
    expect(readBuildStatus("")).toBeNull();
  });

  it("returns null for an unknown job and for junk on disk", () => {
    expect(readBuildStatus("0123456789abcdef")).toBeNull();

    writeFileSync(join(QUEUE, "0123456789abcdef.status.json"), "not json");
    expect(readBuildStatus("0123456789abcdef")).toBeNull();

    writeFileSync(
      join(QUEUE, "0123456789abcdef.status.json"),
      JSON.stringify({ v: 1, job: "0123456789abcdef", state: "exploded" }),
    );
    expect(readBuildStatus("0123456789abcdef")).toBeNull();
  });

  it("prefers a running build over a newer finished one", () => {
    statusFile("aaaaaaaaaaaaaaaa", {
      state: "succeeded",
      slug: "markdown-notes",
      updated_at: "2026-05-01T00:00:00Z",
    });
    statusFile("bbbbbbbbbbbbbbbb", {
      state: "running",
      slug: "markdown-notes",
      updated_at: "2026-01-01T00:00:00Z",
    });

    // The thing the operator is watching is the thing in flight.
    expect(latestBuildStatus("markdown-notes")?.job).toBe("bbbbbbbbbbbbbbbb");
  });

  it("ignores other apps' builds", () => {
    statusFile("cccccccccccccccc", { state: "succeeded", slug: "something-else" });
    expect(latestBuildStatus("markdown-notes")).toBeNull();
  });

  it("returns null when the queue does not exist", () => {
    process.env.STUDIO_BUILD_QUEUE_DIR = join(ROOT, "absent");
    expect(latestBuildStatus("markdown-notes")).toBeNull();
    expect(buildQueueDir()).toBe(join(ROOT, "absent"));
  });
});

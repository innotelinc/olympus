import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/projects/[id]/build/route";
import { POST as CANCEL } from "@/app/api/projects/[id]/build/cancel/route";
import {
  RUNNER_STALE_SECONDS,
  type BuildStatus,
  buildQueueDir,
  latestBuildStatus,
  listBuildHistory,
  readBuildStatus,
  readRunnerState,
  requestCancel,
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

/**
 * A request the runner has not claimed yet — the state a job is in between the
 * click and the first status file, which is the window the panel polls in.
 */
function requestFile(job: string, fields: Record<string, unknown> = {}): void {
  writeFileSync(
    join(QUEUE, `${job}.request.json`),
    JSON.stringify({
      v: 1,
      job,
      action: "build",
      slug: "markdown-notes",
      title: "Markdown Notes",
      requested_by: "studio",
      requested_at: new Date().toISOString(),
      ...fields,
    }),
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

  it("answers a queued job the runner has not claimed yet", async () => {
    // The panel polls the job it just queued; the runner writes the first status
    // when it claims the request, so this window is reachable on every build — and
    // answering it with "No such build job" is a lie about work that was accepted.
    const project = seed();
    requestFile("0123456789abcdef");

    const response = await get(project.id, "?job=0123456789abcdef");

    expect(response.status).toBe(200);
    const payload = (await response.json()) as { build: BuildStatus };
    expect(payload.build.state).toBe("queued");
    expect(payload.build.slug).toBe("markdown-notes");
    expect(payload.build.message).toMatch(/waiting for the build runner/i);
  });

  it("says a claimed job is starting, not still waiting", async () => {
    // The runner renames the request and writes its first status in two steps, so a
    // poll can land between them. It has been picked up, and saying otherwise sends
    // the operator looking for a runner that is working.
    const project = seed();
    requestFile("0123456789abcdef");
    writeFileSync(join(QUEUE, "0123456789abcdef.running.json"), "{}");

    const payload = (await (await get(project.id, "?job=0123456789abcdef")).json()) as {
      build: BuildStatus;
    };

    expect(payload.build.state).toBe("queued");
    expect(payload.build.message).toMatch(/picked this up/i);
  });

  it("still 404s a job the queue has no marker for", async () => {
    const project = seed();
    requestFile("1111111111111111");

    const response = await get(project.id, "?job=2222222222222222");

    expect(response.status).toBe(404);
  });

  it("reports a queued job as the app's latest, so a reload keeps the thread", async () => {
    const project = seed();
    requestFile("0123456789abcdef");

    const payload = (await (await get(project.id)).json()) as { build: BuildStatus | null };

    expect(payload.build?.state).toBe("queued");
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

  it("reads a preview's address and its action", () => {
    statusFile("0123456789abcdef", {
      state: "succeeded",
      action: "preview",
      slug: "markdown-notes",
      message: "Previewing markdown-notes — running on https://markdown-notes.example.",
      preview_url: "https://markdown-notes.example",
      published_url: null,
    });

    const status = readBuildStatus("0123456789abcdef");
    expect(status?.action).toBe("preview");
    expect(status?.previewUrl).toBe("https://markdown-notes.example");
    // A preview publishes nothing, and reading its address as a published one would
    // show a name that was never registered at the edge.
    expect(status?.publishedUrl).toBeNull();
  });

  it("calls a status written before the action field existed a build", () => {
    // Every status on disk from before this field is a factory build. Guessing
    // "publish" would relabel finished history as something it was not.
    statusFile("0123456789abcdef", { state: "succeeded", slug: "markdown-notes" });

    const status = readBuildStatus("0123456789abcdef");
    expect(status?.action).toBe("build");
    expect(status?.previewUrl).toBeNull();
  });

  it("does not read an unusable action as a preview", () => {
    statusFile("0123456789abcdef", { state: "running", action: "previews" });
    expect(readBuildStatus("0123456789abcdef")?.action).toBe("build");
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

  it("reads a cancelled build as cancelled, not as a failure", () => {
    statusFile("0123456789abcdef", { state: "cancelled", slug: "markdown-notes" });
    expect(readBuildStatus("0123456789abcdef")?.state).toBe("cancelled");
  });
});

/* ---- history ------------------------------------------------------------ */

describe("listBuildHistory", () => {
  it("lists only this app's builds, newest first", () => {
    statusFile("aaaaaaaaaaaaaaaa", {
      state: "failed",
      slug: "markdown-notes",
      updated_at: "2026-01-01T00:00:00Z",
    });
    statusFile("bbbbbbbbbbbbbbbb", {
      state: "succeeded",
      slug: "markdown-notes",
      updated_at: "2026-02-01T00:00:00Z",
    });
    statusFile("cccccccccccccccc", { state: "succeeded", slug: "something-else" });

    const history = listBuildHistory("markdown-notes");
    expect(history.map((entry) => entry.job)).toEqual(["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"]);
  });

  it("pins a running build to the top even if its timestamp is older", () => {
    // Queueing and starting can leave a running job's updated_at behind a build
    // that finished a second later; the running one is what is being watched.
    statusFile("aaaaaaaaaaaaaaaa", {
      state: "succeeded",
      slug: "markdown-notes",
      updated_at: "2026-02-01T00:00:00Z",
    });
    statusFile("bbbbbbbbbbbbbbbb", {
      state: "running",
      slug: "markdown-notes",
      updated_at: "2026-01-01T00:00:00Z",
    });

    expect(listBuildHistory("markdown-notes")[0].job).toBe("bbbbbbbbbbbbbbbb");
  });

  it("honours the limit", () => {
    for (const [index, job] of ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"].entries()) {
      statusFile(job, {
        state: "succeeded",
        slug: "markdown-notes",
        updated_at: `2026-0${index + 1}-01T00:00:00Z`,
      });
    }

    expect(listBuildHistory("markdown-notes", 2)).toHaveLength(2);
  });

  it("skips junk and unreadable files instead of failing the list", () => {
    statusFile("aaaaaaaaaaaaaaaa", { state: "succeeded", slug: "markdown-notes" });
    writeFileSync(join(QUEUE, "bbbbbbbbbbbbbbbb.status.json"), "not json");
    statusFile("cccccccccccccccc", { state: "exploded", slug: "markdown-notes" });
    writeFileSync(join(QUEUE, "dddddddddddddddd.log"), "a log is not a status");

    expect(listBuildHistory("markdown-notes").map((entry) => entry.job)).toEqual([
      "aaaaaaaaaaaaaaaa",
    ]);
  });

  it("is empty when the queue does not exist", () => {
    process.env.STUDIO_BUILD_QUEUE_DIR = join(ROOT, "absent");
    expect(listBuildHistory("markdown-notes")).toEqual([]);
  });
});

/* ---- cancelling --------------------------------------------------------- */

describe("requestCancel", () => {
  it("writes a marker the runner polls for", () => {
    statusFile("0123456789abcdef", { state: "running", slug: "markdown-notes" });

    const status = requestCancel("0123456789abcdef");
    expect(status.slug).toBe("markdown-notes");

    const marker = JSON.parse(
      readFileSync(join(QUEUE, "0123456789abcdef.cancel.json"), "utf8"),
    ) as Record<string, unknown>;
    expect(marker.v).toBe(1);
    expect(marker.job).toBe("0123456789abcdef");
    expect(marker.requested_by).toBe("studio");
  });

  it("refuses a job id that is not one", () => {
    for (const job of ["", "../../etc/passwd", "ABCDEF0123456789", "nope"]) {
      expect(() => requestCancel(job)).toThrowError(/No such build job/);
    }
    expect(readdirSync(QUEUE).filter((name) => name.endsWith(".cancel.json"))).toEqual([]);
  });

  it("refuses a build that is not running", () => {
    statusFile("aaaaaaaaaaaaaaaa", { state: "succeeded", slug: "markdown-notes" });
    statusFile("bbbbbbbbbbbbbbbb", { state: "cancelled", slug: "markdown-notes" });

    // Recording a stop for a finished build would rewrite history.
    expect(() => requestCancel("aaaaaaaaaaaaaaaa")).toThrowError(/already succeeded/);
    expect(() => requestCancel("bbbbbbbbbbbbbbbb")).toThrowError(/already was cancelled/);
    expect(readdirSync(QUEUE).filter((name) => name.endsWith(".cancel.json"))).toEqual([]);
  });

  it("does not overwrite an unrelated file", () => {
    statusFile("0123456789abcdef", { state: "running", slug: "markdown-notes" });
    requestCancel("0123456789abcdef");

    // The marker is its own file: the status/log of the job it is stopping stay
    // readable while the runner acts on it.
    expect(readdirSync(QUEUE).sort()).toEqual([
      "0123456789abcdef.cancel.json",
      "0123456789abcdef.status.json",
    ]);
  });
});

/* ---- POST — cancel a build --------------------------------------------- */

describe("POST /api/projects/[id]/build/cancel", () => {
  function cancel(id: string, body?: unknown, headers: Record<string, string> = TOKEN) {
    return CANCEL(
      new Request(`http://studio.test/api/projects/${id}/build/cancel`, {
        method: "POST",
        headers: {
          ...(body === undefined ? {} : { "content-type": "application/json" }),
          ...headers,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      context(id),
    );
  }

  it("is gated like every other route", async () => {
    const project = seed();
    statusFile("0123456789abcdef", { state: "running", slug: "markdown-notes" });

    expect((await cancel(project.id, { job: "0123456789abcdef" }, {})).status).toBe(401);
    expect(readdirSync(QUEUE).filter((name) => name.endsWith(".cancel.json"))).toEqual([]);
  });

  it("404s an app that is not in this library", async () => {
    expect((await cancel("nosuchapp1234", { job: "0123456789abcdef" })).status).toBe(404);
  });

  it("400s when no job is named", async () => {
    const project = seed();
    expect((await cancel(project.id, {})).status).toBe(400);
    expect((await cancel(project.id)).status).toBe(400);
  });

  it("404s an unknown job and 409s one that already finished", async () => {
    const project = seed();
    expect((await cancel(project.id, { job: "0123456789abcdef" })).status).toBe(404);

    statusFile("0123456789abcdef", { state: "succeeded", slug: "markdown-notes" });
    expect((await cancel(project.id, { job: "0123456789abcdef" })).status).toBe(409);
  });

  it("accepts a running build and says what happens next", async () => {
    const project = seed();
    statusFile("0123456789abcdef", { state: "running", slug: "markdown-notes" });

    const response = await cancel(project.id, { job: "0123456789abcdef" });
    expect(response.status).toBe(202);

    const payload = (await response.json()) as { requested: boolean; job: string; message: string };
    expect(payload.requested).toBe(true);
    expect(payload.job).toBe("0123456789abcdef");
    // 202 is "the request is on disk", so the message has to point at the status
    // file the panel is already following rather than claim it is stopped.
    expect(payload.message).toContain("reports it on its next poll");
    expect(readdirSync(QUEUE)).toContain("0123456789abcdef.cancel.json");
  });
});

describe("the plan travels with the request", () => {
  const PLAN = {
    name: "Markdown Notes",
    kind: "app",
    summary: "Notes with a preview pane.",
    runtime: { language: "python", frameworks: ["flask"], database: "sqlite" },
    run: {
      install: "pip install -r requirements.txt",
      build: "",
      start: "python app.py",
      port: 8000,
      healthcheck: "/healthz",
    },
    files: [{ path: "app.py", purpose: "the server" }],
  };

  async function requestFor(project: Project, body?: Record<string, unknown>) {
    const response = await post(project.id, body);
    const payload = (await response.json()) as { job: string };
    return JSON.parse(
      readFileSync(join(QUEUE, `${payload.job}.request.json`), "utf8"),
    ) as Record<string, unknown>;
  }

  it("sends the stored plan with a build", async () => {
    // This is what makes Build It work on a reloaded project: without the plan the
    // runner has nothing but the two stacks it used to know.
    const project = seed({ plan: PLAN });
    beat();

    const request = await requestFor(project);
    const plan = request.plan as Record<string, unknown>;
    expect(plan.run).toMatchObject({ start: "python app.py", port: 8000 });
    expect((plan.runtime as Record<string, unknown>).language).toBe("python");
  });

  it("sends the stored plan with a publish", async () => {
    const project = seed({ plan: PLAN });
    beat();

    const request = await requestFor(project, { action: "publish" });
    expect(request.action).toBe("publish");
    expect((request.plan as Record<string, unknown>).kind).toBe("app");
    // A publish carries the files as well: it builds what is on screen.
    expect(Array.isArray(request.files)).toBe(true);
  });

  it("sends the stored plan with a preview", async () => {
    // A preview packages the files on screen just as a publish does; the only
    // difference is that it stops before the name.
    const project = seed({ plan: PLAN });
    beat();

    const request = await requestFor(project, { action: "preview" });
    expect(request.action).toBe("preview");
    expect((request.plan as Record<string, unknown>).kind).toBe("app");
    expect(Array.isArray(request.files)).toBe(true);
  });

  it("omits the plan for a project that has none, rather than inventing one", async () => {
    // A project saved before the planner existed is built by its own packager. A
    // guessed plan would change what Publish It builds.
    const project = seed();
    beat();

    expect(await requestFor(project)).not.toHaveProperty("plan");
    expect(await requestFor(project, { action: "publish" })).not.toHaveProperty("plan");
    expect(await requestFor(project, { action: "preview" })).not.toHaveProperty("plan");
  });
});

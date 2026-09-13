import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/projects/[id]/build/log/route";
import { MAX_LOG_BYTES, readBuildLog } from "@/lib/build-queue";
import { specSlug } from "@/lib/factory-spec";
import { ANONYMOUS_NAMESPACE, type Project, saveProject } from "@/lib/projects";

// Mocked for the same reason the other route tests mock it: a real .env on this
// machine must not be able to decide an assertion.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const ROOT = join(process.cwd(), "tests", ".tmp", "build-log");
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
const JOB = "0123456789abcdef";

function context(id: string): { params: Promise<{ id: string }> } {
  return { params: Promise.resolve({ id }) };
}

function seed(title: string): Project {
  const { project } = saveProject(ANONYMOUS_NAMESPACE, {
    title,
    files: [{ path: "index.html", contents: "<!doctype html>\n" }],
  });
  return project;
}

function statusFile(job: string, slug: string, fields: Record<string, unknown> = {}): void {
  writeFileSync(
    join(QUEUE, `${job}.status.json`),
    JSON.stringify({ v: 1, job, slug, state: "succeeded", ...fields }),
  );
}

function logFile(job: string, contents: string): void {
  writeFileSync(join(QUEUE, `${job}.log`), contents);
}

function get(id: string, query = "", headers: Record<string, string> = TOKEN) {
  return GET(new Request(`http://studio.test/api/projects/${id}/build/log${query}`, { headers }), context(id));
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

describe("authorisation", () => {
  it("refuses an unauthenticated read", async () => {
    const app = seed("Markdown Notes");
    statusFile(JOB, specSlug(app.title));
    logFile(JOB, "building\n");

    const response = await get(app.id, `?job=${JOB}`, {});
    expect(response.status).toBe(401);
  });

  it("requires a job id", async () => {
    const app = seed("Markdown Notes");
    const response = await get(app.id);
    expect(response.status).toBe(400);
  });

  it("404s an app that does not exist", async () => {
    const response = await get("nosuchapp", `?job=${JOB}`);
    expect(response.status).toBe(404);
  });
});

describe("ownership", () => {
  it("serves the log of this app's own build", async () => {
    const app = seed("Markdown Notes");
    statusFile(JOB, specSlug(app.title));
    logFile(JOB, "==> manufacture: spec: build-requests/markdown-notes.md\n");

    const response = await get(app.id, `?job=${JOB}`);
    expect(response.status).toBe(200);
    const payload = (await response.json()) as { text: string; job: string; state: string };
    expect(payload.job).toBe(JOB);
    expect(payload.state).toBe("succeeded");
    expect(payload.text).toContain("manufacture: spec");
  });

  it("refuses another app's build log, and does not say it exists", async () => {
    // The queue is one directory shared by every tenant, so a job id alone would
    // let any signed-in user read any other user's build by guessing 16 hex
    // characters. The 404 is the same one a missing job gets, on purpose.
    const mine = seed("Markdown Notes");
    const theirs = seed("Expense Splitter");
    statusFile(JOB, specSlug(theirs.title));
    logFile(JOB, "SECRET: their build output\n");

    const response = await get(mine.id, `?job=${JOB}`);
    expect(response.status).toBe(404);
    const payload = (await response.json()) as { error: string };
    expect(payload.error).not.toContain("Expense");
    expect(JSON.stringify(payload)).not.toContain("SECRET");
  });

  it("404s a job id that names no build", async () => {
    const app = seed("Markdown Notes");
    const response = await get(app.id, "?job=ffffffffffffffff");
    expect(response.status).toBe(404);
  });

  it("refuses a path-shaped job id instead of reading outside the queue", async () => {
    const app = seed("Markdown Notes");
    for (const job of ["../../etc/passwd", "..", `${JOB}/../../etc/passwd`]) {
      const response = await get(app.id, `?job=${encodeURIComponent(job)}`);
      expect(response.status).toBe(404);
    }
  });
});

describe("readBuildLog", () => {
  it("returns an empty log for a build that has not written one yet", () => {
    // The runner writes the log when it claims the request, so a build queued a
    // moment ago legitimately has none — 404 would be a lie about the build.
    const app = seed("Markdown Notes");
    statusFile(JOB, specSlug(app.title));

    const log = readBuildLog(JOB);
    expect(log).not.toBeNull();
    expect(log?.text).toBe("");
    expect(log?.bytes).toBe(0);
    expect(log?.truncated).toBe(false);
  });

  it("returns null for a job with no status", () => {
    expect(readBuildLog("ffffffffffffffff")).toBeNull();
  });

  it("returns null for a job id that is not one", () => {
    expect(readBuildLog("../../etc/passwd")).toBeNull();
  });

  it("hands over a small log whole", () => {
    seed("Markdown Notes");
    statusFile(JOB, "markdown-notes");
    logFile(JOB, "line one\nline two\n");

    const log = readBuildLog(JOB);
    expect(log?.text).toBe("line one\nline two\n");
    expect(log?.truncated).toBe(false);
    expect(log?.bytes).toBe(18);
  });

  it("sends the tail of a log too big for a browser", () => {
    seed("Markdown Notes");
    statusFile(JOB, "markdown-notes");
    logFile(JOB, `${"x".repeat(MAX_LOG_BYTES)}\nTHE END\n`);

    const log = readBuildLog(JOB);
    expect(log?.truncated).toBe(true);
    expect(log?.text).toContain("THE END");
    expect(log?.text).toContain("earlier log omitted");
    expect(log?.bytes).toBeGreaterThan(MAX_LOG_BYTES);
    // The marker is a constant, so the payload cannot hide behind it and grow.
    expect(log?.text.length).toBeLessThan(MAX_LOG_BYTES + 200);
  });

  it("starts a truncated log on a line boundary", () => {
    // A byte offset lands mid-line; half a line of mojibake at the top is how a
    // reader concludes the log is corrupt.
    seed("Markdown Notes");
    statusFile(JOB, "markdown-notes");
    logFile(JOB, `${"a".repeat(MAX_LOG_BYTES - 10)}PARTIAL LINE THAT IS CUT\nfirst whole line\n`);

    const log = readBuildLog(JOB);
    expect(log?.truncated).toBe(true);
    expect(log?.text).not.toContain("PARTIAL LINE THAT IS CUT");
    expect(log?.text).toContain("first whole line");
    expect(log?.text.endsWith("first whole line\n")).toBe(true);
  });
});

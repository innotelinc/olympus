import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  ANONYMOUS_NAMESPACE,
  MAX_FILE_CHARS,
  MAX_FILES,
  MAX_TITLE_CHARS,
  ProjectError,
  deleteProject,
  listProjects,
  namespaceFor,
  parseStoredFiles,
  readProject,
  saveProject,
} from "@/lib/projects";

/**
 * The store is on disk, so each test owns a directory under the gitignored
 * tests/.tmp and points STUDIO_DATA_DIR at it. Nothing here reads the real
 * library.
 */

const TMP = join(process.cwd(), "tests", ".tmp", "projects");
const NAMESPACE = namespaceFor("test-subject");

const html = (body: string) => [{ path: "index.html", contents: body }];
const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

beforeEach(() => {
  rmSync(TMP, { recursive: true, force: true });
  process.env.STUDIO_DATA_DIR = TMP;
});

afterEach(() => {
  rmSync(TMP, { recursive: true, force: true });
  delete process.env.STUDIO_DATA_DIR;
});

describe("identity mapping", () => {
  it("hashes the subject into a fixed-length directory name", () => {
    const namespace = namespaceFor("some-oidc-subject");

    expect(namespace).toMatch(/^u-[0-9a-f]{32}$/);
    expect(namespace).not.toContain("some-oidc-subject");
    // Same subject, same library — that is what survives a restart.
    expect(namespaceFor("some-oidc-subject")).toBe(namespace);
  });

  it("maps different subjects to different libraries", () => {
    expect(namespaceFor("user-a")).not.toBe(namespaceFor("user-b"));
  });

  it("falls back to one shared library when there is no identity", () => {
    expect(namespaceFor(null)).toBe(ANONYMOUS_NAMESPACE);
    expect(namespaceFor(undefined)).toBe(ANONYMOUS_NAMESPACE);
    expect(namespaceFor("   ")).toBe(ANONYMOUS_NAMESPACE);
  });

  it("cannot be traversed even from a hostile subject", () => {
    for (const subject of ["../../etc", "..", "a/b", "C:\\Windows"]) {
      expect(namespaceFor(subject)).toMatch(/^u-[0-9a-f]{32}$/);
    }
  });
});

describe("save and read", () => {
  it("creates an app and reads it back", () => {
    const { project, created } = saveProject(NAMESPACE, {
      title: "Pomodoro",
      prompt: "a pomodoro timer",
      files: html("<h1>timer</h1>"),
    });

    expect(created).toBe(true);
    expect(project.id).toMatch(/^[A-Za-z0-9_-]{6,64}$/);

    const loaded = readProject(NAMESPACE, project.id);
    expect(loaded).toMatchObject({ title: "Pomodoro", prompt: "a pomodoro timer" });
    expect(loaded?.files).toEqual([{ path: "index.html", contents: "<h1>timer</h1>" }]);
  });

  it("updates in place, keeping the id but not the timestamp", async () => {
    const first = saveProject(NAMESPACE, { title: "Draft", files: html("one") });
    await delay(5);

    const second = saveProject(NAMESPACE, {
      id: first.project.id,
      title: "Renamed",
      files: html("two"),
    });

    expect(second.created).toBe(false);
    expect(second.project.id).toBe(first.project.id);
    expect(second.project.createdAt).toBe(first.project.createdAt);
    expect(second.project.updatedAt > first.project.updatedAt).toBe(true);
    expect(listProjects(NAMESPACE)).toHaveLength(1);
    expect(readProject(NAMESPACE, first.project.id)?.files[0].contents).toBe("two");
  });

  it("keeps the existing title when an update does not supply one", () => {
    const saved = saveProject(NAMESPACE, { title: "Keep me", files: html("x") });
    const updated = saveProject(NAMESPACE, { id: saved.project.id, files: html("y") });

    expect(updated.project.title).toBe("Keep me");
  });

  it("defaults an unnamed app and caps a long title", () => {
    const unnamed = saveProject(NAMESPACE, { files: [] });
    expect(unnamed.project.title).toBe("Untitled app");

    const long = saveProject(NAMESPACE, { id: unnamed.project.id, title: "x".repeat(400) });
    expect(long.project.title).toHaveLength(MAX_TITLE_CHARS);

    const messy = saveProject(NAMESPACE, {
      id: unnamed.project.id,
      title: "   spaced \n out   title  ",
    });
    expect(messy.project.title).toBe("spaced out title");
  });

  it("lists newest first with a file count", async () => {
    const older = saveProject(NAMESPACE, { title: "Older", files: html("a") });
    await delay(5);
    const newer = saveProject(NAMESPACE, {
      title: "Newer",
      files: [
        { path: "index.html", contents: "a" },
        { path: "app.js", contents: "b" },
      ],
    });

    const listed = listProjects(NAMESPACE);
    expect(listed.map((entry) => entry.id)).toEqual([newer.project.id, older.project.id]);
    expect(listed[0]).toMatchObject({ title: "Newer", fileCount: 2 });
    expect(listed[1].fileCount).toBe(1);
  });

  it("returns null for an app that does not exist", () => {
    expect(readProject(NAMESPACE, "abcdefgh")).toBeNull();
    expect(listProjects(NAMESPACE)).toEqual([]);
  });
});

describe("isolation", () => {
  it("does not leak an app across identities", () => {
    const mine = saveProject(NAMESPACE, { title: "Private", files: html("secret") });
    const theirs = namespaceFor("someone-else");

    expect(listProjects(theirs)).toEqual([]);
    expect(readProject(theirs, mine.project.id)).toBeNull();
    expect(deleteProject(theirs, mine.project.id)).toBe(false);
    expect(readProject(NAMESPACE, mine.project.id)).not.toBeNull();
  });
});

describe("path safety", () => {
  it("drops absolute and traversing file paths instead of storing them", () => {
    const files = parseStoredFiles([
      { path: "../evil.js", contents: "x" },
      { path: "/etc/passwd", contents: "x" },
      { path: "src/../escape.js", contents: "x" },
      { path: "C:\\Windows\\system32", contents: "x" },
      { path: "deep/nested/app.js", contents: "ok" },
      null,
      "not a file",
    ]);

    expect(files.map((file) => file.path)).toEqual(["deep/nested/app.js"]);
  });

  it("deduplicates repeated paths", () => {
    const files = parseStoredFiles([
      { path: "index.html", contents: "first" },
      { path: "index.html", contents: "second" },
    ]);

    expect(files).toHaveLength(1);
    expect(files[0].contents).toBe("first");
  });

  it("refuses to touch anything outside the library", () => {
    const decoy = join(process.cwd(), "tests", ".tmp", "decoy.json");
    mkdirSync(join(decoy, ".."), { recursive: true });
    writeFileSync(decoy, "keep\n");

    try {
      expect(readProject(NAMESPACE, "../decoy")).toBeNull();
      expect(deleteProject(NAMESPACE, "../decoy")).toBe(false);
      expect(deleteProject(NAMESPACE, "%2e%2e%2fdecoy")).toBe(false);
      expect(readFileSync(decoy, "utf8")).toBe("keep\n");
    } finally {
      rmSync(decoy, { force: true });
    }
  });
});

describe("bounds", () => {
  it("refuses an oversized file with 413", () => {
    expect(() =>
      saveProject(NAMESPACE, {
        files: [{ path: "index.html", contents: "x".repeat(MAX_FILE_CHARS + 1) }],
      }),
    ).toThrow(ProjectError);

    try {
      saveProject(NAMESPACE, {
        files: [{ path: "index.html", contents: "x".repeat(MAX_FILE_CHARS + 1) }],
      });
    } catch (error) {
      expect((error as ProjectError).status).toBe(413);
    }
  });

  it("refuses too many files", () => {
    const files = Array.from({ length: MAX_FILES + 2 }, (_, index) => ({
      path: `file-${index}.js`,
      contents: "x",
    }));

    expect(() => saveProject(NAMESPACE, { files })).toThrow(/Too many files/i);
  });
});

describe("resilience", () => {
  it("skips an unreadable entry instead of failing the whole library", () => {
    const saved = saveProject(NAMESPACE, { title: "Good", files: html("ok") });
    const dir = join(TMP, NAMESPACE);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "corrupttest.json"), "{ not json");

    expect(listProjects(NAMESPACE).map((entry) => entry.id)).toEqual([saved.project.id]);
  });

  it("treats a corrupt file as missing rather than throwing", () => {
    const dir = join(TMP, NAMESPACE);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "corrupttest.json"), "{ not json");

    expect(readProject(NAMESPACE, "corrupttest")).toBeNull();
  });
});

describe("delete", () => {
  it("removes an app and reports a second delete", () => {
    const saved = saveProject(NAMESPACE, { title: "Temp", files: html("x") });

    expect(deleteProject(NAMESPACE, saved.project.id)).toBe(true);
    expect(readProject(NAMESPACE, saved.project.id)).toBeNull();
    expect(deleteProject(NAMESPACE, saved.project.id)).toBe(false);
  });
});

describe("the plan a project is built to", () => {
  const PLAN = {
    name: "Weight Tracker",
    kind: "app",
    summary: "Daily weigh-ins.",
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

  it("is stored and read back", () => {
    const { project } = saveProject(NAMESPACE, {
      title: "Weight Tracker",
      files: [{ path: "app.py", contents: "print('hi')" }],
      plan: PLAN,
    });

    const loaded = readProject(NAMESPACE, project.id);
    expect(loaded?.plan?.run.start).toBe("python app.py");
    expect(loaded?.plan?.run.port).toBe(8000);
    expect(loaded?.plan?.slug).toBe("weight-tracker");
  });

  it("is what the runner builds from, so an absent one stays absent", () => {
    // A project saved before the planner existed has no plan, and its own packager
    // still builds it. Storing a guess would change what "Publish It" builds.
    const { project } = saveProject(NAMESPACE, { title: "Legacy", files: html("x") });
    expect(readProject(NAMESPACE, project.id)?.plan).toBeNull();
  });

  it("survives a revision that does not mention one", () => {
    // A development turn does not re-plan, so the client sends no plan — and the
    // project must keep the one it has rather than becoming unbuildable.
    const first = saveProject(NAMESPACE, {
      title: "Weight Tracker",
      files: [{ path: "app.py", contents: "one" }],
      plan: PLAN,
    });

    const second = saveProject(NAMESPACE, {
      id: first.project.id,
      title: "Weight Tracker",
      files: [{ path: "app.py", contents: "two" }],
    });

    expect(second.project.plan?.run.start).toBe("python app.py");
  });

  it("is re-read rather than trusted, so a stored one cannot be weakened", () => {
    const { project } = saveProject(NAMESPACE, {
      title: "Weight Tracker",
      files: [{ path: "app.py", contents: "x" }],
      plan: { ...PLAN, run: { ...PLAN.run, port: 80 } },
    });

    // Refused at the boundary, and the out-of-range port is replaced by the default
    // rather than reaching a container that could not bind it.
    expect(readProject(NAMESPACE, project.id)?.plan?.run.port).toBe(3000);
  });

  it("is dropped entirely when it is unusable", () => {
    const { project } = saveProject(NAMESPACE, {
      title: "Broken",
      files: html("x"),
      plan: { name: "Broken", run: {} },
    });

    expect(readProject(NAMESPACE, project.id)?.plan).toBeNull();
  });

  it("takes its kind from the plan, which is what the project was built to", () => {
    // The plan is the planner's answer to "what is this" — it read the request. A
    // `kind` alongside it is a leftover from a client that used to choose, so the
    // plan wins rather than the two disagreeing in opposite directions.
    const { project } = saveProject(NAMESPACE, {
      title: "Site",
      kind: "app",
      files: html("x"),
      plan: { ...PLAN, kind: "website" },
    });

    expect(project.kind).toBe("website");
    expect(project.plan?.kind).toBe("website");
  });

  it("keeps the kind it already had when a revision arrives without a plan", () => {
    // Projects saved before the planner existed have no plan, and a revision of one
    // must not relabel it "app" by default.
    const { project } = saveProject(NAMESPACE, {
      title: "Old site",
      kind: "website",
      files: html("x"),
    });

    const { project: revised } = saveProject(NAMESPACE, {
      id: project.id,
      title: "Old site",
      files: html("y"),
    });

    expect(revised.kind).toBe("website");
    expect(revised.plan).toBeNull();
  });
});

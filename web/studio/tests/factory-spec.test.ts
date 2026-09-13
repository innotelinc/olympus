import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FactorySpecError,
  MAX_APPENDIX_CHARS,
  SPEC_FILENAME_PATTERN,
  buildFactorySpec,
  entryPoint,
  factoryRequestsDir,
  specSlug,
  writeFactorySpec,
} from "@/lib/factory-spec";
import type { Project } from "@/lib/projects";

// Pure and filesystem tests both: `factoryRequestsDir` calls loadRepoEnv(), so
// the loader is mocked out and every test sets the variables it needs. Nothing
// here reads or writes a real build-requests/ directory.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const TMP = join(process.cwd(), "tests", ".tmp", "factory-spec");
const NOT_A_DIR = join(TMP, "not-a-directory");

const MANAGED = ["STUDIO_FACTORY_REQUESTS_DIR"] as const;
let saved: Record<string, string | undefined> = {};

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "abc123XY_Z-",
    title: "Markdown Notes",
    // Stated, not defaulted: the kind decides the prompt, the verification bar and
    // what "finished" means, and a helper that left it out would hide that.
    kind: "app",
    prompt: "A markdown notes app with a live preview pane.",
    // No plan: this is a project saved before the planner existed, which is exactly
    // the case that still has to export a spec and still has to build.
    plan: null,
    // A full-stack app: the data model the API is derived from, the interface, and
    // the styles. The three files the model is allowed to write, and no others.
    files: [
      {
        path: "server/schema.sql",
        contents:
          "CREATE TABLE IF NOT EXISTS notes (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  body TEXT NOT NULL\n);\n",
      },
      { path: "src/App.tsx", contents: "export default function App() { return null; }\n" },
      { path: "src/index.css", contents: "body { margin: 0; }\n" },
    ],
    createdAt: "2026-09-12T00:00:00.000Z",
    updatedAt: "2026-09-12T01:00:00.000Z",
    ...overrides,
  };
}

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }

  rmSync(TMP, { recursive: true, force: true });
  mkdirSync(TMP, { recursive: true });
  process.env.STUDIO_FACTORY_REQUESTS_DIR = TMP;
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(TMP, { recursive: true, force: true });
});

/* ---- the filename ------------------------------------------------------- */

describe("specSlug", () => {
  it("is the title, lowercased and dashed", () => {
    expect(specSlug("Markdown Notes")).toBe("markdown-notes");
    expect(specSlug("  My App!! (v2) ")).toBe("my-app-v2");
  });

  it("never produces a path-shaped name", () => {
    // The point: a title is attacker-influenced text that becomes a filename.
    expect(specSlug("../../etc/passwd")).toBe("etc-passwd");
    expect(specSlug("a/b\\c")).toBe("a-b-c");
    expect(specSlug("..")).toBe("studio-app");
    expect(specSlug("   ")).toBe("studio-app");
    expect(specSlug("🎉🎉")).toBe("studio-app");
  });

  it("bounds the length and never ends on a dash", () => {
    const long = specSlug("A".repeat(100));
    expect(long).toHaveLength(60);
    expect(long.endsWith("-")).toBe(false);

    // The dash that lands on the cut is stripped, not kept.
    const cut = specSlug(`${"a".repeat(59)} b`);
    expect(cut.endsWith("-")).toBe(false);
    expect(cut.startsWith("-")).toBe(false);
  });
});

/* ---- the document ------------------------------------------------------- */

describe("buildFactorySpec", () => {
  it("uses the factory template's headings, in order", () => {
    const { markdown } = buildFactorySpec(project());

    expect(markdown.startsWith("# Application Specification: Markdown Notes")).toBe(true);

    const headings = [
      "## 🎯 Core Purpose",
      "## 🧰 Tech Stack",
      "## 🛠️ Key Features & Pages",
      "## 🚦 Verification Criteria",
    ]
      .map((heading) => markdown.indexOf(heading))
      .filter((at) => at !== -1);

    expect(headings).toHaveLength(4);
    expect([...headings].sort((left, right) => left - right)).toEqual(headings);
  });

  it("carries the instruction through as the purpose", () => {
    const { markdown } = buildFactorySpec(project());
    expect(markdown).toContain("A markdown notes app with a live preview pane.");
  });

  it("says so plainly when Studio recorded no instruction", () => {
    const { markdown } = buildFactorySpec(project({ prompt: "   " }));
    expect(markdown).toContain("Studio saved no instruction for this app");
  });

  it("infers the stack from the files it was given", () => {
    const { markdown } = buildFactorySpec(project());
    expect(markdown).toContain("- React 19 + TypeScript client, Node HTTP API, SQLite");
    expect(markdown).toContain("- CSS");
    expect(markdown).toContain("- JavaScript / TypeScript");
    expect(markdown).toContain("- SQL / SQLite");
    expect(markdown).not.toContain("- Python");
  });

  it("asks for the truth instead of inventing a stack for an empty build", () => {
    const { markdown } = buildFactorySpec(project({ files: [] }));
    expect(markdown).toContain("Not inferred from the file set");
    expect(markdown).toContain("Studio saved no files for this app.");
  });

  it("names the entry point and the files' sizes", () => {
    const app = project();
    const { markdown } = buildFactorySpec(app);
    const css = app.files.find((file) => file.path === "src/index.css");

    expect(markdown).toContain("The client's entry point is `src/App.tsx`.");
    expect(markdown).toContain(`**\`src/index.css\`** — styles (${css?.contents.length} B)`);
    // The schema is called out as the data model, because it is the thing that
    // decides what the API can do — not just another file in the list.
    expect(markdown).toContain("**`server/schema.sql`** — data model");
  });

  it("offers a runnable verification step rather than a slogan", () => {
    // An app's bar is that it runs and its API answers. "Someone opened a page"
    // passes for an app whose first request 400s.
    const app = buildFactorySpec(project()).markdown;
    expect(app).toContain("scripts/package-app.py");
    expect(app).toContain("/api/health");
    expect(app).toContain("`400`");

    const node = buildFactorySpec(
      project({ files: [{ path: "package.json", contents: "{}" }] }),
    ).markdown;
    expect(node).toContain("`npm ci`");
    expect(node).toContain("`npm test`");

    const python = buildFactorySpec(
      project({ files: [{ path: "test_app.py", contents: "def test_x(): pass\n" }] }),
    ).markdown;
    expect(python).toContain("`python3 -m unittest`");
  });

  it("inlines the build as reference, so this continues rather than restarts", () => {
    const { markdown } = buildFactorySpec(project());
    expect(markdown).toContain("## 📎 Reference build (from Studio)");
    expect(markdown).toContain("### `server/schema.sql`");
    expect(markdown).toContain("CREATE TABLE IF NOT EXISTS notes");
    expect(markdown).toContain("export default function App");
  });

  it("drops to an inventory when the build is too big to inline", () => {
    const huge = project({
      files: [{ path: "big.js", contents: "x".repeat(MAX_APPENDIX_CHARS + 1) }],
    });
    const { markdown } = buildFactorySpec(huge);

    expect(markdown).toContain("too large to inline here");
    expect(markdown).not.toContain("x".repeat(MAX_APPENDIX_CHARS + 1));
    // The inventory survives, so the operator still sees what the build was.
    expect(markdown).toContain("**`big.js`**");
  });

  it("is deterministic — the same app always produces the same bytes", () => {
    const app = project();
    expect(buildFactorySpec(app)).toEqual(buildFactorySpec(app));
  });

  it("names the file after the app", () => {
    expect(buildFactorySpec(project()).filename).toBe("markdown-notes.md");
    expect(buildFactorySpec(project({ title: "  " })).filename).toBe("studio-app.md");
  });
});

/**
 * A website project, which is the case the split exists for: the spec has to make
 * the difference between "generated" and "finished" impossible to miss, because
 * a factory that treats a React site as a finished app produces source nobody can
 * open.
 */
function website(overrides: Partial<Project> = {}): Project {
  return project({
    title: "Product Site",
    kind: "website",
    prompt: "A product marketing site with a hero, pricing and a footer.",
    files: [
      { path: "src/App.tsx", contents: "export default function App() { return null; }\n" },
      { path: "src/components/Hero.tsx", contents: "export const Hero = () => null;\n" },
    ],
    ...overrides,
  });
}

describe("website specs", () => {
  it("states the kind, because everything downstream branches on it", () => {
    expect(buildFactorySpec(website()).markdown).toContain("Kind: **website**");
    expect(buildFactorySpec(project()).markdown).toContain("Kind: **app**");
  });

  it("leads the stack with the kind rather than inferring it from filenames", () => {
    const { markdown } = buildFactorySpec(website());
    expect(markdown).toContain("Vite + React 19 + TypeScript");
    expect(markdown).not.toContain("Self-contained HTML / CSS / JavaScript");
  });

  it("verifies on the build, not on the files existing", () => {
    const { markdown } = buildFactorySpec(website());
    expect(markdown).toContain("npm ci && npm run build");
    expect(markdown).toContain("dist/index.html");
    // An app's bar — "someone opened it" — would pass for a site that does not build.
    expect(markdown).not.toContain("Open `index.html` —");
  });

  it("names the packaging step, since make app alone leaves a site unbuildable", () => {
    const { markdown, nextSteps } = buildFactorySpec(website());
    expect(markdown).toContain("## 🌐 Packaging & delivery");
    expect(markdown).toContain("scripts/package-website.py product-site --publish");
    expect(nextSteps.join(" ")).toContain("scripts/package-project.py product-site");
    expect(nextSteps.join(" ")).toContain("scripts/app-runtime.py --up product-site");
  });

  it("tells an app it still has to be packaged and run", () => {
    // The opposite of what a website needs to be told, and the failure is the same
    // shape: a `dist/` that renders and cannot save anything is not an app.
    const { markdown, nextSteps } = buildFactorySpec(project());
    expect(markdown).not.toContain("## 🌐 Packaging & delivery");
    expect(markdown).toContain("## 🧱 Packaging & runtime");
    expect(nextSteps.join(" ")).toContain("scripts/package-project.py markdown-notes");
    expect(nextSteps.join(" ")).toContain("scripts/app-runtime.py --up markdown-notes");
    expect(nextSteps.join(" ")).toContain("Publish It");
  });

  it("returns the next steps as the same list the spec prints", () => {
    const spec = buildFactorySpec(website());
    for (const step of spec.nextSteps) {
      // The markdown renders them as an ordered list, so the leading marker is the
      // only difference — the text itself has to be identical or the UI and the
      // document would disagree about what to do next.
      expect(spec.markdown).toContain(step);
    }
  });
});

describe("planned specs", () => {
  const PLAN = {
    name: "Macro Log",
    slug: "macro-log",
    kind: "app" as const,
    summary: "Log meals and see the day's totals.",
    runtime: { language: "python", frameworks: ["flask"], database: "sqlite" },
    run: {
      install: "pip install -r requirements.txt",
      build: "",
      start: "python app.py",
      port: 5000,
      healthcheck: "/healthz",
    },
    files: [{ path: "app.py", purpose: "the server" }],
    notes: null,
  };

  const planned = (overrides: Partial<Project> = {}) => project({ plan: PLAN, ...overrides });

  it("names the plan's packager and not the fixed-stack ones", () => {
    // The failure this prevents is not hypothetical. A spec whose instructions say
    // "package the client with package-app.py" is read by the factory agent, which
    // then runs it — writing the React/Node scaffold over the stack the plan chose.
    // The first planned factory build did exactly that, and served a directory its
    // own image did not have.
    const { markdown, nextSteps } = buildFactorySpec(planned());
    const steps = nextSteps.join(" ");

    expect(steps).toContain("scripts/package-project.py markdown-notes");
    expect(steps).toContain("scripts/app-runtime.py --up markdown-notes");
    expect(steps).not.toContain("scripts/package-app.py");
    expect(markdown).not.toContain("scripts/package-app.py markdown-notes");
    // Said outright, because the agent will otherwise reach for a command it knows.
    expect(steps).toContain("Do not run `package-app.py`");
  });

  it("states the plan's stack instead of the one the packagers used to impose", () => {
    const { markdown } = buildFactorySpec(planned());
    expect(markdown).toContain("python (flask) + sqlite");
    expect(markdown).toContain("`python app.py` on port 5000");
    expect(markdown).toContain("`/healthz`");
    // The pre-planner sentences, which would tell the factory to build React + Node.
    expect(markdown).not.toContain("React 19 + TypeScript client, Node HTTP API, SQLite");
  });

  it("verifies with the commands and the path that will actually run", () => {
    const { markdown } = buildFactorySpec(planned());
    expect(markdown).toContain("pip install -r requirements.txt");
    expect(markdown).toContain("answers `/healthz` on port 5000");
    // An app's pre-planner bar named a file the old packager wrote, so "verified"
    // could mean "packaged by something that no longer builds this project".
    expect(markdown).not.toContain("dist/client/index.html");
    expect(markdown).not.toContain("/api/health");
  });

  it("keeps a planned website on its plan too", () => {
    // A planned website is an image with nginx in it, not a static tree, so the old
    // `package-website.py --publish` section would describe the wrong pipeline.
    const { markdown, nextSteps } = buildFactorySpec(
      planned({ kind: "website", title: "Product Site" }),
    );
    expect(markdown).not.toContain("## 🌐 Packaging & delivery");
    expect(markdown).toContain("## 🧱 Packaging & runtime");
    expect(nextSteps.join(" ")).toContain("package-project.py product-site");
    // The command form, not the name: the prohibition on running it is the point of
    // the section and has to be allowed to name it.
    expect(markdown).not.toContain("python3 scripts/package-website.py");
  });

  it("tells the agent not to package, because packaging is the next step", () => {
    const { markdown } = buildFactorySpec(planned());
    expect(markdown).toContain("do not run a");
    expect(markdown).toContain("packager: packaging is the step after this one");
  });

  it("sends a project with no stored plan down the plan-driven pipeline anyway", () => {
    // A project saved before the planner has no stack to state, but the factory
    // plans one from the spec before it builds — so the packaging instructions have
    // to be the ones that path takes. Naming the pre-planner packager here is what
    // made the factory agent run it, over the stack the factory had just planned.
    const { markdown, nextSteps } = buildFactorySpec(project());
    const steps = nextSteps.join(" ");

    expect(steps).toContain("scripts/package-project.py markdown-notes");
    expect(steps).not.toContain("scripts/package-app.py markdown-notes");
    // The older packagers are still named, as the fallback they are — a build
    // directory with no `plan.json` — rather than as the way to package this.
    expect(steps).toContain("no `plan.json`");
    expect(markdown).toContain("the factory plans the stack from this spec");
  });
});

describe("entryPoint", () => {
  it("prefers index.html and falls back to any html", () => {
    const files = [
      { path: "about.html", contents: "" },
      { path: "index.html", contents: "" },
    ];
    expect(entryPoint(files)).toBe("index.html");
    expect(entryPoint([{ path: "about.html", contents: "" }])).toBe("about.html");
    expect(entryPoint([{ path: "app.js", contents: "" }])).toBeNull();
  });

  it("prefers the client of a full-stack app, then its data model", () => {
    expect(entryPoint(project().files, "app")).toBe("src/App.tsx");
    expect(entryPoint([{ path: "server/schema.sql", contents: "" }], "app")).toBe(
      "server/schema.sql",
    );
    // A website has no server, so its schema is not an entry point for it.
    expect(entryPoint([{ path: "server/schema.sql", contents: "" }], "website")).toBeNull();
  });
});

/* ---- writing ------------------------------------------------------------ */

describe("factoryRequestsDir", () => {
  it("honours the override", () => {
    expect(factoryRequestsDir()).toBe(TMP);
  });

  it("falls back to build-requests/ beside the checkout", () => {
    delete process.env.STUDIO_FACTORY_REQUESTS_DIR;
    expect(factoryRequestsDir()).toBe(join(process.cwd(), "build-requests"));
  });
});

describe("writeFactorySpec", () => {
  it("writes the spec where the factory looks", () => {
    const app = project();
    const result = writeFactorySpec(app);

    expect(result.filename).toBe("markdown-notes.md");
    expect(SPEC_FILENAME_PATTERN.test(result.filename)).toBe(true);
    expect(dirname(result.path)).toBe(TMP);
    expect(result.replaced).toBe(false);
    expect(result.bytes).toBe(Buffer.byteLength(buildFactorySpec(app).markdown, "utf8"));

    // Exactly the bytes that were returned — no second rendering.
    expect(readFileSync(result.path, "utf8")).toBe(buildFactorySpec(app).markdown);
  });

  it("leaves no temporary file behind", () => {
    writeFactorySpec(project());
    expect(readdirSync(TMP)).toEqual(["markdown-notes.md"]);
  });

  it("refuses to overwrite a spec an operator may have edited", () => {
    writeFactorySpec(project());

    // Stand in for an operator's edit.
    writeFileSync(join(TMP, "markdown-notes.md"), "# hand-edited\n");

    expect(() => writeFactorySpec(project())).toThrow(FactorySpecError);
    try {
      writeFactorySpec(project());
    } catch (error) {
      expect((error as FactorySpecError).status).toBe(409);
    }

    expect(readFileSync(join(TMP, "markdown-notes.md"), "utf8")).toBe("# hand-edited\n");
  });

  it("replaces it when the operator asks", () => {
    writeFactorySpec(project());
    writeFileSync(join(TMP, "markdown-notes.md"), "# hand-edited\n");

    const result = writeFactorySpec(project({ prompt: "Updated." }), { overwrite: true });

    expect(result.replaced).toBe(true);
    expect(readFileSync(result.path, "utf8")).toContain("Updated.");
  });

  it("keeps a hostile title inside the requests directory", () => {
    const result = writeFactorySpec(project({ title: "../../../../etc/passwd" }));

    expect(result.filename).toBe("etc-passwd.md");
    expect(dirname(result.path)).toBe(TMP);
    expect(readdirSync(TMP)).toEqual(["etc-passwd.md"]);
  });

  it("names the fix when the directory cannot be written", () => {
    // A regular file where the directory should be: the write fails the way a
    // missing or read-only mount fails, without depending on uid permissions.
    writeFileSync(NOT_A_DIR, "not a directory\n");
    process.env.STUDIO_FACTORY_REQUESTS_DIR = NOT_A_DIR;

    try {
      writeFactorySpec(project());
      throw new Error("expected a refusal");
    } catch (error) {
      expect(error).toBeInstanceOf(FactorySpecError);
      expect((error as FactorySpecError).status).toBe(503);
      expect((error as FactorySpecError).message).toContain("chown 1001:1001");
      expect((error as FactorySpecError).message).toContain("STUDIO_FACTORY_REQUESTS_DIR");
    }
  });
});

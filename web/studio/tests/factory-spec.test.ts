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
    prompt: "A markdown notes app with a live preview pane.",
    files: [
      { path: "index.html", contents: "<!doctype html>\n<h1>Notes</h1>\n" },
      { path: "styles.css", contents: "body { margin: 0; }\n" },
      { path: "app.js", contents: "console.log('notes');\n" },
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
    expect(markdown).toContain("- HTML");
    expect(markdown).toContain("- CSS");
    expect(markdown).toContain("- JavaScript / TypeScript");
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
    const css = app.files.find((file) => file.path === "styles.css");

    expect(markdown).toContain("The app opens at `index.html`.");
    expect(markdown).toContain(`**\`styles.css\`** — styles (${css?.contents.length} B)`);
  });

  it("offers a runnable verification step rather than a slogan", () => {
    expect(buildFactorySpec(project()).markdown).toContain("Open `index.html`");

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
    expect(markdown).toContain("### `index.html`");
    expect(markdown).toContain("<!doctype html>");
    expect(markdown).toContain("console.log('notes');");
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

import { describe, expect, it } from "vitest";
import {
  DEFAULT_PORT,
  MAX_PORT,
  MIN_PORT,
  PlanError,
  extractJsonObject,
  parsePlan,
  planMessages,
  slugify,
} from "@/lib/plan";

/** A complete, valid plan, so each case can break exactly one thing. */
function validPlan(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    name: "Weight Tracker",
    summary: "Tracks daily weigh-ins with a small React client and a JSON API.",
    runtime: { language: "node", frameworks: ["react", "express"], database: "sqlite" },
    run: {
      install: "npm install",
      build: "npm run build",
      start: "npm start",
      port: 3000,
      healthcheck: "/",
    },
    files: [
      { path: "package.json", purpose: "Dependencies and scripts" },
      { path: "src/App.tsx", purpose: "The interface" },
    ],
    notes: null,
    ...overrides,
  });
}

describe("slugify", () => {
  it("turns a name into a hostname label", () => {
    expect(slugify("Weight Tracker")).toBe("weight-tracker");
    expect(slugify("  Recipe   Box!!  ")).toBe("recipe-box");
  });

  it("folds accents rather than dropping the letter", () => {
    expect(slugify("Café Bar")).toBe("cafe-bar");
  });

  it("never leaves a leading or trailing hyphen", () => {
    expect(slugify("--hello--world--")).toBe("hello-world");
    expect(slugify("!!!")).toBe("app");
    expect(slugify("!!!", "website")).toBe("website");
  });

  it("caps the length, including after a trailing hyphen is trimmed", () => {
    const slug = slugify("a".repeat(60));
    expect(slug.length).toBe(40);
    expect(slug).not.toMatch(/-$/);
  });

  it("is stable, so a republish keeps the same name", () => {
    expect(slugify("My App")).toBe(slugify("My App"));
  });
});

describe("extractJsonObject", () => {
  it("reads an object surrounded by prose", () => {
    expect(extractJsonObject('Here you go: {"a":1} hope that helps')).toBe('{"a":1}');
  });

  it("ignores braces inside strings", () => {
    const text = '{"note":"a } inside a string","ok":true}';
    expect(extractJsonObject(text)).toBe(text);
  });

  it("handles an escaped quote before a closing brace", () => {
    const text = String.raw`{"note":"say \"}\" now","ok":true}`;
    expect(extractJsonObject(text)).toBe(text);
  });

  it("returns null when the object is unterminated", () => {
    expect(extractJsonObject('{"a":1')).toBeNull();
  });

  it("returns null when there is no object at all", () => {
    expect(extractJsonObject("no json here")).toBeNull();
  });
});

describe("parsePlan", () => {
  it("parses a complete plan", () => {
    const plan = parsePlan(validPlan(), "app");

    expect(plan.name).toBe("Weight Tracker");
    expect(plan.slug).toBe("weight-tracker");
    expect(plan.kind).toBe("app");
    expect(plan.runtime.language).toBe("node");
    expect(plan.runtime.frameworks).toEqual(["react", "express"]);
    expect(plan.runtime.database).toBe("sqlite");
    expect(plan.run).toEqual({
      install: "npm install",
      build: "npm run build",
      start: "npm start",
      port: 3000,
      healthcheck: "/",
    });
    expect(plan.files.map((file) => file.path)).toEqual(["package.json", "src/App.tsx"]);
    expect(plan.notes).toBeNull();
  });

  it("reads a plan that arrives inside a markdown fence", () => {
    const plan = parsePlan("```json\n" + validPlan() + "\n```", "app");
    expect(plan.slug).toBe("weight-tracker");
  });

  it("takes the kind from the caller, not the reply", () => {
    // A model that answers "app" for a request the user marked as a website must
    // not be able to change what the button they pressed means.
    const plan = parsePlan(validPlan({ kind: "app" }), "website");
    expect(plan.kind).toBe("website");
  });

  it("refuses a plan with no start command", () => {
    const text = validPlan({ run: { install: "npm install", build: "", start: "" } });
    expect(() => parsePlan(text, "app")).toThrow(PlanError);
    expect(() => parsePlan(text, "app")).toThrow(/start command/);
  });

  it("refuses a reply with no JSON object", () => {
    expect(() => parsePlan("I would build a tracker.", "app")).toThrow(/JSON plan/);
  });

  it("refuses JSON that does not parse", () => {
    expect(() => parsePlan('{"name": "x", "run": {"start": "npm start"}', "app")).toThrow(
      /could not be parsed|JSON plan/,
    );
  });

  it("falls back to a name and summary rather than failing on them", () => {
    const plan = parsePlan(validPlan({ name: "", summary: "" }), "app");
    expect(plan.name).toBe("New app");
    expect(plan.summary).toBe("No summary was given.");
  });

  it("names a website when a website has no name", () => {
    expect(parsePlan(validPlan({ name: "" }), "website").name).toBe("New website");
  });

  it("replaces a port below the usable range with the default", () => {
    const plan = parsePlan(validPlan({ run: { start: "npm start", port: 80 } }), "app");
    expect(plan.run.port).toBe(DEFAULT_PORT);
  });

  it("replaces a port above the usable range with the default", () => {
    const plan = parsePlan(validPlan({ run: { start: "npm start", port: 60000 } }), "app");
    expect(plan.run.port).toBe(DEFAULT_PORT);
    expect(MIN_PORT).toBeLessThan(MAX_PORT);
  });

  it("accepts a port written as a string", () => {
    const plan = parsePlan(validPlan({ run: { start: "npm start", port: "8080" } }), "app");
    expect(plan.run.port).toBe(8080);
  });

  it("keeps a healthcheck that is a path and rejects one that is a URL", () => {
    expect(parsePlan(validPlan({ run: { start: "npm start", healthcheck: "/healthz" } }), "app").run.healthcheck).toBe(
      "/healthz",
    );
    expect(
      parsePlan(validPlan({ run: { start: "npm start", healthcheck: "http://example.com" } }), "app")
        .run.healthcheck,
    ).toBe("/");
  });

  it("strips a query string from a healthcheck", () => {
    const plan = parsePlan(validPlan({ run: { start: "npm start", healthcheck: "/health?deep=1" } }), "app");
    expect(plan.run.healthcheck).toBe("/health");
  });

  it("flattens a command onto one line", () => {
    const plan = parsePlan(
      validPlan({ run: { install: "npm ci", start: "npm   run   start\n  " } }),
      "app",
    );
    expect(plan.run.start).toBe("npm run start");
  });

  it("drops a file entry that escapes the project root", () => {
    const plan = parsePlan(
      validPlan({
        files: [
          { path: "../../etc/passwd", purpose: "nope" },
          { path: "src/App.tsx", purpose: "the interface" },
        ],
      }),
      "app",
    );
    expect(plan.files.map((file) => file.path)).toEqual(["src/App.tsx"]);
  });

  it("deduplicates the file list", () => {
    const plan = parsePlan(
      validPlan({
        files: [
          { path: "src/App.tsx", purpose: "first" },
          { path: "src/App.tsx", purpose: "again" },
        ],
      }),
      "app",
    );
    expect(plan.files).toHaveLength(1);
    expect(plan.files[0].purpose).toBe("first");
  });

  it("survives a plan whose fields are the wrong type entirely", () => {
    const plan = parsePlan(
      JSON.stringify({
        name: 42,
        summary: [],
        runtime: "node",
        run: { start: "npm start", port: {}, healthcheck: 7 },
        files: "package.json",
      }),
      "app",
    );

    expect(plan.name).toBe("New app");
    expect(plan.runtime.language).toBe("unknown");
    expect(plan.runtime.database).toBeNull();
    expect(plan.files).toEqual([]);
    expect(plan.run.port).toBe(DEFAULT_PORT);
  });
});

describe("planMessages", () => {
  it("states whether it is planning an app or a website", () => {
    const app = planMessages("a tracker", "app");
    const website = planMessages("a shop", "website");
    expect(app[0].content).toMatch(/FULL-STACK APPLICATION/);
    expect(website[0].content).toMatch(/WEBSITE/);
  });

  it("frames an existing project as an addition, not a rewrite", () => {
    const messages = planMessages("add charts", "app", [
      { path: "src/App.tsx", contents: "export default function App() { return null; }" },
    ]);

    expect(messages).toHaveLength(3);
    expect(messages[1].content).toMatch(/already exists/);
    expect(messages[1].content).toMatch(/src\/App\.tsx/);
    expect(messages[1].content).toMatch(/addition/);
    expect(messages[2].content).toBe("add charts");
  });

  it("sends only the system prompt and the request for a new project", () => {
    expect(planMessages("a tracker", "app")).toHaveLength(2);
  });
});

/**
 * The Studio → factory handoff.
 *
 * Studio and the factory build in opposite directions and, until now, did not
 * meet: Studio turns a sentence into a running app in the browser, while the
 * factory turns `build-requests/<name>.md` into a real app (`make app SPEC=…` →
 * the `archon-greenfield` workflow → `builds/`, and the same on push via
 * `.github/workflows/olympus-app-builder.yml`). A build you liked in Studio had
 * no way to become factory input.
 *
 * This module is that bridge, and it is deliberately **deterministic**: the spec
 * is assembled from what the build actually is — the instruction, the file set,
 * their sizes — with no second model call. A handoff you cannot predict is a
 * handoff you cannot review, and this file is meant to be read (and edited) by
 * the operator before anything is manufactured.
 *
 * The headings mirror `factory/APP_SPEC_TEMPLATE.md` exactly, so what lands in
 * `build-requests/` has the same shape `make new-request` scaffolds.
 */

import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { loadRepoEnv, repoRoot } from "./env";
import type { BuildPlan } from "./plan";
import type { Project, ProjectKind, StoredFile } from "./projects";

/**
 * Cap on the reference appendix. Beyond this the spec lists the files without
 * their contents: a spec that dwarfs what it specifies is worse than a short
 * one, and the factory builds from the spec, not from a code dump.
 */
export const MAX_APPENDIX_CHARS = 60_000;

/** A filename we are willing to write. Strict, because it becomes a path. */
export const SPEC_FILENAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,59}\.md$/;

export type FactorySpec = {
  /** Suggested filename inside `build-requests/` — path-safe by construction. */
  filename: string;
  markdown: string;
  /**
   * What to do with it next, in order, as commands that work as written.
   *
   * Returned alongside the markdown rather than only inside it: the UI offers
   * these as actions, and an operator should not have to copy a command out of a
   * document the app just generated in order to run the app's own handoff.
   */
  nextSteps: string[];
};

export type FactoryWriteResult = {
  filename: string;
  /** Absolute path on this machine (a container path under compose). */
  path: string;
  bytes: number;
  /** True when an existing spec was replaced. */
  replaced: boolean;
  nextSteps: string[];
};

/** A refusal the route can turn into a status. Never a crash. */
export class FactorySpecError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "FactorySpecError";
  }
}

/* ---- naming ------------------------------------------------------------- */

/**
 * A `build-requests/` filename stem. Everything outside `[a-z0-9]` collapses to
 * a single dash, so the result cannot contain a separator, a dot, or a leading
 * dash — the same posture as `safeFilePath`, but for a name we generate.
 */
export function specSlug(title: string): string {
  const slug = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 60)
    .replace(/-+$/, "");

  return slug || "studio-app";
}

function bytesLabel(count: number): string {
  if (count < 1024) return `${count} B`;
  return `${(count / 1024).toFixed(1)} KB`;
}

function matches(files: StoredFile[], pattern: RegExp): boolean {
  return files.some((file) => pattern.test(file.path.toLowerCase()));
}

/**
 * The file to open first, which depends on what the build is.
 *
 * A full-stack app has no page to open — its client is JSX that nothing runs until
 * it is built, so the file that matters is the interface the operator wrote and,
 * second, the data model the whole API is derived from. A website's is its
 * component. `index.html` is still checked last for both: it is what a build made
 * before either contract existed looks like, and an exported spec should say what
 * it is rather than claim the build is empty.
 */
export function entryPoint(files: StoredFile[], kind: ProjectKind = "app"): string | null {
  const first = (pattern: RegExp) => files.find((file) => pattern.test(file.path))?.path ?? null;

  const client = first(/(^|\/)src\/App\.(tsx|jsx)$/i);
  if (client) return client;
  if (kind === "app") {
    const schema = first(/(^|\/)server\/schema\.sql$/i);
    if (schema) return schema;
  }

  return first(/(^|\/)index\.html?$/i) ?? first(/\.html?$/i);
}

/* ---- inference ---------------------------------------------------------- */

function detectStack(files: StoredFile[], kind: ProjectKind, plan: BuildPlan | null): string[] {
  const stack: string[] = [];

  // The kind is stated first and unconditionally. It is not inferred from the file
  // names because it is not an inference: it is what the operator chose, and the
  // factory has to honour it (a website needs packaging; an app must not get it).
  //
  // A confirmed plan replaces the two sentences below, because they were the fixed
  // stacks the packagers used to impose — and a spec that states the old stack is
  // how the factory builds the old stack, whatever the project was planned as.
  if (plan) {
    const frameworks =
      plan.runtime.frameworks.length > 0 ? ` (${plan.runtime.frameworks.join(", ")})` : "";
    const database = plan.runtime.database ? ` + ${plan.runtime.database}` : "";
    stack.push(
      `${plan.runtime.language}${frameworks}${database} — from the plan this project was built to, which is what it is packaged and run as`,
    );
    stack.push(
      `Runs with \`${plan.run.start}\` on port ${plan.run.port}, health checked at \`${plan.run.healthcheck}\``,
    );
    // Named because it changes where the factory has to look when something is
    // empty: a Convex-targeted app whose reads return nothing has a deployment
    // problem, not a container problem, and the spec is what the factory reads.
    if (plan.target === "convex") {
      stack.push(
        "Data and functions on Convex (self-hosted in Atlas) — the deployment URL reaches the build as `CONVEX_URL`, and the container hosts the client only",
      );
    }
  } else if (kind === "website") {
    stack.push("Vite + React 19 + TypeScript (packaged to static `dist/`)");
  } else {
    stack.push(
      "React 19 + TypeScript client, Node HTTP API, SQLite (packaged into one " +
        "container per app — see `scripts/package-app.py`)",
    );
  }

  if (matches(files, /(^|\/)package\.json$/)) stack.push("Node.js (`package.json` present)");
  if (matches(files, /(^|\/)requirements\.txt$/) || matches(files, /\.py$/)) stack.push("Python");
  if (matches(files, /\.html?$/)) stack.push("HTML");
  if (matches(files, /\.css$/)) stack.push("CSS");
  if (matches(files, /\.(js|mjs|cjs|jsx|ts|tsx)$/)) stack.push("JavaScript / TypeScript");
  if (matches(files, /\.(sql|db|sqlite)$/)) stack.push("SQL / SQLite");
  if (matches(files, /\.(json|ya?ml|toml)$/)) stack.push("Config (JSON / YAML / TOML)");

  // The kind line above is what the operator chose; this is what the files say, and
  // with no files it says nothing. Claiming a stack from an empty build is the one
  // case where guessing is worse than admitting the gap — the factory would
  // manufacture something nobody described.
  if (files.length === 0) {
    stack.push("Not inferred from the file set — no files were saved, confirm before manufacturing");
  }

  return stack;
}

function verificationCriteria(
  files: StoredFile[],
  kind: ProjectKind,
  plan: BuildPlan | null,
): string[] {
  // A planned project's bar is its own plan, which is unusual in a good way: the
  // criteria are the commands that will actually run and the port that will actually
  // answer, so a build cannot pass this section and fail packaging. Naming a file
  // the old scaffolder produced is how "verified" came to mean "packaged by
  // something that no longer builds this project".
  if (plan) {
    const criteria: string[] = [];
    if (plan.run.install) criteria.push(`\`${plan.run.install}\` completes`);
    if (plan.run.build) criteria.push(`\`${plan.run.build}\` completes`);
    criteria.push(
      `the container built from \`plan.json\` starts with \`${plan.run.start}\` and answers \`${plan.run.healthcheck}\` on port ${plan.run.port}`,
    );
    criteria.push(
      kind === "website"
        ? "the served page renders at 360px and 1440px with no console errors"
        : "every screen the spec lists works against the API: with rows, with no rows, and while the request is in flight",
    );
    return criteria;
  }

  // A website's bar is the build, and nothing else it does matters if that fails.
  if (kind === "website") {
    return [
      "`npm ci && npm run build` completes and writes `dist/index.html`",
      "the packaged site is staged with `scripts/package-website.py <slug> --publish`",
      "`dist/` renders at 360px and 1440px with no console errors",
    ];
  }

  // An app's bar is that it runs and that its API answers — the client building is
  // necessary and nowhere near sufficient, because the failure this catches is a
  // screen that renders and then errors on its first request.
  if (matches(files, /(^|\/)server\/schema\.sql$/)) {
    return [
      "`python3 scripts/package-app.py <slug>` completes and writes `dist/client/index.html`",
      "the image builds and `make app-up SLUG=<slug>` answers `GET /api/health`",
      "every table in `server/schema.sql` is reachable at `/api/<table>` and every column the client sends exists on it (a mismatch is a `400`, not a silent drop)",
      "the client renders with rows, with no rows, and while the request is in flight",
    ];
  }

  if (matches(files, /(^|\/)package\.json$/)) return ["`npm ci`", "`npm test`"];
  if (matches(files, /(^|\/)(test_.*|.*_test)\.py$/)) return ["`python3 -m unittest`"];

  const entry = entryPoint(files, kind);
  if (entry) {
    return [
      `Open \`${entry}\` — Studio rendered it and reported no console errors before saving.`,
    ];
  }

  return ["State the command that proves this app works before manufacturing it."];
}

function fileRole(file: StoredFile): string {
  const name = file.path.toLowerCase();
  if (/(^|\/)schema\.sql$/.test(name)) return "data model — every table here becomes an `/api/<table>` endpoint";
  if (/\.sql$/.test(name)) return "SQL";
  if (/(^|\/)src\/app\.(tsx|jsx)$/.test(name)) return "the interface";
  if (/\.html?$/.test(name)) return "entry point";
  if (/\.css$/.test(name)) return "styles";
  if (/\.(js|mjs|cjs|jsx|ts|tsx)$/.test(name)) return "behaviour";
  if (/\.json$/.test(name)) return "data / config";
  if (/\.md$/.test(name)) return "documentation";
  return "supporting file";
}

/** Fence language for the appendix. Presentation only — never a path decision. */
function languageFor(path: string): string {
  const name = path.toLowerCase();
  if (/\.html?$/.test(name)) return "html";
  if (/\.css$/.test(name)) return "css";
  if (/\.(js|mjs|cjs)$/.test(name)) return "javascript";
  if (/\.(ts|tsx)$/.test(name)) return "typescript";
  if (/\.json$/.test(name)) return "json";
  if (/\.ya?ml$/.test(name)) return "yaml";
  if (/\.py$/.test(name)) return "python";
  if (/\.md$/.test(name)) return "markdown";
  return "text";
}

/* ---- assembly ----------------------------------------------------------- */

/**
 * Assemble the spec. Pure: the same project produces the same bytes, so the
 * output can be asserted in tests and diffed in review.
 */
export function buildFactorySpec(project: Project): FactorySpec {
  const files = project.files;
  const total = files.reduce((sum, file) => sum + file.contents.length, 0);
  const filename = `${specSlug(project.title)}.md`;
  const kind = project.kind;
  const steps = nextSteps(project, filename);

  const lines: string[] = [];

  lines.push(`# Application Specification: ${project.title}`, "");
  lines.push(`> Exported from Olympus Studio on ${project.updatedAt} (project \`${project.id}\`).`);
  lines.push(`>`);
  lines.push(`> Kind: **${kind === "website" ? "website" : "app"}**.`);
  lines.push("");

  // The handoff, first because it is the point of the file, and as commands rather
  // than prose so nothing has to be worked out twice.
  lines.push("## ▶︎ Next steps", "");
  steps.forEach((step, index) => lines.push(`${index + 1}. ${step}`));
  lines.push("");

  lines.push("## 🎯 Core Purpose", "");
  lines.push(
    project.prompt.trim() ||
      "Describe what the application does in one clear sentence — Studio saved no instruction for this app.",
    "",
  );

  lines.push("## 🧰 Tech Stack", "");
  for (const item of detectStack(files, kind, project.plan)) lines.push(`- ${item}`);
  lines.push("");

  lines.push("## 🛠️ Key Features & Pages", "");
  if (files.length === 0) {
    lines.push("Studio saved no files for this app.", "");
  } else {
    lines.push("As built in Studio:", "");
    for (const file of files) {
      lines.push(`- **\`${file.path}\`** — ${fileRole(file)} (${bytesLabel(file.contents.length)})`);
    }
    lines.push("");

    const entry = entryPoint(files, kind);
    if (entry) {
      lines.push(
        kind === "app"
          ? `The client's entry point is \`${entry}\`.`
          : `The site opens at \`${entry}\`.`,
        "",
      );
    }
  }

  lines.push("## 🚦 Verification Criteria", "");
  for (const item of verificationCriteria(files, kind, project.plan)) lines.push(`- ${item}`);
  lines.push("");

  // What packages this, and what the factory agent must not do to it.
  //
  // A planned project is packaged from its own plan, and that is worth a section of
  // its own because the failure it prevents is specific: an agent reading "package
  // the client with `package-app.py`" will run it, and that packager writes the
  // fixed React/Node scaffold into the working directory — over the stack the plan
  // had just chosen. The first planned factory build did exactly that, and the app
  // it produced served a directory its own image did not contain.
  if (project.plan) {
    const plan = project.plan;
    const frameworks = plan.runtime.frameworks.length > 0 ? ` (${plan.runtime.frameworks.join(", ")})` : "";
    const database = plan.runtime.database ? ` and ${plan.runtime.database}` : "";
    lines.push("## 🧱 Packaging & runtime", "");
    lines.push(
      `The stack is the plan's: ${plan.runtime.language}${frameworks}${database}.`,
      "`scripts/package-project.py` generates the Dockerfile from that plan — the base image",
      "from `runtime.language`, the plan's install and build commands run inside it — and",
      "`scripts/app-runtime.py` runs the image on the plan's port, checking the plan's",
      "healthcheck path. Nothing is installed on the host, and nothing else decides the stack.",
      "",
    );
    lines.push(
      "```bash",
      `python3 scripts/package-project.py ${specSlug(project.title)}`,
      `python3 scripts/app-runtime.py --up ${specSlug(project.title)} --build   # image + container`,
      "```",
      "",
    );
    lines.push(
      "Write only the files the plan lists, into the working directory, and do not run a",
      "packager: packaging is the step after this one, and the older packagers build a stack",
      "of their own rather than the one this project was planned in.",
      "",
    );
    if (plan.target === "convex") {
      lines.push(
        "**Data target: Convex.** The schema and the functions deploy to the self-hosted",
        "Convex that Atlas runs; `scripts/package-project.py` reads `CONVEX_URL` from the",
        "build environment and points the client at it (`CONVEX_URL`, `VITE_CONVEX_URL`,",
        "`NEXT_PUBLIC_CONVEX_URL`), so packaging refuses the build rather than producing a",
        "client that cannot reach a deployment. Deploying the functions themselves needs a",
        "deployment-scoped key in the build environment as `CONVEX_DEPLOY_KEY` — never the",
        "platform admin key, and never written into the project.",
        "",
      );
    }
  } else if (kind === "app") {
    // An app is not finished by generating it either, and what it needs is different
    // in kind: a client build is not an app, it is half of one. The other half is the
    // server that owns the database, and saying so here is what stops whoever picks
    // this up from shipping a `dist/` that cannot save anything.
    lines.push("## 🧱 Packaging & runtime", "");
    lines.push(
      "This is a full-stack application: React client, Node HTTP API, SQLite. The model",
      "wrote `src/App.tsx` and `server/schema.sql`; `scripts/package-app.py` writes the",
      "Vite project, the server that serves both the client and `/api/<table>`, and the",
      "`Dockerfile`. The API is derived from the tables in the schema, so a table the",
      "schema does not declare does not exist at runtime.",
      "",
    );
    lines.push(
      "```bash",
      `python3 scripts/package-app.py ${specSlug(project.title)}   # client build + archive`,
      `python3 scripts/app-runtime.py --up ${specSlug(project.title)} --build   # image + container`,
      "```",
      "",
    );
    lines.push(
      "Each app runs as its own container with its own loopback port and its own SQLite",
      "file under `OLYMPUS_APPS_ROOT` — see `docs/site-publishing.md`.",
      "",
    );
    lines.push(
      "This project has no stored plan, so the factory plans the stack from this spec",
      "before it builds — the Tech Stack above is what it starts from — and writes",
      "`plan.json` beside the build. `scripts/package-project.py` packages from that plan.",
      "The packager named above is the pre-planner one and applies only to a build",
      "directory with no `plan.json`.",
      "",
    );
  }

  // A website is not finished by generating it, and the difference is the whole
  // reason the spec has a kind. Saying so here is what stops the factory (or a
  // human) treating the source as the deliverable.
  if (kind === "website" && !project.plan) {
    lines.push("## 🌐 Packaging & delivery", "");
    lines.push(
      "This is a React site, so the source does not run anywhere on its own — JSX needs a",
      "build. The Vite project around `src/App.tsx` is generated by",
      "`scripts/package-website.py` with fixed dependencies, so the only thing the source",
      "has to satisfy is `src/App.tsx` default-exporting a component that imports nothing",
      "except `react`.",
      "",
    );
    lines.push("```bash", `python3 scripts/package-website.py ${specSlug(project.title)} --publish`, "```", "");
    lines.push(
      "That writes `builds/<slug>/dist/`, an archive of source + dist, and stages the built",
      "site under `OLYMPUS_SITES_ROOT` (default `/var/lib/olympus/sites`). Publishing it to a",
      "public name additionally needs an edge host — see `docs/site-publishing.md`.",
      "",
    );
    lines.push(
      "This project has no stored plan, so the factory plans the stack from this spec",
      "before it builds and writes `plan.json` beside the build; a planned website is an",
      "image with its server in it rather than a staged static tree, and it is",
      "`scripts/package-project.py` that packages it.",
      "",
    );
  }

  // The appendix is what makes this "continue building" rather than "build
  // something like this" — bounded so the spec stays readable.
  if (files.length > 0) {
    lines.push("## 📎 Reference build (from Studio)", "");
    if (total > MAX_APPENDIX_CHARS) {
      lines.push(
        `This build totals ${bytesLabel(total)}, too large to inline here. Fetch it from Studio`,
        `(project \`${project.id}\`) if you need the implementation.`,
        "",
      );
    } else {
      lines.push(
        "The files below are the build this spec came from. Treat them as reference for",
        "features and intent rather than a structure to preserve — the factory should build",
        "the app the spec describes.",
        "",
      );

      for (const file of files) {
        lines.push(`### \`${file.path}\``, "", "```" + languageFor(file.path), file.contents, "```", "");
      }
    }
  }

  return { filename, markdown: lines.join("\n"), nextSteps: steps };
}

/**
 * The handoff, as runnable commands.
 *
 * A list rather than a paragraph because these are the same two or three steps
 * every time, and the automatable ones are offered as buttons by the export flow.
 * The website ones include packaging, because for a website `make app` is only
 * half the job — it ends with source and nothing that runs.
 */
export function nextSteps(project: Project, filename: string): string[] {
  const slug = specSlug(project.title);
  const steps = [
    `Manufacture it here: \`make app SPEC=build-requests/${filename}\``,
    `Or commit it (\`git add build-requests/${filename}\`) — \`.github/workflows/olympus-app-builder.yml\` builds it on push.`,
  ];

  // A planned project is packaged from its own plan, and the two commands that
  // follow are the plan's packager and the plan's runtime. The older packagers are
  // named only when there is no plan, because naming one here is not advice to a
  // reader — the factory agent reads these steps too, and an instruction to run
  // `package-app.py` makes it generate the fixed React/Node scaffold *over* the
  // stack its own plan just chose. That is not hypothetical: it is what the first
  // planned factory build did, and the app it produced served a directory its image
  // did not have.
  if (project.plan) {
    steps.push(
      `Package and run it from its own plan: \`python3 scripts/package-project.py ${slug}\` then \`python3 scripts/app-runtime.py --up ${slug} --build\``,
    );
    steps.push(
      `Or do the same from Studio, which needs no shell: **Preview It** runs it and frames it under \`${slug}-preview.<suffix>\`, and **Publish It** puts it on \`${slug}.<suffix>\` (see docs/site-publishing.md).`,
    );
    steps.push(
      "Do not run `package-app.py` or `package-website.py` for this project. They build a fixed stack and would replace what the plan already decided.",
    );
    return steps;
  }

  // No stored plan: this project predates the planner, so the spec cannot state a
  // stack. The factory still plans one — `make app` picks the stack from this spec
  // and writes `plan.json` beside the build — so it is *that* plan which packages
  // and runs the result, and naming the older packagers here would describe a
  // pipeline this build will not take. They are worth a line only because a build
  // directory with no `plan.json` at all is still reachable by hand.
  steps.push(
    `Package and run it from the plan the factory wrote: \`python3 scripts/package-project.py ${slug}\` then \`python3 scripts/app-runtime.py --up ${slug} --build\``,
  );
  steps.push(
    `Or do it from Studio, which needs no shell: **Preview It** runs it and frames it under \`${slug}-preview.<suffix>\`, and **Publish It** puts it on \`${slug}.<suffix>\` (see docs/site-publishing.md).`,
  );
  steps.push(
    "Do not run `package-app.py` or `package-website.py` unless the build directory has no `plan.json`; they build a fixed stack and would ignore the one this spec was planned in.",
  );

  return steps;
}

/* ---- writing ------------------------------------------------------------ */

/**
 * Where specs are written.
 *
 * Under compose the `studio` service bind-mounts the repository's
 * `build-requests/` at `/app/build-requests`, and the container's cwd is `/app`
 * with no `.git` above it — so `repoRoot()` resolves to `/app` and the default
 * lands exactly on the mount. A local `next dev` run resolves to the checkout
 * instead, which is the same directory. `STUDIO_FACTORY_REQUESTS_DIR` overrides
 * both when a deployment keeps its requests elsewhere.
 */
export function factoryRequestsDir(): string {
  loadRepoEnv();
  const configured = process.env.STUDIO_FACTORY_REQUESTS_DIR?.trim();
  if (configured) return resolve(configured);
  return join(repoRoot(), "build-requests");
}

/**
 * Write a spec into `build-requests/`.
 *
 * Every path here is generated, never parsed from input, but the filename is
 * still re-validated against a strict pattern and the resolved target is
 * confirmed to sit inside the requests directory: the cost of that check is
 * nothing, and it is the difference between a bug and a traversal.
 *
 * Existing specs are not replaced unless `overwrite` is set — an operator's
 * edited spec is worth more than a fresh export of the app it came from.
 */
export function writeFactorySpec(
  project: Project,
  options: { overwrite?: boolean } = {},
): FactoryWriteResult {
  const spec = buildFactorySpec(project);

  if (!SPEC_FILENAME_PATTERN.test(spec.filename)) {
    throw new FactorySpecError("Refusing to write a spec with an unsafe filename.", 500);
  }

  const dir = factoryRequestsDir();
  const target = join(dir, spec.filename);

  if (!target.startsWith(dir + sep)) {
    throw new FactorySpecError("Refusing to write outside build-requests/.", 500);
  }

  const replaced = existsSync(target);
  if (replaced && !options.overwrite) {
    throw new FactorySpecError(
      `build-requests/${spec.filename} already exists — export with overwrite to replace it.`,
      409,
    );
  }

  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    throw new FactorySpecError(notWritable(dir), 503);
  }

  // Write beside the target and rename, so a crash mid-write cannot leave a
  // half-written spec that the factory would happily manufacture from.
  const temporary = join(dir, `.${spec.filename}.${randomBytes(4).toString("hex")}.tmp`);

  try {
    writeFileSync(temporary, spec.markdown, { encoding: "utf8", mode: 0o644 });
    renameSync(temporary, target);
  } catch {
    rmSync(temporary, { force: true });
    throw new FactorySpecError(notWritable(dir), 503);
  }

  return {
    filename: spec.filename,
    path: target,
    bytes: Buffer.byteLength(spec.markdown, "utf8"),
    replaced,
    nextSteps: spec.nextSteps,
  };
}

/**
 * The two ways this fails in practice, named so the fix is in the message: the
 * directory is not mounted (a plain `docker compose up` without the bind), or
 * it is mounted but owned by someone the container's uid cannot write as.
 */
function notWritable(dir: string): string {
  return (
    `Could not write to ${dir}. Studio runs as uid 1001: the directory must be ` +
    `bind-mounted and writable by that uid (chown 1001:1001), or set ` +
    `STUDIO_FACTORY_REQUESTS_DIR to a writable path.`
  );
}

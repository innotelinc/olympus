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

/** The file a browser would open first, if the build produced one. */
export function entryPoint(files: StoredFile[]): string | null {
  const html = files.filter((file) => /\.html?$/i.test(file.path));
  if (html.length === 0) return null;
  return (html.find((file) => /(^|\/)index\.html?$/i.test(file.path)) ?? html[0]).path;
}

/* ---- inference ---------------------------------------------------------- */

function detectStack(files: StoredFile[], kind: ProjectKind): string[] {
  const stack: string[] = [];

  // The kind is stated first and unconditionally. It is not inferred from the file
  // names because it is not an inference: it is what the operator chose, and the
  // factory has to honour it (a website needs packaging; an app must not get it).
  if (kind === "website") {
    stack.push("Vite + React 19 + TypeScript (packaged to static `dist/`)");
  } else {
    stack.push("Self-contained HTML / CSS / JavaScript (no build step)");
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

function verificationCriteria(files: StoredFile[], kind: ProjectKind): string[] {
  // A website's bar is the build, and nothing else it does matters if that fails.
  if (kind === "website") {
    return [
      "`npm ci && npm run build` completes and writes `dist/index.html`",
      "the packaged site is staged with `scripts/package-website.py <slug> --publish`",
      "`dist/` renders at 360px and 1440px with no console errors",
    ];
  }

  if (matches(files, /(^|\/)package\.json$/)) return ["`npm ci`", "`npm test`"];
  if (matches(files, /(^|\/)(test_.*|.*_test)\.py$/)) return ["`python3 -m unittest`"];

  const entry = entryPoint(files);
  if (entry) {
    return [`Open \`${entry}\` — Studio's sandboxed preview rendered it with no console errors.`];
  }

  return ["State the command that proves this app works before manufacturing it."];
}

function fileRole(file: StoredFile): string {
  const name = file.path.toLowerCase();
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
  for (const item of detectStack(files, kind)) lines.push(`- ${item}`);
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

    const entry = entryPoint(files);
    if (entry) lines.push(`The app opens at \`${entry}\`.`, "");
  }

  lines.push("## 🚦 Verification Criteria", "");
  for (const item of verificationCriteria(files, kind)) lines.push(`- ${item}`);
  lines.push("");

  // A website is not finished by generating it, and the difference is the whole
  // reason the spec has a kind. Saying so here is what stops the factory (or a
  // human) treating the source as the deliverable.
  if (kind === "website") {
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

  if (project.kind === "website") {
    steps.push(
      `Package the result into a servable site: \`python3 scripts/package-website.py ${slug} --publish\``,
    );
    steps.push(
      "Built sites live under /var/lib/olympus/sites — serve one on a name with `make site-publish SLUG=" +
        slug +
        " HOST=<name>` (see docs/site-publishing.md).",
    );
  } else {
    steps.push(
      "The app is self-contained: open builds/" + slug + "/index.html, or drop it on any static host.",
    );
  }

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

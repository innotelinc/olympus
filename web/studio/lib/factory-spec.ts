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
import type { Project, StoredFile } from "./projects";

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
};

export type FactoryWriteResult = {
  filename: string;
  /** Absolute path on this machine (a container path under compose). */
  path: string;
  bytes: number;
  /** True when an existing spec was replaced. */
  replaced: boolean;
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

function detectStack(files: StoredFile[]): string[] {
  const stack: string[] = [];

  if (matches(files, /(^|\/)package\.json$/)) stack.push("Node.js (`package.json` present)");
  if (matches(files, /(^|\/)requirements\.txt$/) || matches(files, /\.py$/)) stack.push("Python");
  if (matches(files, /\.html?$/)) stack.push("HTML");
  if (matches(files, /\.css$/)) stack.push("CSS");
  if (matches(files, /\.(js|mjs|cjs|jsx|ts|tsx)$/)) stack.push("JavaScript / TypeScript");
  if (matches(files, /\.(sql|db|sqlite)$/)) stack.push("SQL / SQLite");
  if (matches(files, /\.(json|ya?ml|toml)$/)) stack.push("Config (JSON / YAML / TOML)");

  if (stack.length === 0) {
    stack.push("Not inferred from the file set — confirm before manufacturing");
  }

  return stack;
}

function verificationCriteria(files: StoredFile[]): string[] {
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

  const lines: string[] = [];

  lines.push(`# Application Specification: ${project.title}`, "");
  lines.push(`> Exported from Olympus Studio on ${project.updatedAt} (project \`${project.id}\`).`);
  lines.push(">");
  lines.push(`> Manufacture it locally with \`make app SPEC=build-requests/${filename}\`.`);
  lines.push(
    "> Committing it under `build-requests/` also triggers `.github/workflows/olympus-app-builder.yml`.",
  );
  lines.push("");

  lines.push("## 🎯 Core Purpose", "");
  lines.push(
    project.prompt.trim() ||
      "Describe what the application does in one clear sentence — Studio saved no instruction for this app.",
    "",
  );

  lines.push("## 🧰 Tech Stack", "");
  for (const item of detectStack(files)) lines.push(`- ${item}`);
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
  for (const item of verificationCriteria(files)) lines.push(`- ${item}`);
  lines.push("");

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

  return { filename, markdown: lines.join("\n") };
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

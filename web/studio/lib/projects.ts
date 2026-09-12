import { createHash, randomBytes } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
  type Dirent,
} from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { loadRepoEnv, repoRoot } from "./env";

/**
 * Saved apps, per identity.
 *
 * Studio used to keep generated files in React state only, so a reload lost the
 * app. The library stores each build on disk under a directory owned by the
 * signed-in Cerulean Authentik subject, and every route reaches it through
 * `namespaceFor` — the same one-way mapping, so no caller has to decide where a
 * given user's files live.
 *
 * Why the subject is hashed rather than used directly: `sub` is a claim from the
 * identity provider. Authentik happens to send an opaque hash here
 * (`sub_mode: hashed_user_id`), but a claim is not a filename — it is attacker-
 * influenced by definition if the provider is ever swapped, and a value that can
 * be `../../etc` must never reach a path join. Hashing it to a fixed-length hex
 * name removes that class of bug instead of relying on validation to catch it.
 *
 * Nothing here trusts an identifier to be path-shaped: project ids are matched
 * against a strict pattern and file paths are normalized before they are stored.
 */

export type StoredFile = {
  path: string;
  contents: string;
};

export type Project = {
  id: string;
  title: string;
  /** The instruction that produced (or last revised) this app. */
  prompt: string;
  files: StoredFile[];
  createdAt: string;
  updatedAt: string;
};

export type ProjectSummary = {
  id: string;
  title: string;
  updatedAt: string;
  fileCount: number;
};

export type SaveResult = {
  project: Project;
  created: boolean;
};

/**
 * Namespace used when OIDC is not configured. Studio is then a single-operator
 * tool, so there is exactly one library and no identity to separate.
 */
export const ANONYMOUS_NAMESPACE = "single-operator";

/** Bounds, matching the route handler's posture: refuse rather than truncate. */
export const MAX_PROJECTS = 200;
export const MAX_FILES = 40;
export const MAX_FILE_CHARS = 200_000;
export const MAX_PROJECT_CHARS = 2_000_000;
export const MAX_PROMPT_CHARS = 8_000;
export const MAX_TITLE_CHARS = 120;
export const MAX_PATH_CHARS = 200;

const ID_PATTERN = /^[A-Za-z0-9_-]{6,64}$/;

/** A refusal the route can turn into a status. Never a crash. */
export class ProjectError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ProjectError";
  }
}

/* ---- identity ----------------------------------------------------------- */

/**
 * Map an OIDC subject onto a directory name.
 *
 * Fixed-length hex, so the result cannot contain a separator, a dot segment, or
 * anything else that changes where a path points. The same subject always maps
 * to the same directory, which is what makes the library persist across
 * restarts without a database.
 */
export function namespaceFor(sub: string | null | undefined): string {
  const subject = sub?.trim();
  if (!subject) return ANONYMOUS_NAMESPACE;
  return `u-${createHash("sha256").update(subject).digest("hex").slice(0, 32)}`;
}

/* ---- location ----------------------------------------------------------- */

/**
 * Where the library lives. `STUDIO_DATA_DIR` wins (the container points it at a
 * volume); otherwise `data/studio` beside the checkout, which `.gitignore`
 * already covers.
 */
export function studioDataDir(): string {
  loadRepoEnv();
  const configured = process.env.STUDIO_DATA_DIR?.trim();
  if (configured) return resolve(configured);
  return join(repoRoot(), "data", "studio");
}

function namespaceDir(namespace: string): string {
  return join(studioDataDir(), namespace);
}

function projectPath(namespace: string, id: string): string {
  return join(namespaceDir(namespace), `${id}.json`);
}

/* ---- validation --------------------------------------------------------- */

export function newProjectId(): string {
  return randomBytes(9).toString("base64url");
}

export function isProjectId(value: unknown): value is string {
  return typeof value === "string" && ID_PATTERN.test(value.trim());
}

export function parseTitle(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().replace(/\s+/g, " ").slice(0, MAX_TITLE_CHARS);
}

/**
 * Normalize a generated file path, or drop it.
 *
 * Stored paths end up in the code viewer and the preview document, so they stay
 * relative and traversal-free — the same contract `parseFiles` produces.
 */
export function safeFilePath(value: unknown): string {
  const raw = typeof value === "string" ? value.trim().replace(/\\/g, "/") : "";
  if (!raw || raw.length > MAX_PATH_CHARS) return "";
  if (isAbsolute(raw) || /^[a-zA-Z]:/.test(raw)) return "";

  const segments = raw.split("/").filter((segment) => segment && segment !== ".");
  if (segments.length === 0 || segments.some((segment) => segment === "..")) return "";

  return segments.join("/");
}

/** Validate the incoming file list, enforcing the size caps. */
export function parseStoredFiles(value: unknown): StoredFile[] {
  if (!Array.isArray(value)) return [];

  const files: StoredFile[] = [];
  const seen = new Set<string>();
  let total = 0;

  for (const entry of value) {
    if (typeof entry !== "object" || entry === null) continue;

    const record = entry as Record<string, unknown>;
    const path = safeFilePath(record.path);
    if (!path || seen.has(path)) continue;

    const contents = typeof record.contents === "string" ? record.contents : "";
    if (contents.length > MAX_FILE_CHARS) {
      throw new ProjectError(
        `"${path}" is too large to save (${contents.length} characters, limit ${MAX_FILE_CHARS}).`,
        413,
      );
    }

    seen.add(path);
    total += contents.length;
    files.push({ path, contents });

    if (files.length > MAX_FILES) {
      throw new ProjectError(`Too many files to save (limit ${MAX_FILES}).`, 413);
    }
  }

  if (total > MAX_PROJECT_CHARS) {
    throw new ProjectError(
      `This app is too large to save (${total} characters, limit ${MAX_PROJECT_CHARS}).`,
      413,
    );
  }

  return files;
}

function parseProject(value: unknown): Project | null {
  if (typeof value !== "object" || value === null) return null;

  const record = value as Record<string, unknown>;
  if (!isProjectId(record.id)) return null;

  const files: StoredFile[] = [];
  if (Array.isArray(record.files)) {
    for (const entry of record.files) {
      if (typeof entry !== "object" || entry === null) continue;
      const file = entry as Record<string, unknown>;
      const path = safeFilePath(file.path);
      if (!path) continue;
      files.push({ path, contents: typeof file.contents === "string" ? file.contents : "" });
    }
  }

  const updatedAt =
    typeof record.updatedAt === "string" ? record.updatedAt : new Date(0).toISOString();

  return {
    id: record.id.trim(),
    title: parseTitle(record.title) || "Untitled app",
    prompt: typeof record.prompt === "string" ? record.prompt : "",
    files,
    createdAt: typeof record.createdAt === "string" ? record.createdAt : updatedAt,
    updatedAt,
  };
}

/* ---- reads -------------------------------------------------------------- */

/** Full project, or null when it does not exist (or is unreadable). */
export function readProject(namespace: string, id: string): Project | null {
  if (!isProjectId(id)) return null;

  const file = projectPath(namespace, id.trim());
  if (!existsSync(file)) return null;

  try {
    return parseProject(JSON.parse(readFileSync(file, "utf8")));
  } catch {
    // A half-written or hand-edited file is one dead entry, not a broken library.
    return null;
  }
}

/** Newest first. Unreadable entries are skipped rather than failing the list. */
export function listProjects(namespace: string): ProjectSummary[] {
  const dir = namespaceDir(namespace);

  let entries: Dirent[];
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }

  const summaries: ProjectSummary[] = [];

  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) continue;

    const id = entry.name.slice(0, -".json".length);
    const project = readProject(namespace, id);
    if (!project) continue;

    summaries.push({
      id: project.id,
      title: project.title,
      updatedAt: project.updatedAt,
      fileCount: project.files.length,
    });
  }

  summaries.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  return summaries;
}

/* ---- writes ------------------------------------------------------------- */

function writeProject(namespace: string, project: Project): void {
  const dir = namespaceDir(namespace);
  mkdirSync(dir, { recursive: true, mode: 0o700 });

  const target = projectPath(namespace, project.id);
  // Write beside the target and rename: a crash mid-write leaves the previous
  // version intact instead of a truncated file that would read as corruption.
  const temporary = join(dir, `.${project.id}.${randomBytes(4).toString("hex")}.tmp`);

  try {
    const body = `${JSON.stringify(project, null, 2)}\n`;
    writeFileSync(temporary, body, { encoding: "utf8", mode: 0o600 });
    renameSync(temporary, target);
  } catch (error) {
    rmSync(temporary, { force: true });
    throw error;
  }
}

/**
 * Create or update an app. Supplying an existing `id` updates it in place and
 * keeps `createdAt`; anything else creates a new one under a fresh id.
 */
export function saveProject(namespace: string, input: Record<string, unknown>): SaveResult {
  const files = parseStoredFiles(input.files);
  const prompt = typeof input.prompt === "string" ? input.prompt.slice(0, MAX_PROMPT_CHARS) : "";

  const requestedId = isProjectId(input.id) ? input.id.trim() : "";
  const existing = requestedId ? readProject(namespace, requestedId) : null;

  if (!existing && listProjects(namespace).length >= MAX_PROJECTS) {
    throw new ProjectError(
      `This account already has ${MAX_PROJECTS} saved apps. Delete one to make room.`,
      413,
    );
  }

  const timestamp = new Date().toISOString();
  const project: Project = {
    id: existing?.id ?? newProjectId(),
    title: parseTitle(input.title) || existing?.title || "Untitled app",
    prompt: prompt || existing?.prompt || "",
    files,
    createdAt: existing?.createdAt ?? timestamp,
    updatedAt: timestamp,
  };

  writeProject(namespace, project);
  return { project, created: !existing };
}

/** True when something was removed. An unknown id is not an error. */
export function deleteProject(namespace: string, id: string): boolean {
  if (!isProjectId(id)) return false;

  try {
    rmSync(projectPath(namespace, id.trim()));
    return true;
  } catch {
    return false;
  }
}

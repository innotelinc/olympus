/**
 * Queue a Studio build for the host-side runner.
 *
 * Studio cannot run `make app` itself. Its final image is a traced Next.js
 * bundle — no Archon CLI, no Codex CLI, no `uv`, no checkout — because a
 * browser-facing service has no business carrying a coding agent. The runner
 * (`scripts/build-runner.py`, installed as `olympus-build-runner.service`) runs
 * where the toolchain actually is, and this module is the handoff: it writes the
 * spec, then a small request file into a directory both sides share.
 *
 * The queue is untrusted input on the runner's side, and it is treated that way
 * there — the spec path is re-resolved against `build-requests/`, the slug is
 * re-validated, and the build directory is re-derived. Nothing here is trusted
 * just because it came from here; this module's job is to write a request that is
 * correct, and to read back what the runner recorded.
 */

import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { loadRepoEnv, repoRoot } from "./env";
import { FactorySpecError, specSlug, writeFactorySpec } from "./factory-spec";
import type { Project } from "./projects";

/** Job ids as the runner enforces them: lowercase hex, exactly 16 characters. */
export const JOB_ID_PATTERN = /^[0-9a-f]{16}$/;

/**
 * How long a runner heartbeat stays meaningful. The runner beats every 10s
 * while idle and at least every 5s during a build, so a minute of silence means
 * it is not there — and queueing a build nobody will pick up is worse than
 * saying so.
 */
export const RUNNER_STALE_SECONDS = 60;

export type BuildState = "running" | "succeeded" | "failed";

export type BuildArtifact = {
  dir: string;
  files: number | null;
  bytes: number | null;
  entry: string | null;
};

export type BuildStatus = {
  job: string;
  state: BuildState;
  slug: string | null;
  title: string | null;
  spec: string | null;
  requestedAt: string | null;
  requestedBy: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
  exitCode: number | null;
  message: string;
  artifact: BuildArtifact | null;
  logTail: string;
};

export type RunnerState = {
  /** True when a heartbeat is recent enough to believe a runner is alive. */
  live: boolean;
  /** Seconds since the last heartbeat, or null when there is none. */
  ageSeconds: number | null;
  pid: number | null;
  host: string | null;
  busyWith: string | null;
};

export type QueuedBuild = {
  job: string;
  slug: string;
  filename: string;
  spec: string;
  path: string;
  replaced: boolean;
  runner: RunnerState;
};

/** A refusal the routes turn into a status. Never a crash. */
export class BuildQueueError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "BuildQueueError";
  }
}

/* ---- locations ---------------------------------------------------------- */

/**
 * Where requests and statuses live.
 *
 * `.factory/` is the repository's untracked runtime-state tree — the same one
 * the scheduler markers use — so a queue of requests and logs lands somewhere
 * that is already understood to be runtime state rather than source. Under
 * compose the directory is bind-mounted into the container; `STUDIO_BUILD_QUEUE_DIR`
 * overrides it.
 */
export function buildQueueDir(): string {
  loadRepoEnv();
  const configured = process.env.STUDIO_BUILD_QUEUE_DIR?.trim();
  if (configured) return resolve(configured);
  return join(repoRoot(), ".factory", "build-queue");
}

function readJson(path: string): unknown {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as unknown;
  } catch {
    return null;
  }
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/* ---- runner presence ---------------------------------------------------- */

/**
 * Whether a build runner is alive, from the heartbeat it writes.
 *
 * Clock skew between the runner and this process is possible in principle; the
 * ages are clamped at zero so a slightly-future timestamp reads as "just now"
 * rather than as a negative age that no comparison handles sensibly.
 */
export function readRunnerState(): RunnerState {
  const dir = buildQueueDir();
  const raw = readJson(join(dir, "runner.heartbeat.json"));

  if (typeof raw !== "object" || raw === null) {
    return { live: false, ageSeconds: null, pid: null, host: null, busyWith: null };
  }

  const beat = raw as Record<string, unknown>;
  const beatAt = asString(beat.beat_at);
  const parsed = beatAt ? Date.parse(beatAt) : Number.NaN;
  const ageSeconds = Number.isFinite(parsed)
    ? Math.max(0, Math.round((Date.now() - parsed) / 1000))
    : null;

  return {
    live: ageSeconds !== null && ageSeconds <= RUNNER_STALE_SECONDS,
    ageSeconds,
    pid: asNumber(beat.pid),
    host: asString(beat.host),
    busyWith: asString(beat.busy_with),
  };
}

/* ---- queueing ----------------------------------------------------------- */

/** A job id we are willing to use as a filename. Generated, then re-checked. */
function newJobId(): string {
  const job = randomBytes(8).toString("hex");
  if (!JOB_ID_PATTERN.test(job)) {
    throw new BuildQueueError("Could not generate a job id.", 500);
  }
  return job;
}

/**
 * Write the spec and queue a build for it.
 *
 * `replace` is one decision that covers both overwrites: an existing spec in
 * `build-requests/` and an existing app in `builds/`. Both are refused without
 * it, because both destroy something the operator may have been comparing
 * against. The UI asks first, exactly as the export flow does.
 */
export function queueBuild(project: Project, options: { replace?: boolean } = {}): QueuedBuild {
  const replace = options.replace === true;
  const dir = buildQueueDir();

  // Writability first. A queue that cannot be written is a deployment fault, and
  // reporting "no runner" for it would send the operator to fix the wrong thing.
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    throw new BuildQueueError(notWritable(dir), 503);
  }

  const runner = readRunnerState();
  if (!runner.live) {
    throw new BuildQueueError(
      "No build runner is alive, so nothing would pick this up. Start it with " +
        "`sudo systemctl start olympus-build-runner` (install it first with " +
        "`scripts/install-build-runner.sh`) — see `python3 scripts/build-runner.py --list`.",
      503,
    );
  }

  // The spec is regenerated unconditionally: the build must describe what is on
  // screen, not whatever was exported last time. The conflict is surfaced the
  // same way the export route surfaces it, so the UI has one flow to handle.
  const written = writeFactorySpec(project, { overwrite: replace });

  const slug = specSlug(project.title);
  const job = newJobId();
  const request = {
    v: 1,
    job,
    spec: `build-requests/${written.filename}`,
    slug,
    title: project.title.slice(0, 120),
    requested_by: "studio",
    requested_at: new Date().toISOString(),
    replace,
  };

  // Beside the target, then renamed: the runner scans this directory
  // continuously, and a half-written request must never be claimable.
  const target = join(dir, `${job}.request.json`);
  const temporary = join(dir, `.${job}.request.json.${process.pid}.tmp`);

  try {
    writeFileSync(temporary, `${JSON.stringify(request, null, 2)}\n`, { encoding: "utf8", mode: 0o644 });
    renameSync(temporary, target);
  } catch {
    rmSync(temporary, { force: true });
    throw new BuildQueueError(notWritable(dir), 503);
  }

  return {
    job,
    slug,
    filename: written.filename,
    spec: request.spec,
    path: target,
    replaced: written.replaced,
    runner,
  };
}

/**
 * The two ways this fails in practice, named so the fix is in the message: the
 * queue is not mounted, or it is mounted but owned by someone uid 1001 cannot
 * write as.
 */
function notWritable(dir: string): string {
  return (
    `Could not write to the build queue at ${dir}. Studio runs as uid 1001: the ` +
    `directory must be bind-mounted and writable by that uid (chown 1001:1001), ` +
    `or set STUDIO_BUILD_QUEUE_DIR to a writable path.`
  );
}

/* ---- reading status ----------------------------------------------------- */

/** Normalise a status file written by another process into the shape we render. */
function toBuildStatus(raw: unknown): BuildStatus | null {
  if (typeof raw !== "object" || raw === null) return null;

  const record = raw as Record<string, unknown>;
  const job = asString(record.job);
  if (!job || !JOB_ID_PATTERN.test(job)) return null;

  const state = asString(record.state);
  if (state !== "running" && state !== "succeeded" && state !== "failed") return null;

  const artifact = record.artifact;

  return {
    job,
    state,
    slug: asString(record.slug),
    title: asString(record.title),
    spec: asString(record.spec),
    requestedAt: asString(record.requested_at),
    requestedBy: asString(record.requested_by),
    startedAt: asString(record.started_at),
    finishedAt: asString(record.finished_at),
    updatedAt: asString(record.updated_at),
    exitCode: asNumber(record.exit_code),
    message: asString(record.message) ?? "",
    artifact:
      typeof artifact === "object" && artifact !== null
        ? {
            dir: asString((artifact as Record<string, unknown>).dir) ?? "",
            files: asNumber((artifact as Record<string, unknown>).files),
            bytes: asNumber((artifact as Record<string, unknown>).bytes),
            entry: asString((artifact as Record<string, unknown>).entry),
          }
        : null,
    logTail: asString(record.log_tail) ?? "",
  };
}

/**
 * Read one job's status. The job id is validated before it is used as a path, so
 * a caller-supplied value can never name a file outside the queue.
 */
export function readBuildStatus(job: string): BuildStatus | null {
  if (!JOB_ID_PATTERN.test(job)) return null;
  return toBuildStatus(readJson(join(buildQueueDir(), `${job}.status.json`)));
}

/**
 * The most recent finished or in-progress build for an app, so reopening it
 * shows the last result instead of forgetting that one ever ran.
 */
export function latestBuildStatus(slug: string): BuildStatus | null {
  const dir = buildQueueDir();

  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return null;
  }

  let newest: BuildStatus | null = null;

  for (const name of names) {
    if (!name.endsWith(".status.json") || name.startsWith(".")) continue;

    const status = toBuildStatus(readJson(join(dir, name)));
    if (!status || status.slug !== slug) continue;

    const at = status.updatedAt ? Date.parse(status.updatedAt) : Number.NaN;
    const best = newest?.updatedAt ? Date.parse(newest.updatedAt) : Number.NaN;
    // A running build always wins: it is the thing the operator is watching.
    const newer =
      newest === null ||
      status.state === "running" ||
      (newest.state !== "running" && Number.isFinite(at) && (!Number.isFinite(best) || at > best));

    if (newer) newest = status;
  }

  return newest;
}

/** Re-exported so a route can distinguish "the spec exists" from other failures. */
export { FactorySpecError };

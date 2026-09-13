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
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  readSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { loadRepoEnv, repoRoot } from "./env";
import { FactorySpecError, specSlug, writeFactorySpec } from "./factory-spec";
import type { Project, ProjectKind } from "./projects";

/** Job ids as the runner enforces them: lowercase hex, exactly 16 characters. */
export const JOB_ID_PATTERN = /^[0-9a-f]{16}$/;

/**
 * How long a runner heartbeat stays meaningful. The runner beats every 10s
 * while idle and at least every 5s during a build, so a minute of silence means
 * it is not there — and queueing a build nobody will pick up is worse than
 * saying so.
 */
export const RUNNER_STALE_SECONDS = 60;

/**
 * `cancelled` is its own state, not a flavour of `failed`: the operator asked for
 * it, and a panel that reports a deliberate stop as a failure trains people to
 * ignore failures.
 */
export type BuildState = "running" | "succeeded" | "failed" | "cancelled";

/**
 * Which job the queue is carrying.
 *
 * `build` runs the factory on a spec (`make app`). `publish` takes the files
 * Studio has on screen, packages them and puts them on a name. `preview` takes the
 * same files and packages them the same way, then stops before the name — the
 * project runs, so the frame has something real behind it, but nothing is
 * announced. None of the three is a flag on another: they manufacture different
 * things, and one of them manufactures nothing at all.
 */
export type BuildAction = "build" | "publish" | "preview";

export type BuildArtifact = {
  dir: string;
  files: number | null;
  bytes: number | null;
  entry: string | null;
};

/**
 * What packaging a website produced, as `package-website.py` recorded it.
 *
 * Separate from `artifact` because it answers a different question. `artifact`
 * says the agent wrote files; this says the toolchain turned them into something
 * that can be served. A website build can have the first and not the second, and
 * that state must not render as success.
 */
export type BuildSite = {
  entry: string | null;
  distFiles: number | null;
  distBytes: number | null;
  sourceFiles: number | null;
  zip: string | null;
  builtAt: string | null;
};

export type BuildStatus = {
  job: string;
  state: BuildState;
  /** What kind of job this is. A status written before the field existed is a build. */
  action: BuildAction;
  slug: string | null;
  title: string | null;
  spec: string | null;
  requestedAt: string | null;
  requestedBy: string | null;
  /**
   * The language the project was planned in, when it has a plan. Reported on every
   * status of the job, so a build in progress can say what it is building rather
   * than only where it got to.
   */
  language: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
  exitCode: number | null;
  message: string;
  artifact: BuildArtifact | null;
  /** Present only for a packaged website build. */
  site: BuildSite | null;
  /** Present only for a publish job that put the site on a name. */
  publishedUrl: string | null;
  /**
   * Present only for a preview job: the address the project was started on, as
   * `app-runtime.py` recorded it. Deliberately not the published URL — a preview
   * has no published URL, and reading one as the other would show a name that was
   * never registered.
   */
  previewUrl: string | null;
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
  /** Empty for a publish, which writes no spec. */
  filename: string;
  spec: string;
  path: string;
  replaced: boolean;
  runner: RunnerState;
  /** Echoed back so the UI can say what it queued without re-reading the request. */
  kind: ProjectKind;
  publish: boolean;
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
 * Make sure the queue can be written and a runner will read it, then write one
 * request atomically.
 *
 * Both jobs need this and both would otherwise get it subtly different: the
 * writability check first (a queue that cannot be written is a deployment fault,
 * and reporting "no runner" for it sends the operator to fix the wrong thing),
 * then the runner heartbeat, then write-beside-and-rename — the runner scans the
 * directory continuously, so a half-written request must never be claimable.
 */
function submitRequest(dir: string, job: string, request: Record<string, unknown>): { runner: RunnerState; target: string } {
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

  const target = join(dir, `${job}.request.json`);
  const temporary = join(dir, `.${job}.request.json.${process.pid}.tmp`);

  try {
    writeFileSync(temporary, `${JSON.stringify(request, null, 2)}\n`, { encoding: "utf8", mode: 0o644 });
    renameSync(temporary, target);
  } catch {
    rmSync(temporary, { force: true });
    throw new BuildQueueError(notWritable(dir), 503);
  }

  return { runner, target };
}

/**
 * Write the spec and queue a build for it.
 *
 * `replace` is one decision that covers both overwrites: an existing spec in
 * `build-requests/` and an existing app in `builds/`. Both are refused without
 * it, because both destroy something the operator may have been comparing
 * against. The UI asks first, exactly as the export flow does.
 */
export function queueBuild(
  project: Project,
  options: { replace?: boolean; publish?: boolean } = {},
): QueuedBuild {
  const replace = options.replace === true;
  const publish = options.publish === true;
  const dir = buildQueueDir();

  // The spec is regenerated unconditionally: the build must describe what is on
  // screen, not whatever was exported last time. The conflict is surfaced the
  // same way the export route surfaces it, so the UI has one flow to handle.
  const written = writeFactorySpec(project, { overwrite: replace });

  const slug = specSlug(project.title);
  const job = newJobId();
  const { runner, target } = submitRequest(dir, job, {
    v: 1,
    job,
    action: "build",
    spec: `build-requests/${written.filename}`,
    slug,
    title: project.title.slice(0, 120),
    requested_by: "studio",
    requested_at: new Date().toISOString(),
    replace,
    // The runner has to know which contract to hold the build to: for a website,
    // writing files is not the finish line, packaging into `dist/` is.
    kind: project.kind,
    // The plan travels with the request, and it is what the runner builds from:
    // the language picks the base image, the commands are what runs, and the port
    // is where the result listens. A project with no plan is one saved before the
    // planner existed, and the runner's older packagers still build it.
    plan: project.plan ?? undefined,
    publish,
  });

  return {
    job,
    slug,
    filename: written.filename,
    spec: `build-requests/${written.filename}`,
    path: target,
    replaced: written.replaced,
    runner,
    kind: project.kind,
    publish,
  };
}

/**
 * Publish the files on screen, without running the factory.
 *
 * The files travel with the request because the runner cannot read Studio's data
 * volume — and because the point of this action is to put *these* files on a
 * name. No spec is written: a spec is factory input, and a publish that left one
 * behind would be picked up by the next bare `make app`.
 */
export function queuePublish(project: Project): QueuedBuild {
  const dir = buildQueueDir();
  const slug = specSlug(project.title);
  const job = newJobId();

  const { runner, target } = submitRequest(dir, job, {
    v: 1,
    job,
    action: "publish",
    slug,
    title: project.title.slice(0, 120),
    requested_by: "studio",
    requested_at: new Date().toISOString(),
    kind: project.kind,
    plan: project.plan ?? undefined,
    files: project.files.map((file) => ({ path: file.path, contents: file.contents })),
  });

  return {
    job,
    slug,
    filename: "",
    spec: "",
    path: target,
    replaced: false,
    runner,
    kind: project.kind,
    publish: true,
  };
}

/**
 * Package the files on screen and run them, without putting them on a name.
 *
 * The same request as a publish with one step missing, and the same files: what is
 * previewed has to be what is on screen, or the frame shows a project that no
 * longer exists. `preview` is its own action rather than a publish with a flag
 * because the difference is a public act — registering a name at the edge — and a
 * preview must be able to happen without it.
 */
export function queuePreview(project: Project): QueuedBuild {
  const dir = buildQueueDir();
  const slug = specSlug(project.title);
  const job = newJobId();

  const { runner, target } = submitRequest(dir, job, {
    v: 1,
    job,
    action: "preview",
    slug,
    title: project.title.slice(0, 120),
    requested_by: "studio",
    requested_at: new Date().toISOString(),
    kind: project.kind,
    plan: project.plan ?? undefined,
    files: project.files.map((file) => ({ path: file.path, contents: file.contents })),
  });

  return {
    job,
    slug,
    filename: "",
    spec: "",
    path: target,
    replaced: false,
    runner,
    kind: project.kind,
    publish: false,
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
  if (state !== "running" && state !== "succeeded" && state !== "failed" && state !== "cancelled") {
    return null;
  }

  const artifact = record.artifact;
  const site = record.site;

  return {
    job,
    state,
    action: parseAction(record.action),
    slug: asString(record.slug),
    title: asString(record.title),
    spec: asString(record.spec),
    requestedAt: asString(record.requested_at),
    requestedBy: asString(record.requested_by),
    language: asString(record.language),
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
    site:
      typeof site === "object" && site !== null
        ? {
            entry: asString((site as Record<string, unknown>).entry),
            distFiles: asNumber((site as Record<string, unknown>).dist_files),
            distBytes: asNumber((site as Record<string, unknown>).dist_bytes),
            sourceFiles: asNumber((site as Record<string, unknown>).source_files),
            zip: asString((site as Record<string, unknown>).zip),
            builtAt: asString((site as Record<string, unknown>).built_at),
          }
        : null,
    publishedUrl: asString(record.published_url),
    previewUrl: asString(record.preview_url),
    logTail: asString(record.log_tail) ?? "",
  };
}

/**
 * Which job a status describes.
 *
 * An unrecognised or absent value is a build, which is what every status written
 * before this field existed describes — and guessing "publish" for one of those
 * would relabel finished history.
 */
function parseAction(value: unknown): BuildAction {
  return value === "publish" || value === "preview" ? value : "build";
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
  // The same ordering the history lists, cut to one. Keeping a second
  // implementation of "which build is current" here is how the panel and the
  // history end up disagreeing about it.
  return listBuildHistory(slug, 1)[0] ?? null;
}

/* ---- history ------------------------------------------------------------ */

/**
 * Every status file on disk, newest first.
 *
 * Read from the queue rather than from an index of our own: the directory is the
 * only record, and it is written by the runner even when this container is
 * restarted mid-build. One bad file is skipped rather than failing the list.
 */
function allStatuses(): BuildStatus[] {
  const dir = buildQueueDir();

  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }

  const statuses: BuildStatus[] = [];
  for (const name of names) {
    if (!name.endsWith(".status.json") || name.startsWith(".")) continue;
    const status = toBuildStatus(readJson(join(dir, name)));
    if (status) statuses.push(status);
  }
  return statuses;
}

/** When a build last said anything, as a sort key that tolerates missing times. */
function recency(status: BuildStatus): number {
  const stamp = status.updatedAt ?? status.finishedAt ?? status.startedAt ?? status.requestedAt;
  const parsed = stamp ? Date.parse(stamp) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * This app's builds, newest first — what the panel lists under the button.
 *
 * A running build is pinned to the top regardless of timestamps: it is the one
 * the operator is watching, and queueing and starting can leave its `updatedAt`
 * slightly older than a build that finished a second later.
 */
export function listBuildHistory(slug: string, limit = 10): BuildStatus[] {
  const mine = allStatuses().filter((status) => status.slug === slug);

  mine.sort((left, right) => {
    if (left.state === "running" && right.state !== "running") return -1;
    if (right.state === "running" && left.state !== "running") return 1;
    return recency(right) - recency(left);
  });

  return mine.slice(0, Math.max(1, limit));
}

/* ---- cancelling --------------------------------------------------------- */

/**
 * Ask the runner to stop a build.
 *
 * Studio cannot signal a host process — different PID namespace, and it has no
 * business having one. So the request is a file the runner polls, and the answer
 * to "did it work" is the status file changing, which is what the panel already
 * follows. Only a running build can be cancelled: for anything else this is a
 * no-op the caller should not be told succeeded.
 */
export function requestCancel(job: string): BuildStatus {
  if (!JOB_ID_PATTERN.test(job)) {
    throw new BuildQueueError("No such build job.", 404);
  }

  const status = readBuildStatus(job);
  if (!status) {
    throw new BuildQueueError("No such build job.", 404);
  }
  if (status.state !== "running") {
    throw new BuildQueueError(
      `That build already ${status.state === "cancelled" ? "was cancelled" : status.state}.`,
      409,
    );
  }

  const dir = buildQueueDir();
  const target = join(dir, `${job}.cancel.json`);
  const temporary = join(dir, `.${job}.cancel.json.${process.pid}.tmp`);
  const marker = {
    v: 1,
    job,
    requested_by: "studio",
    requested_at: new Date().toISOString(),
  };

  try {
    writeFileSync(temporary, `${JSON.stringify(marker, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o644,
    });
    renameSync(temporary, target);
  } catch {
    rmSync(temporary, { force: true });
    throw new BuildQueueError(notWritable(dir), 503);
  }

  return status;
}

/* ---- the log ------------------------------------------------------------ */

/**
 * How much of a build log a browser is handed.
 *
 * A build's log is whatever Codex said, and Codex is not brief — the runner's own
 * files run to tens of kilobytes for a one-file app, and a stalled build's log is
 * bigger than a finished one's. The tail is the part that says what happened, so
 * the tail is what is sent, and the size is stated rather than silently applied.
 */
export const MAX_LOG_BYTES = 200_000;

export type BuildLog = {
  job: string;
  slug: string | null;
  state: BuildState;
  text: string;
  /** Bytes on disk, which is what tells the reader something was cut. */
  bytes: number;
  truncated: boolean;
};

/**
 * Read one job's log, newest bytes first — the whole thing when it is small.
 *
 * Read from the tail of the file rather than by reading it all and slicing:
 * a runaway build log should not be pulled into the Studio container's memory to
 * then throw most of it away. The read starts at a byte offset, so a truncated
 * log is trimmed forward to the next newline — half a line of mojibake at the top
 * of a log is exactly how a reader concludes the log is corrupt.
 *
 * A job with no log file yet is not an error: the runner writes the log when it
 * claims the request, so a build queued a moment ago legitimately has none, and
 * "no output yet" is a more useful answer than 404.
 */
export function readBuildLog(job: string, limitBytes: number = MAX_LOG_BYTES): BuildLog | null {
  if (!JOB_ID_PATTERN.test(job)) return null;

  const status = readBuildStatus(job);
  if (!status) return null;

  const path = join(buildQueueDir(), `${job}.log`);
  const base: Omit<BuildLog, "text" | "bytes" | "truncated"> = {
    job: status.job,
    slug: status.slug,
    state: status.state,
  };

  let size: number;
  try {
    size = statSync(path).size;
  } catch {
    return { ...base, text: "", bytes: 0, truncated: false };
  }

  const limit = Math.max(1, limitBytes);
  const truncated = size > limit;
  const start = truncated ? size - limit : 0;
  const length = size - start;

  let text: string;
  try {
    const descriptor = openSync(path, "r");
    try {
      const buffer = Buffer.alloc(length);
      readSync(descriptor, buffer, 0, length, start);
      text = buffer.toString("utf8");
    } finally {
      closeSync(descriptor);
    }
  } catch {
    // Unreadable (permissions, a file removed between stat and open) is reported
    // as no output rather than as a 500: the caller already has the status.
    return { ...base, text: "", bytes: size, truncated: false };
  }

  if (truncated) {
    // Drop the partial line the byte offset landed inside, then say so. The byte
    // offset is not a line boundary and no amount of care makes it one.
    const firstBreak = text.indexOf("\n");
    if (firstBreak !== -1) text = text.slice(firstBreak + 1);
    text = `… [earlier log omitted — showing the last ${kilobytes(length)} of ${kilobytes(size)}]\n${text}`;
  }

  return { ...base, text, bytes: size, truncated };
}

/** Local to this module so the marker reads like the panel's own units. */
function kilobytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
}

/** Re-exported so a route can distinguish "the spec exists" from other failures. */
export { FactorySpecError };

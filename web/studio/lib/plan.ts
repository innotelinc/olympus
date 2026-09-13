/**
 * The build plan — what Studio is about to build, before it builds it.
 *
 * WHY THIS EXISTS. Studio used to decide the stack on the user's behalf: an app
 * was React + Vite + a generated Node/SQLite server, a website was React + Vite
 * with no server, and a request that wanted neither got the closest of the two.
 * That is a fine default and a bad contract. A user who says "a small Flask app
 * with Postgres" or "an Astro blog" is not asking for React and a SQLite file.
 *
 * So the stack is now proposed, not assumed. The model reads the request and
 * answers with a plan: what it is, what it will be built from, which commands
 * install, build and start it, which port it listens on, and which files it
 * intends to write. Studio shows that to the user, and only once it is confirmed
 * does generation begin.
 *
 * THE PLAN IS A CONTRACT, NOT A SUGGESTION. The commands in `run` are what the
 * runner will actually execute, in a container, to turn the generated tree into
 * something running behind a preview URL. That is why this module validates them
 * strictly and refuses a plan it cannot execute: a plan whose `start` command is
 * missing produces a project that builds and then does nothing, and the person
 * who finds out is the user, minutes later, looking at an empty preview.
 *
 * Two fields are deliberately NOT the model's to choose:
 *   - `slug` is derived here from the name, because it becomes a hostname.
 *   - The port is the app's port *inside its container*. The runtime maps it to a
 *     host port it allocates; a plan cannot claim to listen on 3001 (Studio),
 *     20128 (the gateway) or 16379 (the session store) and mean it.
 */

import { parseKind, type ProjectKind } from "./projects";

/** Longest a command may be. Generous, but a plan is not a shell script. */
const MAX_COMMAND_CHARS = 500;
const MAX_NAME_CHARS = 80;
const MAX_SUMMARY_CHARS = 400;
const MAX_FILES = 60;
const MAX_PATH_CHARS = 200;
const MAX_PURPOSE_CHARS = 200;
const MAX_NOTES_CHARS = 1_200;

/**
 * Ports a project may listen on inside its container.
 *
 * Below 1024 needs root in the container, which the runtime does not grant, so a
 * plan that asks for :80 is a plan that fails at boot for a reason the user
 * cannot see. Above 49151 is the ephemeral range the client side uses for its own
 * outbound sockets, which is a poor place to ask a server to sit.
 */
export const MIN_PORT = 1024;
export const MAX_PORT = 49151;

/** What most dev servers and frameworks default to, so a missing port is usually right. */
export const DEFAULT_PORT = 3000;

export type PlanRuntime = {
  /** The base image the project needs, as the planner describes it (e.g. "node", "python"). */
  language: string;
  /** Framework names worth showing a person. Not used to build anything. */
  frameworks: string[];
  /** Datastore, or `null` when the project genuinely has none. */
  database: string | null;
};

export type PlanRun = {
  /** Installs dependencies. Empty when the project has none. */
  install: string;
  /** Produces the deployable output. Empty when there is nothing to compile. */
  build: string;
  /** Starts the server and stays in the foreground. Required. */
  start: string;
  /** The port `start` listens on, inside the container. */
  port: number;
  /** A path that answers 2xx once the app is up. Defaults to "/". */
  healthcheck: string;
};

export type PlanFile = {
  path: string;
  purpose: string;
};

export type BuildPlan = {
  name: string;
  slug: string;
  /** Derived from the request, never from the model — see the module comment. */
  kind: ProjectKind;
  summary: string;
  runtime: PlanRuntime;
  run: PlanRun;
  files: PlanFile[];
  notes: string | null;
};

/** A refusal the route turns into a sentence, rather than a crash. */
export class PlanError extends Error {}

/* ---- slugs --------------------------------------------------------------- */

/**
 * A name as a hostname label.
 *
 * This is the one part of a plan that reaches the network, so it is normalised
 * here rather than trusted: lowercase, ASCII letters, digits and single hyphens,
 * no leading or trailing hyphen, and never empty. A name like "Café & Bar / v2"
 * becomes `cafe-bar-v2` — and a plan whose name normalises to nothing falls back
 * to the kind, because an empty label is not a hostname.
 */
export function slugify(value: string, fallback: ProjectKind = "app"): string {
  const slug = value
    .normalize("NFKD")
    // Strip combining marks so "café" folds to "cafe" instead of losing the e.
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");

  return slug || fallback;
}

/* ---- parsing ------------------------------------------------------------- */

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asText(value: unknown, limit: number): string {
  if (typeof value !== "string") return "";
  return value.replace(/\s+/g, " ").trim().slice(0, limit);
}

function asCommand(value: unknown): string {
  if (typeof value !== "string") return "";
  // A command is one line. A newline in it is either a model wrapping its output
  // or an attempt at a second command, and neither is something to execute.
  return value.trim().replace(/\s+/g, " ").slice(0, MAX_COMMAND_CHARS);
}

function asStringList(value: unknown, limit: number): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const entry of value) {
    const text = asText(entry, 60);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= limit) break;
  }
  return out;
}

function asPort(value: unknown): number {
  const numeric = typeof value === "number" ? value : Number.parseInt(asText(value, 10), 10);
  if (!Number.isFinite(numeric)) return DEFAULT_PORT;
  const port = Math.round(numeric);
  return port >= MIN_PORT && port <= MAX_PORT ? port : DEFAULT_PORT;
}

/** A same-origin path, never a URL — the healthcheck is fetched by the runtime, not by a browser. */
function asHealthPath(value: unknown): string {
  const text = asText(value, 200);
  if (!text || !text.startsWith("/")) return "/";
  return text.split("?")[0] || "/";
}

function asPath(value: unknown): string {
  const text = asText(value, MAX_PATH_CHARS).replace(/^\.?\//, "");
  if (!text || text.includes("..") || text.startsWith("/")) return "";
  return text;
}

/**
 * The plan out of a model reply.
 *
 * The model is asked for JSON and nothing else, but a reply that arrives wrapped
 * in a fence or with a sentence in front of it is a normal thing to see, not a
 * failure — so the first `{` and the last `}` are used as the envelope. What
 * follows is validated field by field, and a plan missing something the runner
 * needs is refused with a sentence naming the missing piece.
 *
 * `kind` is passed in rather than read from the payload: the user chose it in the
 * UI, and letting a model reply change what "app" means would make the button
 * the user pressed decorative.
 */
export function parsePlan(text: string, kind: ProjectKind): BuildPlan {
  const raw = extractJsonObject(text);
  if (!raw) {
    throw new PlanError("The planner did not return a JSON plan. Try rephrasing the request.");
  }

  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new PlanError("The planner returned JSON that could not be parsed. Try again.");
  }

  const root = asRecord(payload);
  if (!root) throw new PlanError("The planner returned a plan that is not an object.");

  const name = asText(root.name, MAX_NAME_CHARS) || (kind === "website" ? "New website" : "New app");
  const run = asRecord(root.run) ?? {};
  const runtime = asRecord(root.runtime) ?? {};

  const start = asCommand(run.start);
  if (!start) {
    // Refused rather than defaulted. Guessing a start command produces a project
    // that builds and then serves nothing, which reads as a runner fault.
    throw new PlanError(
      "The plan has no start command, so nothing would run. Ask for it again with more detail about how the project runs.",
    );
  }

  const files = readFiles(root.files);

  return {
    name,
    slug: slugify(name, kind),
    kind,
    summary: asText(root.summary, MAX_SUMMARY_CHARS) || "No summary was given.",
    runtime: {
      language: asText(runtime.language, 40) || "unknown",
      frameworks: asStringList(runtime.frameworks, 8),
      database: asText(runtime.database, 40) || null,
    },
    run: {
      install: asCommand(run.install),
      build: asCommand(run.build),
      start,
      port: asPort(run.port),
      healthcheck: asHealthPath(run.healthcheck),
    },
    files,
    notes: asText(root.notes, MAX_NOTES_CHARS) || null,
  };
}

function readFiles(value: unknown): PlanFile[] {
  if (!Array.isArray(value)) return [];

  const files: PlanFile[] = [];
  const seen = new Set<string>();

  for (const entry of value) {
    const record = asRecord(entry);
    if (!record) continue;

    const path = asPath(record.path);
    if (!path || seen.has(path)) continue;
    seen.add(path);

    files.push({ path, purpose: asText(record.purpose, MAX_PURPOSE_CHARS) });
    if (files.length >= MAX_FILES) break;
  }

  return files;
}

/**
 * The first JSON object in a reply.
 *
 * Scanned rather than regex-matched, because a brace inside a string literal in a
 * note or a file purpose would end a regex early. Braces inside strings are
 * tracked, and the object ends at the matching close brace.
 */
export function extractJsonObject(text: string): string | null {
  const start = text.indexOf("{");
  if (start === -1) return null;

  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let index = start; index < text.length; index += 1) {
    const char = text[index];

    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }

    if (char === '"') inString = true;
    else if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }

  // Unterminated: a reply cut off mid-object. The caller reports that as a
  // failure to plan rather than rendering a half-read plan as if it were whole.
  return null;
}

/* ---- prompt -------------------------------------------------------------- */

/**
 * The planning contract.
 *
 * The plan is JSON because it is a *decision* the user reviews and the runner
 * executes, not prose. A fenced code block is explicitly forbidden: a fence
 * around the object is one more thing to strip, and every field of it is read.
 *
 * The stack itself is left open on purpose. The model is told to pick what the
 * request needs, to name a real start command, and — the part that makes the
 * whole feature work — that anything it depends on is installed by the runner in
 * a container, so it is free to choose a package. The old contract said "no
 * dependencies; here is your scaffold"; the new one says "choose, declare, and it
 * will be installed".
 */
export const PLAN_SYSTEM_PROMPT = `You are Studio, the build agent inside Olympus. Before anything is written, you plan what you are about to build so the person can confirm it. You reply with JSON, and nothing else.

A website is static content served over HTTP: pages, styling, images, maybe a little client-side scripting. An application is software that runs on a server and keeps state — the person's data is there when they come back, on another device.

Choose the stack from the request, not from habit. If the person asks for a specific language, framework or database, use it. If they do not, pick what is genuinely best for the job and say why in the summary — a small static site does not need a database, and a tracker that forgets everything is not a tracker.

HOW IT WILL BE RUN. Your plan is executed on a build host in a container built from your \`runtime.language\`:
- \`run.install\` runs first, with network access, to install dependencies.
- \`run.build\` then produces the deployable output. Leave it empty if there is nothing to compile.
- \`run.start\` starts the project and must stay in the foreground. It is served at the port in \`run.port\`, which is the port inside the container. Pick the port your start command actually listens on.
- Anything your project needs must be declared in the project's own files — a package.json, requirements.txt, Gemfile, go.mod, composer.json. Nothing is installed for you.

Reply with exactly this JSON shape and no other keys:
{
  "name": "Weight Tracker",
  "summary": "One or two sentences: what it does and the stack you chose, in the language of the person who asked.",
  "runtime": {
    "language": "node",
    "frameworks": ["react", "express"],
    "database": "sqlite"
  },
  "run": {
    "install": "npm install",
    "build": "npm run build",
    "start": "npm start",
    "port": 3000,
    "healthcheck": "/"
  },
  "files": [
    { "path": "package.json", "purpose": "Dependencies and scripts" }
  ],
  "notes": "Anything the person should know before confirming — a limit, a choice worth a second look, or null."
}

Rules:
- \`runtime.language\` is the base the container needs: "node", "python", "go", "php", "ruby", "static".
- \`runtime.database\` is a datastore name, or null when the project keeps no data. Say "sqlite" rather than picking a client library, and prefer a file-backed database for a single-container project — there is no second service to connect to.
- \`files\` is every file you intend to write, with a short purpose each. It is what the person reads to judge the plan, so list real paths, not directories.
- \`notes\` is where a genuine caveat goes. Do not use it for a summary of the summary, and do not pad it.
- A website must not have a database unless the request needs one.
- No markdown fences, no prose before or after the JSON. The object is the whole reply.`;

/** The planning turn: the request, and the kind the user picked. */
export function planMessages(
  prompt: string,
  kind: ProjectKind,
  priorFiles: { path: string; contents: string }[] = [],
): Array<{ role: "system" | "user"; content: string }> {
  const messages: Array<{ role: "system" | "user"; content: string }> = [
    {
      role: "system",
      content:
        PLAN_SYSTEM_PROMPT +
        `\n\nThis turn you are planning a ${kind === "website" ? "WEBSITE" : "FULL-STACK APPLICATION"}.`,
    },
  ];

  if (priorFiles.length > 0) {
    const label = kind === "website" ? "site" : "application";
    const rendered = priorFiles
      .map((file) => `<file path="${file.path}">\n${file.contents}\n</file>`)
      .join("\n\n");

    messages.push({
      role: "user",
      content:
        `The ${label} already exists in this state:\n\n${rendered}\n\n` +
        `The next instruction develops it further. Plan an addition to what is there — keep the ` +
        `stack unless the instruction asks you to change it, keep every feature that works, and ` +
        `list only the files this change touches or rewrites, noting in your summary that the rest ` +
        `is unchanged.`,
    });
  }

  messages.push({ role: "user", content: prompt });

  return messages;
}

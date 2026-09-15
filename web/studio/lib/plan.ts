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

// From ./kinds, not ./projects: this module is reached by the browser (the page
// component imports `missingPlannedFiles`), and `projects.ts` owns `node:fs`. A
// kind is two words; it has no business pulling a filesystem into a client bundle.
import { parseKind, type ProjectKind } from "./kinds";
import { parseTarget, type PlanTarget } from "./targets";

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
  /**
   * What the planner decided this is — `app` or `website`.
   *
   * Not chosen by the user: Studio used to ask, and the answer was a guess the
   * planner then had to build to. The turn that reads the request is the one that
   * can tell a résumé page from a tracker, so it answers, and everything
   * downstream (the delivery path, the packager, the export spec) reads this
   * rather than asking again.
   */
  kind: ProjectKind;
  /**
   * What serves the project's state — its own container's database, or the
   * deployment's self-hosted Convex backend.
   *
   * Read through `parseTarget` rather than trusted, for the same reason `kind` is:
   * the packager and the runtime each act on this field, so a value neither of them
   * understands has to collapse to the default here rather than being interpreted
   * twice. `./targets` explains why this is a plan field and not a fourth kind, and
   * why a website is always `container`.
   */
  target: PlanTarget;
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
 * `kind` is read from the payload — see `parsePlanObject`.
 */
export function parsePlan(text: string): BuildPlan {
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

  return parsePlanObject(payload);
}

/**
 * A plan that arrived as an already-parsed object.
 *
 * Used by the generation route, where the plan has been to the browser and back:
 * the client confirms it and sends it with the build request. That round trip is
 * exactly why the fields are re-read here rather than trusted — the same readers
 * as `parsePlan`, so a plan cannot be weakened by the trip through the page.
 */
export function parsePlanObject(payload: unknown): BuildPlan {
  const root = asRecord(payload);
  if (!root) throw new PlanError("The plan is not an object.");

  // The planner's own answer, read through `parseKind` rather than trusted: the
  // three consumers of this field (the tools a build runs, the wording of the
  // delivery buttons, and the export spec) each handle two kinds, so anything else
  // has to collapse to one of them here rather than being interpreted three ways.
  const kind = parseKind(root.kind);

  const name = asText(root.name, MAX_NAME_CHARS) || (kind === "website" ? "New website" : "New app");
  const run = asRecord(root.run) ?? {};
  const runtime = asRecord(root.runtime) ?? {};

  const start = asCommand(run.start);
  if (!start) throw new PlanError("The plan has no start command, so nothing would run.");

  return {
    name,
    slug: slugify(name, kind),
    kind,
    // A website is always a container. Static files have nowhere to keep a query and
    // nothing that can run a function, so a website that claimed Convex would be a
    // plan nothing could build — and the packager refuses exactly that, which would
    // turn a vocabulary mistake into a failed build minutes later.
    target: kind === "website" ? "container" : parseTarget(root.target),
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
    files: readFiles(root.files),
    notes: asText(root.notes, MAX_NOTES_CHARS) || null,
  };
}

/**
 * The first planned file the generated set did not write.
 *
 * The plan's file list is the contract the user confirmed, so a plan that says it
 * will write `package.json` and a reply that does not is a mismatch worth naming
 * at the API boundary — the alternative is a build that fails minutes later on
 * the runner, where the message is about a missing module and not about the file
 * that was promised.
 *
 * A **superset** is fine: a model that writes extra files has still honoured the
 * list, and refusing them would punish thoroughness. Only a missing planned file
 * is reported, and only the first, because one sentence naming one file is
 * actionable where a list is noise.
 */
export function missingPlannedFiles(
  files: { path: string }[],
  plan: BuildPlan,
): string | null {
  if (plan.files.length === 0) return null;

  const written = new Set(files.map((file) => file.path.replace(/^\.?\//, "")));
  const missing = plan.files.find((file) => !written.has(file.path));
  return missing ? missing.path : null;
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

YOU ALSO DECIDE WHAT THIS IS, and you say so in \`kind\`. Nobody is choosing it for you, and it is not a setting: it is what the delivery path keys on.
- "website" is static content served over HTTP: pages, styling, images, maybe a little client-side scripting. No server, no state.
- "app" is software that runs on a server and keeps state — what the person enters is there when they come back, on another device.

Read the request and answer from it. A portfolio, a landing page, a résumé, a menu, documentation or a one-off document is a website. A tracker, a booking system, an inbox, a budget, or anything with accounts is an app. When it could genuinely be either, choose the one that serves the request without inventing a server and a database nobody asked for — and if the request is a single page of information, that is a website.

Choose the stack from the request, not from habit. If the person asks for a specific language, framework or database, use it. If they do not, pick what is genuinely best for the job and say why in the summary — a small static site does not need a database, and a tracker that forgets everything is not a tracker.

HOW IT WILL BE RUN. Your plan is executed on a build host in a container built from your \`runtime.language\`:
- \`run.install\` runs first, with network access, to install dependencies.
- \`run.build\` then produces the deployable output. Leave it empty if there is nothing to compile.
- \`run.start\` starts the project and must stay in the foreground. It is served at the port in \`run.port\`, which is the port inside the container. Pick the port your start command actually listens on.
- Anything your project needs must be declared in the project's own files — a package.json, requirements.txt, Gemfile, go.mod, composer.json. Nothing is installed for you.

Reply with exactly this JSON shape and no other keys:
{
  "name": "Weight Tracker",
  "kind": "app",
  "target": "container",
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
- "static" means there is no toolchain and no process to start — plain HTML, CSS and JavaScript that nginx serves. Leave \`install\` and \`build\` empty and make \`start\` exactly \`nginx -g 'daemon off;'\`. A React or Vite site is "node", because something has to bundle it.
- \`target\` is what serves the state. "container" is the default and almost always right: the project keeps its own database, on disk, beside its code, in its own container. "convex" is only for a request that genuinely needs live, shared, realtime state, and it means the deployment's self-hosted Convex backend serves it — Atlas runs one. Do not choose "convex" for a single-user app that a file-backed database serves. A "website" is always "container".
- \`kind\` is exactly "app" or "website", decided from the request. It has to match the plan you wrote: a "website" has no database and its \`start\` is nginx serving static files, and an "app" has a server that stays in the foreground and keeps state. A plan whose \`kind\` contradicts its own \`runtime\` and \`run\` is the one thing here that cannot be confirmed.
- No markdown fences, no prose before or after the JSON. The object is the whole reply.`;

/**
 * The planning turn: the request, and whatever is already on screen.
 *
 * No kind is passed in. This turn used to be told "you are planning a WEBSITE"
 * because the user had pressed a button, and the field that decided the delivery
 * path was therefore an opinion the planner had to work backwards to — a
 * request for a résumé site planned as an app, or a tracker planned as a
 * website, with the plan and its own kind disagreeing. The planner is the one
 * reader of the request, so the classification is its call.
 */
export function planMessages(
  prompt: string,
  priorFiles: { path: string; contents: string }[] = [],
): Array<{ role: "system" | "user"; content: string }> {
  const messages: Array<{ role: "system" | "user"; content: string }> = [
    { role: "system", content: PLAN_SYSTEM_PROMPT },
  ];

  if (priorFiles.length > 0) {
    const rendered = priorFiles
      .map((file) => `<file path="${file.path}">\n${file.contents}\n</file>`)
      .join("\n\n");

    messages.push({
      role: "user",
      content:
        `The project already exists in this state:\n\n${rendered}\n\n` +
        `The next instruction develops it further. Plan an addition to what is there — keep the ` +
        `stack unless the instruction asks you to change it, keep every feature that works, and ` +
        `list only the files this change touches or rewrites, noting in your summary that the rest ` +
        `is unchanged. Keep \`kind\` as it is unless what the instruction asks for changes what the ` +
        `project is.`,
    });
  }

  messages.push({ role: "user", content: prompt });

  return messages;
}

/* ---- the generation contract --------------------------------------------- */

/**
 * How the project is described to the model that builds it.
 *
 * The plan is restated in full rather than referenced, because this is the only
 * turn that writes code and the plan's details are what the code has to match:
 * the run commands it must work with, the port it must listen on, the files the
 * user saw listed, and the datastore it is allowed to assume.
 */
function describePlan(plan: BuildPlan): string {
  const kindLine =
    plan.kind === "website"
      ? "a WEBSITE — static content served over HTTP: pages, styling, images, and at most a little client-side scripting. It has no server and keeps no state."
      : "a FULL-STACK APPLICATION — software that runs on a server and keeps state, so what the user enters is there when they come back, from another device.";

  const stack = [
    plan.runtime.language,
    ...plan.runtime.frameworks,
    plan.runtime.database ? `database: ${plan.runtime.database}` : "no database",
  ].join(" · ");

  const files =
    plan.files.length > 0
      ? plan.files.map((file) => `- ${file.path}${file.purpose ? ` — ${file.purpose}` : ""}`).join("\n")
      : "- (the planner listed no files; write what the project needs)";

  const steps: string[] = [];
  if (plan.run.install) steps.push(`install  \`${plan.run.install}\``);
  if (plan.run.build) steps.push(`build    \`${plan.run.build}\``);
  steps.push(`start    \`${plan.run.start}\``);

  return [
    `Name: ${plan.name}`,
    `What it is: ${kindLine}`,
    `Stack: ${stack}`,
    plan.target === "convex"
      ? "Data target: Convex — the deployment's self-hosted backend (Atlas). The schema and the functions deploy there; the deployment URL reaches the build as `CONVEX_URL`."
      : "",
    `What it does: ${plan.summary}`,
    `Files the plan listed:\n${files}`,
    plan.notes ? `Notes from the plan: ${plan.notes}` : "",
    [
      "How it will be run — this is fixed, and your project must work with it, in this order:",
      ...steps.map((step) => `  ${step}`),
      `It is served at port ${plan.run.port}.`,
    ].join("\n"),
  ]
    .filter(Boolean)
    .join("\n\n");
}

/**
 * The generation contract: build to the confirmed plan, whatever the stack is.
 *
 * This replaced two hardcoded contracts — one per kind — that specified React,
 * a pinned scaffold and "no dependencies". That was the closest thing to a
 * guarantee in the old design and the reason a request for anything else could
 * not be honoured. The trade is deliberate and worth stating plainly: the
 * project is now wholly the model's, so the safety that used to come from a
 * generated scaffold now comes from the run contract being *tested* — the runner
 * installs, builds and starts what the plan declared and reports the real error
 * if it does not.
 *
 * The one instruction that is not negotiable is the port and the bind address.
 * A server that listens on localhost only is unreachable from outside its
 * container, and that failure is invisible from inside — it reads as "the preview
 * is broken" rather than "the app is fine and the network is wrong".
 */
export function generationMessages(
  prompt: string,
  priorFiles: { path: string; contents: string }[],
  plan: BuildPlan,
): Array<{ role: "system" | "user"; content: string }> {
  const isAddOn = priorFiles.length > 0;
  const label = plan.kind === "website" ? "site" : "application";

  const rules = [
    "Output rules — follow them exactly:",
    "- Reply with file blocks and nothing else. No prose, no preamble, no explanation, no markdown fences.",
    '- Wrap every file exactly like this:\n<file path="src/main.ts">\n...file contents...\n</file>',
    "- Rewrite each file you send in full. Never emit patches, diffs, or partial edits.",
    isAddOn
      ? "- Return every file you are adding or changing. A file you do not mention is kept exactly as it is, so do not re-send a file just to keep it — and do not drop a feature by leaving its file out."
      : "- Write every file the project needs to install, build and run. Nothing exists yet.",
  ].join("\n");

  const ownership = [
    "THE PROJECT IS YOURS IN FULL. There is no scaffold around your files and no dependency is installed for you: anything the project needs to install, build and run must be written by you, declared in that stack's own manifest (package.json, requirements.txt, Gemfile, go.mod, composer.json, pyproject.toml — whatever it uses).",
    "- Declare dependency versions that exist. Do not invent a version number; if you are unsure of the current one, use a range that cannot resolve to nothing.",
    `- The server must listen on port ${plan.run.port} and bind every interface, not just localhost. Binding 127.0.0.1 is the most common way a project that works locally is unreachable once it is running.`,
    "- Read configuration from environment variables and fall back to a working default. There is no .env file to read and no secrets to be given.",
    plan.target === "convex"
      ? `- Persistence and compute are Convex's. Write the schema and the functions under \`convex/\`, and let the client use the deployment the build points it at: \`CONVEX_URL\`, plus the framework-visible \`VITE_CONVEX_URL\` / \`NEXT_PUBLIC_CONVEX_URL\` set to the same value — read whichever your framework exposes. Never hardcode a deployment URL, never write a deploy key into the project, and do not add a database of your own: the Convex deployment is where the data lives.`
      : plan.runtime.database
        ? `- Persistence is ${plan.runtime.database}. Keep what the user enters: a tracker that forgets is not a tracker. A file-backed database lives on disk next to the code and survives a restart.`
        : "- This project keeps no state. Do not add a database or a server it does not need.",
    "- Do not add a step that is not in the plan and do not change the commands above. Nothing else runs.",
  ].join("\n");

  const quality = [
    "Design rules:",
    "- This is something a person will use, not a demo: a real structure, real content, and finished states.",
    "- Semantic HTML, responsive without a grid library, visible focus states, and every control reachable and operable by keyboard.",
    "- Contrast that passes, one coherent colour palette, a real spacing scale, and a typographic hierarchy with sensible measure for text.",
    "- Mobile-first: it has to read well at 360px and look composed at 1440px.",
    "- A destructive action asks first.",
    "- No placeholder lorem ipsum, no TODO comments, no \"coming soon\" sections, no handler behind a button that looks live.",
    "- No external fonts, no CDN tags, no remote images. Inline SVG or CSS gradients.",
    plan.kind === "website"
      ? "- Ship the built output where the build step puts it; do not hardcode a dev-server-only URL."
      : "- Handle the loading, empty and failure states explicitly. An empty list is a normal first screen, not an error.",
  ].join("\n");

  const messages: Array<{ role: "system" | "user"; content: string }> = [
    {
      role: "system",
      content:
        `You are Studio, the build agent inside Olympus. A plan for this project has been confirmed by the person who asked for it, and you now write it. Build what the plan describes — the stack, the structure and the behaviour — not a more familiar stack you would have chosen.\n\n` +
        `THE PLAN\n${describePlan(plan)}\n\n${rules}\n\n${ownership}\n\n${quality}`,
    },
  ];

  if (isAddOn) {
    const rendered = priorFiles
      .map((file) => `<file path="${file.path}">\n${file.contents}\n</file>`)
      .join("\n\n");

    messages.push({
      role: "user",
      content:
        `Current version of the ${label}:\n\n${rendered}\n\n` +
        `Develop it further according to the next instruction. This is an addition to a ${label} that ` +
        `already exists and may already hold real data: keep every file, feature, table and column that ` +
        `is already there unless the instruction asks you to remove it.`,
    });
  }

  messages.push({ role: "user", content: prompt });

  return messages;
}

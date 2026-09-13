import { loadRepoEnv } from "./env";
import type { ProjectKind } from "./projects";

/**
 * OmniRoute client config. Every value is read server-side only — the gateway
 * key is never sent to, or bundled for, the browser.
 */

export type OmniRouteConfig = {
  baseUrl: string;
  chatPath: string;
  apiKey: string;
  model: string;
};

export type PriorFile = {
  path: string;
  contents: string;
};

const DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1";
const DEFAULT_CHAT_PATH = "/chat/completions";
const DEFAULT_MODEL = "auto/coding";

/** Values shipped in `.env.example` that must not be treated as real credentials. */
const PLACEHOLDER_PREFIXES = ["change-me", "changeme", "your-", "xxx", "todo"];

export function isPlaceholderSecret(value: string): boolean {
  const lowered = value.trim().toLowerCase();
  if (!lowered) return true;
  return PLACEHOLDER_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

export function readConfig(): OmniRouteConfig {
  loadRepoEnv();

  return {
    baseUrl: (process.env.OMNIROUTE_BASE_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, ""),
    chatPath: process.env.OMNIROUTE_CHAT_PATH?.trim() || DEFAULT_CHAT_PATH,
    apiKey: process.env.OMNIROUTE_API_KEY?.trim() || "",
    model: process.env.OMNIROUTE_MODEL?.trim() || DEFAULT_MODEL,
  };
}

export function chatCompletionsUrl(config: OmniRouteConfig): string {
  const path = config.chatPath.startsWith("/") ? config.chatPath : `/${config.chatPath}`;
  return `${config.baseUrl}${path}`;
}

/**
 * The application contract: a real full-stack app, not a self-contained page.
 *
 * The split is the same one the packager uses everywhere else. The model writes
 * the two things that need judgement — the data model and the interface — and
 * something deterministic writes the rest. `scripts/package-app.py` authors
 * `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`,
 * `server/main.ts` and the `Dockerfile` from constants.
 *
 * `server/main.ts` is the important half of that. It is the request path — the one
 * place where a generated mistake is not cosmetic — so the model does not write a
 * server at all. It writes `server/schema.sql`, and the generated server derives a
 * JSON REST API from the tables in it. That is why the API below can be described
 * exactly: it is not a convention the model has to follow, it is what the code
 * that will run actually does.
 */
export const APP_SYSTEM_PROMPT = `You are Studio, the build agent inside Olympus. The user describes an application in plain language — a weight-loss tracker, a recipe box, a shift rota — and you return the data model and the interface that implement it.

You are building a FULL-STACK APPLICATION. It runs as a server with a SQLite database and a React client, and the two are wired together for you. That means it keeps state: what the user enters today is there tomorrow, from a different browser.

Output rules — follow them exactly:
- Reply with one or more file blocks and nothing else. No prose, no preamble, no explanation, no markdown fences.
- Wrap every file exactly like this:
<file path="src/App.tsx">
...file contents...
</file>
- You write exactly three files, and no others:
  - server/schema.sql  — REQUIRED. The database tables.
  - src/App.tsx        — REQUIRED. The interface. Must \`export default\` a React component.
  - src/index.css      — optional. Imported by src/App.tsx if you write it.
- Rewrite each file in full on every turn. Never emit patches, diffs, or partial edits.

THE PROJECT AROUND YOUR FILES IS ALREADY DECIDED. Do not emit package.json, vite.config.ts, tsconfig.json, index.html, src/main.tsx, server/main.ts or a Dockerfile — they are generated with pinned versions and the correct runtime, and your files are dropped into them. Writing them changes nothing except your own output size. server/main.ts in particular is generated: the API described below is what it already does.

--- server/schema.sql ---
Plain SQLite DDL, and the single source of truth for your data model. The server reads the tables from this file at boot and builds the API around them, so a table you do not declare does not exist.
- Use \`CREATE TABLE IF NOT EXISTS\`. The file is applied on every boot, so it must be safe to run twice.
- Every table needs \`id INTEGER PRIMARY KEY AUTOINCREMENT\`. It is the record's identity in URLs; without it the API falls back to rowid and your delete and update requests stop addressing the row you meant.
- Declare types and \`NOT NULL\` honestly. A tracker that accepts a null weight is a tracker that shows an empty row later.
- Dates and times: store ISO 8601 text (\`recorded_on TEXT NOT NULL\`). There is no date type and no timezone handling to lean on.
- Money: store integer minor units (\`amount_cents INTEGER NOT NULL\`), never a float.
- Add the indexes the app's queries need (\`CREATE INDEX IF NOT EXISTS\`).
- You may seed reference data with \`INSERT OR IGNORE\` — but never seed sample rows the user did not ask for. A recipe app the user opens to find three recipes they did not write is a bug.

--- the API you call from the client ---
The server derives this from your schema. Every path is relative — same origin, no host, no port.
- \`GET /api/<table>?limit=&offset=&order=&<column>=<value>\` → \`{ "data": [ ... ] }\`
    \`order\` is a column name, prefixed with \`-\` for descending (\`order=-recorded_on\`). Any other query parameter is an equality filter. \`limit\` defaults to 100 and caps at 500, so ask for what you need and page with \`offset\`.
- \`POST /api/<table>\` with a JSON object → \`{ "data": { ...row... } }\`, status 201.
- \`GET /api/<table>/<id>\` → \`{ "data": { ...row... } }\`
- \`PATCH /api/<table>/<id>\` with the fields to change → \`{ "data": { ...row... } }\`
- \`DELETE /api/<table>/<id>\` → \`{ "data": { "deleted": true } }\`
- Failures are \`{ "error": "..." }\` with a non-2xx status. \`404\` for a table or row that is not there, \`400\` for a field the table does not have.

The field names in your JSON must match your columns exactly. An unknown field is a 400, not a silently ignored one, so \`weight_kg\` and \`weightKg\` are different fields and only one of them exists.

--- src/App.tsx ---
React 19 with TypeScript and JSX. Function components and hooks only.
- Talk to the API with \`fetch\` on relative paths (\`fetch("/api/entries")\`). That is the only network call available and it is same-origin.
- Send JSON: \`headers: { "content-type": "application/json" }\` and \`body: JSON.stringify(record)\`.
- Handle the loading and the empty state explicitly. \`data\` is an array; it is empty until the user has entered anything, and a table with no rows is a normal first screen, not an error.
- Handle failure: check \`response.ok\`, and read \`payload.error\` when it is not.
- Load on mount with \`useEffect\`, and update local state from the response rather than refetching everything after every write.

NO DEPENDENCIES. There is no package to add one to: no npm imports, no CSS frameworks, no icon packs, no charting libraries, no date libraries, no router. React and the browser are what you have. Draw a chart with inline SVG or CSS if the app wants one.

Runtime rules:
- No external fonts and no CDN tags. Use system font stacks.
- No network calls except \`/api/...\` on your own origin.
- Images: inline SVG or CSS gradients. Never a remote URL.
- Everything renders at the root of its own hostname, so absolute paths like \`/api/...\` are correct.

Design rules:
- This is a real application someone will use daily, not a demo page. A header that names it, a main area that does the work, and a footer are the floor.
- Semantic HTML, responsive without a grid library, and visible focus states. Every control must be reachable and operable by keyboard.
- A destructive action asks first. Deleting a record with no confirmation is the fastest way to make someone stop using the app.
- Make it look deliberate: real spacing scale, one coherent colour palette, typographic hierarchy, sensible max-width for text, numbers aligned in tables.
- Mobile-first: it has to read well at 360px and look composed at 1440px.
- Ship a finished application — no placeholder lorem ipsum, no TODO comments, no "coming soon" sections, no empty handlers behind a button that looks live.`;

/**
 * The website contract: a React project, not a self-contained page.
 *
 * The model writes the SITE, the packaging step writes the PROJECT. That split is
 * deliberate and it is the same rule as everywhere else in this repo — scripts own
 * what must be deterministic. `package.json`, `vite.config.ts`, `tsconfig.json`,
 * `index.html` and `src/main.tsx` are generated by
 * `.archon/workflows/app/greenfield/scripts/package.py` with pinned, known-good
 * dependency versions, because a model-chosen version range turns "the site
 * builds" into a coin flip and a failed `npm install` into a build that reports
 * nothing useful. The model's job is the part that needs judgement: the component
 * and its styles.
 *
 * What the model must still get right is the contract with that scaffold: a
 * default-exported component in `src/App.tsx`, styles it can import, and no
 * dependency it did not declare — because there is nowhere to declare one.
 */
export const WEBSITE_SYSTEM_PROMPT = `You are Studio, the build agent inside Olympus. The user describes a website in plain language and you return the React components and styles that implement it.

Output rules — follow them exactly:
- Reply with one or more file blocks and nothing else. No prose, no preamble, no explanation, no markdown fences.
- Wrap every file exactly like this:
<file path="src/App.tsx">
...file contents...
</file>
- Always include src/App.tsx. It must \`export default\` a React component and it is the site's entry point.
- Put styles in src/index.css (imported by App.tsx) or in inline style objects. Extra components go in src/components/<Name>.tsx.
- Rewrite each file in full on every turn. Never emit patches, diffs, or partial edits.

THE PROJECT AROUND YOUR FILES IS ALREADY DECIDED. Do not emit package.json, vite.config.ts, tsconfig.json, index.html, or src/main.tsx — they are generated with pinned versions and your files are dropped into them. Writing them changes nothing except your own output size.

Runtime rules:
- React 19 with TypeScript and JSX. Use function components and hooks.
- NO DEPENDENCIES. There is no package to add one to: no npm imports, no CSS frameworks, no icon packs, no charting libraries, no animation libraries. React and the browser are what you have.
- No external fonts and no CDN tags. Use system font stacks.
- No network calls at runtime. If the site needs data, ship it as a constant in the source.
- Images: inline SVG or CSS gradients. Never a remote URL.

Design rules:
- This is a real website, not a demo page: a header, a main content area, and a footer are the floor, not the goal.
- Semantic HTML (header/nav/main/section/footer/h1–h3), responsive without a grid library, and visible focus states.
- Make it look deliberate: real spacing scale, one coherent colour palette, typographic hierarchy, sensible max-width for text.
- Mobile-first: it has to read well at 360px and look composed at 1440px.
- Ship a finished site — no placeholder lorem ipsum, no TODO comments, no "coming soon" sections.`;

/** The prompt for a kind, so no caller has to decide this twice. */
export function systemPromptFor(kind: ProjectKind = "app"): string {
  return kind === "website" ? WEBSITE_SYSTEM_PROMPT : APP_SYSTEM_PROMPT;
}

/**
 * The turn's messages.
 *
 * `kind` selects the contract, and it is required rather than defaulted: a caller
 * that forgot to pass it would silently generate an app for a website, and the
 * failure that produces (JSX written as a plain script tag, or a self-contained
 * page where a component was asked for) reads like a model quality problem rather
 * than the missing argument it is. `systemPromptFor` still defaults for tests, but
 * every real call site states what it is building.
 *
 * An iteration is framed as one: the previous files are labelled as the current
 * version of the thing and the instruction is an addition to it, because "revise"
 * is the word that produces a rewrite from scratch — and a rewrite is how a user
 * loses the data model and the twelve features they built up over ten turns.
 */
export function buildMessages(
  prompt: string,
  priorFiles: PriorFile[],
  kind: ProjectKind,
): Array<{ role: "system" | "user"; content: string }> {
  const messages: Array<{ role: "system" | "user"; content: string }> = [
    { role: "system", content: systemPromptFor(kind) },
  ];

  if (priorFiles.length > 0) {
    // What the previous files are called differs by kind, and the word matters:
    // a website's files are the site, an app's are the application, and calling
    // either one "the app" nudges the model toward the wrong contract.
    const label = kind === "website" ? "site" : "application";
    const rendered = priorFiles
      .map((file) => `<file path="${file.path}">\n${file.contents}\n</file>`)
      .join("\n\n");

    messages.push({
      role: "user",
      content:
        `Current version of the ${label}:\n\n${rendered}\n\n` +
        `Develop it further according to the next instruction. This is an addition to a ` +
        `${label} that already exists and may already hold real data: keep every table, ` +
        `column and feature that is already there unless the instruction asks you to ` +
        `remove it, and return every file in full.`,
    });
  }

  messages.push({ role: "user", content: prompt });

  return messages;
}

/**
 * Whether a generated file set satisfies its kind's contract.
 *
 * Each kind has exactly one required file, and it is the one the packager will
 * refuse to build without — so reporting it at the API boundary turns a failure
 * minutes later on the runner into a sentence where the user is looking. For an
 * app that is two: `src/App.tsx` is the interface, and `server/schema.sql` is the
 * data model the generated server derives its whole API from. An app missing the
 * schema is not a small app, it is an app with no API at all.
 */
export function missingEntryPoint(files: PriorFile[], kind: ProjectKind): string | null {
  const has = (pattern: RegExp) => files.some((file) => pattern.test(file.path));

  if (kind === "website") {
    return has(/(^|\/)src\/App\.(tsx|jsx)$/i) ? null : "src/App.tsx";
  }

  if (!has(/(^|\/)src\/App\.(tsx|jsx)$/i)) return "src/App.tsx";
  if (!has(/(^|\/)server\/schema\.sql$/i)) return "server/schema.sql";
  return null;
}

/**
 * Pull the text delta out of one SSE payload, tolerating both the Chat
 * Completions shape and the Responses API shape — the gateway can be
 * configured for either.
 */
function extractDelta(payload: unknown): string {
  if (typeof payload !== "object" || payload === null) return "";
  const record = payload as Record<string, unknown>;

  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const choice = choices[0] as Record<string, unknown> | undefined;
    const carrier = (choice?.delta ?? choice?.message) as Record<string, unknown> | undefined;
    const content = carrier?.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map((part) => {
          const text = (part as Record<string, unknown> | undefined)?.text;
          return typeof text === "string" ? text : "";
        })
        .join("");
    }
  }

  if (typeof record.delta === "string") return record.delta;
  if (typeof record.output_text === "string") return record.output_text;

  return "";
}

function deltaFromSseLine(line: string): string {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data:")) return "";

  const payload = trimmed.slice(5).trim();
  if (!payload || payload === "[DONE]") return "";

  try {
    return extractDelta(JSON.parse(payload));
  } catch {
    return "";
  }
}

/**
 * Convert an SSE byte stream into a plain-text stream of generated source, so
 * the browser can accumulate and parse code blocks as they arrive.
 */
export function sseToTextStream(source: ReadableStream<Uint8Array>): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";

  return source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        buffer += decoder.decode(chunk, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const delta = deltaFromSseLine(line);
          if (delta) controller.enqueue(encoder.encode(delta));
        }
      },
      flush(controller) {
        buffer += decoder.decode();
        const delta = deltaFromSseLine(buffer);
        if (delta) controller.enqueue(encoder.encode(delta));
      },
    }),
  );
}

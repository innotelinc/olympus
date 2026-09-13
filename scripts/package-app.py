#!/usr/bin/env python3
"""Package a Studio *application* build into something that can be run and served.

WHAT AN APP IS HERE, AND WHY IT IS NOT A WEBSITE. A Studio website is static: the
model writes React, this repo compiles it, and the result is files a web server can
hand out. A Studio *application* is a running service — a weight-loss tracker, a
recipe box — and the difference is not size, it is that it has state. Something has
to receive a POST, validate it, write it down, and give it back on the next page
load. That is a server and a database, and neither exists until it is built here.

THE MODEL WRITES WHAT NEEDS JUDGEMENT; THIS WRITES WHAT MUST BE DETERMINISTIC.
`package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`,
`server/main.ts` and the `Dockerfile` are emitted byte-for-byte from constants
below. They are not model output and not model-tunable. The model writes the three
files that are actually particular to the application:

    server/schema.sql   the tables, and therefore the product's data model
    src/App.tsx         the interface
    src/index.css       the styling (optional)

THE SERVER IS GENERATED, AND THAT IS THE SAFETY PROPERTY. The request path is a
browser-facing service, and it is the one place in a generated app where a mistake
is not a cosmetic bug. So the model never writes it. What it writes instead is
`server/schema.sql`, and `server/main.ts` — generated here — derives a JSON REST API
from the tables in it:

    GET    /api/health
    GET    /api/<table>?limit=&offset=&order=&<column>=...
    POST   /api/<table>
    GET    /api/<table>/<id>
    PATCH  /api/<table>/<id>
    DELETE /api/<table>/<id>

Table and column names are checked against the live schema before they are quoted
into SQL, so a request cannot name something that is not there.

ZERO RUNTIME DEPENDENCIES, ON PURPOSE. `server/main.ts` imports only Node built-ins
(`node:http`, `node:sqlite`, `node:fs`, `node:path`), so the runtime image carries no
`node_modules` at all: no install at start, no native module to compile, no
supply-chain surface behind a public route. The client is compiled to `dist/client`
by the build stage and copied in as static files.

    scripts/package-app.py weight-tracker              # scaffold, install, build, zip
    scripts/package-app.py weight-tracker --scaffold-only
    scripts/package-app.py weight-tracker --check      # report what would happen
    scripts/package-app.py weight-tracker --no-zip
    scripts/package-app.py weight-tracker --image      # also build the runtime image

Exit codes:
    0  packaged (or, with --check/--scaffold-only, the step that was asked for)
    1  the app did not build, or the image did not — the output is in the log
    2  the request made no sense (no such build, missing entry files, no toolchain)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# --- constants ---------------------------------------------------------------

# What the model is told to write, and the whole of what this step requires.
ENTRY_CLIENT = "src/App.tsx"
ENTRY_SCHEMA = "server/schema.sql"

# What the build produces, and what the image serves.
ENTRY_BUILT = "dist/client/index.html"

BUILDS_DIR = "builds"

# A slug safe to use as a directory name and as a container name. Re-checked here
# rather than trusted from the request: the runner validated it, but this script is
# also runnable by hand.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

# The base image. Pinned to a major, not to `latest`: `node:sqlite` needs a Node
# new enough to ship it unflagged, and a floating tag would eventually move out
# from under every already-built application.
NODE_IMAGE = "node:24-alpine"

# Scaffold files. Exact strings, no f-strings, no interpolation: packaging the same
# build twice must produce identical scaffolding or the lockfile stops meaning
# anything.
PACKAGE_JSON = """{
  "name": "olympus-app",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "start": "node server/main.ts"
  },
  "dependencies": {
    "react": "^19.1.0",
    "react-dom": "^19.1.0"
  },
  "devDependencies": {
    "@types/react": "^19.1.0",
    "@types/react-dom": "^19.1.0",
    "@vitejs/plugin-react": "^5.0.0",
    "typescript": "^5.8.0",
    "vite": "^7.0.0"
  }
}
"""

VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Root base, not "./": an application is served at the root of its own hostname, and
// its client fetches /api/* on that same origin.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist/client",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:3000" },
  },
});
"""

# Client only. `server/` is deliberately not included: it is generated code that
# uses Node built-ins, and type-checking it would need `@types/node` in the scaffold
# — one more version to keep true for no gain, since nobody edits it.
TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "moduleDetection": "force",
    "noEmit": true,
    "allowImportingTsExtensions": true
  },
  "include": ["src"]
}
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

MAIN_TSX = """import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

const container = document.getElementById("root");
if (!container) throw new Error("index.html is missing its #root element");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
"""

INDEX_CSS = """:root {
  color-scheme: light dark;
}

*,
*::before,
*::after {
  box-sizing: border-box;
}

body {
  margin: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
"""

# The server. This is the security boundary of a generated application, which is why
# it is generated rather than written per app: the same code path is reviewed once,
# and the model's contribution to the request path is a schema it cannot escape —
# table and column names are matched against the live schema before they are quoted.
SERVER_MAIN = r"""// The application's API and static host. Generated by scripts/package-app.py.
//
// Node built-ins only: the runtime image has no node_modules, so there is nothing
// to install at boot and no third-party code behind a public route.
import { createServer } from "node:http";
import { DatabaseSync } from "node:sqlite";
import { createReadStream, existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, resolve, sep } from "node:path";

const PORT = Number(process.env.PORT ?? 3000);
const DATA_DIR = process.env.DATA_DIR ?? "/data";
// `import.meta.dirname` is the directory this file is in, so the schema travels with
// the server rather than with the process's working directory.
const SCHEMA_PATH = join(import.meta.dirname, "schema.sql");
const PUBLIC_DIR = resolve(process.env.PUBLIC_DIR ?? "public");

const MAX_BODY_BYTES = 256 * 1024;
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

mkdirSync(DATA_DIR, { recursive: true });

// WAL, so a read does not block a write — the normal shape of a small web app.
const db = new DatabaseSync(join(DATA_DIR, "app.sqlite"));
db.exec("PRAGMA journal_mode = WAL");

// The schema is applied on every boot and must therefore be idempotent; the
// generated prompt asks for `CREATE TABLE IF NOT EXISTS` for exactly this reason.
db.exec(readFileSync(SCHEMA_PATH, "utf8"));

function rows(sql: string, ...params: unknown[]) {
  return db.prepare(sql).all(...(params as never[])) as Record<string, unknown>[];
}

const TABLES = rows(
  "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
).map((row) => String(row.name));

// The identifier a request may name. Quoting alone would stop injection but not a
// request for a table that does not exist, which should be a 404 rather than an
// empty list. Matching against the live schema makes both true at once.
function columnsOf(table: string): string[] {
  return rows('PRAGMA table_info("' + table + '")').map((row) => String(row.name));
}

function keyOf(table: string): string {
  const pk = rows('PRAGMA table_info("' + table + '")').find((row) => Number(row.pk) === 1);
  return pk ? String(pk.name) : "rowid";
}

// Quoting is not the guard — `collectionOf` is, because it matches the identifier
// against the live schema first. This is what makes the quoting true even for a name
// that got through it.
const quote = (identifier: string) => '"' + identifier.replaceAll('"', '""') + '"';

function collectionOf(value: string | undefined) {
  const table = (value ?? "").trim();
  if (!TABLES.includes(table)) return null;
  return { table, columns: columnsOf(table), key: keyOf(table) };
}

function sendJson(response: { writeHead: (code: number, headers: Record<string, string>) => void; end: (body: string) => void }, code: number, payload: unknown) {
  const body = JSON.stringify(payload);
  response.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "content-length": String(Buffer.byteLength(body)),
    "cache-control": "no-store",
  });
  response.end(body);
}

function sendError(response: { writeHead: (code: number, headers: Record<string, string>) => void; end: (body: string) => void }, code: number, message: string) {
  sendJson(response, code, { error: message });
}

async function readBody(request: AsyncIterable<Uint8Array>): Promise<Record<string, unknown>> {
  const chunks: Uint8Array[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.byteLength;
    if (size > MAX_BODY_BYTES) throw new Error("request body is too large");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("request body must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

// Only the columns the table actually has, so an unknown field is a 400 rather than
// a silent no-op that looks like it saved.
function pick(collection: { columns: string[] }, body: Record<string, unknown>) {
  const known: Record<string, unknown> = {};
  const unknown = Object.keys(body).filter((name) => !collection.columns.includes(name));
  if (unknown.length > 0) throw new Error(`unknown field(s): ${unknown.join(", ")}`);
  for (const name of collection.columns) {
    if (name in body) known[name] = body[name] as unknown;
  }
  return known;
}

type Row = Record<string, unknown>;

function toParams(row: Row): unknown[] {
  return Object.values(row).map((value) => (value === null || typeof value === "object" ? JSON.stringify(value) : (value as unknown)));
}

function handleList(collection: { table: string; columns: string[] }, url: URL, response: Parameters<typeof sendJson>[0]) {
  const where: string[] = [];
  const params: unknown[] = [];

  for (const [name, value] of url.searchParams) {
    if (name === "limit" || name === "offset" || name === "order") continue;
    if (!collection.columns.includes(name)) return sendError(response, 400, `unknown column: ${name}`);
    where.push(`${quote(name)} = ?`);
    params.push(value);
  }

  const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? DEFAULT_LIMIT) || DEFAULT_LIMIT, 1), MAX_LIMIT);
  const offset = Math.max(Number(url.searchParams.get("offset") ?? 0) || 0, 0);

  let order = "rowid";
  const requested = url.searchParams.get("order");
  if (requested) {
    const descending = requested.startsWith("-");
    const name = descending ? requested.slice(1) : requested;
    if (!collection.columns.includes(name)) return sendError(response, 400, `unknown column: ${name}`);
    order = `${quote(name)} ${descending ? "DESC" : "ASC"}`;
  }

  const filter = where.length > 0 ? ` WHERE ${where.join(" AND ")}` : "";
  const sql = `SELECT * FROM ${quote(collection.table)}${filter} ORDER BY ${order} LIMIT ? OFFSET ?`;
  sendJson(response, 200, { data: rows(sql, ...params, limit, offset) });
}

async function handleCreate(collection: { table: string; columns: string[] }, request: AsyncIterable<Uint8Array>, response: Parameters<typeof sendJson>[0]) {
  const body = await readBody(request);
  const record = pick(collection, body);
  const names = Object.keys(record);
  if (names.length === 0) return sendError(response, 400, "a record needs at least one field");

  const sql = `INSERT INTO ${quote(collection.table)} (${names.map(quote).join(", ")}) VALUES (${names.map(() => "?").join(", ")})`;
  const result = db.prepare(sql).run(...(toParams(record) as never[]));
  const id = result.lastInsertRowid;
  const stored = rows(`SELECT * FROM ${quote(collection.table)} WHERE rowid = ?`, id);
  sendJson(response, 201, { data: stored[0] ?? null });
}

function handleRead(collection: { table: string; key: string }, id: string, response: Parameters<typeof sendJson>[0]) {
  const found = rows(`SELECT * FROM ${quote(collection.table)} WHERE ${quote(collection.key)} = ?`, id);
  if (!found[0]) return sendError(response, 404, "no such record");
  sendJson(response, 200, { data: found[0] });
}

async function handleUpdate(collection: { table: string; key: string; columns: string[] }, id: string, request: AsyncIterable<Uint8Array>, response: Parameters<typeof sendJson>[0]) {
  const record = pick(collection, await readBody(request));
  const names = Object.keys(record);
  if (names.length === 0) return sendError(response, 400, "nothing to update");

  const sql = `UPDATE ${quote(collection.table)} SET ${names.map((name) => `${quote(name)} = ?`).join(", ")} WHERE ${quote(collection.key)} = ?`;
  const result = db.prepare(sql).run(...(toParams(record) as never[]), id);
  if (result.changes === 0) return sendError(response, 404, "no such record");

  const stored = rows(`SELECT * FROM ${quote(collection.table)} WHERE ${quote(collection.key)} = ?`, id);
  sendJson(response, 200, { data: stored[0] ?? null });
}

function handleDelete(collection: { table: string; key: string }, id: string, response: Parameters<typeof sendJson>[0]) {
  const result = db.prepare(`DELETE FROM ${quote(collection.table)} WHERE ${quote(collection.key)} = ?`).run(id);
  if (result.changes === 0) return sendError(response, 404, "no such record");
  sendJson(response, 200, { data: { deleted: true } });
}

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

function serveStatic(pathname: string, response: Parameters<typeof sendJson>[0] & { end: (body?: unknown) => void }) {
  const safe = resolve(join(PUBLIC_DIR, pathname));
  // `startsWith(PUBLIC_DIR + sep)` is the traversal guard: a resolved path that
  // escaped the public directory is not served, whatever it is.
  if (safe !== PUBLIC_DIR && !safe.startsWith(PUBLIC_DIR + sep)) return sendError(response, 403, "forbidden");

  let file = existsSync(safe) && statSync(safe).isFile() ? safe : join(PUBLIC_DIR, "index.html");
  if (!existsSync(file)) return sendError(response, 404, "not found");

  const headers = {
    "content-type": CONTENT_TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
    "content-length": String(statSync(file).size),
    // Hashed build assets are immutable; the entry point is not, or a republished
    // app keeps serving the previous build's HTML.
    "cache-control": file.includes(`${sep}assets${sep}`) ? "public, max-age=31536000, immutable" : "no-cache",
  };
  response.writeHead(200, headers);
  createReadStream(file).pipe(response as never);
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);

    if (url.pathname === "/api/health") {
      return sendJson(response, 200, { ok: true, tables: TABLES });
    }

    const match = url.pathname.match(/^\/api\/([^/]+)(?:\/([^/]+))?(?:\/([^/]+))?$/);
    if (match) {
      const collection = collectionOf(decodeURIComponent(match[1]));
      if (!collection) return sendError(response, 404, "no such collection");
      const id = match[2] ? decodeURIComponent(match[2]) : null;

      if (id === null && request.method === "GET") return handleList(collection, url, response);
      if (id === null && request.method === "POST") return await handleCreate(collection, request, response);
      if (id !== null && request.method === "GET") return handleRead({ ...collection, key: collection.key }, id, response);
      if (id !== null && request.method === "PATCH") return await handleUpdate({ ...collection, key: collection.key }, id, request, response);
      if (id !== null && request.method === "PUT") return await handleUpdate({ ...collection, key: collection.key }, id, request, response);
      if (id !== null && request.method === "DELETE") return handleDelete({ ...collection, key: collection.key }, id, response);

      return sendError(response, 405, `${request.method} is not allowed on this path`);
    }

    if (url.pathname.startsWith("/api/")) return sendError(response, 404, "not found");
    if (request.method !== "GET" && request.method !== "HEAD") return sendError(response, 405, "method not allowed");

    return serveStatic(url.pathname, response);
  } catch (error) {
    // A thrown handler is still a response. An uncaught one hangs the request, and a
    // hung request in a browser reads as a slow app rather than a broken one.
    sendError(response, 500, error instanceof Error ? error.message : "internal error");
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`listening on 0.0.0.0:${PORT} — data in ${DATA_DIR}, serving ${PUBLIC_DIR}`);
});
"""

# The runtime image. Two stages: the client is compiled with the toolchain, and the
# runtime stage carries no node_modules at all. `HEALTHCHECK` matters more than it
# looks — an application that boots and then throws on its first request is exactly
# what a container without one reports as healthy.
DOCKERFILE = """# syntax=docker/dockerfile:1
# Generated by scripts/package-app.py. Not model output.
FROM %(image)s AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM %(image)s
WORKDIR /app
ENV NODE_ENV=production \\
    PORT=3000 \\
    DATA_DIR=/data \\
    PUBLIC_DIR=/app/public
COPY --from=build /app/dist/client ./public
COPY server ./server
EXPOSE 3000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server/main.ts"]
""" % {"image": NODE_IMAGE}

DOCKERIGNORE = """node_modules
dist
data
*.zip
*.log
.git
"""

GITIGNORE = """node_modules/
dist/
data/
*.zip
app.manifest.json
"""

README = """# {title}

Generated in Olympus Studio and packaged with `scripts/package-app.py`.

- `src/` — the React client. This is what you edit.
- `server/schema.sql` — the database tables. Edit this to change the data model.
- `server/main.ts` — the generated HTTP API. Regenerated by the packager; it is not
  the place to make changes, because they will be overwritten on the next build.
- `dist/client/` — the built client.

Run it locally:

```bash
npm ci
npm run build
npm start                  # http://127.0.0.1:3000
```

Or as a container, which is how it is published:

```bash
docker build -t {slug} .
docker run --rm -p 3000:3000 -v {slug}-data:/data {slug}
```

The database lives in `/data/app.sqlite` inside the container. Keep the volume and
the data survives a rebuild; drop it and the tables are recreated from
`server/schema.sql`.
"""

# Everything that belongs in the archive, in the order a reader should meet it.
PROJECT_FILES = (
    "README.md",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "index.html",
    "Dockerfile",
    ".dockerignore",
)

ARCHIVE_DIRS = ("dist", "src", "server")

SCAFFOLD: dict[str, str] = {
    "package.json": PACKAGE_JSON,
    "vite.config.ts": VITE_CONFIG,
    "tsconfig.json": TSCONFIG,
    "index.html": INDEX_HTML,
    "src/main.tsx": MAIN_TSX,
    "src/index.css": INDEX_CSS,
    "server/main.ts": SERVER_MAIN,
    "Dockerfile": DOCKERFILE,
    ".dockerignore": DOCKERIGNORE,
    ".gitignore": GITIGNORE,
}

# Files a build may own that the scaffold must never clobber silently.
PROTECTED_SOURCES = ("src/App.tsx", "server/schema.sql")

# Files the scaffold owns outright: if the model emitted one, it is replaced, because
# a model-written server or Dockerfile is exactly what this step exists to prevent.
SCAFFOLD_OWNS = ("server/main.ts",)


# --- helpers -----------------------------------------------------------------


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str, code: int) -> "NoReturn":  # type: ignore[name-defined]
    note(f"PACKAGE_FAILED: {message}")
    raise SystemExit(code)


def repo_root() -> Path:
    """The checkout this script lives in — not the caller's cwd."""
    root = Path(__file__).resolve().parent.parent
    if not (root / ".archon").is_dir() and not (root / "Makefile").is_file():
        fail(f"{root} does not look like the Olympus checkout", 2)
    return root


def build_dir(root: Path, slug: str) -> Path:
    if not SLUG_PATTERN.match(slug):
        fail(f"unsafe app slug: {slug!r}", 2)

    target = (root / BUILDS_DIR / slug).resolve()
    builds = (root / BUILDS_DIR).resolve()

    if target != builds / slug:
        fail(f"refusing to work outside {BUILDS_DIR}/: {target}", 2)

    if not target.is_dir():
        fail(
            f"no build at {BUILDS_DIR}/{slug} — build the app first "
            f"(make app SPEC=build-requests/{slug}.md)",
            2,
        )

    return target


def require_entry(app_dir: Path, relative: str, description: str) -> Path:
    path = app_dir / relative
    if not path.is_file() or not path.read_text(encoding="utf-8", errors="replace").strip():
        fail(f"{relative} is missing or empty in {app_dir} — {description}", 2)
    return path


def which_npm() -> str:
    npm = shutil.which("npm")
    if not npm:
        fail(
            "npm is not on PATH. Packaging an app needs Node 20+ and npm; the Studio "
            "image deliberately has neither, which is why this runs on the host "
            "(scripts/build-runner.py) rather than in the container.",
            2,
        )
    return npm


def which_docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        fail("docker is not on PATH, so the application's runtime image cannot be built", 2)
    return docker


def write_scaffold(app_dir: Path, *, force: bool) -> list[str]:
    """Write the project files. Returns the relative paths written.

    The model's three files are never overwritten. Everything else here is the
    project around them, and a scaffold that ate `src/App.tsx` would be a very quiet
    way to publish an app with no interface.
    """
    written: list[str] = []

    for relative, contents in SCAFFOLD.items():
        target = app_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and relative in PROTECTED_SOURCES and not force:
            continue

        # `src/index.css` is imported unconditionally by the scaffold's main.tsx, so
        # it must exist — but a stylesheet the model wrote is better than ours.
        if target.exists() and relative == "src/index.css":
            continue

        # A model that emitted its own server or Dockerfile does not get to keep it:
        # this is the file the request path runs through, and the packager is the
        # only author of it.
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == contents:
            continue

        target.write_text(contents, encoding="utf-8")
        written.append(relative)

    readme = app_dir / "README.md"
    if not readme.exists():
        title = app_dir.name.replace("-", " ").strip().title() or app_dir.name
        readme.write_text(README.format(title=title, slug=app_dir.name), encoding="utf-8")
        written.append("README.md")

    return written


def run(command: list[str], cwd: Path) -> int:
    """Run a command, streaming its output straight through to the job log."""
    note(f"$ {' '.join(command)}  (in {cwd})")
    return subprocess.call(command, cwd=str(cwd))  # noqa: S603 - fixed argv, no shell


# --- archive -----------------------------------------------------------------


def app_files(app_dir: Path) -> list[dict]:
    """Everything that belongs in the archive and in the manifest.

    Ordered project files, then `dist/`, then `src/` and `server/`: an operator
    opening the archive sees what it is (and how to run it) before the built output,
    and the output before the source it came from.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    def add(relative: str, path: Path) -> None:
        if relative in seen:
            return
        seen.add(relative)
        rows.append({"path": relative, "bytes": path.stat().st_size})

    for name in PROJECT_FILES:
        path = app_dir / name
        if path.is_file():
            add(name, path)

    for root in ARCHIVE_DIRS:
        base = app_dir / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            add(str(path.relative_to(app_dir)).replace(os.sep, "/"), path)

    return rows


def write_zip(app_dir: Path, rows: list[dict]) -> Path:
    target = app_dir / "app.zip"
    temporary = app_dir / ".app.zip.tmp"

    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            archive.write(app_dir / row["path"], arcname=row["path"])

    temporary.replace(target)
    return target


def write_manifest(app_dir: Path, slug: str, rows: list[dict], zip_path: Path | None) -> Path:
    dist = [row for row in rows if row["path"].startswith("dist/")]
    source = [row for row in rows if row["path"].startswith(("src/", "server/"))]
    manifest = {
        "v": 1,
        "kind": "app",
        "slug": slug,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry": ENTRY_BUILT if dist else None,
        "dist_files": len(dist),
        "dist_bytes": sum(row["bytes"] for row in dist),
        "source_files": len(source),
        "project_files": len(rows) - len(dist) - len(source),
        "image": image_tag(slug),
        "files": rows,
        "zip": zip_path.name if zip_path else None,
    }

    target = app_dir / "app.manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def image_tag(slug: str) -> str:
    """The image name, derived from the slug and nothing else.

    One name per app, and the same one `app-runtime.py` runs: a second naming scheme
    is a second thing to keep in sync, and the failure it produces is a publish that
    runs the previous image.
    """
    return f"olympus-app-{slug}:latest"


def build_image(app_dir: Path, slug: str, sink=None) -> int:
    docker = which_docker()
    command = [docker, "build", "--tag", image_tag(slug), "."]
    note(f"$ {' '.join(command)}  (in {app_dir})")

    if sink is None:
        return subprocess.call(command, cwd=str(app_dir))  # noqa: S603 - fixed argv

    return subprocess.call(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=str(app_dir),
        stdin=subprocess.DEVNULL,
        stdout=sink,
        stderr=subprocess.STDOUT,
    )


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package a Studio-generated full-stack app into a runnable build.",
    )
    parser.add_argument("slug", help="the app slug under builds/")
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="write the project files and stop; no install, no build",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would happen and change nothing",
    )
    parser.add_argument("--no-zip", action="store_true", help="skip the source archive")
    parser.add_argument("--image", action="store_true", help="also build the runtime image")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow the scaffold to replace files it would otherwise leave alone",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    app_dir = build_dir(root, args.slug)
    require_entry(app_dir, ENTRY_CLIENT, "that is the app's interface and nothing renders without it")
    require_entry(app_dir, ENTRY_SCHEMA, "that is the app's data model and the API has nothing to serve without it")

    if args.check:
        lock = (app_dir / "package-lock.json").exists()
        note(f"slug:     {args.slug}")
        note(f"dir:      {app_dir}")
        note(f"client:   {ENTRY_CLIENT} ({(app_dir / ENTRY_CLIENT).stat().st_size} bytes)")
        note(f"schema:   {ENTRY_SCHEMA} ({(app_dir / ENTRY_SCHEMA).stat().st_size} bytes)")
        note(f"npm:      {shutil.which('npm') or '(missing)'}")
        note(f"docker:   {shutil.which('docker') or '(missing)'}")
        note(f"install:  {'npm ci (lockfile present)' if lock else 'npm install (first package)'}")
        note(f"image:    {image_tag(args.slug)} ({'build' if args.image else 'not built'})")
        return 0

    written = write_scaffold(app_dir, force=args.force)
    if written:
        note(f"scaffolded: {', '.join(written)}")

    if args.scaffold_only:
        return 0

    npm = which_npm()
    locked = (app_dir / "package-lock.json").exists()
    install = [npm, "ci"] if locked else [npm, "install", "--no-audit", "--no-fund"]

    if run(install, app_dir) != 0:
        fail(
            f"dependency install failed ({' '.join(install)}). If this host has no npm "
            "registry access, package the app where it does — the same `npm ci` and "
            "`npm run build` work in any Node 20+ environment.",
            1,
        )

    if run([npm, "run", "build"], app_dir) != 0:
        fail("the client did not build — see the output above; dist/client was not written", 1)

    if not (app_dir / ENTRY_BUILT).is_file():
        fail(f"the build reported success but {ENTRY_BUILT} does not exist", 1)

    rows = app_files(app_dir)
    zip_path = None if args.no_zip else write_zip(app_dir, rows)
    manifest = write_manifest(app_dir, args.slug, rows, zip_path)

    dist_bytes = sum(row["bytes"] for row in rows if row["path"].startswith("dist/"))
    note(
        f"packaged {args.slug}: {len([r for r in rows if r['path'].startswith('dist/')])} "
        f"client file(s), {dist_bytes} bytes"
    )
    note(f"manifest: {manifest}")
    if zip_path:
        note(f"archive:  {zip_path} ({zip_path.stat().st_size} bytes)")

    if args.image:
        if build_image(app_dir, args.slug) != 0:
            fail("the runtime image did not build — see the docker output above", 1)
        note(f"image:    {image_tag(args.slug)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

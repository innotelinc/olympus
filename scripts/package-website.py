#!/usr/bin/env python3
"""Package a Studio website build into a real, servable site.

WHY THIS EXISTS. A Studio *app* is finished the moment it is generated: it is
HTML/CSS/JS and the browser runs it directly. A Studio *website* is finished
nowhere near that point. The model writes React components, and JSX is not
something a browser can execute — it needs a build. So a website has one more
step between "generated" and "real", and that step is this script. Until it runs,
the site exists as source and nothing else.

THE MODEL WRITES THE SITE; THIS WRITES THE PROJECT AROUND IT. `package.json`,
`vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx` and the `.gitignore`
are emitted here, byte-for-byte, from constants. They are not model output and not
model-tunable. A model that chose its own dependency ranges turns "the site builds"
into a coin flip, and a failed `npm install` reports nothing about the site. What
the model owns is the one thing that needs judgement: `src/App.tsx` and its styles.

WHAT IS AND IS NOT A DEPENDENCY. The generated site may import `react` and
nothing else — no CSS framework, no icon pack, no charting library — because there
is no package.json for the model to add one to. The scaffold's dependency set is
fixed, which is what makes a build here deterministic instead of a supply-chain
gamble taken once per generated site.

INSTALL IS THE SLOW PART, AND IT IS CACHED BY CONSTRUCTION. The first package of a
build writes `package-lock.json`; every later package of the same build reuses it
(`npm ci`), so a revision is a rebuild rather than a second full install. A build
with no lock and no registry access fails loudly with the command it tried, rather
than producing an empty `dist/` that looks like a site that built.

    scripts/package-website.py todo-list              # scaffold, install, build, zip
    scripts/package-website.py todo-list --scaffold-only
    scripts/package-website.py todo-list --check      # report what would happen
    scripts/package-website.py todo-list --no-zip
    scripts/package-website.py todo-list --publish    # also stage for the host

Exit codes:
    0  packaged (or, with --check/--scaffold-only, the step that was asked for)
    1  the site did not build — the npm output is in the log, in full
    2  the request made no sense (no such build, no src/App.tsx, no toolchain)
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

# The entry point the model is told to write, and the only file this step requires.
ENTRY_SOURCE = "src/App.tsx"
ENTRY_BUILT = "dist/index.html"

# Where `npm` and the built site go, relative to the repository root.
BUILDS_DIR = "builds"

# A slug safe to use as a directory name. Re-checked here rather than trusted from
# the request: the runner validated it, but this script is also runnable by hand.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

# Where a published site is staged. Deliberately outside the checkout: what is
# served is runtime state, and a served tree inside the repo is one `git add -A`
# away from being committed.
DEFAULT_SITES_ROOT = "/var/lib/olympus/sites"

# Scaffold files. Exact strings, no f-strings, no interpolation: two packages of
# the same build must produce identical scaffolding or the lockfile stops meaning
# anything.
PACKAGE_JSON = """{
  "name": "olympus-site",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview"
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

// Relative base: a published site must work from a sub-path as well as a root,
// because the same zip is handed to whoever hosts it and we do not control where.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
"""

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
    <title>Site</title>
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

GITIGNORE = """node_modules/
dist/
"""

README = """# {title}

Generated in Olympus Studio and packaged with `scripts/package-website.py`.

- `src/` — the source, written by the model. This is what you edit.
- `dist/` — the built site. This is what you serve.

```bash
npm ci
npm run build
```

Deploy `dist/` as static files. It needs no server-side runtime.
"""

# Scaffold path -> contents. One table, so "what did it write" has one answer.
SCAFFOLD: dict[str, str] = {
    "package.json": PACKAGE_JSON,
    "vite.config.ts": VITE_CONFIG,
    "tsconfig.json": TSCONFIG,
    "index.html": INDEX_HTML,
    "src/main.tsx": MAIN_TSX,
    "src/index.css": INDEX_CSS,
    ".gitignore": GITIGNORE,
}

# Files a build may own that the scaffold must never clobber silently.
PROTECTED_SOURCES = ("src/App.tsx",)


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

    # The slug pattern already rules out a separator, but the check is the point:
    # it holds even if the pattern is later loosened.
    if target != builds / slug:
        fail(f"refusing to work outside {BUILDS_DIR}/: {target}", 2)

    if not target.is_dir():
        fail(
            f"no build at {BUILDS_DIR}/{slug} — manufacture the site first "
            f"(make app SPEC=build-requests/{slug}.md)",
            2,
        )

    return target


def entry_source(app_dir: Path) -> Path:
    entry = app_dir / ENTRY_SOURCE
    if not entry.is_file() or not entry.read_text(encoding="utf-8", errors="replace").strip():
        fail(
            f"{ENTRY_SOURCE} is missing or empty in {app_dir} — that is the site's "
            "entry point and there is nothing to package without it",
            2,
        )
    return entry


def which_npm() -> str:
    npm = shutil.which("npm")
    if not npm:
        fail(
            "npm is not on PATH. Packaging a website needs Node 20+ and npm; the "
            "Studio image deliberately has neither, which is why this runs on the "
            "host (scripts/build-runner.py) rather than in the container.",
            2,
        )
    return npm


def write_scaffold(app_dir: Path, *, force: bool) -> list[str]:
    """Write the project files. Returns the paths written.

    `src/App.tsx` and anything else the model wrote are never overwritten: the
    scaffold owns the project, the model owns the site, and a scaffold that ate the
    site would be a very quiet way to publish an empty page.
    """
    written: list[str] = []

    for relative, contents in SCAFFOLD.items():
        target = app_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and relative in PROTECTED_SOURCES and not force:
            continue

        # `src/index.css` is imported unconditionally by the scaffold's main.tsx,
        # so it must exist — but a stylesheet the model wrote is better than ours.
        if target.exists() and relative == "src/index.css":
            continue

        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == contents:
            continue

        target.write_text(contents, encoding="utf-8")
        written.append(relative)

    readme = app_dir / "README.md"
    if not readme.exists():
        title = app_dir.name.replace("-", " ").strip().title() or app_dir.name
        readme.write_text(README.format(title=title), encoding="utf-8")
        written.append("README.md")

    return written


def run(command: list[str], cwd: Path) -> int:
    """Run a command, streaming its output straight through to the job log."""
    note(f"$ {' '.join(command)}  (in {cwd})")
    return subprocess.call(command, cwd=str(cwd))  # noqa: S603 - fixed argv, no shell


# The project files that make the archive rebuildable. Without them a zip holds
# `dist/` and `src/` and cannot be installed, which makes it an artifact nobody can
# maintain — the opposite of what handing over source is for.
PROJECT_FILES = (
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "index.html",
    "README.md",
)


def site_files(app_dir: Path) -> list[dict]:
    """Everything that belongs in the zip and in the manifest.

    Ordered project files, then `dist/`, then `src/`: an operator opening the
    archive sees what it is (and how to rebuild it) before the built output, and the
    output before the source it came from. Entry order is the file order in the
    central directory, and it is the only order a reader has.
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

    for root in ("dist", "src"):
        base = app_dir / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            add(str(path.relative_to(app_dir)).replace(os.sep, "/"), path)

    return rows


def write_zip(app_dir: Path, rows: list[dict]) -> Path:
    """One archive holding both halves, dist first.

    Both, not just `dist/`: handing over only the built output is handing over
    something nobody can maintain, and the source is usually a few kilobytes next
    to a `dist/` bundle that is already minified into unreadability.
    """
    target = app_dir / "site.zip"
    temporary = app_dir / ".site.zip.tmp"

    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            archive.write(app_dir / row["path"], arcname=row["path"])

    temporary.replace(target)
    return target


def write_manifest(app_dir: Path, slug: str, rows: list[dict], zip_path: Path | None) -> Path:
    dist = [row for row in rows if row["path"].startswith("dist/")]
    source = [row for row in rows if row["path"].startswith("src/")]
    manifest = {
        "v": 1,
        "kind": "website",
        "slug": slug,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry": ENTRY_BUILT if dist else None,
        "dist_files": len(dist),
        "dist_bytes": sum(row["bytes"] for row in dist),
        # Source only, not "everything that is not dist" — the project files are
        # neither, and counting them as source made the number say something the
        # reader would take for the size of the site the model wrote.
        "source_files": len(source),
        "project_files": len(rows) - len(dist) - len(source),
        "files": rows,
        "zip": zip_path.name if zip_path else None,
    }

    target = app_dir / "site.manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def publish(app_dir: Path, slug: str) -> Path:
    """Stage the built site where the host serves it from.

    A copy, not a move, and `dist/` only: the served root is a delivery surface, and
    the build directory stays the record of what was built. An existing staging
    directory is replaced — it is a copy of a build that is still on disk, so there
    is nothing here that is not reproducible.
    """
    root = Path(os.environ.get("OLYMPUS_SITES_ROOT", DEFAULT_SITES_ROOT)).expanduser()
    dist = app_dir / "dist"

    if not dist.is_dir():
        fail("nothing to publish — dist/ does not exist; run the package step first", 1)

    target = root / slug
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        shutil.rmtree(target)

    shutil.copytree(dist, target)
    return target


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package a Studio-generated React website into dist/ and a zip.",
    )
    parser.add_argument("slug", help="the app slug under builds/")
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="write the Vite project files and stop; no install, no build",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would happen and change nothing",
    )
    parser.add_argument("--no-zip", action="store_true", help="skip the source+dist archive")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="also stage dist/ under OLYMPUS_SITES_ROOT for the host to serve",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow the scaffold to replace files it would otherwise leave alone",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    app_dir = build_dir(root, args.slug)
    entry_source(app_dir)

    if args.check:
        lock = (app_dir / "package-lock.json").exists()
        npm = shutil.which("npm") or "(missing)"
        note(f"slug:     {args.slug}")
        note(f"dir:      {app_dir}")
        note(f"entry:    {ENTRY_SOURCE} ({entry_source(app_dir).stat().st_size} bytes)")
        note(f"npm:      {npm}")
        note(f"install:  {'npm ci (lockfile present)' if lock else 'npm install (first package)'}")
        note(f"publish:  {'yes' if args.publish else 'no'}")
        return 0

    # The scaffold always runs, even on --check-free paths: a build directory that
    # has only src/App.tsx cannot be installed or built, and writing the project
    # files is idempotent.
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
            f"dependency install failed ({' '.join(install)}). If this host has no "
            "npm registry access, package the site where it does — the same `npm ci` "
            "and `npm run build` work in any Node 20+ environment.",
            1,
        )

    if run([npm, "run", "build"], app_dir) != 0:
        fail("vite build failed — see the output above; dist/ was not written", 1)

    if not (app_dir / ENTRY_BUILT).is_file():
        fail(f"the build reported success but {ENTRY_BUILT} does not exist", 1)

    rows = site_files(app_dir)
    zip_path = None if args.no_zip else write_zip(app_dir, rows)
    manifest = write_manifest(app_dir, args.slug, rows, zip_path)

    dist_bytes = sum(row["bytes"] for row in rows if row["path"].startswith("dist/"))
    note(
        f"packaged {args.slug}: {len([r for r in rows if r['path'].startswith('dist/')])} "
        f"dist file(s), {dist_bytes} bytes"
    )
    note(f"manifest: {manifest}")
    if zip_path:
        note(f"archive:  {zip_path} ({zip_path.stat().st_size} bytes)")

    if args.publish:
        staged = publish(app_dir, args.slug)
        note(f"staged:   {staged}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

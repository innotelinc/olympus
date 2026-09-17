#!/usr/bin/env python3
"""Turn a generated project into a runnable image, from the plan that built it.

WHY THIS EXISTS. `package-app.py` and `package-website.py` each knew their stack:
one wrote a React client and a generated Node/SQLite server, the other a Vite
project. That was the whole reason Studio could not build what the user asked for
— the packagers were the second half of the hardcoded stack, and a generator that
stops insisting on React is no use if the step after it does.

So this reads `plan.json` — the plan the person confirmed — and writes the project
around it:

    install, build, start   the commands the plan named, run in order
    runtime.language        chooses the base image
    target                  where the state lives: `convex` points the client at the
                            deployment and still builds a container for the client
    run.port                the port the app listens on inside its container
    run.healthcheck         the path that answers once it is up

INSTALL AND BUILD RUN AT IMAGE BUILD TIME, NOT AT BOOT. `RUN <install>` and
`RUN <build>` mean a failure is a *packaging* failure with the compiler's or the
package manager's own message, in the log the operator is already watching — and
it means the published container starts and serves, rather than starting cleanly
and failing its first request because a dependency was missing.

WHAT THIS DOES NOT DO. It does not read the project. The model owns every file,
including the manifest that declares its own dependencies, because that is what
"conform to whatever the user wants" has to mean. What is deterministic here is
the container: the base image, the port, the data directory, the healthcheck, and
the fact that the commands the user saw are the commands that ran.

    scripts/package-project.py weight-tracker
    scripts/package-project.py weight-tracker --no-build     # write the files only

Exit codes:
    0  packaged (or, with --no-build, would have been)
    1  the image did not build — the reason is in the output above it
    2  the request made no sense (no build, no plan, an unusable plan)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# --- constants ---------------------------------------------------------------

PLAN_NAME = "plan.json"
MANIFEST_NAME = "project.manifest.json"
DOCKERFILE_NAME = "Dockerfile"
DOCKERIGNORE_NAME = ".dockerignore"
ARCHIVE_NAME = "project.zip"

BUILDS_DIR = "builds"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

# A plan's port is the app's port *inside its container*. The range matches what
# the planner is allowed to propose: below 1024 needs a root the container does
# not have, and above 49151 is the ephemeral range a client picks from.
MIN_PORT = 1024
MAX_PORT = 49151

MAX_COMMAND_CHARS = 500

# The deployment URL a Convex-targeted project's client reads. Convex's own tooling
# uses `CONVEX_URL`; a Vite client only sees `VITE_`-prefixed variables and a Next
# client only sees `NEXT_PUBLIC_`-prefixed ones, and both inline them at bundle
# time — which is why the same value is written under all three names rather than
# being handed to the container at run time, where a bundled client could not see it.
#
# It is an address, not a credential, and baking it is the point: the browser has to
# reach that deployment, so it is public wherever it is written.
CONVEX_URL_KEYS: tuple[str, ...] = ("CONVEX_URL", "VITE_CONVEX_URL", "NEXT_PUBLIC_CONVEX_URL")

# The one credential a Convex build may use, and the reason the Dockerfile declares
# it as an `ARG` and never an `ENV`: an `ENV` would carry it into the published
# image, and a running container has nothing to deploy. It is passed as a
# `--build-arg` only when the build environment has it, and Docker records build
# arguments in the image's metadata — so a key scoped to this one deployment
# (`npx convex deploy-key create`) is the only kind that may be used here. The
# platform's admin key is never read, and never passed.
CONVEX_DEPLOY_KEY = "CONVEX_DEPLOY_KEY"

# The base image per language. Pinned, and alpine-based: these images exist, they
# are small, and every one of them carries the busybox `wget` the healthcheck uses.
#
# Rust, Java and .NET are absent on purpose rather than by omission. Each needs a
# much larger toolchain stage and a build artefact copied between them, and a
# half-working entry would be worse than a clear refusal naming what is supported.
BASE_IMAGES: dict[str, str] = {
    "node": "node:24-alpine",
    "python": "python:3.13-alpine",
    "go": "golang:1.24-alpine",
    "php": "php:8.4-cli-alpine",
    "ruby": "ruby:3.4-alpine",
    "static": "nginx:1.29-alpine",
}

# What a language's image needs in order to *compile* a dependency, as apk
# packages. Lockfiles are ignored above, so the tree comes from the manifest the
# model wrote, and a manifest may name something with no prebuilt binary for musl —
# `better-sqlite3` built from source, `sharp`, `bcrypt`. None of the base images
# carries a compiler, so without this such a project is an image that cannot be
# built; node-gyp's own message is about a missing Python, which reads like a Python
# problem and is really a missing toolchain.
#
# The cost is real and paid on every image of that language: the toolchain stays in
# the final layer, which is also what keeps the C++ runtime a compiled module links
# against. That is the trade this makes deliberately — a slower build over a project
# the platform cannot deliver.
#
# Each entry is the *half that is missing*. Every base image above is the official
# one and ships its own interpreter's headers — `Python.h` under
# /usr/local/include/python3.13, `ruby.h` from RbConfig, `phpize` in
# /usr/local/bin — so a distro `python3-dev` would be the *other* interpreter's
# headers, and `php-dev` is not a package that exists. What none of them carries is a
# C toolchain, and that is all these add. Verified per image: `apk add` succeeds and
# the headers are present in python:3.13-alpine, ruby:3.4-alpine and
# php:8.4-cli-alpine, and build-base resolves on golang:1.24-alpine for cgo.
#
# php names `$PHPIZE_DEPS` — the list that image exports for exactly this — rather
# than a copy of its contents, which drifts with the PHP release.
#
# `static` is not here and does not need to be: nginx serves files, so a plan with no
# install step has nothing that could be compiled.
BUILD_TOOLS: dict[str, tuple[str, ...]] = {
    "node": ("python3", "make", "g++"),
    "python": ("build-base",),
    "go": ("build-base",),
    "php": ("$PHPIZE_DEPS",),
    "ruby": ("build-base",),
}

# Language -> (what the runtime stage copies, where it puts it). A language listed
# here is built in two stages, so the toolchain above lives in the build stage and the
# published image is a fresh copy of the base plus what the build produced. A language
# absent from it is built in one stage, toolchain and all.
#
# The line is drawn by **where `install` puts its output**, and it is not a
# preference. Splitting only works when the install lands inside the project: node's
# `npm install` writes `node_modules` into the workdir, so the workdir *is* the
# result. `pip install` writes to site-packages and `bundle install` into the ruby
# installation, both outside the workdir — a runtime stage copied from the build stage
# would be missing everything the install produced, and the app would start and then
# fail on its first route. Those stay single-stage, where the toolchain is the price
# of the install landing somewhere the plan never names.
#
# The other half of the trade, and the reason this is not extended casually: a
# dependency *compiled* from source links against a runtime library the toolchain
# brought in. Node is safe because the base image already links libstdc++ itself.
# Before adding a language here, check that its base image carries what a compiled
# dependency would link against — otherwise the build succeeds and the container dies
# on first use of that module.
RUNTIME_FROM_BUILD: dict[str, tuple[str, str]] = {
    "node": ("/app", "/app"),
}

# What a plan may call a language and still mean one of the above. A planner
# writing "nodejs" or "TypeScript" is naming the same base image, and refusing it
# would be pedantry with a failed build attached.
LANGUAGE_ALIASES: dict[str, str] = {
    "nodejs": "node",
    "node.js": "node",
    "javascript": "node",
    "typescript": "node",
    "ts": "node",
    "js": "node",
    "express": "node",
    "nextjs": "node",
    "next": "node",
    "react": "node",
    "vite": "node",
    "vue": "node",
    "svelte": "node",
    "astro": "node",
    "npm": "node",
    "py": "python",
    "python3": "python",
    "flask": "python",
    "django": "python",
    "fastapi": "python",
    "uvicorn": "python",
    "gunicorn": "python",
    "golang": "go",
    "mod": "go",
    "html": "static",
    "css": "static",
    "vanilla": "static",
    "none": "static",
    "no": "static",
}

# Copied into every image's `.dockerignore`. `node_modules` and friends are the
# host's business, not the image's: a copied-in `node_modules` would shadow what
# `install` resolves, and the image would carry whatever the last host install left.
#
# `package-lock.json` is that same hazard one level up, and it is here for the same
# reason. This script reads the plan and nothing else, so it never writes a lockfile
# and cannot vouch for one that is sitting in the directory — while `npm install`
# trusts a lockfile completely. On 2026-09-14 one was 149 entries where npm resolves
# 200 for that same `package.json`: npm installed the partial tree, `better-sqlite3`
# fell through to `node-gyp rebuild` instead of using the prebuilt binary it ships,
# and the image could not be built at all. What made that hard to see is that it had
# *worked* — as a cached layer, until the context changed. Resolving the tree from
# the manifest is the behaviour the plan describes, and the only one this script can
# make a claim about.
DOCKERIGNORE = """Dockerfile
.dockerignore
plan.json
project.manifest.json
*.zip
*.log
.git
.gitignore
data
node_modules
package-lock.json
__pycache__
*.pyc
.venv
venv
vendor
target
dist
build
"""

# Left out of the archive: the manifests are the packager's account of what it
# made, and the archive itself. Everything else goes in — including the Dockerfile
# and `plan.json`, which are what make the download runnable by somebody who was
# not watching: the Dockerfile is the commands, and the plan is why.
ARCHIVE_SKIP = {
    MANIFEST_NAME,
    ARCHIVE_NAME,
    "MANIFEST.json",
    "app.manifest.json",
    "site.manifest.json",
}


# --- helpers -----------------------------------------------------------------


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str, code: int) -> "NoReturn":  # type: ignore[name-defined]
    note(f"PACKAGE_PROJECT_FAILED: {message}")
    raise SystemExit(code)


def repo_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if not (root / "Makefile").is_file():
        fail(f"{root} does not look like the Olympus checkout", 2)
    return root


def build_dir(root: Path, slug: str) -> Path:
    directory = (root / BUILDS_DIR / slug).resolve()
    builds = (root / BUILDS_DIR).resolve()
    if builds != directory and builds not in directory.parents:
        fail(f"refusing to package outside {builds}: {directory}", 2)
    if not directory.is_dir():
        fail(f"no build at builds/{slug}", 2)
    return directory


def image_tag(slug: str) -> str:
    """One name per project, and the same one `app-runtime.py` runs.

    A second naming scheme would be a second thing to keep in sync, and the failure
    it produces is a publish that runs the previous image.
    """
    return f"olympus-app-{slug}:latest"


def buildx() -> Path:
    """The repo's one image-building command.

    `scripts/buildx` is where the builder is chosen — BuildKit when this host has
    the plugin, the deprecated legacy builder otherwise — so that decision is not
    made twice (see scripts/build-lib.sh). It also owns the "docker is not on
    PATH" refusal, which is why this returns a path rather than a resolved binary.
    """
    wrapper = Path(__file__).resolve().parent / "buildx"
    if not wrapper.is_file():
        fail(f"{wrapper} is missing from this checkout, so nothing here can build an image", 2)
    return wrapper


# --- the plan ----------------------------------------------------------------


def normalise_language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key in BASE_IMAGES:
        return key
    return LANGUAGE_ALIASES.get(key)


def as_command(value: object) -> str:
    """A plan command, as one line.

    Whitespace is collapsed rather than the command being rejected: the planner
    wraps long commands, and a newline inside a `RUN` is a Dockerfile line
    continuation that would join it to the next instruction.
    """
    if not isinstance(value, str):
        return ""
    command = " ".join(value.split())
    if len(command) > MAX_COMMAND_CHARS:
        fail(f"a plan command is longer than {MAX_COMMAND_CHARS} characters", 2)
    return command


def read_plan(directory: Path) -> dict:
    path = directory / PLAN_NAME
    if not path.is_file():
        fail(
            f"no {PLAN_NAME} in {directory}. A project is packaged from the plan that "
            "built it — queue it again from Studio, which sends one.",
            2,
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(f"cannot read {PLAN_NAME}: {error}", 2)

    if not isinstance(payload, dict):
        fail(f"{PLAN_NAME} is not an object", 2)

    return payload


def plan_for_build(directory: Path) -> dict:
    """The plan, checked into the shape the Dockerfile generator needs.

    Every failure here is a *refusal with the reason*, because each of these has a
    specific way of going wrong later that looks like something else: a missing
    start command is a container that exits immediately, an unknown language is a
    `docker build` error about a manifest nobody can act on, and a bad port is a
    container running where nothing can reach it.
    """
    plan = read_plan(directory)

    language = normalise_language(plan.get("runtime", {}).get("language") if isinstance(plan.get("runtime"), dict) else None)
    if language is None:
        raw = ""
        if isinstance(plan.get("runtime"), dict):
            raw = str(plan["runtime"].get("language") or "")
        fail(
            f"this project's language is {raw!r}, which has no build image here. "
            f"Supported: {', '.join(sorted(BASE_IMAGES))}.",
            2,
        )

    run = plan.get("run")
    if not isinstance(run, dict):
        fail("the plan has no `run` section, so there is nothing to start", 2)

    start = as_command(run.get("start"))
    if not start:
        fail("the plan has no start command, so nothing would run", 2)

    install = as_command(run.get("install"))
    build = as_command(run.get("build"))

    if language == "static" and (install or build):
        # `static` means "there is no toolchain", and nginx is not one. A plan that
        # needs to install or compile has a language, and saying so here is the
        # difference between a fix and an image that fails on `npm: not found`.
        fail(
            "the plan says the language is 'static' but also asks to install or build. "
            "Pick the language the build needs (node, python, go, php, ruby), or drop "
            "the install and build commands.",
            2,
        )

    port_raw = run.get("port", 3000)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        fail(f"the plan's port is not a number: {port_raw!r}", 2)
    if not MIN_PORT <= port <= MAX_PORT:
        fail(f"the plan's port {port} is outside {MIN_PORT}-{MAX_PORT}", 2)

    healthcheck = run.get("healthcheck")
    if not isinstance(healthcheck, str) or not healthcheck.startswith("/"):
        healthcheck = "/"
    healthcheck = healthcheck.split("?")[0] or "/"

    target = "convex" if plan.get("target") == "convex" else "container"
    convex_url = ""

    if target == "convex":
        if language == "static":
            # `static` is nginx serving files, and files cannot deploy functions.
            # Refusing here is the difference between a clear sentence and a site that
            # serves but whose every read fails at a deployment that never received a
            # schema.
            fail(
                "this plan targets Convex but its language is 'static', and a static "
                "site has nothing to deploy to it. Pick the language its client needs "
                "(node, python, go, php, ruby), or drop the Convex target.",
                2,
            )

        convex_url = (os.environ.get("CONVEX_URL") or "").strip()
        if not convex_url:
            fail(
                "this plan targets Convex, but CONVEX_URL is not set in the build "
                "environment, so the client would have no deployment to reach. Set "
                "CONVEX_URL (the self-hosted Convex Atlas runs) and build again; set "
                f"{CONVEX_DEPLOY_KEY} as well if the plan's build deploys the "
                "functions.",
                2,
            )

    return {
        "language": language,
        "target": target,
        "convex_url": convex_url,
        "install": install,
        "build": build,
        "start": start,
        "port": port,
        "healthcheck": healthcheck,
        "kind": "website" if plan.get("kind") == "website" else "app",
        "name": str(plan.get("name") or ""),
        "summary": str(plan.get("summary") or ""),
        "database": (plan.get("runtime") or {}).get("database")
        if isinstance(plan.get("runtime"), dict)
        else None,
    }


# --- the Dockerfile ----------------------------------------------------------

# Shell metacharacters that mean the start command is more than one command, or
# needs a shell to mean what it says.
SHELL_META = re.compile(r"[|&;<>()$`*?\[\]{}~!]|\s\|\||&&")

# Quoted spans, which are the one place a metacharacter does not mean what it looks
# like: `node -e "console.log(1)"` is a single command with a parenthesis in an
# argument, and running it through a shell would be a second, unnecessary shell.
QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"", re.S)


def needs_shell(start: str) -> bool:
    """Whether this command has an operator *outside* its quotes."""
    return bool(SHELL_META.search(QUOTED_SPAN.sub("", start)))


def start_instruction(start: str) -> str:
    """`CMD` for the start command, in the form that keeps signals working.

    Three cases, in order of preference:

    1. A plain command — `python app.py`, `node server.js`, `npm start` — becomes
       the exec form. No shell at all, so the application is PID 1 and a `SIGTERM`
       from `docker stop` reaches it and it gets to close its database.
    2. A command with a shell operator in it — `npm run migrate && npm start` — has
       to keep the shell, because the JSON form cannot express `&&`. It is run in
       the shell's own form; the trade is that a signal stops at the shell.
    3. A simple command that sets a variable first — `PORT=3000 node app.js` — also
       keeps the shell, because the JSON form would try to execute a binary named
       `PORT=3000`. It is prefixed with `exec`, which is safe here: there is nothing
       after it to run.

    Getting this wrong is quiet. The first form's absence shows up as a container
    that takes the full ten-second `docker stop` grace period and then has its
    database killed underneath it, which is what the warning Docker prints about
    `JSONArgsRecommended` is pointing at.
    """
    if not needs_shell(start):
        try:
            parts = shlex.split(start)
        except ValueError:
            parts = []
        if parts and not any("=" in token for token in parts):
            return "CMD " + json.dumps(parts)

    if needs_shell(start):
        return f"CMD {start}"

    return f"CMD exec {start}"


def dockerfile_for(plan: dict) -> str:
    language = plan["language"]
    port = plan["port"]
    healthcheck = plan["healthcheck"]
    base = BASE_IMAGES[language]

    header = [
        "# syntax=docker/dockerfile:1",
        "# Generated by scripts/package-project.py from plan.json. Not model output.",
        "#",
        f"# language: {language}",
        f"# start:    {plan['start']}",
    ]

    if plan["target"] == "convex":
        # Read before anything else in the file, because it changes where a failure
        # means to be looked for: this container serves a client, and its data is
        # somewhere else.
        header += [
            "#",
            "# target:   convex — the state (schema and functions) is served by the",
            "#           self-hosted Convex at the deployment below, not by this image",
            f"#           deployment: {plan['convex_url']}",
        ]

    if language == "static":
        # nginx is the server, so the port and the healthcheck are configured rather
        # than passed to an application that does not exist.
        conf = (
            "server {"
            f" listen {port};"
            f" listen [::]:{port};"
            " root /usr/share/nginx/html;"
            " index index.html;"
            " location / { try_files $uri $uri/ /index.html; }"
            "}"
        )
        return (
            "\n".join(
                header
                + [
                    f"FROM {base}",
                    f"RUN printf '%s\\n' '{conf}' > /etc/nginx/conf.d/default.conf",
                    "COPY . /usr/share/nginx/html",
                    f"EXPOSE {port}",
                    f"HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=5 \\",
                    f'  CMD wget -q -O /dev/null "http://127.0.0.1:{port}{healthcheck}" || exit 1',
                    "",
                    "# A static site has no process to start, so the plan's start command is",
                    "# not run here — nginx is the server. `plan.json` still carries it, because",
                    "# the plan is the record of what the user confirmed.",
                    'CMD ["nginx", "-g", "daemon off;"]',
                ]
            )
            + "\n"
        )

    # Only where something is installed. A plan with no install resolves no tree, so
    # there is nothing that could need compiling, and a toolchain would be weight the
    # image never uses.
    tools = BUILD_TOOLS.get(language, ()) if plan["install"] else ()

    # Staging exists to keep the toolchain out of the published image, so a language
    # with no toolchain to hide stays in one stage rather than paying for a copy of its
    # own workdir.
    staged = bool(tools) and language in RUNTIME_FROM_BUILD

    # Written into both stages when the build is split: `ENV` does not carry across a
    # `FROM`, and a runtime stage without it would run the app on a port nothing
    # published and write its database inside the image.
    environment = [
        f"ENV PORT={port} \\",
        "    HOST=0.0.0.0 \\",
        "    DATA_DIR=/data" + (" \\" if plan["target"] == "convex" else ""),
    ]

    if plan["target"] == "convex":
        # The deployment, under every name a client might look for it under. Written
        # into the build stage *and* the runtime stage when the build is split, for the
        # same reason PORT is: `ENV` does not carry across a `FROM`, and a client that
        # cannot see this is a client that renders an empty page.
        environment += [
            f"    CONVEX_URL={plan['convex_url']} \\",
            f"    VITE_CONVEX_URL={plan['convex_url']} \\",
            f"    NEXT_PUBLIC_CONVEX_URL={plan['convex_url']}",
        ]

    lines = header + [
        f"FROM {base} AS build" if staged else f"FROM {base}",
        "WORKDIR /app",
        "",
        "# Given to the project rather than assumed by it. PORT and HOST are what make a",
        "# server reachable from outside its container; DATA_DIR is where anything worth",
        "# keeping survives the next rebuild (it is the one mounted volume).",
    ] + environment

    # Ahead of the source on purpose: a change to the project must not invalidate
    # this layer and pay for the toolchain again on every build.
    if tools:
        lines += [
            "",
            "# `install` resolves its tree from the manifest, which may name something",
            "# that has to be built here rather than downloaded prebuilt.",
            f"RUN apk add --no-cache {' '.join(tools)}",
        ]

    lines += ["", "COPY . ."]

    if plan["target"] == "convex":
        # For the plan's own build step, which is where `npx convex deploy` runs. This
        # is the one credential this script may pass, and the shape it is passed in is
        # deliberate: an `ARG` is available to the `RUN` below and never becomes part of
        # the running container, because nothing that runs needs to deploy anything.
        lines += [
            "",
            "# A Convex deploy key, scoped to this deployment, for the plan's build step.",
            "# Declared as an `ARG`, never an `ENV`: an `ENV` would carry it into the",
            "# published image. Docker does record build arguments in the image metadata,",
            "# so this may only ever be a key scoped to this one deployment — the platform's",
            "# admin key is not read by packaging and must not be used here.",
            f"ARG {CONVEX_DEPLOY_KEY}",
        ]

    if plan["install"]:
        lines += ["", "# From the plan, verbatim. A failure here is a packaging failure with the", "# package manager's own message.", f"RUN {plan['install']}"]

    if plan["build"]:
        lines += ["", f"RUN {plan['build']}"]

    if staged:
        source, destination = RUNTIME_FROM_BUILD[language]
        lines += [
            "",
            "# The published image: a fresh copy of the base plus what the build produced,",
            "# so the toolchain stays in the stage that needed it and never reaches a",
            "# running container. Everything above is build-time only.",
            f"FROM {base}",
            "WORKDIR /app",
            "",
            "# Written again, not inherited: `ENV` does not carry across a `FROM`.",
        ] + environment + [
            f"COPY --from=build {source} {destination}",
        ]

    lines += [
        "",
        f"EXPOSE {port}",
        'VOLUME ["/data"]',
        "",
        "# busybox wget: present in every base image here, and it exits non-zero on a",
        "# 4xx or 5xx, which is what makes this a real check rather than a connect test.",
        f"HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \\",
        f'  CMD wget -q -O /dev/null "http://127.0.0.1:{port}{healthcheck}" || exit 1',
        "",
        start_instruction(plan["start"]),
    ]

    return "\n".join(lines) + "\n"


def write_dockerfile(directory: Path, plan: dict) -> Path:
    """Write the Dockerfile and the .dockerignore.

    `plan.json` is written by the runner, but the Dockerfile is this script's, and
    it is written *unconditionally*: a model that emitted one of its own would
    otherwise be built, which hands it the runtime the plan exists to pin.
    """
    target = directory / DOCKERFILE_NAME
    target.write_text(dockerfile_for(plan), encoding="utf-8")
    (directory / DOCKERIGNORE_NAME).write_text(DOCKERIGNORE, encoding="utf-8")
    return target


# --- the image ---------------------------------------------------------------


def convex_build_args(plan: dict) -> list[str]:
    """The deploy key, as a build argument, only when there is one to pass.

    Absent is a normal state: a plan that does not deploy its functions from the
    image build needs no key at all, and a checkout that has never deployed one has
    none to give. Passing nothing is better than passing an empty string, which
    Docker would record as a value the build then fails to use.
    """
    if plan.get("target") != "convex":
        return []

    key = (os.environ.get(CONVEX_DEPLOY_KEY) or "").strip()
    if not key:
        return []

    return ["--build-arg", f"{CONVEX_DEPLOY_KEY}={key}"]


def build_image(directory: Path, slug: str, plan: dict) -> int:
    """Build the project's image, through the same command the runtime builds with.

    Every flag is passed through unchanged: `--tag` is the name `app-runtime.py`
    runs, and the Convex build arg is the one credential this script may pass.
    """
    command = [str(buildx()), "--tag", image_tag(slug), *convex_build_args(plan), "."]
    note(f"$ {' '.join(command)}  (in {directory})")
    return subprocess.call(command, cwd=str(directory))  # noqa: S603 - fixed argv


# --- the archive -------------------------------------------------------------


def project_files(directory: Path) -> list[dict]:
    """Everything that belongs in the archive: the project, not what we made of it."""
    rows: list[dict] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(directory)).replace(os.sep, "/")
        if relative in ARCHIVE_SKIP or relative.split("/", 1)[0] in ARCHIVE_SKIP:
            continue
        rows.append({"path": relative, "bytes": path.stat().st_size})
    return rows


def write_zip(directory: Path, rows: list[dict]) -> Path | None:
    if not rows:
        return None

    target = directory / ARCHIVE_NAME
    temporary = directory / f".{ARCHIVE_NAME}.tmp"

    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            archive.write(directory / row["path"], arcname=row["path"])

    temporary.replace(target)
    return target


def write_manifest(directory: Path, slug: str, plan: dict, rows: list[dict], zip_path: Path | None) -> Path:
    manifest = {
        "v": 1,
        "kind": plan["kind"],
        # Named so the runtime and an operator reading the build do not have to infer
        # where the app's state lives from the files — a Convex-targeted app's data is
        # not in the container at all.
        "target": plan["target"],
        "slug": slug,
        "language": plan["language"],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image": image_tag(slug),
        "port": plan["port"],
        "healthcheck": plan["healthcheck"],
        "start": plan["start"],
        # What `app-runtime.py` starts, and what the log says. Same keys the older
        # manifests used so a status written before this change still parses.
        "dist_files": len(rows),
        "dist_bytes": sum(row["bytes"] for row in rows),
        "source_files": len(rows),
        "files": rows,
        "zip": zip_path.name if zip_path else None,
    }

    target = directory / MANIFEST_NAME
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a planned project into a runnable image.")
    parser.add_argument("slug", help="the build to package (builds/<slug>)")
    parser.add_argument("--image", help="override the image tag")
    parser.add_argument("--no-build", action="store_true", help="write the files and the manifest, do not build the image")
    # Not the same as --no-build, and the difference is worth naming because it is
    # what makes the Dockerfile reviewable before a build: this writes the
    # Dockerfile from the plan and stops, so `--dry-run` then reading the file is
    # how the plan is inspected without spending a build on it.
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write the Dockerfile from the plan, then stop before the image and the manifest",
    )
    args = parser.parse_args(argv)

    if not SLUG_PATTERN.fullmatch(args.slug):
        fail(f"not a usable slug: {args.slug!r}", 2)

    root = repo_root()
    directory = build_dir(root, args.slug)
    plan = plan_for_build(directory)

    note(
        f"packaging {args.slug} as {plan['language']}"
        f"{' on Convex' if plan['target'] == 'convex' else ''}: "
        f"{plan['install'] or '(no install)'} / {plan['build'] or '(no build)'} / {plan['start']}"
    )

    target = write_dockerfile(directory, plan)
    note(f"wrote {target.relative_to(root)}")

    if args.dry_run:
        note(f"would build {image_tag(args.slug)} from {directory}")
        return 0

    if not args.no_build:
        code = build_image(directory, args.slug, plan)
        if code != 0:
            # The output is already on the caller's stdout, and the runner has it in
            # the job log. This is the line that makes the failure findable.
            fail(f"the image for {args.slug} did not build (docker exit {code})", 1)

    rows = project_files(directory)
    zip_path = write_zip(directory, rows)
    manifest_path = write_manifest(directory, args.slug, plan, rows, zip_path)

    note(f"wrote {manifest_path.relative_to(root)} — {len(rows)} file(s), {image_tag(args.slug)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

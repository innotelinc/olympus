#!/usr/bin/env python3
"""Olympus build runner — run `make app` for builds queued by Studio.

WHY THIS IS A SEPARATE PROCESS, and not something Studio does itself. Studio is
the browser surface; `make app` needs the Archon CLI, the Codex CLI, `uv`, a
checkout and several minutes of wall clock. The Studio image deliberately carries
none of that (its final stage is a traced Next.js bundle — no toolchain, no source
tree), so the browser cannot manufacture an app. The runner is the other half of
the handoff: Studio writes a spec and a request into a directory it already
shares, and this process — which runs where the toolchain actually is — picks the
request up and runs the very same command `make app` and CI run
(`scripts/manufacture.sh`).

TRUST IS THE WHOLE DESIGN. The queue directory is writable by the Studio uid, so
everything in it is untrusted input that this process (frequently root) acts on.
Nothing from a request is ever interpolated into a shell: the spec path is
resolved and confirmed to sit inside `build-requests/`, the slug is matched against
a strict pattern, and the build directory is re-derived here rather than believed.
A request that fails any check is refused and marked failed — never "best effort".

Usage:
    scripts/build-runner.py --serve            # the service: process, then wait
    scripts/build-runner.py --once             # drain what is queued, then exit
    scripts/build-runner.py --list             # queue + status, for operators
    scripts/build-runner.py --submit build-requests/app.md [--replace]
    scripts/build-runner.py --check            # validate the environment, no build

Env:
    BUILD_QUEUE_DIR       where requests/status live (default <repo>/.factory/build-queue)
    BUILD_POLL_SECONDS    idle scan interval (default 5)
    BUILD_TIMEOUT_SECONDS per-build wall clock before it is killed (default 1800)
    MANUFACTURE_SCRIPT    override the command's script (default scripts/manufacture.sh)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The plan contract, from the script beside this one. `sys.path` is set here rather
# than relying on how the process was started, because the tests load this file by
# path and the unit runs it from the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_plan  # noqa: E402 - the path insert above is what makes this importable

PROTOCOL_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent

JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
# The same shape `specSlug()` produces in web/studio/lib/factory-spec.ts: a name
# that cannot contain a separator, a dot, or a leading dash. Re-checked here
# rather than trusted, because it becomes a path component on this side.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

REQUEST_SUFFIX = ".request.json"
RUNNING_SUFFIX = ".running.json"
STATUS_SUFFIX = ".status.json"
LOG_SUFFIX = ".log"
# Dropped by Studio (or an operator) to ask for a stop. A marker file rather than
# a signal because the two sides share a directory, not a process table: Studio
# runs in a container and cannot signal a host process. The runner deletes it
# when it acts, so a stale marker cannot cancel the *next* build.
CANCEL_SUFFIX = ".cancel.json"
# How long a build gets to wind down after SIGTERM before SIGKILL.
CANCEL_GRACE_SECONDS = 10
HEARTBEAT_NAME = "runner.heartbeat.json"
LOCK_NAME = "runner.lock"

# DEFAULTS
DEFAULT_POLL_SECONDS = 5
# How long a candidate Archon CLI gets to answer `--version` before the check
# moves on. A binary that hangs is not the one the build should use either.
ARCHON_VERSION_TIMEOUT_SECONDS = 20
DEFAULT_TIMEOUT_SECONDS = 1800
STATUS_EVERY_SECONDS = 5
HEARTBEAT_EVERY_SECONDS = 10
LOG_TAIL_CHARS = 4000

# What the build is allowed to inherit from the repo `.env`. An allow-list, not a
# deny-list: the `.env` holds the Authentik token, the Cerulean admin password,
# the Vault token and the Telegram bot token, and a build has no business seeing
# any of them. `build-app.py` scrubs the agent's own environment further; this is
# the outer layer.
ENV_ALLOW_PREFIXES = ("OMNIROUTE_", "ARCHON_")
ENV_ALLOW_EXACT = (
    # A Convex-targeted build needs its deployment's address, and — only when the
    # plan's own build step deploys the functions — a key scoped to that one
    # deployment. Both by exact name, never by prefix: `CONVEX_` would also carry
    # the self-hosted backend's admin key, which packaging is written never to read.
    "CONVEX_URL",
    "CONVEX_DEPLOY_KEY",
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LANGUAGE",
    "TMPDIR",
    "SHELL",
    "USER",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class RequestError(Exception):
    """A request we refuse to act on. Carries the sentence shown to the operator."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a `.env` the way the rest of the stack does: KEY=value, # comments.

    No expansion and no shell: a value is data, never code.
    """
    parsed: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        separator = line.find("=")
        if separator == -1:
            continue

        key = line[:separator].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        value = line[separator + 1 :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()

        parsed[key] = value

    return parsed


def allowed_env(
    dotenv: dict[str, str], base: dict[str, str], repo: Path | None = None
) -> dict[str, str]:
    """The environment a build runs with: the operator's, plus allow-listed `.env` keys.

    Real environment wins over the file, so an operator can override a value by
    exporting it — the same precedence the rest of the stack uses.

    The allow-list is applied to BOTH sources, and that is the whole point. An
    earlier version let "real env wins" run over every key in the `.env`, which
    meant anything that happened to be in both — `AUTHENTIK_TOKEN` under a shell
    that exports it, say — crossed into the build. A secret does not become safe
    by arriving from the environment instead of the file.
    """
    env = {key: value for key, value in base.items() if key in ENV_ALLOW_EXACT}

    for key, value in dotenv.items():
        if not (key.startswith(ENV_ALLOW_PREFIXES) or key in ENV_ALLOW_EXACT):
            continue
        env[key] = base.get(key, value)

    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("PATH", os.defpath)
    # The workflow's script nodes declare `runtime: uv`, and `uv` is not always on
    # the unit's PATH — without it the first node fails with "uv: command not
    # found", which reads as a workflow bug rather than a unit that needs one more
    # path. The directory is a setting (BUILD_EXTRA_PATH, colon-separated) instead
    # of a hardcoded list because the runner is no longer root: a per-user install
    # like /root/.local/bin is unreadable to the service account, so the installer
    # puts uv somewhere every user can reach and points this at it.
    configured = (os.environ.get("BUILD_EXTRA_PATH") or "").strip()
    extra_paths = [entry for entry in configured.split(":") if entry] or ["/usr/local/bin"]
    for extra in reversed(extra_paths):
        if extra not in env["PATH"].split(":"):
            env["PATH"] = f"{extra}:{env['PATH']}"

    # git refuses to work in a repository owned by another account
    # ("detected dubious ownership"), and that is the normal case here: the
    # checkout belongs to whoever cloned it — root — while builds run as the
    # service account. Archon asks git for the repo root before anything else, so
    # without this the first node dies on a safe.directory message that says
    # nothing about the build. Passed as command-line config so it travels with
    # the process instead of depending on a gitconfig someone has to remember.
    if repo is not None and "GIT_CONFIG_COUNT" not in env:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = str(repo)
    return env


def resolve_spec(repo: Path, spec: object) -> tuple[Path, str]:
    """The build-request a job names, or a refusal.

    Every rejection here is a shape that would otherwise become a path: an
    absolute path, a `..` segment, a symlink out of the directory, a non-markdown
    file. The resolved path must be *inside* `build-requests/`, checked after
    resolution so a symlink cannot walk out.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise RequestError("the request names no spec")

    candidate = Path(spec.strip())
    if candidate.is_absolute():
        raise RequestError(f"spec must be relative to the repository, not absolute: {spec!r}")
    if ".." in candidate.parts:
        raise RequestError(f"spec must not contain '..': {spec!r}")
    if candidate.suffix.lower() != ".md":
        raise RequestError(f"spec must be a markdown file in build-requests/: {spec!r}")

    requests_dir = (repo / "build-requests").resolve()
    target = (repo / candidate).resolve()

    # `in parents` on the resolved path, so a symlink pointing elsewhere is refused.
    if target.parent != requests_dir:
        raise RequestError(f"spec must sit directly in build-requests/, not {spec!r}")
    if not target.is_file():
        raise RequestError(f"spec not found: {spec}")

    return target, candidate.as_posix()


def resolve_slug(payload: dict, spec_rel: str) -> str:
    """The app slug: from the request when present, else the spec's own name.

    Validated either way, and confirmed to agree with the spec so a request
    cannot point one slug at another's file.
    """
    raw = payload.get("slug")
    if raw is None:
        # Absent means "derive it from the spec", which is what the CLI does.
        slug = Path(spec_rel).stem
    elif isinstance(raw, str):
        # Present means it is judged on its own terms: an empty or padded value is
        # malformed input, not a request to fall back. Guessing here would be a
        # silent coercion of something a caller explicitly sent.
        slug = raw.strip()
    else:
        raise RequestError(f"slug must be a string, got {type(raw).__name__}")

    if not SLUG_PATTERN.fullmatch(slug):
        raise RequestError(f"unsafe app slug: {raw!r}")

    # With no spec there is nothing to cross-check the slug against; the slug is
    # then simply the name of the app directory.
    if spec_rel and slug != Path(spec_rel).stem:
        raise RequestError(
            f"slug {slug!r} does not match the spec name {Path(spec_rel).stem!r}"
        )
    return slug


# The Archon CLI, by the names and locations a deployment puts it in. Ordered to
# match `scripts/manufacture.sh`, which is the code that actually runs it: an
# `ARCHON_BIN` an operator exported, the `ARCHON_BINARY` this stack's `.env` and
# `setup.sh` write, the in-checkout build, then PATH.
ARCHON_CANDIDATES = (
    ("ARCHON_BIN", "env"),
    ("ARCHON_BINARY", "env"),
    ("core-modules/archon/bin/archon", "repo"),
    ("archon", "path"),
)


def archon_candidates(repo: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    """Every place the Archon CLI could be, as (path, source) pairs, in order.

    The check used to ask `shutil.which("archon")` and nothing else, and that is
    the wrong question on every host this stack installs itself on: the CLI lives
    in the checkout (`core-modules/archon/bin/archon`, where `setup.sh` looks and
    where `.archon/config.yaml` points), and PATH is the *last* resort rather than
    the only one. Reporting MISSING for a deployment whose builds work is worse
    than not checking: it sends the operator to install something they already have.

    A relative value is resolved against the repo, because `.env` carries the
    repo-relative form and the runner is started with the repo as its working
    directory, not a subdirectory of it.
    """
    found: list[tuple[str, str]] = []

    for value, kind in ARCHON_CANDIDATES:
        if kind == "path":
            located = shutil.which(value, path=env.get("PATH"))
            if located:
                found.append((located, "on PATH"))
            continue

        if kind == "env":
            configured = (env.get(value) or "").strip()
            if not configured:
                continue
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = repo / candidate
            found.append((str(candidate), value))
            continue

        found.append((str(repo / value), "in this checkout"))

    return found


def resolve_archon(repo: Path, env: dict[str, str]) -> tuple[str | None, str, list[str]]:
    """The first candidate that runs, its source, and everything that was tried.

    "Is present" is not the claim being made here — an unbuilt checkout leaves a
    file that cannot answer. Each candidate has to execute and answer `--version`,
    which is the same test `manufacture.sh` applies before it picks one.
    """
    tried: list[str] = []
    for candidate, source in archon_candidates(repo, env):
        tried.append(candidate)
        if not os.access(candidate, os.X_OK):
            continue
        try:
            result = subprocess.run(  # noqa: S603 - a fixed argv, no shell
                [candidate, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=ARCHON_VERSION_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate, source, tried

    return None, "", tried


def resolve_build_dir(repo: Path, slug: str) -> Path:
    """`builds/<slug>` — re-derived here, then confirmed to stay inside `builds/`."""
    builds = (repo / "builds").resolve()
    target = (builds / slug).resolve()
    if target.parent != builds:
        raise RequestError(f"refusing a build directory outside builds/: {target}")
    return target


def validate_request(repo: Path, payload: object) -> dict:
    """Turn a request file into a job, or raise. The only place a request is read."""
    if not isinstance(payload, dict):
        raise RequestError("request is not a JSON object")
    if payload.get("v") != PROTOCOL_VERSION:
        raise RequestError(f"unsupported protocol version: {payload.get('v')!r}")

    job = payload.get("job")
    if not isinstance(job, str) or not JOB_ID_PATTERN.fullmatch(job):
        raise RequestError(f"malformed job id: {job!r}")

    # What is being built. An unknown value is an app rather than a refusal: every
    # request written before the split means an app, and a queue that refuses the
    # requests already in it would strand a build the operator asked for.
    kind = payload.get("kind")
    if kind not in (None, "app", "website"):
        raise RequestError(f"unknown kind: {kind!r} (expected 'app' or 'website')")

    # The plan the operator confirmed, re-checked here because this file has been to
    # the browser and back. Absent is allowed and means something specific: a
    # project saved before the planner existed, which its own packager still builds.
    plan = parse_plan(payload.get("plan"))

    action = payload.get("action")
    if action not in (None, "build", "publish", "preview"):
        raise RequestError(
            f"unknown action: {action!r} (expected 'build', 'publish' or 'preview')"
        )
    action = action or "build"

    # A preview runs the project in its own container and stops there: no DNS
    # record, no proxy host, nothing announced. That needs something to *run*, and
    # only two things are runnable — a planned project (its Dockerfile comes from
    # the plan) and an app the older packager still builds. A website with no plan
    # is a directory of static files with no process in it, and its only way to be
    # seen is to be published, so that is what the operator is told.
    if action == "preview" and plan is None and (kind or "app") != "app":
        raise RequestError(
            "a preview runs the project in its own container, and this website "
            "predates the planner — publish it to see it"
        )

    # A publish and a preview have no spec, and that is not a shorthand: a spec
    # exists to tell the factory what to manufacture, and neither of these
    # manufactures anything. Writing one anyway would leave factory input behind
    # that a bare `make app` would later build — a delivery with a build as a side
    # effect.
    if action in ("publish", "preview"):
        spec_target: Path | None = None
        spec_rel = ""
        slug = resolve_slug(payload, "")
    else:
        spec_target, spec_rel = resolve_spec(repo, payload.get("spec"))
        slug = resolve_slug(payload, spec_rel)

    title = payload.get("title")

    # `publish` and `preview` are the other actions this queue carries, and they
    # are different jobs rather than flags on the build job: neither runs the
    # factory at all. They take the files Studio has on screen, write them into the
    # app directory and package *those* — which is what "Publish It" and "Preview
    # It" mean. Running a factory build first would deliver whatever Codex
    # produced, which is a different app from the one the operator is looking at.
    files = []
    if action in ("publish", "preview"):
        files = parse_files(payload.get("files"))

    return {
        "v": PROTOCOL_VERSION,
        "job": job,
        "spec": spec_rel,
        "spec_path": spec_target,
        "slug": slug,
        # Display only — never a path, never a command.
        "title": title.strip()[:120] if isinstance(title, str) and title.strip() else slug,
        "requested_by": str(payload.get("requested_by") or "")[:128],
        "requested_at": str(payload.get("requested_at") or now_iso())[:64],
        # Removing a previous build is a real deletion, so it happens only when
        # the request says so explicitly (the UI asks first).
        "replace": payload.get("replace") is True,
        "kind": kind or "app",
        "action": action,
        "plan": plan,
        "files": files,
        # Whether to stage `dist/` for the host to serve once it is built. A
        # website is packaged either way — a site that is not packaged is not a
        # site — but publishing is a separate, visible decision.
        "publish": payload.get("publish") is True,
    }


def parse_plan(value: object) -> dict | None:
    """The plan out of a request, or None when there is not one.

    Checked rather than trusted, and checked *here* rather than in the packager,
    because this is the boundary: the plan was written by Studio from a model's
    reply, sent to a browser, confirmed by a person and sent back. Every field that
    reaches a command or a port is re-read, and a plan that fails is refused before
    a job is claimed rather than half-way through a publish.

    The commands are deliberately only bounded by length and line count. A build
    runs inside a container with no host mounts and no inherited secrets, and the
    whole point of the plan is that the commands are the user's choice — so the
    containment is the container, not an allow-list that would refuse `make`.
    """
    if value is None:
        return None
    try:
        # The rules themselves live in `project_plan`, because three readers need
        # them: this boundary, the packager and runtime that read the plan off disk,
        # and the greenfield workflow that plans a spec before building it. Two
        # copies of "what is a valid plan" drift where drift is most expensive — a
        # plan one side accepts and the other refuses.
        #
        # `coerce` is off: this plan has been to the browser and back, where the
        # parser already typed every field, so a quoted port here means the contract
        # was broken rather than that a model was being loose.
        return project_plan.normalize_plan(value)
    except project_plan.PlanError as error:
        raise RequestError(f"plan: {error}") from error


def read_plan(build_dir: Path) -> dict | None:
    """The plan already in a build directory, validated, or None when there is none.

    A build does not have to have been given a plan to have one: `make app` and the
    app-builder CI job run the greenfield workflow, whose first node plans the spec
    and writes `plan.json` into the directory it is about to build. The plan is the
    same contract either way, so what the build is packaged with is decided by what
    is on disk rather than by which path started it.
    """
    plan = project_plan.read_plan_file(build_dir)
    if plan is None and (build_dir / project_plan.PLAN_NAME).is_file():
        # Present but unreadable: said out loud, because the alternative is a report
        # that reads like a project nobody planned.
        log(f"{build_dir}/{project_plan.PLAN_NAME} is not a usable plan — packaging it the old way")
    return plan


def write_plan(build_dir: Path, plan: dict | None) -> None:
    """Put the plan where the packager and the runtime read it.

    Written after `materialize`, which deletes everything it was not given: a plan
    written first would be swept away, and the packager would report a project with
    no plan as if the operator had never confirmed one.
    """
    if not plan:
        return
    build_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(build_dir / project_plan.PLAN_NAME, plan)


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write beside the target and rename, so a reader never sees a half file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    # Studio reads these as uid 1001; the runner is usually root.
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


# A path Studio may hand over for materialisation. Strict for the same reason the
# stored ones are: this becomes a path under `builds/`, written as the runner's
# user, from a payload that arrived as a file from a browser-facing service.
MAX_FILES = 60
MAX_FILE_CHARS = 400_000
MAX_PATH_CHARS = 200


def safe_file_path(value: object) -> str:
    """A relative, traversal-free path, or `""` for anything else."""
    raw = value.strip().replace("\\", "/") if isinstance(value, str) else ""
    if not raw or len(raw) > MAX_PATH_CHARS:
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return ""
    segments = [segment for segment in raw.split("/") if segment and segment != "."]
    if not segments or any(segment == ".." for segment in segments):
        return ""
    return "/".join(segments)


def parse_files(value: object) -> list[dict]:
    """Validate a `files` payload, or refuse it. Never silently drop entries.

    Dropping a file whose path did not validate would publish a site missing part
    of itself and report success. A refusal is loud and the operator re-sends.
    """
    if not isinstance(value, list) or not value:
        raise RequestError("a publish request must carry the files to publish")
    if len(value) > MAX_FILES:
        raise RequestError(f"too many files to publish: {len(value)} (limit {MAX_FILES})")

    files: list[dict] = []
    seen: set[str] = set()
    total = 0

    for entry in value:
        if not isinstance(entry, dict):
            raise RequestError("every file must be an object with path and contents")
        path = safe_file_path(entry.get("path"))
        if not path:
            raise RequestError(f"unsafe file path: {entry.get('path')!r}")
        if path in seen:
            raise RequestError(f"duplicate file path: {path!r}")
        contents = entry.get("contents")
        if not isinstance(contents, str):
            raise RequestError(f"{path!r} has no contents")
        if len(contents) > MAX_FILE_CHARS:
            raise RequestError(f"{path!r} is too large ({len(contents)} chars, limit {MAX_FILE_CHARS})")
        seen.add(path)
        total += len(contents)
        files.append({"path": path, "contents": contents})

    return files


# Kept across a materialise because they are caches and to delete them is to make
# every publish re-download the dependency tree. `package-lock.json` is also what
# makes a rebuild deterministic, so it is not merely an optimisation.
KEEP_ON_MATERIALIZE = (
    "node_modules/",
    "package-lock.json",
)


def materialize(build_dir: Path, files: list[dict]) -> None:
    """Make the app directory exactly the files given, plus the install cache.

    The payload is the whole truth, so anything else goes. Deleting only the files
    being rewritten is not enough and was the first thing this got wrong: a file the
    operator removed in Studio survived on disk and was published anyway — invisible
    for `src/`, which Vite bundles from imports, but not for `public/` assets, which
    are copied wholesale, or for the archive, which would carry dead files.

    The packager's own outputs (`dist/`, the scaffold, `site.zip`) are deleted here
    and regenerated by the packaging step, so a stale `dist/` can never be served
    against source that has changed.
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    for existing in sorted(build_dir.rglob("*"), reverse=True):
        if not existing.is_file():
            continue
        relative = str(existing.relative_to(build_dir)).replace(os.sep, "/")
        if relative.startswith(KEEP_ON_MATERIALIZE) or relative in KEEP_ON_MATERIALIZE:
            continue
        existing.unlink()

    for entry in files:
        target = build_dir / str(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(entry["contents"]), encoding="utf-8")

    # Empty directories left behind by the sweep would otherwise accumulate, and a
    # `src/` with no files in it reads as a broken build rather than a clean one.
    for existing in sorted(build_dir.rglob("*"), reverse=True):
        if existing.is_dir() and not any(existing.iterdir()):
            existing.rmdir()


def read_site_manifest(build_dir: Path) -> dict | None:
    """The packaging summary `package-website.py` wrote, for a website build.

    Separate from `read_manifest` on purpose: `MANIFEST.json` says the *agent*
    produced files, and this says the *toolchain* turned them into something
    servable. A site that was generated but not packaged is a real and distinct
    state, and collapsing the two would report it as success.
    """
    manifest = read_json(build_dir / "site.manifest.json")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("kind") != "website":
        return None

    return {
        "entry": manifest.get("entry"),
        "dist_files": manifest.get("dist_files"),
        "dist_bytes": manifest.get("dist_bytes"),
        "source_files": manifest.get("source_files"),
        "zip": manifest.get("zip"),
        "built_at": manifest.get("built_at"),
    }


def read_app_manifest(build_dir: Path) -> dict | None:
    """The packaging summary `package-app.py` wrote, for a full-stack app build.

    Read separately from the website's for the same reason those two are separate
    from the agent's `MANIFEST.json`: "the model wrote files" and "there is a client
    bundle and a server to run" are different claims, and only the second one means
    the app can be started.
    """
    manifest = read_json(build_dir / "app.manifest.json")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("kind") != "app":
        return None

    return {
        "entry": manifest.get("entry"),
        "dist_files": manifest.get("dist_files"),
        "dist_bytes": manifest.get("dist_bytes"),
        "source_files": manifest.get("source_files"),
        "image": manifest.get("image"),
        "zip": manifest.get("zip"),
        "built_at": manifest.get("built_at"),
    }


def read_project_manifest(build_dir: Path) -> dict | None:
    """The summary `package-project.py` wrote, for a project built from a plan.

    Preferred over the two older manifests when it is there, because a planned
    project replaced them: the language and the port are what the status reports,
    and reading the wrong file would say "React" about a Flask app.
    """
    manifest = read_json(build_dir / "project.manifest.json")
    if not isinstance(manifest, dict):
        return None

    return {
        "entry": None,
        "dist_files": manifest.get("dist_files"),
        "dist_bytes": manifest.get("dist_bytes"),
        "source_files": manifest.get("source_files"),
        "image": manifest.get("image"),
        "language": manifest.get("language"),
        "port": manifest.get("port"),
        "zip": manifest.get("zip"),
        "built_at": manifest.get("built_at"),
    }


def read_packaged(build_dir: Path, kind: str) -> dict | None:
    """Whatever the packager left behind, or None.

    The plan-driven manifest first, then the per-kind ones: a project packaged
    before the planner existed has no `project.manifest.json`, and reporting it as
    un-packaged would take away a working publish for the sake of a rename.
    """
    return read_project_manifest(build_dir) or (
        read_app_manifest(build_dir) if kind == "app" else read_site_manifest(build_dir)
    )


def image_tag_for(slug: str) -> str:
    """An app's runtime image name. The same derivation `package-app.py` uses, so
    the status and the container cannot disagree about what was published."""
    return f"olympus-app-{slug}:latest"


def read_app_runtime(slug: str) -> dict | None:
    """What `app-runtime.py` recorded for an app, for reporting after a failure.

    Read, never written, and read from the same place the runtime writes it, so a
    publish that fails at the edge can say whether the container is up — which is
    the difference between "retry the name" and "run the whole thing again".
    """
    root = os.environ.get("OLYMPUS_APPS_ROOT", "").strip() or None
    if root is None:
        # The runner is not the runtime; ask the same `.env` it asks.
        for candidate in (Path(__file__).resolve().parent.parent / ".env",):
            try:
                root = parse_env_file(candidate.read_text(encoding="utf-8")).get(
                    "OLYMPUS_APPS_ROOT"
                )
            except OSError:
                root = None
            if root:
                break
    if not root:
        return None

    record = read_json(Path(root) / "runtime" / f"{slug}.json")
    return record if isinstance(record, dict) else None


def read_manifest(build_dir: Path) -> dict | None:
    """The artifact summary `record` wrote, for the status the UI displays."""
    manifest = read_json(build_dir / "MANIFEST.json")
    if not isinstance(manifest, dict):
        return None

    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        return None

    return {
        "dir": str(artifact.get("dir") or build_dir),
        "files": artifact.get("files"),
        "bytes": artifact.get("bytes"),
        "entry": artifact.get("entry"),
    }


def log_tail(log_path: Path, limit: int = LOG_TAIL_CHARS) -> str:
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


class Runner:
    """Owns the queue: claim a request, run it, record what happened."""

    def __init__(
        self,
        repo: Path,
        queue_dir: Path,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        manufacture: Path | None = None,
    ) -> None:
        self.repo = repo
        self.queue = queue_dir
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.manufacture = manufacture or (repo / "scripts" / "manufacture.sh")
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.dotenv = self._load_dotenv()

    # ---- environment ----------------------------------------------------

    def _load_dotenv(self) -> dict[str, str]:
        for name in (".env", ".env.local"):
            path = self.repo / name
            if path.is_file():
                try:
                    return parse_env_file(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
        return {}

    def build_env(self) -> dict[str, str]:
        return allowed_env(self.dotenv, dict(os.environ), self.repo)

    def delivery_env(self) -> dict[str, str]:
        """Environment for packaging/runtime/edge delivery commands, not the agent.

        The model receives only the build allow-list. Delivery commands are different:
        `studio-sites.py` needs the scoped Cerulean service key and the runtime needs
        `SITE_*`/`OLYMPUS_*` settings. Using `build_env()` here silently removed the
        service key, so previews reached `studio-sites.py` with only the template
        break-glass password and stopped after the container was already running.
        """
        env = dict(os.environ)
        for key, value in self.dotenv.items():
            if key.startswith(("CERULEAN_", "SITE_", "OLYMPUS_", "APP_")):
                # Delivery must use the same checked-out deployment configuration
                # that the runner was started for. A stale service environment can
                # retain the template Cerulean password and mask a real service key
                # in .env, causing a build to package successfully and fail only at
                # preview registration.
                env[key] = value
        env.setdefault("PATH", os.defpath)
        env.setdefault("HOME", os.path.expanduser("~"))
        return env

    # ---- queue plumbing -------------------------------------------------

    def requests(self) -> list[Path]:
        try:
            found = [
                path
                for path in self.queue.iterdir()
                if path.name.endswith(REQUEST_SUFFIX) and not path.name.startswith(".")
            ]
        except OSError:
            return []
        # Oldest first, and stable: a filename sort is enough because job ids are
        # random, and the queue holds one or two entries in practice.
        return sorted(found)

    def claim(self, request: Path) -> Path | None:
        """Move the request to its running name. Atomic, so one runner wins.

        The rename is what makes the queue safe to scan repeatedly and safe to run
        twice: whichever process renames the file owns the job.
        """
        running = request.with_name(request.name[: -len(REQUEST_SUFFIX)] + RUNNING_SUFFIX)
        try:
            os.replace(request, running)
        except OSError:
            return None
        return running

    def status_path(self, job: str) -> Path:
        return self.queue / f"{job}{STATUS_SUFFIX}"

    def log_path(self, job: str) -> Path:
        return self.queue / f"{job}{LOG_SUFFIX}"

    def write_status(self, job: str, **fields: object) -> None:
        payload = {"v": PROTOCOL_VERSION, "job": job, "updated_at": now_iso()}
        payload.update(fields)
        # Both delivery addresses are always present, `null` when the job did not
        # produce one. A reader then never has to tell "no address" from "the writer
        # forgot the key", and the UI's preview picks between two fields rather than
        # between two fields and their absence.
        for name in ("published_url", "preview_url"):
            payload.setdefault(name, None)
        try:
            write_json_atomic(self.status_path(job), payload)
        except OSError as error:
            log(f"could not write status for {job}: {error}")

    def heartbeat(self, started_at: str, busy: str | None = None) -> None:
        try:
            write_json_atomic(
                self.queue / HEARTBEAT_NAME,
                {
                    "v": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "repo": str(self.repo),
                    "started_at": started_at,
                    "beat_at": now_iso(),
                    "busy_with": busy,
                },
            )
        except OSError:
            pass

    # ---- running one job ------------------------------------------------

    def process(self, request: Path) -> None:
        running = self.claim(request)
        if running is None:
            return

        payload = read_json(running)
        job = running.name[: -len(RUNNING_SUFFIX)]

        try:
            spec = validate_request(self.repo, payload)
        except RequestError as error:
            log(f"refused {job}: {error}")
            self.write_status(
                job,
                state="failed",
                slug=None,
                message=f"Refused: {error}",
                exit_code=None,
            )
            running.unlink(missing_ok=True)
            return

        slug = str(spec["slug"])
        started_at = now_iso()
        log(f"building {slug} from {spec['spec']} (job {job})")
        self.write_status(
            job,
            **self.job_fields(spec),
            state="running",
            started_at=started_at,
            message="starting the build",
            exit_code=None,
            artifact=None,
            site=None,
            log_tail="",
        )

        # A publish and a preview are different jobs, not variations on a build:
        # they take the files they were given, package them and run them. Neither
        # runs the factory, and neither writes a spec. The difference between them is
        # one step — the name — and it is reported as what it is rather than as a
        # build that did not happen.
        action = spec.get("action")
        if action in ("publish", "preview"):
            site = None
            published_url = None
            preview_url = None
            if action == "preview":
                code, detail, site, _unused, preview_url = self.run_delivery(job, spec)
            else:
                code, detail, site, published_url, _unused = self.run_delivery(job, spec)
            succeeded = code == 0
            self.write_status(
                job,
                **self.job_fields(spec),
                state="succeeded" if succeeded else "failed",
                started_at=started_at,
                finished_at=now_iso(),
                exit_code=code,
                message=detail,
                artifact=None,
                site=site,
                published_url=published_url,
                preview_url=preview_url,
                log_tail=log_tail(self.log_path(job)),
            )
            done = "previewed" if action == "preview" else "published"
            log(f"{done if succeeded else action + ' failed'} {slug}: {detail}")
            running.unlink(missing_ok=True)
            return

        exit_code, detail, state_override = self.run_build(job, spec, started_at)

        # Both kinds have one more step, and it runs only when the agent produced
        # something — packaging an empty directory would fail with a compiler error
        # about a missing src/App.tsx, which says nothing about the real problem.
        package_code = 0
        planned = isinstance(spec.get("plan"), dict)
        if state_override is None and exit_code == 0 and (planned or spec.get("kind") in self.PACKAGERS):
            # Only when the agent actually produced something. Packaging a directory
            # the agent never wrote fails on a missing src/App.tsx, and that message
            # says nothing about the real problem — which is that nothing was
            # manufactured. `read_manifest` is the agent's own account of its output.
            if read_manifest(resolve_build_dir(self.repo, slug)) is not None:
                package_code, package_detail = self.run_package(job, spec)
                if package_code != 0:
                    detail = package_detail

        # A cancelled build is reported as cancelled, not as a failure with an
        # inscrutable exit code — and a half-written app directory is removed, so
        # the next attempt is not blocked by the clobber guard on a directory the
        # operator never meant to keep.
        if state_override == "cancelled":
            cancelled_dir = resolve_build_dir(self.repo, slug)
            if cancelled_dir.is_dir() and read_manifest(cancelled_dir) is None:
                shutil.rmtree(cancelled_dir, ignore_errors=True)
                detail = f"{detail} The partial build was removed."
            log(f"cancelled {slug}: {detail}")
            self.write_status(
                job,
                **self.job_fields(spec),
                state="cancelled",
                started_at=started_at,
                finished_at=now_iso(),
                exit_code=exit_code,
                message=detail,
                artifact=None,
                site=None,
                log_tail=log_tail(self.log_path(job)),
            )
            self.cancel_path(job).unlink(missing_ok=True)
            running.unlink(missing_ok=True)
            return

        build_dir = resolve_build_dir(self.repo, slug)
        kind = spec.get("kind") if spec.get("kind") in self.PACKAGERS else "app"
        artifact = read_manifest(build_dir)
        site = read_packaged(build_dir, kind) if kind in self.PACKAGERS else None

        # For a packaged kind, a manifest is necessary and not sufficient: the files
        # exist but the thing does not run until it packages, and reporting it as
        # built would hand the operator a directory to deploy that deploys nothing.
        succeeded = exit_code == 0 and package_code == 0 and artifact is not None
        if succeeded and (planned or kind in self.PACKAGERS):
            succeeded = site is not None

        if succeeded:
            message = (
                f"Built builds/{slug} — {artifact.get('files')} file(s), "
                f"{artifact.get('bytes')} bytes, entry {artifact.get('entry')}."
            )
            if site and site.get("image"):
                # A planned project ends as an image, app or website alike. The
                # language is what the operator chose and the plan is what carries it.
                language = site.get("language") or kind
                message += (
                    f" Packaged as {language}: {site.get('dist_files')} file(s), "
                    f"{site.get('dist_bytes')} bytes. Publish It to run it as "
                    f"{site.get('image')}."
                )
            elif site:
                message += (
                    f" Packaged: {site.get('dist_files')} dist file(s), "
                    f"{site.get('dist_bytes')} bytes, served at {site.get('entry')}."
                )
        elif exit_code == 0 and package_code != 0:
            message = detail or "The project did not build. See the build log."
        elif exit_code == 0 and artifact is None:
            message = (
                "The build command reported success but wrote no manifest — "
                "nothing was manufactured. See the build log."
            )
        else:
            message = detail or f"The build failed (exit {exit_code}). See the build log."

        log(f"{'built' if succeeded else 'failed'} {slug}: {message}")
        self.write_status(
            job,
            **self.job_fields(spec),
            state="succeeded" if succeeded else "failed",
            started_at=started_at,
            finished_at=now_iso(),
            exit_code=exit_code,
            message=message,
            artifact=artifact,
            site=site,
            published_url=None,
            log_tail=log_tail(self.log_path(job)),
        )
        running.unlink(missing_ok=True)

    @staticmethod
    def job_fields(spec: dict) -> dict:
        """The identity fields every status carries, so a reader never has to merge."""
        plan = spec.get("plan") if isinstance(spec.get("plan"), dict) else None
        runtime = plan.get("runtime") if plan and isinstance(plan.get("runtime"), dict) else {}
        return {
            "slug": spec["slug"],
            "title": spec["title"],
            "spec": spec["spec"],
            "kind": spec.get("kind", "app"),
            # The language the project was planned in, when it has a plan. Reported on
            # every status of the job so a running build can say what it is building.
            "language": (str(runtime.get("language"))[:40] if runtime.get("language") else None),
            "requested_at": spec["requested_at"],
            "requested_by": spec["requested_by"],
            # What kind of job this is, so a panel can say what a running job is doing
            # rather than only that it is running. A build, a publish and a preview
            # all take minutes, and "Building…" over a preview is the wrong word.
            "action": spec.get("action") or "build",
        }

    def cancel_path(self, job: str) -> Path:
        return self.queue / f"{job}{CANCEL_SUFFIX}"

    def cancel_requested(self, job: str) -> bool:
        return self.cancel_path(job).exists()

    def _stop_tree(self, child: subprocess.Popen, graceful: bool) -> None:
        """Stop the build and everything it started.

        A build is a tree — manufacture.sh → archon → codex — and signalling only
        the shell we spawned leaves the agent running, holding the model gateway
        and still writing files into the app directory after the operator was told
        the build had stopped. The child is started in its own session so the
        whole group can be signalled at once.
        """
        if child.poll() is not None:
            return
        try:
            group = os.getpgid(child.pid)
        except OSError:
            group = None

        def signal_tree(sig: int) -> None:
            if group is None:
                child.send_signal(sig)
                return
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError):
                child.send_signal(sig)

        signal_tree(signal.SIGTERM if graceful else signal.SIGKILL)
        if not graceful:
            child.wait()
            return
        try:
            child.wait(timeout=CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            signal_tree(signal.SIGKILL)
            child.wait()

    def run_build(self, job: str, spec: dict, started_at: str) -> tuple[int, str, str | None]:
        """Run the build, streaming to the job log.

        Returns (exit_code, detail, state_override). The override is how a cancel
        is recorded as `cancelled` rather than as a mysterious failure: the exit
        code of a killed process cannot say why it died.

        The child writes straight to the log file, so the parent can report a live
        tail while it runs — which is the difference between a Studio panel that
        moves and one that says "running" for half an hour.
        """
        slug = str(spec["slug"])
        build_dir = resolve_build_dir(self.repo, slug)

        if spec["replace"] and build_dir.is_dir():
            # Explicitly requested by the request (the UI confirms first). The
            # workflow refuses to overwrite an existing app on purpose, so a
            # rebuild has to say so rather than appear to work and do nothing.
            shutil.rmtree(build_dir, ignore_errors=True)
            log(f"removed the previous build at {build_dir}")

        command = ["bash", str(self.manufacture), str(spec["spec"])]
        started = time.monotonic()

        # A marker left over from a previous job with this id would cancel this
        # run before it started. Claiming the job means this id is ours now.
        self.cancel_path(job).unlink(missing_ok=True)

        try:
            with self.log_path(job).open("wb") as sink:
                self.child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    command,
                    cwd=str(self.repo),
                    env=self.build_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    # Own session, so the whole build tree can be signalled.
                    start_new_session=True,
                )
                # `started_at` was written before the child existed; keep it.
                last_status = time.monotonic()
                while self.child.poll() is None:
                    if self.stopping or self.cancel_requested(job):
                        why = "a stop was requested" if self.stopping else "cancelled from Studio"
                        self._stop_tree(self.child, graceful=True)
                        self.child = None
                        self.cancel_path(job).unlink(missing_ok=True)
                        log(f"{slug}: {why} (job {job})")
                        return 130, f"The build was stopped — {why}.", "cancelled"

                    elapsed = time.monotonic() - started
                    if elapsed > self.timeout_seconds:
                        self._stop_tree(self.child, graceful=False)
                        self.child = None
                        return (
                            124,
                            f"The build exceeded {self.timeout_seconds}s and was killed.",
                            None,
                        )

                    if time.monotonic() - last_status >= STATUS_EVERY_SECONDS:
                        last_status = time.monotonic()
                        self.write_status(
                            job,
                            **self.job_fields(spec),
                            state="running",
                            started_at=started_at,
                            # A live tail, so the Studio panel moves during a build
                            # rather than saying "running" for half an hour.
                            message=f"building — {int(elapsed)}s elapsed",
                            exit_code=None,
                            artifact=None,
                            site=None,
                            log_tail=log_tail(self.log_path(job)),
                        )
                        self.heartbeat(self.started_at, busy=slug)

                    time.sleep(1)

                exit_code = self.child.wait() if self.child else 130
        except OSError as error:
            return 1, f"Could not start the build command: {error}", None
        finally:
            self.child = None

        return exit_code, "", None

    def site_suffix(self) -> str:
        """`SITE_HOST_SUFFIX` from the repo `.env` — the domain a site answers under.

        Read here only to *report* the published URL in the status. The publisher
        reads it itself, so the two cannot disagree about where the site went; if
        this is empty the URL is simply omitted rather than invented.
        """
        return str(self._load_dotenv().get("SITE_HOST_SUFFIX") or "").strip().strip('"').strip("'")

    def run_delivery(
        self, job: str, spec: dict
    ) -> tuple[int, str, dict | None, str | None, str | None]:
        """Run — and, for a publish, name — what Studio has on screen, with no factory.

        Two jobs share this body because their first two steps are identical and the
        difference is the third:

          publish  packages the files, runs the container, then registers the name at
                   the edge. The project is public.
          preview  packages the files and runs the container, and stops. No DNS
                   record, no proxy host, nothing announced. What it produces is a
                   *running* project to put in the frame, which is what a preview is
                   for: looking at the build without committing a name to it.

        Neither runs the factory. A build runs the model: `make app`, Archon, Codex,
        then packaging. These take the files the operator is *looking at*, write them
        into the app directory and package those. Running the factory first would
        deliver whatever the model produced instead, which is a different project from
        the one on screen.

        Returns `(exit_code, detail, site, published_url, preview_url)`.
        """
        slug = str(spec["slug"])
        kind = spec.get("kind") if spec.get("kind") in self.PACKAGERS else "app"
        plan = spec.get("plan") if isinstance(spec.get("plan"), dict) else None
        files = spec.get("files") or []
        build_dir = resolve_build_dir(self.repo, slug)

        preview = spec.get("action") == "preview"
        verb = "previewing" if preview else "publishing"
        steps = (
            self.preview_steps(slug, kind, plan)
            if preview
            else self.publish_steps(slug, kind, plan)
        )
        what = plan.get("runtime", {}).get("language") if plan else kind

        log(f"{verb} {slug} from {len(files)} file(s) as {what}")
        with self.log_path(job).open("ab") as sink:
            sink.write(f"\n=== {verb} {slug} ({what}) ===\n".encode())
            sink.write(f"materialising {len(files)} file(s) into builds/{slug}\n".encode())
            sink.flush()

            try:
                materialize(build_dir, files)
                # After the sweep, never before: materialize removes everything it was
                # not given, so a plan written first would not survive it.
                write_plan(build_dir, plan)
            except OSError as error:
                return 1, f"Could not write the files into builds/{slug}: {error}", None, None, None

            for command in steps:
                sink.write(f"\n$ {' '.join(command)}\n".encode())
                sink.flush()
                code = subprocess.call(  # noqa: S603 - fixed argv, no shell
                    command,
                    cwd=str(self.repo),
                    env=self.delivery_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                )
                if code != 0:
                    detail = (
                        f"{verb.capitalize()} stopped at `{Path(command[1]).name}` "
                        f"(exit {code}). The files are in builds/" + slug
                        + " — see the log for what it said."
                    )
                    # An app that reached its runtime step is *running* even when the
                    # last step failed, and the last step is the name. Saying only
                    # "failed" would read as "nothing happened", and the retry is a
                    # different command from the one that produced the container.
                    if Path(command[1]).name == "studio-sites.py":
                        record = read_app_runtime(slug)
                        if record:
                            retry = (
                                f"run Preview It again — no rebuild needed."
                                if preview
                                else f"`make site-publish SLUG={slug}` — no rebuild needed."
                            )
                            detail += (
                                f" The application itself is running on 127.0.0.1:{record.get('port')}; "
                                f"only the edge is missing. Retry with {retry}"
                            )
                    return code, detail, None, None, None

        site = read_packaged(build_dir, kind)
        suffix = self.site_suffix()
        url = f"https://{slug}.{suffix}" if suffix else None

        if preview:
            # The address comes from the runtime's own record, never from a string
            # composed here: what the frame will hold has to be where the project was
            # actually started. `app-runtime.py --up` writes that record after the
            # healthcheck answers, so a preview with no URL is a project that is not
            # running — and framing an address nothing answers is the blank pane this
            # whole path exists to replace.
            record = read_app_runtime(slug)
            running_url = (
                str(record.get("preview_url"))
                if record and record.get("preview_url")
                else None
            )
            if running_url is None:
                return (
                    1,
                    f"{slug} packaged but the runtime recorded no preview address — "
                    "nothing to frame. See the log.",
                    None,
                    None,
                    None,
                )
            detail = (
                f"Previewing {slug} — running on {running_url}. Nothing was published: "
                f"{slug}'s own name was not registered at the edge, and no DNS record was "
                "made."
            )
            if site and site.get("image"):
                detail += f" Running as {site.get('image')}."
            return 0, detail, site, None, running_url

        detail = f"Published {slug}"
        if site:
            noun = f"{site.get('language')} file(s)" if site.get("language") else "file(s)"
            detail += f" — {site.get('dist_files')} {noun}, {site.get('dist_bytes')} bytes"
        # An image is what runs, and every published project has one now.
        detail += f". Running as {image_tag_for(slug)}." if site and site.get("image") else "."
        if url:
            detail += f" Live at {url}."
        return 0, detail, site, url, None

    def publish_steps(self, slug: str, kind: str, plan: dict | None) -> list[list[str]]:
        """The commands a publish runs, in order.

        Three steps, and the same three whether the project is an app or a website:

          1. `package-project.py` — the Dockerfile from the plan, install, build,
             image. Every kind ends as an image now, because a website is served by
             nginx and an app by its own process, and a container is how you get
             either without a toolchain on the host.
          2. `app-runtime.py --up --build` — run it on a loopback port and write the
             vhost that puts its name on it.
          3. `studio-sites.py --publish` — the DNS record and the edge proxy host.

        A project with no plan is the old path: its own packager builds it, and a
        website is staged into the static tree instead of run as a container. That
        path exists only for projects saved before the planner did.
        """
        scripts = self.repo / "scripts"

        if not plan:
            if kind == "app":
                return [
                    ["python3", str(scripts / "package-app.py"), slug],
                    ["python3", str(scripts / "app-runtime.py"), "--up", slug, "--build"],
                    ["python3", str(scripts / "studio-sites.py"), "--publish", slug],
                ]
            return [
                ["python3", str(scripts / "package-website.py"), slug, "--publish"],
                ["python3", str(scripts / "studio-sites.py"), "--publish", slug],
            ]

        return [
            ["python3", str(scripts / "package-project.py"), slug],
            ["python3", str(scripts / "app-runtime.py"), "--up", slug, "--build"],
            ["python3", str(scripts / "studio-sites.py"), "--publish", slug],
        ]

    def preview_steps(self, slug: str, kind: str, plan: dict | None) -> list[list[str]]:
        """The commands a preview runs: what a publish runs, on a name of its own.

        The container is the same one a publish runs, and `--preview` gives it a
        second vhost so the frame can reach it. The last step is the difference: a
        publish registers `<slug>.<suffix>`, a preview registers
        `<slug>-preview.<suffix>`. Registering is unavoidable — the pane is https and
        an iframe of a plain-http address is blocked — so the choice is which name,
        and the project's own name is the one a preview must not take.

        None of this publishes anything: the project's real name is never added to
        the edge, so a preview of a project nobody has published is a name that does
        not exist.
        """
        scripts = self.repo / "scripts"
        # A plan means the generic packager writes the Dockerfile; no plan means this
        # project predates the planner and its own packager builds it. A website with
        # no plan never reaches here — `validate_request` refuses it, because static
        # files are not a process and there would be nothing to run.
        packager = "package-project.py" if plan else "package-app.py"
        return [
            ["python3", str(scripts / packager), slug],
            ["python3", str(scripts / "app-runtime.py"), "--up", slug, "--build", "--preview"],
            ["python3", str(scripts / "studio-sites.py"), "--preview", slug],
        ]

    # Which script turns a generated build into something real, per kind. A website
    # is static files; an app is a client build and a runtime image.
    PACKAGERS = {
        "website": "package-website.py",
        "app": "package-app.py",
    }

    def run_package(self, job: str, spec: dict) -> tuple[int, str]:
        """Package a generated build — the step between generated and real.

        Runs only after the agent has written the source. The model's output is the
        input here, not the thing being verified: the packager owns the Vite
        project, installs the pinned dependencies and builds, and a TypeScript error
        in the model's component fails *this* step with the compiler's own message
        rather than being found much later by whoever opened the site.

        Both kinds need it, for the same reason and with the same shape: the model
        writes the part that needs judgement and this writes the project around it.
        A website ends as `dist/`; an app ends as `dist/client` plus the generated
        server that serves it.

        Appended to the same job log, so the Studio panel shows one continuous
        build rather than two that have to be stitched together.
        """
        slug = str(spec["slug"])
        kind = spec.get("kind") if spec.get("kind") in self.PACKAGERS else "app"
        plan = spec.get("plan") if isinstance(spec.get("plan"), dict) else None
        build_dir = resolve_build_dir(self.repo, slug)

        # A spec-driven build — `make app`, the app-builder CI job — carries no plan
        # in its request and has one on disk, written by the workflow's planning node
        # before the agent ran. Reading it here is what makes the two paths package
        # the same way instead of the CLI path falling back to the fixed scaffold.
        if plan is None:
            plan = read_plan(build_dir)

        if plan:
            # A planned project: the generic packager, and no `--publish` flag,
            # because staging `dist/` is a website-only shortcut for a project with
            # no runtime. This one has an image.
            packager = "package-project.py"
            what = str((plan.get("runtime") or {}).get("language") or kind)
        else:
            packager = self.PACKAGERS[kind]
            what = f"{kind} (no plan)"

        command = ["python3", str(self.repo / "scripts" / packager), slug]
        if plan is None and spec.get("publish") and kind == "website":
            command.append("--publish")

        try:
            write_plan(build_dir, plan)
        except OSError as error:
            return 1, f"Could not write the plan into builds/{slug}: {error}"

        log(f"packaging {slug} ({what})")
        with self.log_path(job).open("ab") as sink:
            sink.write(f"\n=== packaging {slug} ({what}) ===\n".encode())
            sink.flush()
            exit_code = subprocess.call(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=str(self.repo),
                env=self.build_env(),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )

        if exit_code != 0:
            noun = "site" if kind == "website" else "app"
            return exit_code, (
                f"The {noun} was generated but did not build (exit {exit_code}). "
                "The files are in builds/" + slug + " — see the build output in the log."
            )

        return 0, ""

    # ---- lifecycle -------------------------------------------------------

    def recover(self) -> None:
        """Fail jobs left running by a previous runner, so none stay stuck forever.

        A `.running.json` older than the timeout cannot be progress: either the
        process died or the build was killed. Reporting it as failed is honest and
        lets the operator queue it again.
        """
        try:
            stale = list(self.queue.glob(f"*{RUNNING_SUFFIX}"))
        except OSError:
            return

        for path in stale:
            job = path.name[: -len(RUNNING_SUFFIX)]
            age = time.time() - path.stat().st_mtime
            if age < self.timeout_seconds:
                continue
            log(f"failing stale job {job} (running for {int(age)}s with no runner)")
            self.write_status(
                job,
                state="failed",
                slug=None,
                message="The runner stopped while this build was in progress.",
                exit_code=None,
            )
            path.unlink(missing_ok=True)

    def serve(self) -> int:
        self.started_at = now_iso()
        self.queue.mkdir(parents=True, exist_ok=True)
        self.recover()
        log(f"build runner ready — repo {self.repo}, queue {self.queue}")

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        last_beat = 0.0
        while not self.stopping:
            pending = self.requests()
            if pending:
                self.process(pending[0])
                continue

            if time.monotonic() - last_beat >= HEARTBEAT_EVERY_SECONDS:
                last_beat = time.monotonic()
                self.heartbeat(self.started_at)

            time.sleep(self.poll_seconds)

        log("build runner stopping")
        return 0

    def _stop(self, *_: object) -> None:
        self.stopping = True


def default_queue_dir(repo: Path) -> Path:
    configured = os.environ.get("BUILD_QUEUE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # `.factory/` is this repo's untracked runtime state — the same tree the
    # scheduler markers use. Nothing under it is tracked, which is exactly right
    # for a queue that holds build requests and logs.
    return repo / ".factory" / "build-queue"


def submit(queue: Path, repo: Path, spec: str, *, replace: bool) -> tuple[int, dict | None]:
    """Queue a build. Reuses the runner's own validation, so the CLI and the UI
    cannot disagree about what is acceptable."""
    payload = {
        "v": PROTOCOL_VERSION,
        "job": os.urandom(8).hex(),
        "spec": spec,
        "requested_by": f"cli:{os.environ.get('USER') or 'unknown'}",
        "requested_at": now_iso(),
        "replace": replace,
    }
    try:
        spec_target, spec_rel = resolve_spec(repo, spec)
        payload["spec"] = spec_rel
        payload["slug"] = resolve_slug(payload, spec_rel)
        payload["title"] = payload["slug"]
        _ = spec_target
    except RequestError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2, None

    queue.mkdir(parents=True, exist_ok=True)
    target = queue / f"{payload['job']}{REQUEST_SUFFIX}"
    write_json_atomic(target, payload)
    return 0, payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run queued Olympus app builds (the `make app` executor)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="process the queue, then wait (default)")
    mode.add_argument("--once", action="store_true", help="process what is queued now, then exit")
    mode.add_argument("--list", action="store_true", help="show the queue and its statuses")
    mode.add_argument("--check", action="store_true", help="validate the environment without building")
    mode.add_argument("--submit", metavar="SPEC", help="queue a build for SPEC")
    parser.add_argument("--replace", action="store_true", help="with --submit: rebuild over an existing app")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository root")
    parser.add_argument("--queue", default="", help="queue directory")
    parser.add_argument("--timeout", type=int, default=0, help="per-build seconds")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    queue = Path(args.queue).expanduser().resolve() if args.queue else default_queue_dir(repo)
    timeout = args.timeout or int(os.environ.get("BUILD_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    poll = int(os.environ.get("BUILD_POLL_SECONDS") or DEFAULT_POLL_SECONDS)

    if args.check:
        missing = [p for p in (repo / "scripts" / "manufacture.sh",) if not p.is_file()]
        # Parsed once and kept: the runtime roots below are read from the file rather
        # than from `env`, because the allow-list deliberately does not carry them
        # into a build (they are not the build's business) — but whether this host
        # can *write* them is the check's business.
        dotenv = (
            parse_env_file((repo / ".env").read_text(encoding="utf-8"))
            if (repo / ".env").is_file()
            else {}
        )
        env = allowed_env(dotenv, dict(os.environ), repo)
        # Reported because the runner no longer has to be root: an operator
        # checking a fresh install wants to see *which* account is about to run
        # builds, and whether it can read the .env containing the gateway key.
        print(f"user        uid {os.geteuid()} ({os.environ.get('USER') or 'unknown'})")
        print(f"repo        {repo}")
        print(f"queue       {queue}")
        print(f"queue writable  {os.access(queue if queue.exists() else repo, os.W_OK)}")
        print(f".env readable   {(repo / '.env').is_file() and os.access(repo / '.env', os.R_OK)}")
        print(f"timeout     {timeout}s")
        print(f"gateway     {env.get('OMNIROUTE_BASE_URL', '<unset>')}")
        print(f"model       {env.get('OMNIROUTE_MODEL', '<unset>')}")
        print(f"fallback    {env.get('OMNIROUTE_MODEL_FALLBACK', '<unset>')}")
        archon, archon_source, archon_tried = resolve_archon(repo, env)
        label = f"  {'archon':<9} "
        if archon:
            print(f"{label}{archon}" + (f"  ({archon_source})" if archon_source else ""))
        else:
            print(f"{label}MISSING")
            for candidate in archon_tried:
                print(f"  {'':<9}   tried {candidate}")
            missing.append("the Archon CLI (ARCHON_BINARY, core-modules/archon/bin/archon or PATH)")
        for tool in ("codex", "uv", "bash"):
            found = shutil.which(tool, path=env["PATH"])
            print(f"  {tool:<9} {found or 'MISSING'}")

        # Docker, and the one plugin the delivery needs, are checked because a job
        # reaches them LAST: the image is built — and for a publish or a preview a
        # container is run — only after a model run has already been paid for.
        # Learning it here costs a second; learning it in the job log costs the job.
        docker_bin = shutil.which("docker", path=env["PATH"])
        print(f"  {'docker':<9} {docker_bin or 'MISSING'}")
        if not docker_bin:
            missing.append("docker — the image is built and the container is run at the end of every job")
        else:
            # The only honest test for the plugin: `docker buildx` can be absent, and
            # it can be present but refuse to run. Without it docker falls back to the
            # deprecated legacy builder, which ignores the `# syntax=docker/dockerfile:1`
            # every generated Dockerfile starts with — the file then asks for the
            # BuildKit frontend and the builder does something else.
            buildx = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [docker_bin, "buildx", "version"],
                capture_output=True,
                text=True,
                check=False,
            )
            buildx_version = (buildx.stdout or "").strip().splitlines()
            if buildx.returncode == 0 and buildx_version:
                print(f"  {'buildx':<9} {buildx_version[0]}")
            else:
                print(f"  {'buildx':<9} MISSING")
                missing.append(
                    "the docker buildx plugin — otherwise every image is built by the "
                    "deprecated legacy builder (apt-get install -y docker-buildx-plugin)"
                )

        # The app runtime's own trees, which live outside the checkout and are handed
        # to the build account by scripts/install-build-runner.sh. A build that cannot
        # write them fails at the last step of a job that has already manufactured and
        # packaged — the most expensive place to find out, and the exact failure this
        # check exists to move forward by twenty minutes.
        for label, key, default in (
            ("apps", "OLYMPUS_APPS_ROOT", "/var/lib/olympus/apps"),
            ("sites", "OLYMPUS_SITES_ROOT", "/var/lib/olympus/sites"),
        ):
            root = Path(dotenv.get(key) or os.environ.get(key) or default)
            writable = os.access(root, os.W_OK)
            print(f"  {label:<9} {root} — {'writable' if writable else 'NOT WRITABLE'}")
            if not writable:
                # `os.access`, not a mode read: the question is whether THIS account
                # can write it, and answering it as someone else is how a host ends up
                # green on the check and red on the next build.
                missing.append(
                    f"{root} writable by uid {os.geteuid()} ({key}) — scripts/"
                    "install-build-runner.sh creates it and hands it to the build account"
                )

        for path in missing:
            print(f"missing: {path}")
        return 1 if missing else 0

    if args.submit:
        code, payload = submit(queue, repo, args.submit, replace=args.replace)
        if code == 0 and payload:
            print(f"queued {payload['job']} — {payload['spec']} (app {payload['slug']})")
        return code

    if args.list:
        if not queue.is_dir():
            print(f"no queue at {queue} (nothing has been submitted)")
            return 0
        heartbeat = read_json(queue / HEARTBEAT_NAME)
        if isinstance(heartbeat, dict):
            print(f"runner  pid {heartbeat.get('pid')} on {heartbeat.get('host')} "
                  f"— last beat {heartbeat.get('beat_at')}"
                  + (f", busy with {heartbeat['busy_with']}" if heartbeat.get("busy_with") else ""))
        else:
            print("runner  NOT RUNNING (no heartbeat)")
        for path in sorted(queue.iterdir()):
            if path.name.endswith(REQUEST_SUFFIX):
                print(f"queued  {path.name[: -len(REQUEST_SUFFIX)]}")
            elif path.name.endswith(RUNNING_SUFFIX):
                print(f"running {path.name[: -len(RUNNING_SUFFIX)]}")
        for path in sorted(queue.glob(f"*{STATUS_SUFFIX}")):
            status = read_json(path)
            if not isinstance(status, dict):
                continue
            print(f"{str(status.get('state', '?')):<7} {status.get('job')}  {status.get('message')}")
        return 0

    runner = Runner(repo, queue, timeout_seconds=timeout, poll_seconds=poll)
    lock_path = queue / LOCK_NAME
    queue.mkdir(parents=True, exist_ok=True)

    # One runner per queue. Two would both scan, and though the claim is atomic,
    # two concurrent `make app` runs is exactly the load this avoids.
    #
    # A permissions failure here is NOT "another runner is running" — it is the
    # account this unit runs as being unable to use the queue at all, which is
    # what a switch from a root install leaves behind (a root-owned lock file the
    # new service account cannot open). Saying so saves reading the errno.
    try:
        lock = lock_path.open("w")
    except PermissionError:
        log(
            f"cannot open {lock_path} as uid {os.geteuid()}: the queue directory is "
            "owned by another account — re-run scripts/install-build-runner.sh to "
            "hand it to the build user"
        )
        return 1
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"another build runner already holds {lock_path}")
        return 1

    if args.once:
        runner.started_at = now_iso()
        runner.recover()
        processed = 0
        while True:
            pending = runner.requests()
            if not pending:
                break
            runner.process(pending[0])
            processed += 1
        log(f"drained {processed} job(s)")
        return 0

    return runner.serve()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as error:
        print(f"build-runner: {error}", file=sys.stderr)
        sys.exit(1)

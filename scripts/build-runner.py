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

    if slug != Path(spec_rel).stem:
        raise RequestError(
            f"slug {slug!r} does not match the spec name {Path(spec_rel).stem!r}"
        )
    return slug


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

    spec_target, spec_rel = resolve_spec(repo, payload.get("spec"))
    slug = resolve_slug(payload, spec_rel)

    title = payload.get("title")

    # What is being built. An unknown value is an app rather than a refusal: every
    # request written before the split means an app, and a queue that refuses the
    # requests already in it would strand a build the operator asked for.
    kind = payload.get("kind")
    if kind not in (None, "app", "website"):
        raise RequestError(f"unknown kind: {kind!r} (expected 'app' or 'website')")

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
        # Whether to stage `dist/` for the host to serve once it is built. A
        # website is packaged either way — a site that is not packaged is not a
        # site — but publishing is a separate, visible decision.
        "publish": payload.get("publish") is True,
    }


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

        exit_code, detail, state_override = self.run_build(job, spec, started_at)

        # A website has one more step. It runs only when the agent produced
        # something — packaging an empty directory would fail with a compiler error
        # about a missing src/App.tsx, which says nothing about the real problem.
        package_code = 0
        if state_override is None and exit_code == 0 and spec.get("kind") == "website":
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
        artifact = read_manifest(build_dir)
        site = read_site_manifest(build_dir) if spec.get("kind") == "website" else None

        # For a website, a manifest is necessary and not sufficient: the files exist
        # but the site does not run until it packages, and reporting it as built
        # would hand the operator a directory to deploy that deploys nothing.
        succeeded = exit_code == 0 and package_code == 0 and artifact is not None
        if succeeded and spec.get("kind") == "website":
            succeeded = site is not None

        if succeeded:
            message = (
                f"Built builds/{slug} — {artifact.get('files')} file(s), "
                f"{artifact.get('bytes')} bytes, entry {artifact.get('entry')}."
            )
            if site:
                message += (
                    f" Packaged: {site.get('dist_files')} dist file(s), "
                    f"{site.get('dist_bytes')} bytes, served at {site.get('entry')}."
                )
        elif exit_code == 0 and package_code != 0:
            message = detail or "The site did not package. See the build log."
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
            log_tail=log_tail(self.log_path(job)),
        )
        running.unlink(missing_ok=True)

    @staticmethod
    def job_fields(spec: dict) -> dict:
        """The identity fields every status carries, so a reader never has to merge."""
        return {
            "slug": spec["slug"],
            "title": spec["title"],
            "spec": spec["spec"],
            "kind": spec.get("kind", "app"),
            "requested_at": spec["requested_at"],
            "requested_by": spec["requested_by"],
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

    def run_package(self, job: str, spec: dict) -> tuple[int, str]:
        """Package a website build into `dist/` — the step between generated and real.

        Runs only after the agent has written the source. The model's output is the
        input here, not the thing being verified: `package-website.py` owns the Vite
        project, installs the pinned dependencies and builds, and a TypeScript error
        in the model's component fails *this* step with the compiler's own message
        rather than being found much later by whoever opened the site.

        Appended to the same job log, so the Studio panel shows one continuous
        build rather than two that have to be stitched together.
        """
        slug = str(spec["slug"])
        command = ["python3", str(self.repo / "scripts" / "package-website.py"), slug]
        if spec.get("publish"):
            command.append("--publish")

        log(f"packaging {slug} as a website")
        with self.log_path(job).open("ab") as sink:
            sink.write(f"\n=== packaging {slug} (website) ===\n".encode())
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
            return exit_code, (
                f"The site was generated but did not package (exit {exit_code}). "
                "The files are in builds/" + slug + " — see the packaging output in the log."
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
        env = allowed_env(
            parse_env_file((repo / ".env").read_text(encoding="utf-8"))
            if (repo / ".env").is_file()
            else {},
            dict(os.environ),
            repo,
        )
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
        for tool in ("archon", "codex", "uv", "bash"):
            found = shutil.which(tool, path=env["PATH"])
            print(f"  {tool:<9} {found or 'MISSING'}")
        if missing:
            for path in missing:
                print(f"missing: {path}")
            return 1
        return 0

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

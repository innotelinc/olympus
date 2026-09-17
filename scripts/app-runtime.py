#!/usr/bin/env python3
"""Run a packaged Studio application as its own container, and give it a name.

WHAT THIS IS FOR. A Studio website is files, and publishing one is a copy into a
served directory. An application is a *process*: it has a database, it has to be
started, and it has to be reachable at a hostname. Packaging produces an image
(`scripts/package-app.py`); this turns that image into a running thing and wires
the name to it.

ONE CONTAINER PER APP, AND ONE PORT NOBODY ELSE SEES. Each application gets its own
container, its own SQLite file and its own port, so two apps cannot collide over a
table name or a database connection. The port is published to loopback only:
`olympus-sites` runs on the host's network namespace (compose `network_mode: host`),
so it can reach `127.0.0.1:<port>` — and nothing on the LAN can. The only way in is
through the edge, by name, which is where the TLS and the identities live.

THE NAME IS ROUTED BY A GENERATED VHOST, NOT BY A PORT PER SITE. Publishing writes
one nginx `server` block for `<slug>.<SITE_HOST_SUFFIX>` that proxies to that app's
loopback port, then reloads `olympus-sites`. An exact `server_name` beats the static
template's regex, so a name that is an app never falls back to `$site` static
looking. This is also why the edge config stays fixed: `studio-sites.py --publish
<slug>` forwards to the sites port for every app, forever.

    scripts/app-runtime.py --up weight-tracker
    scripts/app-runtime.py --up weight-tracker --build      # build the image first
    scripts/app-runtime.py --status weight-tracker          # JSON
    scripts/app-runtime.py --list
    scripts/app-runtime.py --logs weight-tracker
    scripts/app-runtime.py --down weight-tracker            # stop, keep the data
    scripts/app-runtime.py --remove weight-tracker          # and delete the data

Exit codes:
    0  done (or, with --dry-run, would have been)
    1  docker refused, or the container will not run
    2  the request made no sense (no such build, bad slug, bad port range)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# --- constants ---------------------------------------------------------------

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

BUILDS_DIR = "builds"

# Runtime state, deliberately outside the checkout: containers, ports and databases
# are runtime state, and a tree of them inside the repo is one `git add -A` away
# from being committed.
DEFAULT_APP_ROOT = "/var/lib/olympus/apps"

# Ports an app may be given. A range rather than an ephemeral port: the number is
# written into a vhost and has to keep meaning the same thing across restarts.
DEFAULT_PORT_BASE = 21400
DEFAULT_PORT_RANGE = 200

# The port an app listens on *inside* its container when its plan does not say.
# Not the published one — what changes per app is the host side — and a default
# rather than a rule: a planned project declares its own port, and it is installed
# into the image as `ENV PORT`, so the two have to agree or nothing is reachable.
CONTAINER_PORT = 3000

# The plan the packager built the project from. Read here for the container port and
# the healthcheck path, because those are two facts the runtime needs and only the
# plan knows: a Python app on :8000 and a Node app on :3000 are the same publish.
PLAN_NAME = "plan.json"
MIN_PORT = 1024
MAX_PORT = 49151

# Where the packagers leave their summary, newest name first. Their presence is what
# "already packaged" means — not the image, which is what we are about to build.
MANIFEST_NAMES = ("project.manifest.json", "app.manifest.json", "site.manifest.json")

# The name `olympus-sites` is started under, so the reload can find it.
SITES_CONTAINER = "olympus-sites"

DEFAULT_SUFFIX = "studio.olympus.innotel.us"


# --- helpers -----------------------------------------------------------------


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str, code: int) -> "NoReturn":  # type: ignore[name-defined]
    note(f"APP_RUNTIME_FAILED: {message}")
    raise SystemExit(code)


def repo_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if not (root / "Makefile").is_file():
        fail(f"{root} does not look like the Olympus checkout", 2)
    return root


def read_env(path: Path) -> dict[str, str]:
    """The repo `.env`, read the way the rest of the stack reads it."""
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = line.find("=")
        if separator == -1:
            continue
        key = line[:separator].strip()
        value = line[separator + 1 :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            parsed[key] = value

    return parsed


def docker(*args: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        fail("docker is not on PATH; an application cannot be run without it", 2)

    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [docker_bin, *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def ensure_data_dir(runtime: "Runtime", slug: str) -> Path:
    """Create this app's data directory, or say which account has to be able to.

    The root lives outside the checkout and is handed to the build account by
    `scripts/install-build-runner.sh` rather than created here: a run that discovers
    it is unwritable at this point has already manufactured and packaged, and the
    uncaught PermissionError that used to come out of here named neither the path's
    owner nor the script that sets it — it read as a crash in this file.
    """
    target = runtime.data_dir(slug)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fail(
            f"cannot create {target} ({error}). The app data root {runtime.data} must "
            "exist and be writable by the account builds run as — "
            "scripts/install-build-runner.sh hands it to the build user",
            2,
        )
    return target


def port_is_free(port: int) -> bool:
    """Whether the host can bind it. A port a *previous* container holds is not free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# --- state -------------------------------------------------------------------


class Runtime:
    """Where an app's runtime state lives: one directory tree, one JSON per app."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = env
        self.runtime = root / "runtime"
        self.nginx = root / "nginx"
        self.data = root / "data"

        self.port_base = int(env.get("APP_PORT_BASE") or DEFAULT_PORT_BASE)
        self.port_range = int(env.get("APP_PORT_RANGE") or DEFAULT_PORT_RANGE)
        self.suffix = (env.get("SITE_HOST_SUFFIX") or DEFAULT_SUFFIX).rstrip(".").lower()
        self.site_port = int(env.get("SITE_PORT") or 20130)

        if self.port_range < 1:
            fail("APP_PORT_RANGE must be at least 1", 2)

    def state_path(self, slug: str) -> Path:
        return self.runtime / f"{slug}.json"

    def read(self, slug: str) -> dict | None:
        try:
            record = json.loads(self.state_path(slug).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def write(self, slug: str, record: dict) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        target = self.state_path(slug)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def forget(self, slug: str) -> None:
        self.state_path(slug).unlink(missing_ok=True)

    def claimed_ports(self) -> set[int]:
        claimed: set[int] = set()
        try:
            states = list(self.runtime.glob("*.json"))
        except OSError:
            return claimed

        for path in states:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            port = record.get("port") if isinstance(record, dict) else None
            if isinstance(port, int):
                claimed.add(port)

        return claimed

    def allocate_port(self, slug: str) -> int:
        """The port this app already has, or the first free one in the range.

        Stable across restarts: a container that came back on a different port would
        leave its vhost pointing at nothing, which reads as the app being down.
        """
        existing = self.read(slug)
        if existing and isinstance(existing.get("port"), int):
            return int(existing["port"])

        claimed = self.claimed_ports()
        for offset in range(self.port_range):
            candidate = self.port_base + offset
            if candidate in claimed or not port_is_free(candidate):
                continue
            return candidate

        fail(
            f"no free port in {self.port_base}-{self.port_base + self.port_range - 1}. "
            "Widen APP_PORT_RANGE in .env, or remove an app you no longer run "
            "(scripts/app-runtime.py --remove <slug>).",
            2,
        )

    def hostname(self, slug: str) -> str:
        return f"{slug}.{self.suffix}"

    def url(self, slug: str) -> str:
        return f"https://{self.hostname(slug)}"

    def preview_hostname(self, slug: str) -> str:
        """The name a preview is framed on: `<slug>-preview.<suffix>`.

        Deliberately not the project's own name. A preview has to be reachable from
        the browser it is shown in — an https page cannot frame a plain-http one —
        so it needs a name at the edge. Registering the project's *own* name there is
        publishing, which is the decision a preview exists to defer, so the preview
        gets a name of its own that says what it is.
        """
        return f"{slug}-preview.{self.suffix}"

    def preview_url(self, slug: str) -> str:
        return f"https://{self.preview_hostname(slug)}"

    def preview_vhost_path(self, slug: str) -> Path:
        return self.nginx / f"{slug}-preview.conf"

    def data_dir(self, slug: str) -> Path:
        return self.data / slug


def vhost(runtime: Runtime, slug: str, port: int, hostname: str | None = None) -> str:
    """The nginx `server` block that puts this app on one name.

    An exact `server_name` is what makes this work: nginx prefers it over the static
    template's regex `server_name`, so an app's name never falls through to the
    static tree. Everything is proxied, including `/api`, because the app serves its
    own client and its own API from the same origin.

    The name is a parameter because the same container answers on two of them: the
    project's own, and the one a preview is framed on. Both blocks are identical
    apart from the name, and writing them from one function is what keeps them from
    drifting — a preview that proxied differently from the published site would be a
    preview of something else.
    """
    name = hostname or runtime.hostname(slug)
    return f"""# Generated by scripts/app-runtime.py — do not edit by hand.
# {slug}: {name} -> 127.0.0.1:{port}
server {{
    listen       {runtime.site_port};
    listen  [::]:{runtime.site_port};
    server_name  {name};

    # An application holds a websocket-shaped connection open for as long as the tab
    # is; the default 60s read timeout shows up as a page that stops loading.
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 4m;

    location / {{
        proxy_pass         http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
    }}
}}
"""


def write_vhost(
    runtime: Runtime,
    slug: str,
    port: int,
    hostname: str | None = None,
    filename: str | None = None,
) -> Path:
    runtime.nginx.mkdir(parents=True, exist_ok=True)
    target = runtime.nginx / (filename or f"{slug}.conf")
    target.write_text(vhost(runtime, slug, port, hostname), encoding="utf-8")
    return target


class VhostInvalid(RuntimeError):
    """The generated vhost is config nginx refuses to start with.

    Its own type because the caller's response differs from a plain reload failure: this
    one names a file *we* just wrote, so that file can be taken back out and the edge
    returned to its previous configuration. "nginx would not reload" for any other
    reason means the config was accepted and something else went wrong.
    """


def reload_sites(dry_run: bool) -> str:
    """Make `olympus-sites` re-read the generated vhosts.

    A reload, not a restart: nginx re-reads its config and keeps serving during it.
    Its absence is reported rather than failed on — the vhosts are read at the next
    start anyway, so a publish on a host where the sites edge is not up is a publish
    whose name starts working when the edge does.
    """
    if dry_run:
        return "would reload olympus-sites"

    running = docker("ps", "--format", "{{.Names}}").stdout or ""
    if SITES_CONTAINER not in running.split():
        return f"{SITES_CONTAINER} is not running — the name works when it is"

    result = docker("exec", SITES_CONTAINER, "nginx", "-t")
    if result.returncode != 0:
        # The config is not applied if nginx would not start with it, which is the
        # one failure that must not be silent: a broken vhost takes down every other
        # site on the edge, not just this app.
        raise VhostInvalid(f"the generated vhost is not valid nginx config:\n{result.stdout}")

    result = docker("exec", SITES_CONTAINER, "nginx", "-s", "reload")
    if result.returncode != 0:
        raise RuntimeError(f"nginx would not reload:\n{result.stdout}")

    return "reloaded olympus-sites"


def image_exists(image: str) -> bool:
    return docker("image", "inspect", image).returncode == 0


def container_state(name: str) -> str:
    """`running`, `exited`, or `absent`."""
    result = docker("inspect", "--format", "{{.State.Status}}", name)
    if result.returncode != 0:
        return "absent"
    return (result.stdout or "").strip() or "absent"


def read_plan(app_dir: Path) -> dict:
    """The plan this project was packaged from, or an empty dict.

    A missing plan is not a failure here: a project packaged before the planner
    existed has an image and a Dockerfile but no `plan.json`, and refusing to start
    it would break every published app. The defaults below are exactly what those
    packagers generated, so the old path keeps working unchanged.
    """
    try:
        payload = json.loads((app_dir / PLAN_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def plan_port(plan: dict) -> int:
    """The port inside the container, from the plan, within the usable range.

    Out-of-range is corrected rather than refused: no published project can be
    started on :80 without a root the container does not have, and failing the
    publish would leave the operator with an app that builds and cannot run. The
    planner refuses the same value at the other end, so this is the second gate.
    """
    run = plan.get("run")
    raw = run.get("port") if isinstance(run, dict) else None
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return CONTAINER_PORT
    return port if MIN_PORT <= port <= MAX_PORT else CONTAINER_PORT


def plan_healthcheck(plan: dict) -> str:
    run = plan.get("run")
    raw = run.get("healthcheck") if isinstance(run, dict) else None
    if not isinstance(raw, str) or not raw.startswith("/"):
        return "/api/health"
    return raw.split("?")[0] or "/api/health"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def health(runtime: Runtime, slug: str, port: int, timeout: float, path: str = "/api/health") -> bool:
    """Whether the app answers on loopback. A container that started is not an app
    that works: the process can boot and still fail its first request, which is
    exactly what the generated Dockerfile's HEALTHCHECK is also looking for.

    The path comes from the plan, so this asks the project's own "am I up" endpoint
    rather than a path only the generated server had. A 200 is required, not any
    answer: a 500 is a container that is running and broken.
    """
    request = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                connection.sendall(request)
                if b" 200 " in connection.recv(256):
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def already_packaged(app_dir: Path) -> bool:
    """Whether a packager has run here — a Dockerfile *and* a manifest it wrote.

    Both, because either alone is a half-state: a Dockerfile with no manifest is a
    build that was interrupted before the image was made, and a manifest with no
    Dockerfile cannot be built at all.
    """
    if not (app_dir / "Dockerfile").is_file():
        return False
    return any((app_dir / name).is_file() for name in MANIFEST_NAMES)


# --- operations --------------------------------------------------------------


def up(runtime: Runtime, args: argparse.Namespace) -> int:
    slug = args.up
    app_dir = (repo_root() / BUILDS_DIR / slug).resolve()
    if not app_dir.is_dir():
        fail(f"no build at builds/{slug} — package it first (make app-package SLUG={slug})", 2)

    plan = read_plan(app_dir)
    container_port = plan_port(plan)
    health_path = plan_healthcheck(plan)

    image = args.image or f"olympus-app-{slug}:latest"
    name = args.container or f"olympus-app-{slug}"
    port = runtime.allocate_port(slug)

    if args.build or not image_exists(image):
        if args.dry_run:
            note(f"would build {image} from {app_dir}")
            note(f"would run it on 127.0.0.1:{port}:{container_port}, health {health_path}")
        else:
            # The project may have been packaged by the runner already. If it has not
            # — someone ran this by hand against a generated build — package it first,
            # because `docker build` needs the Dockerfile and the install step.
            if not already_packaged(app_dir):
                # A plan means the generic packager wrote the Dockerfile; no plan means
                # this project predates the planner, and its own packager built it.
                packager = "package-project.py" if plan else "package-app.py"
                command = ["python3", str(repo_root() / "scripts" / packager), slug]
                note(f"$ {' '.join(command)}")
                if subprocess.call(command, cwd=str(repo_root())) != 0:  # noqa: S603
                    fail(f"{slug} did not package, so its image cannot be built", 1)
            else:
                # Through `scripts/buildx`, the same command the packagers build with:
                # it is where BuildKit-vs-legacy is decided, and a fallback that
                # spelled out `docker build` here would be a second answer to it.
                buildx = repo_root() / "scripts" / "buildx"
                if not buildx.is_file():
                    fail(f"{buildx} is missing, so there is no command here that builds an image", 2)
                command = [str(buildx), "--tag", image, "."]
                note(f"$ {' '.join(command)}")
                if subprocess.call(command, cwd=str(app_dir)) != 0:  # noqa: S603
                    fail(f"the runtime image for {slug} did not build", 1)

    vhost_path = runtime.nginx / f"{slug}.conf"
    # `--preview` adds a second name for the *same* container, and nothing else. The
    # project's own vhost is written either way, so a project that is already
    # published keeps answering on its name while it is being previewed.
    preview_vhost_path = runtime.preview_vhost_path(slug) if args.preview else None

    if args.dry_run:
        note(f"would run {name} as {image} on 127.0.0.1:{port}, data in {runtime.data_dir(slug)}")
        note(f"would write {vhost_path} and reload {SITES_CONTAINER}")
        if preview_vhost_path is not None:
            note(f"would also write {preview_vhost_path} for {runtime.preview_url(slug)}")
        print(runtime.url(slug))
        return 0

    # After the dry-run return, so a run that says it would do nothing does nothing:
    # the vhost files are the two things on this path that outlive the process.
    #
    # Whether each already existed is kept, because a reload the edge refuses has to be
    # undone by taking back only what this run added: unlinking a published app's own
    # vhost would drop a name that was working before this call.
    vhost_existed = vhost_path.exists()
    preview_existed = preview_vhost_path.exists() if preview_vhost_path is not None else False
    # The data root lives outside the checkout and is handed to the build account by
    # `scripts/install-build-runner.sh`, not created here — a run that discovers it
    # is unwritable at this point has already manufactured and packaged. Reported as
    # the path and the account rather than as an uncaught PermissionError, which
    # reads as a crash in this script and names neither.
    ensure_data_dir(runtime, slug)
    write_vhost(runtime, slug, port)
    if preview_vhost_path is not None:
        write_vhost(
            runtime,
            slug,
            port,
            runtime.preview_hostname(slug),
            f"{slug}-preview.conf",
        )

    # Replaced, not restarted: an app whose source changed is a different image, and
    # a restart would keep running the old one under the same tag.
    if container_state(name) != "absent":
        docker("rm", "-f", name)

    command = [
        "run",
        "--detach",
        "--name",
        name,
        "--restart",
        "unless-stopped",
        "--label",
        f"olympus.app={slug}",
        "--publish",
        f"127.0.0.1:{port}:{container_port}",
        "--volume",
        f"{runtime.data_dir(slug)}:/data",
        image,
    ]
    result = docker(*command)
    if result.returncode != 0:
        fail(f"could not start {name}:\n{result.stdout}", 1)

    if not health(runtime, slug, port, args.wait, health_path):
        logs = docker("logs", "--tail", "30", name).stdout or ""
        docker("rm", "-f", name)
        fail(f"{name} did not answer {health_path} within {args.wait}s:\n{logs}", 1)

    try:
        reload_note = reload_sites(False)
    except VhostInvalid as error:
        # The container stays up, and that is the point of this branch. What failed is
        # the *edge* taking the new config — nginx is still serving its last good one,
        # and this app is running on its loopback port — so removing the container would
        # turn a routing fault into a lost build. The files written a moment ago are the
        # only thing that changed, so the ones this run created come back out and the
        # edge is asked again: if it reloads, every other site on it was never affected.
        if not vhost_existed:
            vhost_path.unlink(missing_ok=True)
        if preview_vhost_path is not None and not preview_existed:
            preview_vhost_path.unlink(missing_ok=True)
        try:
            reload_sites(False)
        except RuntimeError:
            note("the edge is still on its previous configuration")
        fail(
            f"{slug} is running on 127.0.0.1:{port}, but the edge would not take its "
            f"name:\n{error}\n"
            f"The container was left running and the rejected vhost was taken back out. "
            f"Fix the cause and retry with `scripts/app-runtime.py --up {slug}`.",
            1,
        )
    except RuntimeError as error:
        # The config passed `nginx -t`, so there is nothing here worth taking back out:
        # the edge accepted the vhost and failed to finish the reload, and the name starts
        # working at the next reload. The container still stays — a routing fault is not a
        # reason to throw away a build — but it is reported as a failure, because the name
        # is not live and a caller that treated this as success would publish to nothing.
        fail(
            f"{slug} is running on 127.0.0.1:{port}, but the edge did not reload:\n{error}\n"
            f"The container was left running; the name starts working after the next "
            f"reload. Retry with `scripts/app-runtime.py --up {slug}`.",
            1,
        )

    record = {
        "v": 1,
        "slug": slug,
        "container": name,
        "image": image,
        "port": port,
        # Kept in the record as well as the plan: the record is what the runner and
        # the status read, and a preview needs the path that answers without having
        # to find and parse a plan that may not exist.
        "container_port": container_port,
        "healthcheck": health_path,
        "language": (plan.get("runtime") or {}).get("language") if isinstance(plan.get("runtime"), dict) else None,
        "url": runtime.url(slug),
        # Where a preview of this project can be framed. Recorded on the run rather
        # than derived by the reader: the name is the runtime's decision (`-preview`,
        # under the same suffix) and a reader composing it would be a second opinion
        # about a hostname.
        "preview_url": runtime.preview_url(slug) if preview_vhost_path is not None else None,
        "data": str(runtime.data_dir(slug)),
        "vhost": str(vhost_path),
        "preview_vhost": str(preview_vhost_path) if preview_vhost_path is not None else None,
        "started_at": now_stamp(),
    }
    runtime.write(slug, record)

    print(json.dumps(record, indent=2))
    # The vhost written above is what makes the name answer on olympus-sites, which
    # is this host. Registering the name at the *edge* is a different step with a
    # different owner — `studio-sites.py --publish`, run by `make app-publish` — and
    # `--up` is run on its own too, by hand and as the middle of that same target.
    # So the note cannot say "published" or "not published": both are wrong in one
    # of the two cases. What it can say is what it did, and name the command that
    # answers the other question — because the report that follows a stale URL is
    # "it's not resolving", and the honest thing is not to have implied it would.
    note(
        f"{slug} is running on 127.0.0.1:{port} ({reload_note}). Its name "
        f"{runtime.hostname(slug)} answers on olympus-sites; whether the edge "
        f"publishes it is a separate step — "
        f"`make site-check HOST={runtime.hostname(slug)}` reports that."
    )
    return 0


def down(runtime: Runtime, args: argparse.Namespace) -> int:
    slug = args.down
    record = runtime.read(slug) or {}
    name = str(record.get("container") or f"olympus-app-{slug}")

    if args.dry_run:
        note(f"would stop and remove {name}, keep {runtime.data_dir(slug)}, drop the vhost")
        return 0

    if container_state(name) != "absent":
        docker("rm", "-f", name)

    # The vhost goes with the container: leaving it would proxy a name to a port
    # nothing listens on, which reads as a broken edge rather than a stopped app.
    # The preview vhost goes the same way — it is the same port, and a stopped
    # project has no preview either.
    (runtime.nginx / f"{slug}.conf").unlink(missing_ok=True)
    runtime.preview_vhost_path(slug).unlink(missing_ok=True)
    runtime.forget(slug)

    try:
        reload_note = reload_sites(False)
    except RuntimeError as error:
        note(f"warning: {error}")

    print(f"{slug} is stopped. Its data is still at {runtime.data_dir(slug)}.")
    note(reload_note)
    return 0


def remove(runtime: Runtime, args: argparse.Namespace) -> int:
    slug = args.remove
    record = runtime.read(slug) or {}

    if args.dry_run:
        note(f"would stop the container, delete {runtime.data_dir(slug)} and {record.get('image')}")
        return 0

    down(runtime, argparse.Namespace(down=slug, dry_run=False, **{"env_file": args.env_file}))

    image = str(record.get("image") or f"olympus-app-{slug}:latest")
    data = runtime.data_dir(slug)
    if data.exists():
        shutil.rmtree(data)

    docker("image", "rm", "-f", image)

    print(f"{slug} is gone — container, database and image.")
    return 0


def status(runtime: Runtime, args: argparse.Namespace) -> int:
    slug = args.status
    record = runtime.read(slug)
    if record is None:
        if args.json:
            print(json.dumps({"slug": slug, "state": "absent"}))
        else:
            print(f"{slug} is not running.")
        return 0

    name = str(record.get("container"))
    state = container_state(name)
    payload = {
        **record,
        "state": state,
        "healthy": state == "running" and health(runtime, slug, int(record["port"]), 3),
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{slug}: {state} on 127.0.0.1:{record.get('port')} — {record.get('url')}")
    return 0


def listing(runtime: Runtime, args: argparse.Namespace) -> int:
    try:
        states = sorted(runtime.runtime.glob("*.json"))
    except OSError:
        states = []

    records: list[dict] = []
    for path in states:
        record = runtime.read(path.stem)
        if record:
            record["state"] = container_state(str(record.get("container")))
            records.append(record)

    if args.json:
        print(json.dumps(records, indent=2))
        return 0

    if not records:
        print("No applications are running.")
        return 0

    for record in records:
        print(
            f"  {record.get('slug'):<28} {record.get('state'):<9} "
            f"127.0.0.1:{record.get('port'):<6} {record.get('url')}"
        )
    return 0


def logs(runtime: Runtime, args: argparse.Namespace) -> int:
    record = runtime.read(args.logs) or {}
    name = str(record.get("container") or f"olympus-app-{args.logs}")
    result = docker("logs", "--tail", str(args.tail), name, capture=False)
    return result.returncode


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a packaged Studio application.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--up", metavar="SLUG", help="build if needed, run, and wire the name")
    action.add_argument("--down", metavar="SLUG", help="stop the container, keep its data")
    action.add_argument("--remove", metavar="SLUG", help="stop it and delete its data and image")
    action.add_argument("--status", metavar="SLUG", help="report one app")
    action.add_argument("--list", action="store_true", help="report every app")
    action.add_argument("--logs", metavar="SLUG", help="tail the container's output")

    parser.add_argument("--build", action="store_true", help="rebuild the image before running")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="also answer on <slug>-preview.<suffix>, the name a preview is framed on",
    )
    parser.add_argument("--image", default="", help="run this image instead of the slug's")
    parser.add_argument("--container", default="", help="run under this container name")
    parser.add_argument(
        "--wait",
        type=float,
        default=45.0,
        help="seconds to wait for the plan's healthcheck path after starting (default 45)",
    )
    parser.add_argument("--tail", type=int, default=50, help="lines for --logs")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--env-file", default="", help="path to .env")
    parser.add_argument("--root", default="", help="runtime state root (default OLYMPUS_APPS_ROOT)")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = repo_root()
    env_path = Path(args.env_file) if args.env_file else root / ".env"
    env = read_env(env_path)
    # The process environment wins, so a caller can override a value without editing
    # the file — the same precedence the rest of the stack uses.
    env.update({key: value for key, value in os.environ.items() if key.startswith(("APP_", "OLYMPUS_", "SITE_"))})

    state_root = Path(
        args.root or env.get("OLYMPUS_APPS_ROOT") or DEFAULT_APP_ROOT
    ).expanduser()
    runtime = Runtime(state_root, env)

    if args.up:
        if not SLUG_PATTERN.match(args.up):
            fail(f"unsafe app slug: {args.up!r}", 2)
        return up(runtime, args)
    if args.down:
        return down(runtime, args)
    if args.remove:
        return remove(runtime, args)
    if args.status:
        return status(runtime, args)
    if args.logs:
        return logs(runtime, args)
    return listing(runtime, args)


if __name__ == "__main__":
    raise SystemExit(main())

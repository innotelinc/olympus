#!/usr/bin/env python3
"""Did the publish actually publish? And if not, which half is missing?

WHY THIS EXISTS. `make site-check` answers "does the name answer". That is the right
question and it is not the whole one: when the answer is *no*, the operator has two
very different problems and nothing in the check tells them apart. The app may not be
running, or it may be running perfectly with its name never registered at the edge.
The first is a rebuild; the second is one command that rebuilds nothing. Until this
existed the second was only discoverable by reading the job log, and the runner's own
message said "run Preview It again" — which repackages and restarts a container that
was already healthy, for a name that was simply absent.

WHAT IT DOES, IN ORDER:

    1. the same check `site-check` runs (`scripts/site-check.py`, imported — not a
       second opinion about what "serving" means), on the published name or, with
       `--preview`, on `<slug>-preview.<suffix>`;
    2. only when that failed, the local half: is the app's container up (the runtime
       record plus `docker inspect`), and is this name registered at the edge
       (Cerulean's proxy hosts, read with the same client `studio-sites.py` writes
       with);
    3. a verdict with the one command that fixes it.

WHO READS IT. `scripts/build-runner.py` records it in the job status, so the Studio
panel shows what a publish was *checked against* rather than only that its last step
returned zero; `scripts/studio-sites.py` records it after registering a name, so a
publish done by hand from the command line leaves the same evidence as one made from
the UI. Both write the same file, so the panel and the operator are looking at one
record rather than two that can disagree.

    make site-evidence SLUG=todo-list
    make site-evidence SLUG=todo-list ARGS=--preview
    python3 scripts/delivery-evidence.py todo-list.studio.olympus.innotel.us --json
    python3 scripts/delivery-evidence.py todo-list --record --json

Exit codes — they *are* the verdict, so a caller does not have to parse prose:
    0  the name serves
    1  it does not, and re-running the delivery is the fix
    2  nothing to check, or the checkout is not configured for it
    3  the app is running and only the name is missing — retry the edge step alone
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
DEFAULT_SUFFIX = "studio.olympus.innotel.us"
PREVIEW_LABEL = "-preview"
EVIDENCE_VERSION = 1


def fail(message: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print(f"delivery-evidence: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_script(name: str, filename: str):
    """Import a sibling script by filename — they are hyphenated, so `import` cannot.

    The same three lines `olympus-tui.py` uses; the alternative is a second copy of
    the site check, which is exactly the drift this file exists to prevent.
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:  # pragma: no cover - only if the file is gone
        fail(f"{SCRIPTS / filename} is missing", 2)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.strip():
            values[key.strip()] = value
    return values


def apps_root(env: dict[str, str], default: Path | None = None) -> Path | None:
    """Where the application runtime keeps its state, or None.

    Read from the environment first and `.env` second, in that order: the build
    runner is started with the checkout's settings already exported, and on a host
    where it is not, the file the runtime itself reads is the answer.
    """
    value = (os.environ.get("OLYMPUS_APPS_ROOT") or env.get("OLYMPUS_APPS_ROOT") or "").strip()
    if value:
        return Path(value)
    if default is not None:
        return default
    return None


def app_record(root: Path | None, slug: str) -> dict | None:
    if root is None:
        return None
    try:
        record = json.loads((root / "runtime" / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def container_running(name: str) -> bool | None:
    """`True`/`False` from Docker, or `None` when there is no Docker to ask.

    `None` is not `False` on purpose: a check run where Docker is out of reach cannot
    claim the container is stopped, and reporting it as stopped would send the
    operator to restart a running app.
    """
    binary = shutil.which("docker")
    if not binary or not name:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "inspect", "--format", "{{.State.Status}}", name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return False
    return (result.stdout or "").strip() == "running"


def app_state(root: Path | None, slug: str) -> tuple[str, dict | None]:
    """`running`, `stopped`, `absent` or `unknown`, with the runtime record.

    The record is what says an app was ever started; Docker is what says it is still
    up. Both are needed: a record with no container is a stopped app, and no record at
    all means nothing was ever delivered under this slug.
    """
    record = app_record(root, slug)
    if record is None:
        return "absent", None

    state = container_running(str(record.get("container") or f"olympus-app-{slug}"))
    if state is None:
        return "unknown", record
    return ("running" if state else "stopped"), record


# `cerulean_api.Api.call` returns `(0, reason)` when nothing answered, and the
# functions around it turn that into `sys.exit(... "(HTTP 0)")`. Recognising the marker
# once means the check and the edge registration agree on what "the edge is down"
# looks like — the state the roadmap's retry path is about.
UNREACHABLE_MARKER = "HTTP 0"


def unreachable(error: object) -> bool:
    return UNREACHABLE_MARKER in str(error)


def host_for(slug: str, suffix: str, preview: bool) -> str:
    label = f"{slug}{PREVIEW_LABEL}" if preview else slug
    return f"{label}.{suffix.rstrip('.').lower()}"


def resolve_target(value: str, suffix: str, preview: bool) -> tuple[str, str, bool]:
    """`(host, slug, preview)` from a name or a slug.

    A name is taken as given, and the slug is read back out of it — which is why the
    answer says what it checked instead of assuming the caller's view of it. A slug is
    turned into this deployment's own name, so `SLUG=todo-list` and the wildcard
    suffix cannot drift apart.
    """
    asked = (value or "").strip().lower().rstrip(".")
    if not asked:
        fail("nothing to check: give a slug, or a name")
    if "." in asked:
        own = suffix.rstrip(".").lower()
        if not asked.endswith(f".{own}"):
            # A name that is not under this deployment's publishing suffix — the
            # gateway, Studio itself — has no slug to look up. It is still worth
            # checking, so the slug comes back empty and only the name's own answer and
            # the edge's registration are reported.
            return asked, "", asked.endswith(PREVIEW_LABEL)
        base = asked[: -len(own) - 1]
        slug = base[: -len(PREVIEW_LABEL)] if base.endswith(PREVIEW_LABEL) else base
        return asked, slug, base.endswith(PREVIEW_LABEL)
    return host_for(asked, suffix, preview), asked, preview


def edge_state(env_path: Path, env: dict[str, str], fqdn: str, insecure: bool) -> str:
    """Is this name registered at the edge — `registered`, `missing`, `unreachable`.

    Asked through `cerulean_api`, the same client and the same credential
    `studio-sites.py` writes the record with: a check that used a different door would
    be reporting on its own path, not on the one publishing uses.
    """
    cerulean_api = load_script("cerulean_api", "cerulean_api.py")
    skipped: set[str] = set()
    read = lambda name, fallback="": cerulean_api.setting(env_path, name, fallback, skipped)  # noqa: E731
    base = env.get("CERULEAN_DNS_API_URL") or read("CERULEAN_DNS_API_URL")
    if not base:
        return "unknown"
    zone = env.get("CERULEAN_ZONE") or read("CERULEAN_ZONE")
    token = env.get("CERULEAN_API_TOKEN") or read("CERULEAN_API_TOKEN")
    password = env.get("CERULEAN_ADMIN_PASSWORD") or read("CERULEAN_ADMIN_PASSWORD")

    try:
        api = cerulean_api.connect_client(base, insecure, token=token, password=password)
    except SystemExit as error:
        # The client refuses a malformed key by exiting — and it exits the same way
        # when the login itself could not be sent, which is the unreachable case.
        return "unreachable" if unreachable(error) else "unknown"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return "unreachable"

    api.zone = zone
    status, _payload = api.call("/api/domains")
    # `Api.call` answers `(0, reason)` when nothing answered at all, which is the one
    # failure that is the edge's and not this name's.
    if status == 0:
        return "unreachable"
    if status != 200:
        return "unknown"

    try:
        hosts = cerulean_api.list_proxy_hosts(api)
    except SystemExit:
        return "unknown"
    return "registered" if cerulean_api.find_proxy_host(hosts, fqdn) is not None else "missing"


class Outcome(NamedTuple):
    """The verdict, and the one command that changes it."""

    code: int
    verdict: str
    detail: str
    retry: str | None


def classify(served: bool, app: str, edge: str, slug: str, preview: bool) -> Outcome:
    """The verdict from the two halves. Pure, so every branch can be asserted.

    Written as a table rather than as nested conditions because the interesting part
    is which combinations are *not* the same problem: an app that is up with a name
    missing is one command away, while an app that is up with its name registered is a
    fault between them that no retry of the delivery will fix.
    """
    if served:
        return Outcome(0, "served", "the name serves", None)

    if not slug:
        # A name outside the publishing suffix. There is no app behind it to look up,
        # so the only honest thing to report is that it is not one of ours — and this
        # branch exists so such a name is not reported as a stopped application.
        return Outcome(
            1,
            "not-a-published-name",
            f"the name does not answer, and it is not under this deployment's publishing "
            f"suffix, so there is no app here to look up (edge: {edge})",
            None,
        )

    re_register = f"make edge-preview SLUG={slug}" if preview else f"make edge-publish SLUG={slug}"

    if app == "running":
        if edge == "missing":
            return Outcome(
                3,
                "name-missing",
                "the app is running; its name is not registered at the edge, so nothing "
                "routes to it",
                re_register,
            )
        if edge == "unreachable":
            return Outcome(
                3,
                "edge-unreachable",
                "the app is running, and Cerulean did not answer, so the name could not "
                "be registered — nothing was rebuilt, and nothing needs to be",
                re_register,
            )
        if edge == "registered":
            return Outcome(
                1,
                "registered-but-not-serving",
                "the app is running and the name is registered, so the fault is between "
                "them: the generated vhost on the sites edge, or the edge's route to "
                "this host",
                None,
            )
        return Outcome(
            1,
            "not-serving",
            "the app is running but the name does not answer, and the edge could not be "
            "read to say whether it is registered",
            None,
        )

    if app == "stopped":
        return Outcome(
            1,
            "app-stopped",
            "the name does not answer and the container is not running — this needs the "
            "project run again, not just its name",
            f"make app-publish SLUG={slug}" if not preview else None,
        )

    if app == "unknown":
        return Outcome(
            1,
            "app-unknown",
            "the name does not answer and Docker could not be asked whether the "
            "container is up",
            None,
        )

    return Outcome(
        1,
        "app-absent",
        "nothing has been delivered under this slug — no runtime record exists",
        None,
    )


def evidence_path(root: Path | None, fqdn: str) -> Path | None:
    if root is None:
        return None
    return root / "evidence" / f"{fqdn}.json"


def read_evidence(root: Path | None, fqdn: str) -> dict | None:
    path = evidence_path(root, fqdn)
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_evidence(root: Path | None, fqdn: str, payload: dict) -> Path | None:
    """Record the check, atomically, replacing the previous one for this name.

    Replaced rather than appended: this answers "what is true now", and a history of
    verdicts nobody reads is a directory that grows on every build. The count of
    checks is kept instead, because "how many times has this been looked at" is the
    question an operator actually asks of it.
    """
    path = evidence_path(root, fqdn)
    if path is None:
        return None
    previous = read_evidence(root, fqdn) or {}
    try:
        checks = int(previous.get("checks") or 0) + 1
    except (TypeError, ValueError):
        checks = 1
    payload = {**payload, "checks": checks, "first_checked_at": previous.get("first_checked_at") or payload.get("checked_at")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        print(f"delivery-evidence: could not record {path}: {error}", file=sys.stderr)
        return None
    return path


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utf8_stderr() -> None:  # pragma: no cover - cosmetic
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def evaluate(
    fqdn: str,
    slug: str,
    preview: bool,
    env_path: Path,
    env: dict[str, str] | None = None,
    *,
    timeout: float = 25.0,
    insecure: bool = False,
    root: Path | None = None,
) -> tuple[dict, Outcome]:
    """Check a name and decide what its answer means. The whole script is this call.

    Exposed as a function because two callers need the verdict, not the CLI:
    `make site-evidence` (through `main` below) and `scripts/studio-sites.py`, which
    runs it immediately after registering a name — so the registration's report and
    the panel's evidence are the same reading of the same name.
    """
    settings = env if env is not None else load_env_file(env_path)
    site_check = load_script("site_check", "site-check.py")
    check = site_check.check_host(fqdn, env_path, timeout)
    served = bool(check.get("ok"))

    if root is None:
        root = apps_root(settings)
    if served:
        # The edge state is not probed when the name already answers: "registered" is
        # what was just demonstrated, and asking Cerulean would make a passing check
        # depend on a service the answer does not need.
        app, edge, record = "unknown", "registered", app_record(root, slug)
    elif slug:
        app, record = app_state(root, slug)
        # The edge is only asked when the app is up: for a stopped app the answer
        # changes nothing about what has to happen next.
        edge = edge_state(env_path, settings, fqdn, insecure) if app == "running" else "unknown"
    else:
        # A name outside the publishing suffix. Nothing to look up locally, but the
        # edge can still say whether the name is registered — which is the useful half.
        app, record = "unknown", None
        edge = edge_state(env_path, settings, fqdn, insecure)

    outcome = classify(served, app, edge, slug, preview)
    payload = {
        "v": EVIDENCE_VERSION,
        "checked_at": now_stamp(),
        "host": fqdn,
        "slug": slug,
        "preview": preview,
        "served": served,
        "verdict": outcome.verdict,
        "detail": outcome.detail,
        "retry": outcome.retry,
        "app": {
            "state": app,
            "port": (record or {}).get("port"),
            "container": (record or {}).get("container"),
        },
        "edge": edge,
        # The check's own account, kept whole: the note explains *which gate* answered
        # for a name behind the identity provider, and the error is what went wrong
        # when nothing did.
        "check": {
            "note": check.get("note") or "",
            "error": check.get("error"),
            "code": check.get("code"),
            "final": check.get("final"),
        },
    }
    return payload, outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether a published (or preview) name actually serves.",
        epilog="The argument is a slug or a name; a slug becomes <slug>.<SITE_HOST_SUFFIX>.",
    )
    parser.add_argument("target", nargs="?", default="", help="a slug, or a full name to check")
    parser.add_argument("--slug", default="", help="the app's slug (same as the positional)")
    parser.add_argument("--host", default="", help="the name to check (same as the positional)")
    parser.add_argument("--preview", action="store_true", help="check <slug>-preview.<suffix> instead")
    parser.add_argument("--suffix", default="", help="override SITE_HOST_SUFFIX")
    parser.add_argument("--env-file", default="", help="where this deployment is configured (default .env)")
    parser.add_argument("--timeout", type=float, default=25.0, help="seconds to wait for the name")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification on the Cerulean API")
    parser.add_argument("--record", action="store_true", help="write the verdict under <apps root>/evidence")
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    utf8_stderr()

    env_path = Path(args.env_file) if args.env_file else REPO_ROOT / ".env"
    env = load_env_file(env_path)
    suffix = (args.suffix or env.get("SITE_HOST_SUFFIX") or DEFAULT_SUFFIX).strip()

    target = (args.host or args.slug or args.target).strip()
    fqdn, slug, preview = resolve_target(target, suffix, args.preview)
    if "." not in fqdn:
        fail(f"{fqdn!r} is not a name to check", 2)

    root = apps_root(env)
    payload, outcome = evaluate(
        fqdn,
        slug,
        preview,
        env_path,
        env,
        timeout=args.timeout,
        insecure=args.insecure,
        root=root,
    )
    check = payload["check"]

    if args.record:
        write_evidence(root, fqdn, payload)

    if args.json:
        print(json.dumps(payload, indent=2))
        return outcome.code

    if outcome.code == 0:
        print(f"delivery: ok — https://{fqdn}/ {check.get('note') or 'serves'}")
    else:
        print(f"delivery: {outcome.verdict} — https://{fqdn}/ did not serve.", file=sys.stderr)
        print(f"  {outcome.detail}", file=sys.stderr)
        if check.get("error"):
            print(f"  the check said: {check['error']}", file=sys.stderr)
        if outcome.retry:
            print(f"  fix: {outcome.retry}", file=sys.stderr)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())

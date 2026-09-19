#!/usr/bin/env python3
"""Restore an OmniRoute gateway's provider connections from a local data dir.

WHY THIS EXISTS. OmniRoute keeps provider connections inside the data dir it was
started with. Swap that data dir — a container recreated on a fresh volume, or a
move from a host-run server to a container — and the gateway comes back with
*zero* connections. It still answers inference on its free/no-auth providers, so
nothing looks broken, but `auto/coding` now has no credentialed provider to pick:
the combo's last-known-good pins to a provider with no credentials and every
turn after the first answers

    503  No credentials for opencode

which is exactly the failure that made multi-turn app builds impossible.

The credentials themselves are not lost — they are still in the old data dir,
encrypted with *that* dir's STORAGE_ENCRYPTION_KEY. `omniroute auth export`
decrypts them locally, and this script copies them into the gateway that is
actually serving traffic.

    scripts/omniroute-restore-providers.py --dry-run          # what would be added
    scripts/omniroute-restore-providers.py                    # add the missing ones
    scripts/omniroute-restore-providers.py --replace          # also re-write existing

Authentication against the target gateway is either a management-scoped API key
(`--api-key`) or the dashboard password (`--password` /
`OMNIROUTE_DASHBOARD_PASSWORD`). The password login is the route that works for
a stock container, where the machine-derived CLI token is unavailable because a
container has no /etc/machine-id.

Only API-key connections can be copied: OAuth connections (github et al.) need
their own `omniroute providers auth <provider>` flow against the target, and free
providers need no credential at all. Both are reported as skipped, not silently
dropped.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_SOURCE = "/root/.omniroute"
DEFAULT_TARGET = os.environ.get("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1")
REQUEST_TIMEOUT = 30

# Where a Node user-install actually puts the CLI. A systemd timer runs with the
# minimal PATH (`/usr/local/sbin:...:/bin`), which does not include nvm, volta or
# `~/.local/bin` — so `omniroute` is on PATH for the operator who runs the backup
# by hand and invisible to the timer that is supposed to run it daily. That is
# not a cosmetic difference: the export is the half of the backup that reads the
# connections, so the timer's copy failed every night while the manual one
# worked, and "the backup is current" was only ever true right after someone ran
# it. These globs are searched after PATH so an explicit install always wins.
CLI_FALLBACK_GLOBS = (
    "~/.nvm/versions/node/*/bin/omniroute",
    "~/.volta/bin/omniroute",
    "~/.local/bin/omniroute",
    "/usr/local/bin/omniroute",
    "/opt/homebrew/bin/omniroute",
)


class GatewayError(RuntimeError):
    """A gateway call failed; the message is safe to show the operator."""


def find_cli() -> str:
    """Locate the OmniRoute CLI, or raise with the ways to point at it.

    `OMNIROUTE_BIN` wins (an explicit statement about this host), then PATH, then
    the user-install locations above. Newest nvm version first: an old node in the
    list is still runnable but not the one anyone means.
    """
    override = (os.environ.get("OMNIROUTE_BIN") or "").strip()
    if override:
        if os.access(override, os.X_OK) and os.path.isfile(override):
            return override
        raise GatewayError(f"OMNIROUTE_BIN is set to {override}, which is not an executable file")

    found = shutil.which("omniroute")
    if found:
        return found

    for pattern in CLI_FALLBACK_GLOBS:
        matches = sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
        for candidate in matches:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    raise GatewayError(
        "`omniroute` is not on PATH and not in the usual user-install locations "
        "(nvm/volta/~/.local/bin). Install the OmniRoute CLI, set OMNIROUTE_BIN to "
        "its full path, or pass --creds FILE."
    )


def normalise_base_url(url: str) -> str:
    """The management API lives at the gateway root, not under the /v1 prefix."""
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


def call(
    method: str,
    url: str,
    body: dict | None = None,
    *,
    cookie: str | None = None,
    bearer: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[dict | None, dict]:
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("accept", "application/json")
    if payload is not None:
        request.add_header("content-type", "application/json")
    if cookie:
        request.add_header("cookie", cookie)
    if bearer:
        request.add_header("authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            headers = response.headers
    except urllib.error.HTTPError as error:
        raw = error.read()
        headers = error.headers
        detail = raw.decode("utf-8", "replace").strip() or error.reason
        try:
            parsed = json.loads(raw)
            detail = parsed.get("error", {}).get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise GatewayError(f"{method} {url} -> HTTP {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise GatewayError(f"{method} {url} -> {error.reason}") from None
    if not raw:
        return None, headers
    try:
        return json.loads(raw), headers
    except ValueError:
        raise GatewayError(f"{method} {url} -> non-JSON response") from None


def classify(entry: dict) -> tuple[str, str | None]:
    """What can be copied from one exported connection.

    Returns (kind, credential): `apikey` with the secret, or `oauth`/`none` with
    None. Only an API key can be replayed — an OAuth connection needs its own
    flow on the target, and a free provider needs no credential at all — so the
    caller reports both kinds as skipped rather than importing something that
    would look configured and fail at the first request.
    """
    credential = entry.get("apiKey") or entry.get("credential")
    if credential:
        return "apikey", str(credential)
    if str(entry.get("authType") or entry.get("auth_type") or "").lower() == "oauth":
        return "oauth", None
    return "none", None


def export_credentials(source: str, creds_file: str | None) -> list[dict]:
    """Return the source data dir's connections with credentials decrypted."""
    if creds_file:
        with open(creds_file, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        entries = parsed if isinstance(parsed, list) else parsed.get("connections", [])
        return [entry for entry in entries if isinstance(entry, dict)]

    binary = find_cli()
    if not os.path.isdir(source):
        raise GatewayError(f"source data dir not found: {source}")

    handle, path = tempfile.mkstemp(prefix="omniroute-creds-", suffix=".json")
    os.close(handle)
    os.chmod(path, 0o600)
    try:
        env = dict(os.environ, DATA_DIR=source)
        env.pop("OMNIROUTE_BASE_URL", None)
        result = subprocess.run(
            [binary, "auth", "export", "--format", "json", "--out", path, "--force"],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GatewayError(
                f"`omniroute auth export` failed ({result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[:400]}"
            )
        with open(path, "r", encoding="utf-8") as exported:
            parsed = json.load(exported)
        entries = parsed if isinstance(parsed, list) else parsed.get("connections", [])
        return [entry for entry in entries if isinstance(entry, dict)]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def login(target: str, password: str | None) -> str:
    if not password:
        raise GatewayError(
            "no credential for the target gateway; pass --api-key or --password "
            "(or set OMNIROUTE_DASHBOARD_PASSWORD)"
        )
    body, headers = call("POST", f"{target}/api/auth/login", {"password": password})
    if isinstance(body, dict) and body.get("success") is False:
        raise GatewayError("dashboard login rejected the password")
    # HTTPMessage.get is case-insensitive; a plain dict would miss `set-cookie`.
    cookie = headers.get("Set-Cookie", "") or ""
    token = cookie.split(";", 1)[0].strip()
    if not token:
        raise GatewayError("dashboard login returned no session cookie")
    return token


def list_connections(target: str, auth_headers: dict) -> list[dict]:
    body, _ = call("GET", f"{target}/api/providers?limit=5000", **auth_headers)
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in ("connections", "providers", "data"):
            rows = body.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    raise GatewayError("unexpected response shape from GET /api/providers")


def clear_lkgp(target: str, auth_headers: dict) -> bool:
    """Drop the combo pin, so `auto/*` re-selects with the new providers.

    The pin is "last known good": the combo remembers the provider that last
    answered, and before this restore that provider was a credential-less free
    one. It survives adding connections, so without this the gateway keeps
    routing to the provider that 503s and the import looks like it did nothing.
    """
    body, _ = call("DELETE", f"{target}/api/settings/lkgp-cache", **auth_headers)
    return bool(isinstance(body, dict) and body.get("cleared"))


def auth_for(target: str, api_key: str | None, password: str | None) -> dict:
    """Prefer an explicit management key; fall back to a dashboard session.

    Returns kwargs for `call`, so a wrong credential fails here — before any
    write — rather than halfway through an import.
    """
    if api_key:
        try:
            call("GET", f"{target}/api/providers?limit=1", bearer=api_key)
        except GatewayError as error:
            message = str(error)
            if "HTTP 401" in message or "HTTP 403" in message:
                raise GatewayError(
                    "the API key was rejected as a management token"
                ) from None
            raise
        return {"bearer": api_key}
    return {"cookie": login(target, password)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"OmniRoute data dir to read credentials from (default {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help="gateway to write to (default $OMNIROUTE_BASE_URL)",
    )
    parser.add_argument("--creds", help="use an existing `auth export` JSON instead of exporting")
    parser.add_argument("--api-key", help="management-scoped key for the target gateway")
    parser.add_argument(
        "--password",
        default=os.environ.get("OMNIROUTE_DASHBOARD_PASSWORD"),
        help="dashboard password for the target gateway (or OMNIROUTE_DASHBOARD_PASSWORD)",
    )
    parser.add_argument("--replace", action="store_true", help="re-add connections that exist")
    parser.add_argument(
        "--keep-lkgp",
        action="store_true",
        help="leave the combo pin alone (by default it is cleared once providers are added)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args()

    target = normalise_base_url(args.target)
    try:
        entries = export_credentials(args.source, args.creds)
        auth = auth_for(target, args.api_key, args.password)
    except GatewayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        existing = list_connections(target, auth)
    except GatewayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    present = {
        (str(row.get("provider") or ""), str(row.get("name") or ""))
        for row in existing
    }

    added: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for entry in entries:
        provider = str(entry.get("provider") or "").strip()
        name = str(entry.get("name") or provider).strip()
        if not provider:
            continue
        kind, credential = classify(entry)
        if kind != "apikey":
            skipped.append(
                {
                    "provider": provider,
                    "name": name,
                    "reason": "oauth (needs its own auth flow)" if kind == "oauth" else "no credential",
                }
            )
            continue
        if (provider, name) in present and not args.replace:
            skipped.append({"provider": provider, "name": name, "reason": "already present"})
            continue
        if args.dry_run:
            added.append(f"{provider}/{name} (dry-run)")
            continue
        body = {"provider": provider, "name": name, "apiKey": credential}
        try:
            call("POST", f"{target}/api/providers", body, **auth)
        except GatewayError as error:
            failed.append({"provider": provider, "name": name, "error": str(error)})
            continue
        added.append(f"{provider}/{name}")

    summary = {
        "target": target,
        "source": args.source,
        "existing": len(existing),
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "lkgp_cleared": False,
    }

    if added and not args.dry_run and not args.keep_lkgp and not failed:
        try:
            summary["lkgp_cleared"] = clear_lkgp(target, auth)
        except GatewayError as error:
            print(f"warning: could not clear the combo pin: {error}", file=sys.stderr)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"gateway   {target}")
        print(f"source    {args.source} ({len(entries)} connection(s))")
        print(f"existing  {len(existing)} connection(s) before this run")
        for item in added:
            print(f"  + {item}")
        for item in skipped:
            print(f"  = {item['provider']}/{item['name']} ({item['reason']})")
        for item in failed:
            print(f"  ! {item['provider']}/{item['name']}: {item['error']}", file=sys.stderr)
        if summary["lkgp_cleared"]:
            print("combo pin cleared — auto/* will re-select on the next request")
        elif args.dry_run:
            print("dry run — nothing written")

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

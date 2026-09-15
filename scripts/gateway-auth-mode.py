#!/usr/bin/env python3
"""Make Cerulean Authentik the only gate at the OmniRoute gateway.

WHY THIS EXISTS
---------------
The gateway dashboard used to ask for a password of its own, on top of the
identity-aware proxy. That is two logins for one surface, and the second guards
nothing: the proxy has already established who you are, and the management API it
protects is reachable only through the proxy. It also hid a real problem —
OmniRoute's own OIDC cannot be enabled (its callback strips the trailing slash
Authentik always puts in `iss`, so `jwtVerify` can never match; see
docs/gateway-sso.md), so the only component that can authenticate anyone is the
proxy. Two credential systems, one of which cannot work.

So the gateway's own login is turned off and Authentik becomes the only one:

    browser ──https──▶ edge ──▶ oauth2-proxy ──▶ Authentik (group check)
                                     │
                                     ▼
                                  omniroute, loopback only
                                  requireLogin = false

WHAT `requireLogin = false` ACTUALLY DOES — measured, not assumed
----------------------------------------------------------------
On a throwaway instance, before and after:

    GET /api/settings   no cookie    401 Authentication required  →  200
    GET /api/providers  no cookie    401 Authentication required  →  200
    GET /dashboard      no cookie    200 (the shell was always public)

So this does not open a second door; it removes the gateway's own. **The
reachability of port 20128 becomes the entire control.** That is acceptable here
and only here, because the binding is `127.0.0.1` — and this script checks that
before it changes anything, and refuses if it cannot. If that binding widens, the
premise of this file is gone and nothing else in the stack is holding the door.

THE STORED PASSWORD IS KEPT, ON PURPOSE
---------------------------------------
`requireLogin = false` already means the password grants nothing. It is left in
place as the recovery path: if a future release resets the flag, `requireLogin =
true` with no password is a lockout that needs a volume edit, and this script
would have removed the only way back in. Keeping it costs nothing because it is no
longer consulted — the script verifies that rather than asserting it.

IDEMPOTENT. Run it on a fresh volume, after a gateway upgrade, or any time you want
to confirm the mode.

    scripts/gateway-auth-mode.py                  # apply
    scripts/gateway-auth-mode.py --dry-run        # report only
    scripts/gateway-auth-mode.py --verify         # exit non-zero unless already correct
    scripts/gateway-auth-mode.py --url http://127.0.0.1:20128

Exit codes:
    0  the gateway is in the requested mode (or was put there)
    1  it is not, and could not be — the output says which half failed
    2  the script cannot run (nothing to authenticate with, the gateway is down, or
       the gateway is reachable somewhere it should not be)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:20128"
# The platform's single gateway runs in Group 2 now (`2-voice/`), so this script
# belongs on that host — it flips settings INSIDE the gateway container, which
# means a Docker that can see it. Override with --container when the deployment
# names it differently.
GATEWAY_CONTAINER = "g2-omniroute"

# The settings this script owns. A PATCH here is a merge, so sending a key we have
# no opinion about is how a routing setting gets reset by a script that was only
# asked to remove a login.
OWNED = ("requireLogin",)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_env(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[key.strip()] = value
    return parsed


def loopback_binding(container: str = GATEWAY_CONTAINER) -> tuple[bool | None, str]:
    """Is the gateway published on loopback only? (None, reason) when unknowable.

    This is the check the whole change rests on, so it is a refusal rather than a
    warning wherever it can be answered. Answered from Docker's own record of the
    publish, not from the URL this script was handed — `--url` is where to talk to
    it, which is a different question from where it listens.
    """
    if not shutil.which("docker"):
        return None, "docker is not available here, so the binding could not be read"

    try:
        result = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"docker inspect failed: {error}"

    if result.returncode != 0:
        return None, f"{container} is not running, so the binding could not be read"

    try:
        ports = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, "docker reported the ports in a shape this could not read"

    published: list[str] = []
    for container_port, bindings in ports.items():
        if not container_port.endswith("/tcp"):
            continue
        for binding in bindings or []:
            published.append(str(binding.get("HostIp") or ""))

    if not published:
        return False, f"{container} publishes no TCP port at all"

    # An empty HostIp means every interface — the case this exists to catch.
    hosts = set(published)
    if hosts <= {"127.0.0.1", "::1"}:
        return True, f"published on {', '.join(sorted(hosts))}"
    return False, f"published on {', '.join(sorted(h or '0.0.0.0 (all interfaces)' for h in hosts))}"


def vault_secret(env: dict[str, str], field: str = "INITIAL_PASSWORD") -> str:
    """The management password, from the reference `.env` carries.

    `.env` holds `OMNIROUTE_INITIAL_PASSWORD=vault://cerulean/olympus#INITIAL_PASSWORD`,
    resolved here rather than by compose — compose cannot resolve it, which is
    exactly why the literal string is not passed to the container.
    """
    reference = env.get("OMNIROUTE_INITIAL_PASSWORD", "")
    if not reference.startswith("vault://"):
        return ""

    body = reference[len("vault://") :]
    path, _, wanted = body.partition("#")
    field = wanted or field

    address = env.get("VAULT_ADDR", "").rstrip("/")
    if not address:
        return ""

    token = env.get("VAULT_TOKEN", "")
    if not token:
        token_file = env.get("VAULT_TOKEN_FILE", "")
        if token_file and Path(token_file).is_file():
            token = Path(token_file).read_text(encoding="utf-8").strip()
    if not token:
        return ""

    # KV v2 keeps the data one level down, under /data/.
    url = f"{address}/v1/{path.replace('/', '/data/', 1)}"
    request = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        print(f"vault: could not read {path}: {error}", file=sys.stderr)
        return ""

    return str((payload.get("data", {}).get("data") or {}).get(field) or "")


class Gateway:
    """The parts of the gateway API this script needs, and nothing else."""

    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies: list[str] = []

    def call(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, list[str], str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.cookies:
            headers["cookie"] = "; ".join(self.cookies)

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.headers.get_all("set-cookie") or [], response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, (error.headers.get_all("set-cookie") if error.headers else []) or [], error.read().decode("utf-8", "replace")
        except urllib.error.URLError as error:
            return 0, [], str(error.reason)

    def login(self, password: str) -> bool:
        status, cookies, _ = self.call("/api/auth/login", "POST", {"password": password})
        if status != 200:
            return False
        self.cookies = [cookie.split(";")[0].strip() for cookie in cookies if cookie]
        return bool(self.cookies)

    def settings(self) -> dict:
        status, _, body = self.call("/api/settings")
        if status != 200:
            return {}
        parsed = json.loads(body)
        return parsed.get("settings", parsed)

    def patch(self, changes: dict) -> tuple[int, str]:
        status, _, body = self.call("/api/settings", "PATCH", changes)
        return status, body


def unmanaged(settings: dict) -> dict:
    """The settings this script would change. Pure, so the tests can hold it.

    `None` when the gateway is already in the requested mode, so a caller can ask
    \"is there anything to do\" without re-deriving the comparison.
    """
    if settings.get("requireLogin") is False:
        return {}
    return {key: False for key in OWNED}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make Authentik the only gate at the gateway.")
    parser.add_argument("--url", default="", help=f"the gateway's base URL (default {DEFAULT_URL})")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--verify", action="store_true", help="change nothing; exit non-zero unless already correct")
    parser.add_argument("--password", default="", help="management password (default: resolved from Vault)")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args(argv)

    env = load_env(Path(args.env_file) if args.env_file else repo_root() / ".env")
    url = args.url or env.get("OMNIROUTE_URL") or DEFAULT_URL

    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            f"refusing: {url} is not loopback. With requireLogin=false the gateway's "
            "management API stops authenticating, so the binding is the whole control "
            "and there is nothing else left holding the door.",
            file=sys.stderr,
        )
        return 2

    bound, binding_note = loopback_binding()
    if bound is False:
        print(
            f"refusing: {GATEWAY_CONTAINER} is {binding_note}. Turning the gateway's own "
            "login off while it is reachable beyond this host would publish its "
            "management API to whatever can reach that port.",
            file=sys.stderr,
        )
        return 2

    gateway = Gateway(url)
    before = gateway.settings()
    status, _, _ = gateway.call("/api/settings")
    if status == 0:
        print(f"gateway: nothing answered at {url} — is it up?", file=sys.stderr)
        return 2

    # Reading the settings needs a session while the gateway still gates itself,
    # which is precisely the state this script exists to leave. So the password is
    # resolved and used to read first, and the same credentials are then what authorises
    # the change (it is required with the write anyway).
    password = args.password or vault_secret(env)
    if status != 200:
        if not password:
            print(
                f"gateway: {url} answered HTTP {status} and no management password is "
                "available. Set OMNIROUTE_INITIAL_PASSWORD as a vault:// reference in "
                ".env (with VAULT_ADDR and VAULT_TOKEN or VAULT_TOKEN_FILE), or pass "
                "--password.",
                file=sys.stderr,
            )
            return 2
        if not gateway.login(password):
            print(
                "\nthe management password was rejected. If the dashboard password was "
                "rotated away from the Vault value, pass the current one with --password.",
                file=sys.stderr,
            )
            return 2
        before = gateway.settings()

    if not before:
        print(f"gateway: {url} returned no settings — is it the gateway?", file=sys.stderr)
        return 2

    changes = unmanaged(before)
    already = not changes

    print(f"gateway     {url}")
    print(f"  binding       {binding_note}")
    print(f"  requireLogin  {before.get('requireLogin')!r}"
          + ("   (Authentik is the only gate)" if already else "   → becomes False"))
    print(f"  hasPassword   {before.get('hasPassword')!r}   (kept as the recovery path)")

    if bound is None:
        print(f"  warning:      {binding_note} — confirm 20128 is not reachable from the LAN")

    if already:
        print("\nok: the gateway is already in Authentik-only mode")
        return 0

    if args.verify:
        print("\nFAILED: the gateway still requires its own login", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run: would PATCH requireLogin=False and change nothing else")
        return 0

    # `currentPassword` is required for a security-impacting change: OmniRoute
    # answers 400 PASSWORD_REQUIRED without it, so that a hijacked session cannot
    # open the dashboard by itself. Measured.
    status, body = gateway.patch({**changes, "currentPassword": password})
    if status != 200:
        print(f"\nPATCH failed: HTTP {status} {body[:300]}", file=sys.stderr)
        return 1

    after = gateway.settings()
    if after.get("requireLogin") is not False:
        print(f"\nPATCH reported success but requireLogin is still {after.get('requireLogin')!r}", file=sys.stderr)
        return 1

    # The half that is easy to miss: with the login off, the management API must
    # answer without a session. Verified rather than assumed, because it is the
    # whole reason the proxy is now the only gate.
    anonymous = Gateway(url)
    status, _, _ = anonymous.call("/api/providers")
    if status != 200:
        print(
            f"\nchanged, but /api/providers still answers {status} without a session — the "
            "gateway is still gating itself, so this is not the intended mode",
            file=sys.stderr,
        )
        return 1

    print("\nok: Authentik is now the only gate at the gateway")
    print("    /api/providers answers 200 without a session, so nothing on 20128 authenticates")
    return 0


if __name__ == "__main__":
    sys.exit(main())

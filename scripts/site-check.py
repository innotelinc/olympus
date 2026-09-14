#!/usr/bin/env python3
"""Does a published name answer — and if not, which gate stopped it?

WHY THIS IS A SCRIPT AND NOT A ONE-LINER ANY MORE. `make site-check` asked one
question, `curl -o /dev/null -w %{http_code}` on `https://HOST/`, and accepted only
`200`. That is right for a published site and wrong for every name that is behind the
identity provider, which on this deployment is two of the six things it is pointed at:

    https://studio.olympus.innotel.us/   307 -> /api/auth/login   (then Authentik)
    https://gateway.olympus.innotel.us/  302 -> Authentik

Both answered exactly as designed and both were reported `FAILED`. A check that
calls a working host broken sends the operator to fix the wrong thing, and the
second time it happens they stop believing the check — so the verdict now follows
the redirect and says where it landed.

WHAT IT WILL NOT DO. It will not accept a redirect as a page. A name whose redirects
end somewhere other than the configured issuer is reported with the host it reached
and fails, because "it redirects somewhere" is not an answer to "does it serve".
And a 200 is still checked for actually being a page: the edge's own error page
answers 200 on some configurations, and that is the difference the original check was
written to catch.

    make site-check HOST=todo-list.studio.olympus.innotel.us
    python3 scripts/site-check.py studio.olympus.innotel.us

Exit codes:
    0  the name answers — a built page, or a redirect to the identity provider
    1  it does not, and the output says what came back instead
    2  nothing to check (no host given, or it is not a hostname)
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_MARKERS = ("<div id=\"root\"", "<script", "<html")
MAX_BYTES = 20000

# Where an auth-gated name is expected to send a browser. Read from .env rather
# than hardcoded: this is the same issuer the stack authenticates against, and a
# deployment that moves its IdP must not have to edit a check.
ISSUER_KEYS = ("OIDC_ISSUER_URL", "GATEWAY_OIDC_ISSUER_URL")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def issuer_hosts(env: dict[str, str]) -> set[str]:
    """The hosts this deployment considers its identity provider."""
    hosts: set[str] = set()
    for key in ISSUER_KEYS:
        host = urllib.parse.urlsplit(env.get(key, "")).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def fetch(url: str, timeout: float) -> dict:
    """One GET, following redirects, and what came back at the end of them."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "olympus-site-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the URL is built here
            body = response.read(MAX_BYTES).decode("utf-8", "replace")
            return {"code": response.status, "final": response.geturl(), "body": body}
    except urllib.error.HTTPError as error:
        # Includes the redirect limit: urllib raises with the last hop in `geturl`.
        body = ""
        try:
            body = error.read(MAX_BYTES).decode("utf-8", "replace")
        except OSError:
            pass
        return {"code": error.code, "final": error.geturl(), "body": body}
    except (urllib.error.URLError, OSError) as error:
        return {"error": str(getattr(error, "reason", error))}


def verdict(result: dict, host: str, issuers: set[str]) -> tuple[bool, str, str | None]:
    """Is this the answer a published name should give? Pure, so it can be asserted.

    Returning a reason rather than a bare boolean is the point of the rewrite: the
    operator needs to be told which gate answered, not that the host is broken.
    """
    if result.get("error"):
        return False, "", f"no answer from https://{host}/: {result['error']}"

    code = result.get("code")
    final = result.get("final") or ""
    final_host = host_of(final)
    asked = host.rstrip(".").lower()

    if final_host and final_host != asked:
        if final_host in issuers:
            return True, f"auth-gated — redirects to {final_host} (the identity provider)", None
        return (
            False,
            "",
            f"https://{host}/ redirects off this name to {final_host} (HTTP {code}), which is not "
            "the configured identity provider — the name is not serving its own page",
        )

    if code == 200:
        body = result.get("body") or ""
        if any(marker in body.lower() for marker in PAGE_MARKERS):
            return True, "serves a built page", None
        return False, "", f"https://{host}/ answered 200 but not with a page"

    if 300 <= int(code or 0) < 400:
        # Ended on the same name, still redirecting: a loop or a gate on a path it
        # never leaves. Say where, because "HTTP 307" alone was the unhelpful part.
        return False, "", f"https://{host}/ still redirecting (HTTP {code}) to {final or 'nowhere named'}"

    return False, "", f"HTTP {code} from https://{host}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Confirm a published name answers.")
    parser.add_argument("host", nargs="?", default="", help="the name to check")
    parser.add_argument("--host", dest="host_flag", default="", help="the name to check (same as the positional)")
    parser.add_argument("--env-file", default="", help="where the issuer is configured (default .env)")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    host = (args.host or args.host_flag).strip()
    if not host:
        print("usage: site-check.py <host>   (or: make site-check HOST=<host>)", file=sys.stderr)
        return 2
    if "." not in host:
        print(f"site-check: {host!r} is not a hostname to check", file=sys.stderr)
        return 2

    env_path = Path(args.env_file) if args.env_file else REPO_ROOT / ".env"
    issuers = issuer_hosts(load_env(env_path))

    result = fetch(f"https://{host}/", args.timeout)
    ok, note, failure = verdict(result, host, issuers)

    if args.json:
        import json

        print(json.dumps({"host": host, "ok": ok, "note": note, "error": failure, **result}, indent=2))
        return 0 if ok else 1

    if ok:
        print(f"site: ok — https://{host}/ {note}")
        if not issuers:
            print(
                f"     note: no issuer in {env_path} — a redirect off this name would have failed "
                "rather than being reported as a gate"
            )
        return 0

    print(f"site: FAILED — {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

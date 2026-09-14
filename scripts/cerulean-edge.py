#!/usr/bin/env python3
"""Publish a public name at the edge: DNS record, certificate, NPM proxy host.

WHY THIS EXISTS. `docs/gateway-sso.md` used to end with three operator steps —
"fill in CERULEAN_ADMIN_PASSWORD, add a proxy host on the NPM edge, publish the
service on the LAN" — because Cerulean's own UI was the only place that knew how
to do them. It is not: Cerulean's API exposes all three, and doing them by hand
once leaves the next deployment to rediscover the wiring. This runs the same
three steps, in the order that works, idempotently.

    DNS record  →  certificate  →  export to NPM  →  proxy host

The order is load-bearing. A certificate validated by HTTP-01 needs the name to
resolve first, and a proxy host with `ssl_forced` needs a certificate to point at.
Re-running is safe: each step reports what is already there rather than
duplicating it, and nothing here deletes.

    make gateway-edge ARGS="--dry-run"     # what it would do
    make gateway-edge                      # do it

Configuration comes from the repo-root `.env` (the same file the stack reads):
CERULEAN_DNS_API_URL, CERULEAN_ADMIN_PASSWORD, CERULEAN_ZONE, plus the name and
the service it fronts. Every value can be overridden on the command line.

THE API CLIENT LIVES ELSEWHERE NOW. Everything from `Api` down is in
`scripts/cerulean_api.py`, because publishing a Studio site needs the same four
steps and, more importantly, the same answer to "which certificate covers this
name" — a wildcard only makes publishing instant if both callers agree on what it
covers. This file keeps its CLI and its behaviour; the names below are re-exported
so existing callers and tests see the same module surface they always did.

Exit codes — distinct because they send you to different places:
    0  the name is published (or would be, under --dry-run)
    1  Cerulean refused or the edge did not converge
    2  the checkout is not configured for this
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Running this as a script puts its directory on sys.path; loading it as a module
# (the unit tests do) does not. Setting it explicitly makes both work, which is
# the whole cost of splitting the client out.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cerulean_api import (  # noqa: E402 - the path insert above is what makes this importable
    DEFAULT_CERT_WAIT_SECONDS,
    DEFAULT_RENEW_DAYS,
    TEMPLATE_PLACEHOLDERS,
    Api,
    certificate_covers,
    ensure_certificate,
    ensure_proxy_host,
    ensure_record,
    find_proxy_host,
    find_zone,
    is_usable,
    list_proxy_hosts,
    load_env_file,
    looks_like_host,
    parse_expiry,
    record_value,
    select_certificate,
    select_record,
    setting,
    unusable_dns_session,
    zone_relative,
)

# The deny rule is written by NPM's own API, not through Cerulean: Cerulean's NPM
# passthrough accepts `advanced_config` and drops it (measured — a 200 that updated
# `modified_on`, with the field still empty on read-back from both sides).
# scripts/npm_api.py carries the measurement and the full-body rule that makes the
# PUT safe.
from npm_api import NpmApi, closed_paths_config  # noqa: E402 - same path insert

# Kept as module-level names because they were before the split and callers
# (including scripts/tests/test_cerulean_edge.py) reach for them here.
__all__ = [
    "Api",
    "DEFAULT_CERT_WAIT_SECONDS",
    "DEFAULT_RENEW_DAYS",
    "TEMPLATE_PLACEHOLDERS",
    "certificate_covers",
    "closed_paths_config",
    "ensure_certificate",
    "ensure_proxy_host",
    "ensure_record",
    "find_proxy_host",
    "find_zone",
    "is_usable",
    "list_proxy_hosts",
    "load_env_file",
    "parse_expiry",
    "record_value",
    "refused_paths",
    "select_certificate",
    "select_record",
    "setting",
    "unusable_dns_session",
    "zone_relative",
]

# The pre-split names, still honoured: `_relative` and `_looks_like_host` were
# private here and are asserted by the tests.
_relative = zone_relative
_looks_like_host = looks_like_host


def deny_paths(env_path: Path, fqdn: str, paths: list[str], insecure: bool, dry_run: bool) -> str:
    """Refuse `paths` on the public name, by writing NPM's own host config.

    `None` from `closed_paths_config` means every path asked for was "/" — nothing to
    write, and saying so is better than an empty PUT.
    """
    snippet = closed_paths_config(paths)
    if not snippet:
        return "nothing to refuse — every path given was \"/\" (that is `sites-down`)"

    env = load_env_file(env_path)
    base = env.get("NPM_BASE_URL", "")
    identity = env.get("NPM_EMAIL", "")
    secret = env.get("NPM_PASSWORD", "")
    if not all((base, identity, secret)):
        sys.exit(
            "--deny-path needs NPM_BASE_URL, NPM_EMAIL and NPM_PASSWORD in "
            f"{env_path}: the rule is written straight to the edge, because Cerulean's "
            "NPM passthrough drops advanced_config (measured; see scripts/npm_api.py)."
        )

    api = NpmApi(base, identity, secret, insecure)
    api.login()
    host = api.proxy_host(fqdn)
    if host is None:
        sys.exit(f"NPM has no proxy host for {fqdn}, so there is nothing to apply the rule to.")
    return api.set_advanced_config(host, snippet, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a public name at the Cerulean/NPM edge.")
    parser.add_argument("--fqdn", default="", help="public name to publish (default GATEWAY_PUBLIC_HOST)")
    parser.add_argument("--zone", default="", help="DNS zone in Cerulean (default CERULEAN_ZONE)")
    parser.add_argument("--record-name", default="", help="record name, zone-relative or FQDN (default: <host minus zone>)")
    parser.add_argument("--record-type", default="CNAME")
    parser.add_argument("--record-value", default="", help="record target (default: the zone apex, matching the other *.olympus names)")
    parser.add_argument("--forward-host", default="", help="service address the edge should reach")
    parser.add_argument("--forward-port", type=int, default=0)
    parser.add_argument("--renew-days", type=int, default=DEFAULT_RENEW_DAYS, help="reuse a certificate only if it lasts this long")
    parser.add_argument("--cert-wait", type=int, default=DEFAULT_CERT_WAIT_SECONDS, help="seconds to wait for issuance")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification on the Cerulean API")
    parser.add_argument(
        "--deny-path",
        action="append",
        default=[],
        metavar="PATH",
        help="refuse PATH on this public name at the edge (repeatable). See closed_paths_config",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    skipped: set[str] = set()

    def read(name: str, fallback: str = "") -> str:
        return setting(env_path, name, fallback, skipped)

    api_base = read("CERULEAN_DNS_API_URL")
    password = read("CERULEAN_ADMIN_PASSWORD")
    zone = args.zone or read("CERULEAN_ZONE")
    fqdn = (args.fqdn or read("GATEWAY_PUBLIC_HOST")).rstrip(".").lower()
    forward_host = args.forward_host or read("GATEWAY_SSO_EDGE_FORWARD_HOST")
    forward_port = args.forward_port or int(read("GATEWAY_SSO_PORT", "20129") or 20129)

    missing = [
        name
        for name, value in (
            ("CERULEAN_DNS_API_URL", api_base),
            ("CERULEAN_ADMIN_PASSWORD", password),
            ("CERULEAN_ZONE", zone),
            ("GATEWAY_PUBLIC_HOST", fqdn),
        )
        if not value
    ]
    if missing:
        sys.exit(
            "Not configured for this: " + ", ".join(missing) + f" — set them in {env_path}.\n"
            "CERULEAN_ADMIN_PASSWORD is the local login for the Cerulean host, not the Authentik token."
        )
    if forward_host and not looks_like_host(forward_host):
        sys.exit(f"--forward-host does not look like an address: {forward_host}")
    if not forward_host:
        sys.exit(
            "No --forward-host, and GATEWAY_SSO_EDGE_FORWARD_HOST is not set in .env.\n"
            "That is the LAN address of the host running the SSO proxy (the Olympic stack's host), "
            "NOT the gateway and NOT the edge."
        )

    record_name = args.record_name or zone_relative(fqdn, zone)
    record_value_target = args.record_value or zone

    api = Api(api_base, args.insecure)
    api.login(password)

    print(f"Cerulean: {api.base}")
    print(f"  zone:     {zone}")
    print(f"  name:     {fqdn}")
    print(f"  forwards: http://{forward_host}:{forward_port}")
    if args.dry_run:
        print("  (dry run — nothing will be written)")

    zone_row = find_zone(api, zone)
    print(f"\n1. DNS record\n   {ensure_record(api, int(zone_row['id']), fqdn, record_name, args.record_type, record_value_target, args.dry_run)}")

    cert_id, report = ensure_certificate(api, fqdn, args.dry_run, args.cert_wait, args.renew_days)
    print(f"\n2. Certificate\n   {report}")

    print(f"\n3. Edge\n   {ensure_proxy_host(api, fqdn, forward_host, forward_port, cert_id, args.dry_run)}")

    # Step 4, separately and through NPM, because step 3 cannot carry it — see the
    # import comment above. Only when asked: passing nothing leaves the host's config
    # exactly as it is, which is what a site publish wants.
    if args.deny_path:
        print(f"\n4. Path refusal\n   {deny_paths(env_path, fqdn, args.deny_path, args.insecure, args.dry_run)}")

    if args.dry_run:
        print("\ndry run — nothing was written")
        return 0

    print(f"\n{fqdn} is published. Verify with:\n  curl -sI https://{fqdn}/ | head -3")
    return 0


if __name__ == "__main__":
    sys.exit(main())

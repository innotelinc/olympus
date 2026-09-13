#!/usr/bin/env python3
"""Put a Studio build on a name under one wildcard, and do it instantly.

WHY A WILDCARD. The first version of this published one name at a time: a DNS
record, a Let's Encrypt certificate (HTTP-01, which needs the record live first),
an export into NPM, then a proxy host. It works, and it takes a minute or more per
site — which is the wrong shape for "publish with a click". Publishing cannot wait
on a certificate that a wildcard could have carried all along.

So there are two operations here and they are deliberately unequal:

    --wildcard     once per deployment. `*.studio.olympus.innotel.us` -> the zone
                   apex, plus one wildcard certificate that covers every name
                   under it. Slow, and it happens once.
    --publish      per build. Adds a proxy host pointing at whatever is serving
                   this slug. No DNS, no certificate, no waiting — seconds.
    --preview      the same, on `<slug>-preview.<suffix>`. A preview is framed in
                   the browser, and an https page cannot frame a plain-http one, so
                   a preview needs a name at the edge — just not the project's own.

The wildcard is a *deliberately shared* certificate, and `cerulean_api` prefers a
dedicated one when a name has it, so a site that later needs its own certificate
can have one without this path fighting it.

WHY THE NAME IS DERIVED, NOT TYPED. The operator names an app in Studio; the
hostname is `<slug>.<suffix>`, where the slug is the same one the build directory
uses. A free-form name would be a second namespace to keep in sync with the first,
and the requests that go wrong would go wrong silently — pointing at the wrong
site's host.

    scripts/studio-sites.py --wildcard
    scripts/studio-sites.py --publish todo-list --forward-port 20130
    scripts/studio-sites.py --publish weight-tracker --forward-port 21301 --repoint
    scripts/studio-sites.py --preview weight-tracker
    scripts/studio-sites.py --list
    scripts/studio-sites.py --remove todo-list

Exit codes:
    0  done (or, with --dry-run, would have been)
    1  Cerulean refused, or the wildcard is not usable
    2  the checkout is not configured, or the arguments do not make sense
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cerulean_api import (  # noqa: E402 - the path insert above is what makes this importable
    DEFAULT_CERT_WAIT_SECONDS,
    DEFAULT_RENEW_DAYS,
    Api,
    delete_proxy_host,
    ensure_certificate,
    ensure_proxy_host,
    ensure_record,
    find_proxy_host,
    find_zone,
    list_proxy_hosts,
    looks_like_host,
    select_certificate,
    setting,
    zone_relative,
)

# The suffix becomes a DNS label's parent, so its shape is not cosmetic: a suffix
# with a trailing dot, a scheme, or a path produces names that resolve to nothing
# and a certificate request that fails a minute later.
SUFFIX_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# A DNS label: what a slug has to be to become the leftmost part of a name.
LABEL_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

DEFAULT_SUFFIX = "studio.olympus.innotel.us"


def fail(message: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print(f"studio-sites: {message}", file=sys.stderr)
    raise SystemExit(code)


def normalise_slug(value: str) -> str:
    """A slug that is safe as a DNS label, or a refusal.

    Deliberately uncompromising: a slug is *not* sanitised into shape here. It is
    compared against what it would become, and rejected if they differ, because a
    silent rewrite would publish a name nobody asked for and nothing would say so.
    """
    slug = (value or "").strip().lower()
    if not slug:
        fail("no slug given")
    if slug != value.strip().lower().replace("_", "-") or not LABEL_PATTERN.match(slug):
        fail(
            f"{value!r} cannot be a hostname label. Use lowercase letters, digits and "
            "dashes, at most 63 characters, not starting or ending with a dash."
        )
    return slug


class Config:
    """Everything this script reads from `.env`, resolved once."""

    def __init__(self, env_path: Path) -> None:
        self.env_path = env_path
        skipped: set[str] = set()
        read = lambda name, fallback="": setting(env_path, name, fallback, skipped)  # noqa: E731

        self.api_base = read("CERULEAN_DNS_API_URL")
        self.password = read("CERULEAN_ADMIN_PASSWORD")
        self.zone = read("CERULEAN_ZONE")
        self.suffix = (read("SITE_HOST_SUFFIX", DEFAULT_SUFFIX) or DEFAULT_SUFFIX).rstrip(".").lower()
        self.forward_host = read("SITE_EDGE_FORWARD_HOST")
        self.port = int(read("SITE_PORT", "20130") or 20130)
        self.renew_days = int(read("SITE_CERT_RENEW_DAYS", str(DEFAULT_RENEW_DAYS)) or DEFAULT_RENEW_DAYS)
        self.cert_wait = int(read("SITE_CERT_WAIT_SECONDS", str(DEFAULT_CERT_WAIT_SECONDS)) or DEFAULT_CERT_WAIT_SECONDS)

        if not SUFFIX_PATTERN.match(self.suffix):
            fail(f"SITE_HOST_SITE_SUFFIX is not a domain: {self.suffix!r}")

    def require_cerulean(self) -> None:
        missing = [
            name
            for name, value in (
                ("CERULEAN_DNS_API_URL", self.api_base),
                ("CERULEAN_ADMIN_PASSWORD", self.password),
                ("CERULEAN_ZONE", self.zone),
            )
            if not value
        ]
        if missing:
            fail(
                "Not configured for this: " + ", ".join(missing) + f" — set them in {self.env_path}.\n"
                "CERULEAN_ADMIN_PASSWORD is the Cerulean host's own login, not the Authentik token.",
                2,
            )

    def require_forward(self, override: str) -> str:
        host = override or self.forward_host
        if not host:
            fail(
                "SITE_EDGE_FORWARD_HOST is empty. That is the LAN address of the host "
                "serving the sites (the Olympic stack's host), and without it the edge "
                "would publish a name that answers nothing.",
                2,
            )
        if not looks_like_host(host):
            fail(f"forward host does not look like an address: {host}")
        return host

    @property
    def wildcard(self) -> str:
        return f"*.{self.suffix}"


def hostname_for(config: Config, slug: str) -> str:
    return f"{normalise_slug(slug)}.{config.suffix}"


def preview_hostname_for(config: Config, slug: str) -> str:
    """The name a preview is framed on: `<slug>-preview.<suffix>`.

    Derived here for the same reason a published name is: it becomes a DNS label, and
    a second place that builds one is a second place that can build it wrongly. The
    `-preview` label is also what keeps a preview from being mistaken for a published
    site when someone reads the edge's host list.
    """
    return f"{normalise_slug(slug)}-preview.{config.suffix}"


def connect(config: Config, insecure: bool) -> Api:
    api = Api(config.api_base, insecure)
    api.login(config.password)
    return api


def ensure_wildcard(api: Api, config: Config, dry_run: bool) -> int:
    """DNS record + wildcard certificate. Prints a report; returns the cert id."""
    print(f"Cerulean: {api.base}")
    print(f"  zone:     {config.zone}")
    print(f"  wildcard: {config.wildcard}")
    print(f"  covers:   <name>.{config.suffix}")
    if dry_run:
        print("  (dry run — nothing will be written)")

    zone_row = find_zone(api, config.zone)
    report = ensure_record(
        api,
        int(zone_row["id"]),
        config.wildcard,
        zone_relative(config.wildcard, config.zone),
        "CNAME",
        config.zone,
        dry_run,
    )
    print(f"\n1. DNS record\n   {report}")

    cert_id, cert_report = ensure_certificate(
        api, config.wildcard, dry_run, config.cert_wait, config.renew_days
    )
    print(f"\n2. Wildcard certificate\n   {cert_report}")
    if cert_id is None and not dry_run:
        fail("The wildcard certificate did not issue, so nothing can be published under it.", 1)
    return int(cert_id or 0)


def ensure_wildcard_ready(api: Api, config: Config) -> int:
    """The wildcard must already exist before a publish borrows it.

    Checked rather than created on the fly: creating it during a publish would put
    the slow path (certificate issuance) back in the fast one, which is the whole
    thing this design avoids.
    """
    now_cert_id, report = ensure_certificate(api, config.wildcard, True, 0, config.renew_days)
    if now_cert_id is None:
        fail(
            f"No certificate covers {config.wildcard}. Run `scripts/studio-sites.py --wildcard` once "
            f"({report}).",
            1,
        )
    return now_cert_id


def publish(api: Api, config: Config, args: argparse.Namespace) -> int:
    slug = normalise_slug(args.publish)
    fqdn = hostname_for(config, slug)
    forward_host = config.require_forward(args.forward_host)
    forward_port = args.forward_port or config.port

    cert_id = ensure_wildcard_ready(api, config)
    print(f"Certificate: id {cert_id} covers {fqdn}")

    report = ensure_proxy_host(
        api,
        fqdn,
        forward_host,
        forward_port,
        cert_id,
        args.dry_run,
        repoint=args.repoint,
    )
    print(f"Edge: {report}")

    if args.dry_run:
        print(f"\ndry run — https://{fqdn}/ would serve http://{forward_host}:{forward_port}")
        return 0

    print(f"\nhttps://{fqdn}/  —  Verify with: make site-check HOST={fqdn}")
    return 0


def preview(api: Api, config: Config, args: argparse.Namespace) -> int:
    """Put `<slug>-preview.<suffix>` on the edge, pointing where a publish points.

    A preview has to be reachable from the browser it is shown in: the pane is an
    https document, and an iframe of a plain-http address is blocked as mixed
    content. So it needs a name. What it must *not* need is the project's own name —
    registering that is publishing, which is the decision a preview exists to defer.

    The target is the same service a publish targets, which is why this is the same
    call with a different name and no new machinery: the per-app vhost in
    `olympus-sites` is what routes the preview name to the container's port.

    `repoint` is on here and off for a publish, and the difference is ownership: the
    preview name belongs to this project and always points at the same place, so an
    entry that disagrees is stale rather than somebody else's live site.
    """
    slug = normalise_slug(args.preview)
    fqdn = preview_hostname_for(config, slug)
    forward_host = config.require_forward(args.forward_host)
    forward_port = args.forward_port or config.port

    cert_id = ensure_wildcard_ready(api, config)
    print(f"Certificate: id {cert_id} covers {fqdn}")

    report = ensure_proxy_host(
        api,
        fqdn,
        forward_host,
        forward_port,
        cert_id,
        args.dry_run,
        repoint=True,
    )
    print(f"Edge: {report}")

    if args.dry_run:
        print(f"\ndry run — https://{fqdn}/ would serve http://{forward_host}:{forward_port}")
        return 0

    print(f"\nhttps://{fqdn}/  —  a preview name. Nothing is published under {slug}.{config.suffix}.")
    return 0


def remove(api: Api, config: Config, args: argparse.Namespace) -> int:
    fqdn = hostname_for(config, args.remove)
    hosts = list_proxy_hosts(api)
    host = find_proxy_host(hosts, fqdn)
    if host is None:
        print(f"{fqdn} is not published; nothing to remove.")
        return 0

    print(delete_proxy_host(api, int(host["id"]), args.dry_run))
    print(
        "\nThe DNS name still resolves — it is covered by the wildcard — and the "
        "staged files are still on disk. This removes the site from the edge only."
    )
    return 0


def listing(api: Api, config: Config) -> int:
    hosts = list_proxy_hosts(api)
    ours = [host for host in hosts if host_suffix(host, config)]
    if not ours:
        print(f"No sites are published under {config.suffix}.")
        return 0

    print(f"Published under {config.suffix}:")
    for host in sorted(ours, key=lambda h: str((h.get("domain_names") or [""])[0])):
        names = ", ".join(str(n) for n in (host.get("domain_names") or []))
        target = f"{host.get('forward_scheme')}://{host.get('forward_host')}:{host.get('forward_port')}"
        print(f"  {names:<52} -> {target}  (host #{host.get('id')})")
    return 0


def host_suffix(host: dict, config: Config) -> bool:
    wanted = f".{config.suffix}"
    return any(str(n).rstrip(".").lower().endswith(wanted) for n in (host.get("domain_names") or []))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish Studio builds under one wildcard name.",
        epilog="Names are always <slug>.<SITE_HOST_SUFFIX>.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--wildcard", action="store_true", help="create the wildcard DNS record + certificate (once)")
    action.add_argument("--publish", metavar="SLUG", help="point <slug>.<suffix> at a service")
    action.add_argument(
        "--preview",
        metavar="SLUG",
        help="point <slug>-preview.<suffix> at the same service, for a preview to be framed on",
    )
    action.add_argument("--remove", metavar="SLUG", help="remove a published name from the edge")
    action.add_argument("--list", action="store_true", help="list what is published")

    parser.add_argument("--forward-host", default="", help="service address (default SITE_EDGE_FORWARD_HOST)")
    parser.add_argument("--forward-port", type=int, default=0, help="service port (default SITE_PORT)")
    parser.add_argument("--repoint", action="store_true", help="allow moving an existing name to a new target")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification on the Cerulean API")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    config = Config(env_path)
    config.require_cerulean()

    api = connect(config, args.insecure)

    if args.wildcard:
        ensure_wildcard(api, config, args.dry_run)
        if args.dry_run:
            print("\ndry run — nothing was written")
            return 0
        print(f"\nEvery <name>.{config.suffix} is now covered. Publish one with:")
        print(f"  make site-publish SLUG=<slug>")
        return 0

    if args.publish:
        return publish(api, config, args)
    if args.preview:
        return preview(api, config, args)
    if args.remove:
        return remove(api, config, args)
    return listing(api, config)


if __name__ == "__main__":
    sys.exit(main())

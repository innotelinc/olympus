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

Exit codes — distinct because they send you to different places:
    0  the name is published (or would be, under --dry-run)
    1  Cerulean refused or the edge did not converge
    2  the checkout is not configured for this
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CERT_WAIT_SECONDS = 180
DEFAULT_RENEW_DAYS = 30

# Values shipped in .env.example as "put something here". They are not
# credentials, and treating one as a decision is how a stack ends up
# authenticating with the template: measured on this platform, the process
# environment carried `CERULEAN_ADMIN_PASSWORD=change-me-cerulean-admin` from the
# template while `.env` held the real one, and "the environment wins" turned a
# working checkout into an opaque HTTP 401. Same reasoning as the fragment-less
# `vault://` guard in scripts/authentik-studio-app.py: a value that cannot be
# right is skipped and said out loud, rather than preferred.
TEMPLATE_PLACEHOLDERS = {
    "",
    "change-me",
    "changeme",
    "change-password",
    "change-me-admin",
    "change-me-cerulean-admin",
    "admin",
    "password",
    "example",
    "placeholder",
}


def load_env_file(path: Path) -> dict[str, str]:
    """Read `KEY=value` pairs from a .env, skipping comments and blank lines."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def is_usable(value: str) -> bool:
    """Whether a configured value could possibly be a real credential."""
    candidate = value.strip().lower()
    return candidate not in TEMPLATE_PLACEHOLDERS and not candidate.startswith("change-me")


def parse_expiry(value: str) -> datetime | None:
    """Parse the certificate expiry Cerulean reports.

    It renders as `Dec 12 12:24:25 2026 GMT` — a C `asctime` shape, not ISO. The
    zone name is part of the format because `%Z` only matches a literal `GMT`
    here; anything else is treated as unparseable rather than guessed at, since
    the caller's fallback (renew) is cheaper than trusting a wrong date.
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def select_certificate(
    certificates: list[dict],
    fqdn: str,
    now: datetime,
    renew_days: int = DEFAULT_RENEW_DAYS,
) -> dict | None:
    """The existing certificate that can be reused for `fqdn`, if there is one.

    Reuse is only allowed for material that is actually present and comfortably
    inside its validity: an `issued` row with no PEM is the state a failed export
    leaves behind, and pointing a proxy host at it produces a TLS error at the
    edge rather than anything the caller could act on.
    """
    best: dict | None = None
    for cert in certificates:
        names = cert.get("domains") or []
        if isinstance(cert.get("domain"), str):
            names = [*names, cert["domain"]]
        if fqdn not in names:
            continue
        if cert.get("status") != "issued" or not cert.get("hasMaterial"):
            continue
        expires = parse_expiry(str(cert.get("expiresAt") or ""))
        if not expires or expires < now + timedelta(days=renew_days):
            continue
        if best is None or (parse_expiry(str(best.get("expiresAt") or "")) or now) < expires:
            best = cert
    return best


def select_record(records: list[dict], fqdn: str, record_type: str) -> dict | None:
    """The existing record of this type for `fqdn`, matched on the FQDN.

    Technitium answers with fully-qualified names (`gateway.olympus.innotel.us`)
    while the create call takes a zone-relative one, so both sides are compared
    after normalisation rather than assuming which form arrived.
    """
    wanted = fqdn.rstrip(".").lower()
    for record in records:
        if str(record.get("name", "")).rstrip(".").lower() != wanted:
            continue
        if str(record.get("type", "")).upper() != record_type.upper():
            continue
        return record
    return None


def record_value(record: dict) -> str:
    """Technitium spells the payload `rData`; some responses say `value`."""
    return str(record.get("rData") or record.get("value") or "").rstrip(".").lower()


class Api:
    """A thin Cerulean client.

    Every call is one request with a Bearer token, and every failure comes back as
    a `(status, body)` pair so the caller can decide — this script's job is to
    report what Cerulean said, not to translate it.
    """

    def __init__(self, base: str, insecure: bool = False) -> None:
        self.base = base.rstrip("/")
        self.context = ssl._create_unverified_context() if insecure else None
        self.token = ""

    def call(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 60):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)

        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            try:
                parsed = json.loads(detail)
                detail = parsed.get("error") or detail
            except json.JSONDecodeError:
                pass
            return error.code, detail[:300]
        except (urllib.error.URLError, OSError) as error:
            return 0, f"{type(error).__name__}: {getattr(error, 'reason', error)}"

    def login(self, password: str) -> str:
        status, payload = self.call("/api/auth/login", "POST", {"password": password})
        if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
            sys.exit(f"Cerulean login failed (HTTP {status}): {payload}")
        self.token = payload["token"]
        return self.token


def find_zone(api: Api, zone: str) -> dict:
    status, domains = api.call("/api/domains")
    if status != 200 or not isinstance(domains, list):
        sys.exit(f"Could not list zones (HTTP {status}): {domains}")
    for domain in domains:
        if str(domain.get("name", "")).rstrip(".").lower() == zone.rstrip(".").lower():
            return domain
    available = ", ".join(str(d.get("name")) for d in domains) or "none"
    sys.exit(f"Zone {zone} is not registered in Cerulean (has: {available}). Add it under Domains first.")


def ensure_record(
    api: Api,
    zone_id: int,
    fqdn: str,
    record_name: str,
    record_type: str,
    value: str,
    dry_run: bool,
) -> str:
    """Create the record if it is absent. Returns a one-line report.

    `record_name` is what Technitium is asked for (zone-relative is the form its
    API takes); `fqdn` is what the existing records are matched on, because reads
    come back fully qualified.
    """
    status, payload = api.call(f"/api/domains/{zone_id}/records")
    if status != 200:
        sys.exit(f"Could not read zone records (HTTP {status}): {payload}")
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        sys.exit(f"Unexpected record list from Cerulean: {str(payload)[:200]}")

    existing = select_record(records, fqdn, record_type)
    if existing is not None:
        found = record_value(existing)
        if found != value.lower():
            # Silently repointing a name that already answers is how one service
            # takes another's traffic. Refuse and let a human decide.
            sys.exit(
                f"{fqdn} already has {record_type} -> {found}, not {value}. "
                "Refusing to change it; remove the record first if it is wrong."
            )
        return f"{fqdn} {record_type} already -> {found}"

    if dry_run:
        return f"would create {fqdn} {record_type} -> {value}"

    status, payload = api.call(
        f"/api/domains/{zone_id}/records",
        "POST",
        {"type": record_type, "name": record_name, "value": value, "ttl": 300},
    )
    if status not in (200, 201):
        sys.exit(f"Could not create the record (HTTP {status}): {payload}")
    return f"created {fqdn} {record_type} -> {value}"


def ensure_certificate(api: Api, fqdn: str, dry_run: bool, wait: int, renew_days: int) -> tuple[int | None, str]:
    """Return `(certificate_id, report)` for a certificate covering `fqdn`."""
    now = datetime.now(timezone.utc)
    status, certificates = api.call("/api/certificates")
    if status != 200 or not isinstance(certificates, list):
        sys.exit(f"Could not list certificates (HTTP {status}): {certificates}")

    reusable = select_certificate(certificates, fqdn, now, renew_days)
    if reusable is not None:
        return reusable["id"], f"certificate #{reusable['id']} already covers {fqdn} (expires {reusable.get('expiresAt')})"

    if dry_run:
        return None, f"would request a certificate for {fqdn}"

    status, created = api.call("/api/certificates", "POST", {"domain": fqdn, "name": fqdn})
    if status not in (200, 201, 202) or not isinstance(created, dict) or not created.get("id"):
        sys.exit(f"Could not request a certificate (HTTP {status}): {created}")
    cert_id = created["id"]

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        status, cert = api.call(f"/api/certificates/{cert_id}")
        if status != 200 or not isinstance(cert, dict):
            sys.exit(f"Could not poll certificate #{cert_id} (HTTP {status}): {cert}")
        state = cert.get("status")
        if state == "issued" and cert.get("hasMaterial"):
            return cert_id, f"issued certificate #{cert_id} for {fqdn} (expires {cert.get('expiresAt')})"
        if state in ("failed", "error"):
            sys.exit(f"Certificate #{cert_id} {state}: {cert.get('error')}")
        time.sleep(6)
    sys.exit(
        f"Certificate #{cert_id} is still {state} after {wait}s. "
        "It may still finish — check `GET /api/certificates/" + str(cert_id) + "` before re-running."
    )


def ensure_proxy_host(
    api: Api,
    fqdn: str,
    forward_host: str,
    forward_port: int,
    certificate_id: int | None,
    dry_run: bool,
) -> str:
    status, hosts = api.call("/api/npm/hosts")
    if status != 200 or not isinstance(hosts, list):
        sys.exit(f"Could not list NPM hosts (HTTP {status}): {hosts}")

    for host in hosts:
        names = [str(n).lower() for n in (host.get("domain_names") or [])]
        if fqdn.lower() not in names:
            continue
        target = f"{host.get('forward_scheme')}://{host.get('forward_host')}:{host.get('forward_port')}"
        wanted = f"http://{forward_host}:{forward_port}"
        if target != wanted:
            sys.exit(
                f"NPM already serves {fqdn} -> {target}, not {wanted}. "
                "Refusing to repoint a live name; update it in NPM if it is wrong."
            )
        return f"{fqdn} already -> {target} (host #{host.get('id')})"

    if dry_run:
        return f"would create an NPM proxy host {fqdn} -> http://{forward_host}:{forward_port}"

    if certificate_id is None:
        sys.exit("No certificate to attach — refusing to create a host that would serve plain HTTP.")

    status, exported = api.call("/api/npm/export-cert", "POST", {"certificate_id": certificate_id})
    if status not in (200, 201) or not isinstance(exported, dict) or not exported.get("npmCertificateId"):
        sys.exit(f"Could not export the certificate to NPM (HTTP {status}): {exported}")
    npm_cert = exported["npmCertificateId"]

    status, host = api.call(
        "/api/npm/hosts",
        "POST",
        {
            "domain": fqdn,
            "forward_host": forward_host,
            "forward_port": forward_port,
            "forward_scheme": "http",
            "certificate_id": npm_cert,
            "ssl_forced": True,
            "http2_support": True,
        },
    )
    if status not in (200, 201) or not isinstance(host, dict):
        sys.exit(f"Could not create the proxy host (HTTP {status}): {host}")
    return (
        f"created NPM proxy host #{host.get('id')} {fqdn} -> http://{forward_host}:{forward_port} "
        f"(cert {npm_cert}, TLS enforced)"
    )


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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    file_values = load_env_file(env_path)
    skipped: set[str] = set()

    def setting(name: str, fallback: str = "") -> str:
        """Process env, then the file, then the fallback — placeholders skipped.

        The deviation from "the environment wins" is deliberate and narrow: a
        template placeholder is a typo, not an override, and letting one win
        produces a 401 that says nothing about the cause. Reported once per name
        so the skip is never silent.
        """
        for source, candidate in (
            ("the process environment", os.environ.get(name)),
            (str(env_path), file_values.get(name)),
            ("the default", fallback),
        ):
            if candidate is None:
                continue
            if is_usable(candidate):
                return candidate
            if candidate and name not in skipped:
                skipped.add(name)
                print(
                    f"  note:       ignoring {name} from {source} — it is the .env.example "
                    f"placeholder ({candidate!r}), not a credential",
                    file=sys.stderr,
                )
        return ""

    api_base = setting("CERULEAN_DNS_API_URL")
    password = setting("CERULEAN_ADMIN_PASSWORD")
    zone = args.zone or setting("CERULEAN_ZONE")
    fqdn = (args.fqdn or setting("GATEWAY_PUBLIC_HOST")).rstrip(".").lower()
    forward_host = args.forward_host or setting("GATEWAY_SSO_EDGE_FORWARD_HOST")
    forward_port = args.forward_port or int(setting("GATEWAY_SSO_PORT", "20129") or 20129)

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
    if forward_host and not _looks_like_host(forward_host):
        sys.exit(f"--forward-host does not look like an address: {forward_host}")
    if not forward_host:
        sys.exit(
            "No --forward-host, and GATEWAY_SSO_EDGE_FORWARD_HOST is not set in .env.\n"
            "That is the LAN address of the host running the SSO proxy (the Olympic stack's host), "
            "NOT the gateway and NOT the edge."
        )

    record_name = args.record_name or _relative(fqdn, zone)
    record_value = args.record_value or zone

    api = Api(api_base, args.insecure)
    api.login(password)

    print(f"Cerulean: {api.base}")
    print(f"  zone:     {zone}")
    print(f"  name:     {fqdn}")
    print(f"  forwards: http://{forward_host}:{forward_port}")
    if args.dry_run:
        print("  (dry run — nothing will be written)")

    zone_row = find_zone(api, zone)
    print(f"\n1. DNS record\n   {ensure_record(api, int(zone_row['id']), fqdn, record_name, args.record_type, record_value, args.dry_run)}")

    cert_id, report = ensure_certificate(api, fqdn, args.dry_run, args.cert_wait, args.renew_days)
    print(f"\n2. Certificate\n   {report}")

    print(f"\n3. Edge\n   {ensure_proxy_host(api, fqdn, forward_host, forward_port, cert_id, args.dry_run)}")

    if args.dry_run:
        print("\ndry run — nothing was written")
        return 0

    print(f"\n{fqdn} is published. Verify with:\n  curl -sI https://{fqdn}/ | head -3")
    return 0


def _relative(fqdn: str, zone: str) -> str:
    """`gateway.olympus.innotel.us` in zone `innotel.us` -> `gateway.olympus`."""
    suffix = "." + zone.rstrip(".").lower()
    name = fqdn.rstrip(".").lower()
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _looks_like_host(value: str) -> bool:
    parts = value.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    return bool(value) and all(c.isalnum() or c in "-." for c in value)


if __name__ == "__main__":
    sys.exit(main())

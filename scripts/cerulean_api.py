"""The Cerulean client: DNS records, certificates, and NPM proxy hosts.

WHY THIS IS A MODULE. Two callers now need the same four steps — the gateway's
name (`scripts/cerulean-edge.py`) and every published Studio site
(`scripts/studio-sites.py`). The second caller is the one that made this worth
extracting: publishing a site has to be *instant*, and it only is if it reuses a
wildcard certificate instead of requesting one per name, which means it needs the
same certificate-selection logic the gateway path already had. Two copies of
"which certificate covers this name" would drift, and the failure mode of the
drift is a proxy host pointed at a certificate that does not cover it — a TLS
error at the edge that nothing in either script would explain.

WHAT IT IS NOT. Not a wrapper that hides Cerulean: every call returns
`(status, body)` and every refusal says what Cerulean said. The callers decide
what a failure means; this module decides nothing on their behalf except the two
things that are genuinely invariant — which certificate covers a name, and which
record is the one being asked about.

Cerulean's API surface used here:

    POST /api/auth/login                 -> {token}
    GET  /api/domains                    -> zones
    GET  /api/domains/{id}/records       -> zone records
    POST /api/domains/{id}/records       -> create a record
    GET  /api/certificates               -> issued + pending certificates
    POST /api/certificates               -> request one ({"domain", "name"})
    GET  /api/certificates/{id}          -> poll it
    POST /api/npm/export-cert            -> push PEM material into NPM
    GET  /api/npm/hosts                  -> proxy hosts
    POST /api/npm/hosts                  -> create a proxy host
    DELETE /api/npm/hosts/{id}           -> remove one
"""

from __future__ import annotations

import json
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


def setting(env_path: Path, name: str, fallback: str = "", skipped: set[str] | None = None) -> str:
    """Process env, then the file, then the fallback — placeholders skipped.

    The deviation from "the environment wins" is deliberate and narrow: a template
    placeholder is a typo, not an override, and letting one win produces a 401 that
    says nothing about the cause. Reported once per name so the skip is never
    silent. Shared by every caller so the rule is stated once.
    """
    import os

    file_values = load_env_file(env_path)
    for source, candidate in (
        ("the process environment", os.environ.get(name)),
        (str(env_path), file_values.get(name)),
        ("the default", fallback),
    ):
        if candidate is None:
            continue
        if is_usable(candidate):
            return candidate
        if candidate and skipped is not None and name not in skipped:
            skipped.add(name)
            print(
                f"  note:       ignoring {name} from {source} — it is the .env.example "
                f"placeholder ({candidate!r}), not a credential",
                file=sys.stderr,
            )
    return ""


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


def certificate_covers(names: list[str], fqdn: str) -> bool:
    """Whether one of `names` covers `fqdn`, wildcards included.

    RFC 6125: `*.studio.olympus.innotel.us` covers `abc.studio.olympus.innotel.us`
    and nothing else — not the bare `studio.olympus.innotel.us`, and not
    `a.b.studio.olympus.innotel.us`. Getting this wrong in the permissive
    direction means attaching a certificate to a host it does not cover, which the
    edge serves as a browser warning rather than an error anyone reads.

    This is the function that makes a wildcard worth having: without it, an exact
    match means every published site requests its own certificate and publishing
    stops being instant.
    """
    wanted = fqdn.rstrip(".").lower()
    for raw in names:
        name = str(raw).rstrip(".").lower()
        if not name:
            continue
        if name == wanted:
            return True
        if not name.startswith("*."):
            continue
        if wanted.count(".") != name.count("."):
            continue
        if wanted.endswith(name[1:]):
            return True
    return False


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
    wanted = fqdn.rstrip(".").lower()
    best: tuple[int, datetime] | None = None
    best_cert: dict | None = None

    for cert in certificates:
        names = [str(name) for name in certificate_names(cert)]
        if not certificate_covers(names, fqdn):
            continue
        if cert.get("status") != "issued" or not cert.get("hasMaterial"):
            continue
        expires = parse_expiry(str(cert.get("expiresAt") or ""))
        if not expires or expires < now + timedelta(days=renew_days):
            continue

        # An exact-name certificate beats a wildcard that happens to cover it,
        # and a longer expiry beats a shorter one at the same tier. The tier
        # matters because a shared wildcard is a legitimate fallback but a poor
        # default: it couples this name's certificate to every other name the
        # wildcard serves, so a dedicated certificate is preferred whenever one
        # exists. Ties inside a tier are broken on validity so the choice is not
        # arbitrary across runs.
        exact = 1 if wanted in [str(n).rstrip(".").lower() for n in names] else 0
        rank = (exact, expires)
        if best is None or rank > best:
            best = rank
            best_cert = cert

    return best_cert


def certificate_names(cert: dict) -> list[str]:
    """Every name a certificate row carries, including the wildcard it *is*.

    Cerulean records a wildcard as `domain: <base>` plus `wildcard: true`, and its
    `domains` list is not guaranteed to spell the `*.` out. Reading the flag rather
    than trusting the list is what stops a wildcard certificate from looking like it
    covers only its bare base — which would send every publish back down the slow
    per-name issuance path this exists to avoid.
    """
    names = [str(name) for name in (cert.get("domains") or [])]
    domain = cert.get("domain")
    if isinstance(domain, str) and domain:
        names.append(domain)
        if cert.get("wildcard") is True:
            names.append(f"*.{domain.lstrip('*.')}")
    return names


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


def zone_relative(fqdn: str, zone: str) -> str:
    """`gateway.olympus.innotel.us` in zone `innotel.us` -> `gateway.olympus`."""
    suffix = "." + zone.rstrip(".").lower()
    name = fqdn.rstrip(".").lower()
    return name[: -len(suffix)] if name.endswith(suffix) else name


def looks_like_host(value: str) -> bool:
    parts = value.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    return bool(value) and all(c.isalnum() or c in "-." for c in value)


class Api:
    """A thin Cerulean client.

    Every call is one request with a Bearer token, and every failure comes back as
    a `(status, body)` pair so the caller can decide — this module's job is to
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

    def get_list(self, path: str, what: str) -> list[dict]:
        """A GET that must return a list, with the failure named."""
        status, payload = self.call(path)
        if status != 200 or not isinstance(payload, list):
            sys.exit(f"Could not list {what} (HTTP {status}): {payload}")
        return payload


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


def ensure_certificate(
    api: Api, fqdn: str, dry_run: bool, wait: int, renew_days: int
) -> tuple[int | None, str]:
    """Return `(certificate_id, report)` for a certificate covering `fqdn`.

    A `*.` in `fqdn` means a wildcard request, and Cerulean spells that differently
    from what a wildcard looks like: the `domain` field must be the *base* (its
    validator is `/^[a-z0-9.-]+$/`, so a literal `*` there is rejected with
    `Invalid domain name`), and `wildcard: true` is what makes the issued
    certificate carry `*.base`. Measured, after the obvious request failed.
    """
    now = datetime.now(timezone.utc)
    certificates = api.get_list("/api/certificates", "certificates")

    reusable = select_certificate(certificates, fqdn, now, renew_days)
    if reusable is not None:
        covered = reusable.get("domain") or (reusable.get("domains") or [""])[0]
        return (
            reusable["id"],
            f"certificate #{reusable['id']} covers {fqdn} as {covered} "
            f"(expires {reusable.get('expiresAt')})",
        )

    if dry_run:
        return None, f"would request a certificate for {fqdn}"

    body = _certificate_body(fqdn)
    status, created = api.call("/api/certificates", "POST", body)
    if status not in (200, 201, 202) or not isinstance(created, dict) or not created.get("id"):
        sys.exit(f"Could not request a certificate (HTTP {status}): {created}")
    cert_id = created["id"]

    state = "pending"
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


def _certificate_body(fqdn: str) -> dict:
    """The request body for a name — a wildcard when the name says so."""
    if fqdn.startswith("*."):
        base = fqdn[2:]
        return {"domain": base, "wildcard": True, "name": fqdn}
    return {"domain": fqdn, "name": fqdn}


def export_certificate_to_npm(api: Api, certificate_id: int) -> int:
    """Push PEM material into NPM, returning NPM's own certificate id."""
    status, exported = api.call("/api/npm/export-cert", "POST", {"certificate_id": certificate_id})
    if status not in (200, 201) or not isinstance(exported, dict) or not exported.get("npmCertificateId"):
        sys.exit(f"Could not export the certificate to NPM (HTTP {status}): {exported}")
    return int(exported["npmCertificateId"])


def list_proxy_hosts(api: Api) -> list[dict]:
    return api.get_list("/api/npm/hosts", "NPM hosts")


def find_proxy_host(hosts: list[dict], fqdn: str) -> dict | None:
    """The NPM host serving exactly this name, if there is one.

    Matched on the full name list rather than on a prefix: `<slug>.studio.example`
    and `<slug>-old.studio.example` share a prefix, and repointing the wrong one is
    a live site going dark.
    """
    wanted = fqdn.rstrip(".").lower()
    for host in hosts:
        names = [str(n).rstrip(".").lower() for n in (host.get("domain_names") or [])]
        if wanted in names:
            return host
    return None


def ensure_proxy_host(
    api: Api,
    fqdn: str,
    forward_host: str,
    forward_port: int,
    certificate_id: int | None,
    dry_run: bool,
    *,
    repoint: bool = False,
) -> str:
    """Create (or verify) the NPM host for `fqdn`.

    `repoint` exists because a published site's backend moves — a rebuilt app gets
    a new port — while the gateway's name must never be repointed silently. The
    site publisher opts in; the gateway path does not.
    """
    hosts = list_proxy_hosts(api)
    existing = find_proxy_host(hosts, fqdn)
    wanted = f"http://{forward_host}:{forward_port}"

    if existing is not None:
        target = f"{existing.get('forward_scheme')}://{existing.get('forward_host')}:{existing.get('forward_port')}"
        if target == wanted:
            return f"{fqdn} already -> {target} (host #{existing.get('id')})"
        if not repoint:
            sys.exit(
                f"NPM already serves {fqdn} -> {target}, not {wanted}. "
                "Refusing to repoint a live name; update it in NPM if it is wrong."
            )
        return update_proxy_host(api, int(existing["id"]), fqdn, forward_host, forward_port, certificate_id, dry_run)

    if dry_run:
        return f"would create an NPM proxy host {fqdn} -> {wanted}"

    if certificate_id is None:
        sys.exit("No certificate to attach — refusing to create a host that would serve plain HTTP.")

    npm_cert = export_certificate_to_npm(api, certificate_id)
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
        f"created NPM proxy host #{host.get('id')} {fqdn} -> {wanted} "
        f"(cert {npm_cert}, TLS enforced)"
    )


def update_proxy_host(
    api: Api,
    host_id: int,
    fqdn: str,
    forward_host: str,
    forward_port: int,
    certificate_id: int | None,
    dry_run: bool,
) -> str:
    """Point an existing host somewhere new. Only reached with `repoint=True`."""
    if dry_run:
        return f"would repoint NPM host #{host_id} {fqdn} -> http://{forward_host}:{forward_port}"

    body: dict = {
        "domain_names": [fqdn],
        "forward_host": forward_host,
        "forward_port": forward_port,
        "forward_scheme": "http",
        "ssl_forced": True,
        "http2_support": True,
    }
    if certificate_id is not None:
        body["certificate_id"] = export_certificate_to_npm(api, certificate_id)

    status, payload = api.call(f"/api/npm/hosts/{host_id}", "PUT", body)
    if status not in (200, 201, 204):
        sys.exit(f"Could not repoint NPM host #{host_id} (HTTP {status}): {payload}")
    return f"repointed NPM host #{host_id} {fqdn} -> http://{forward_host}:{forward_port}"


def delete_proxy_host(api: Api, host_id: int, dry_run: bool) -> str:
    if dry_run:
        return f"would delete NPM host #{host_id}"
    status, payload = api.call(f"/api/npm/hosts/{host_id}", "DELETE")
    if status not in (200, 204):
        sys.exit(f"Could not delete NPM host #{host_id} (HTTP {status}): {payload}")
    return f"deleted NPM host #{host_id}"

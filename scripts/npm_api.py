#!/usr/bin/env python3
"""NPM's own API — the two things Cerulean's passthrough cannot do.

WHY THIS EXISTS, AND WHY IT IS SEPARATE FROM `cerulean_api.py`. Cerulean is the
right way to reach the edge for everything it models: DNS records, certificates,
proxy hosts, with the credential handling and the certificate-selection logic this
repo has already tested. One field is not modelled there, and it is the field that
closes a door rather than opening one.

Measured on this deployment (`gateway.olympus.innotel.us`, NPM host #198):

    PUT /api/npm/hosts/198   {"advanced_config": "location ^~ /v1/ { return 403; }"}
      → 200, `modified_on` advanced, Cerulean's own read-back still showed
        `advanced_config: ""` … and so did NPM's.
    PUT /api/nginx/proxy-hosts/198  (NPM directly, same body)
      → 200, and `advanced_config` read back verbatim.

So the field is dropped somewhere in the passthrough. The choice was to keep the
rule out of the repo (a hand edit in an NPM UI that the next deployment never
learns) or to write it where it lands. It is written where it lands, and the
measurement above is the reason this module exists at all — a script that reported
success while the edge stayed open would be worse than no script.

WHAT IT IS NOT. Not a general NPM client: `login`, one GET, one PUT, and the
body-assembly rule that makes a PUT safe. Nothing here creates or deletes a host.

`.env` carries NPM_BASE_URL / NPM_EMAIL / NPM_PASSWORD for reading the edge by
hand; this is the first thing that writes with them. They are the NPM admin login,
which is a different credential from CERULEAN_ADMIN_PASSWORD and from the
Authentik token.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# What NPM accepts back on a PUT. Everything else on a host object is computed by
# NPM (`id`, `created_on`, `modified_on`, `owner_user_id`) and is not ours to send.
# `meta` is writable, and is kept because it is where NPM keeps the certificate
# metadata it reads back: dropping it is the kind of "one field changed" that shows
# up later as a renewed certificate the edge does not pick up.
NPM_WRITABLE_FIELDS = (
    "domain_names",
    "forward_scheme",
    "forward_host",
    "forward_port",
    "forward_ip",
    "ipv6",
    "access_list_id",
    "certificate_id",
    "ssl_forced",
    "caching_enabled",
    "block_exploits",
    "advanced_config",
    "meta",
    "allow_websocket_upgrade",
    "http2_support",
    "hsts_enabled",
    "hsts_subdomains",
    "enabled",
    "locations",
)


def closed_paths_config(paths: list[str]) -> str:
    """Nginx for a proxy host, refusing `paths` before anything is forwarded.

    WHY A PUBLIC NAME NEEDS THIS. A name at the edge forwards to one listener, and
    that listener is not necessarily one surface. `gateway.olympus.innotel.us` is the
    dashboard's name: it reaches the identity-aware proxy, which requires an Authentik
    session for everything it serves — and deliberately exempts `/v1`, because
    inference clients send `Authorization: Bearer $OMNIROUTE_API_KEY` rather than a
    session cookie, and an interactive login in front of an API is not a check, it is
    an outage.

    Measured, that exemption was the whole door. The gateway is in Authentik-only mode
    (`make gateway-auth-mode` sets `requireLogin=false`, whose own docstring says the
    loopback binding is then the entire control) and this OmniRoute build does not
    validate the bearer key on `/v1` at all: no key, a bogus key and the real key all
    answer 200, and an unauthenticated `POST /v1/chat/completions` on the public name
    returned a completion. The key the proxy's comment relied on was never checked, so
    the public name handed the internet the provider credentials behind it.

    The rule lives here rather than at the gateway because `requireLogin=false` is a
    deliberate decision this repo must not silently reverse, and because `/v1` has to
    keep working on the LAN — that is what the proxy's `0.0.0.0` listener is for. The
    *public* name stops carrying it; the LAN door keeps the exemption.

    `^~` so a later regex location cannot take the prefix back, and `=` for the bare
    path, which `^~ /v1/` does not match. `return 403` rather than 404: this is a
    refusal, and that is the difference between an edge enforcing a rule and an
    upstream being broken.
    """
    snippet: list[str] = []
    for raw in paths:
        path = "/" + str(raw).strip().strip("/")
        if path == "/":
            # Closing "/" would be a name that answers nothing — that is `sites-down`,
            # not something to write into a live host by accident.
            continue
        snippet.append(f"location ^~ {path}/ {{ return 403; }}")
        snippet.append(f"location = {path} {{ return 403; }}")
    return "\n".join(snippet)


def refused_paths(advanced_config: str) -> str:
    """What a config snippet refuses, read back out of it for a log line.

    Derived rather than carried alongside: a report that says "refusing /v1" while
    the snippet says something else is the failure mode this whole file is about — a
    claim about the edge that the edge does not support.
    """
    paths: list[str] = []
    for line in advanced_config.splitlines():
        if "return 403" not in line or "location" not in line:
            continue
        parts = line.split()
        index = parts.index("location") + 1
        # `location ^~ /v1/ …` and `location = /v1 …`: the modifier, when present,
        # sits between the keyword and the path.
        if index < len(parts) and parts[index] in ("^", "^~", "=", "~", "~*"):
            index += 1
        if index >= len(parts):
            continue
        name = parts[index].rstrip("/")
        if name and name not in paths:
            paths.append(name)
    if paths:
        return f"refuses {', '.join(paths)} at the edge"
    seconds = long_request_seconds(advanced_config)
    if seconds:
        return f"long-request timeouts ({seconds}s) at the edge"
    return "advanced_config set"


# Nginx's read timeout is 60s, and this deployment's NPM answers with 90s. Both are
# shorter than a Studio build, which is not an edge bug — it is a synchronous model
# call behind a proxy sized for a page load.
DEFAULT_LONG_REQUEST_SECONDS = 900


def long_request_config(seconds: int = DEFAULT_LONG_REQUEST_SECONDS) -> str:
    """Nginx for a proxy host whose upstream answers slowly, and streams.

    WHY A STUDIO HOST NEEDS THIS. `POST /api/plan` waits for a whole completion
    before it answers anything — a plan is deliberately not a stream — and a
    2,000-token plan through the gateway's `auto/*` router is not a sub-minute
    operation. Measured through the edge on `studio.olympus.innotel.us`: the request
    was cut at 90s and the browser was handed nginx's own HTML
    ("504 Gateway Time-out"), which the UI can only report as the bare
    "Request failed with status 504." — no model name, no reason, nothing to act on.

    `proxy_buffering off` is the other half: generation streams token-by-token and
    must reach the browser as it arrives, rather than being collected until the
    response ends (which is the same as a timeout for a long build).

    900s rather than "infinite": the ceiling should belong to the model, not to the
    proxy in front of it, but a connection that is genuinely wedged must still end.
    """
    return "\n".join(
        (
            f"proxy_read_timeout {seconds}s;",
            f"proxy_send_timeout {seconds}s;",
            "proxy_buffering off;",
        )
    )


def long_request_seconds(advanced_config: str) -> int | None:
    """The read timeout a snippet sets, or None — read back, never assumed."""
    for line in advanced_config.splitlines():
        parts = line.strip().rstrip(";").split()
        if len(parts) == 2 and parts[0] == "proxy_read_timeout":
            value = parts[1]
            if value.endswith("s") and value[:-1].isdigit():
                return int(value[:-1])
    return None


def payload_fields(host: dict, advanced_config: str) -> dict:
    """The body for a PUT that changes `advanced_config` and nothing else.

    NPM's PUT replaces the object rather than merging into it, so a partial body —
    the forwarding fields and the new snippet, the obvious thing to send — resets
    whatever was not named: TLS enforcement, websocket upgrade, exploits blocking,
    the certificate. All of those are load-bearing here (the SSO proxy is spoken to
    over websockets), and the reset would look like a host nobody had touched.
    """
    body = {field: host[field] for field in NPM_WRITABLE_FIELDS if field in host}
    body["advanced_config"] = advanced_config
    # An empty field is still a claim: `certificate_id: None` detaches the
    # certificate, so it is dropped rather than sent as null.
    for field in ("certificate_id", "access_list_id"):
        if body.get(field) is None:
            body.pop(field, None)
    return body


class NpmApi:
    """Login, then one host by name. Every failure comes back as `(status, body)`."""

    def __init__(self, base: str, identity: str, secret: str, insecure: bool = False) -> None:
        self.base = base.rstrip("/")
        self.identity = identity
        self.secret = secret
        self.context = ssl._create_unverified_context() if insecure else None
        self.token = ""

    def call(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")[:400]
        except (urllib.error.URLError, OSError) as error:
            return 0, str(getattr(error, "reason", error))

    def login(self) -> str:
        status, payload = self.call("/api/tokens", "POST", {"identity": self.identity, "secret": self.secret})
        if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
            sys.exit(f"NPM login refused (HTTP {status}): {payload}\nNPM_EMAIL / NPM_PASSWORD in .env are the NPM admin login.")
        self.token = payload["token"]
        return self.token

    def proxy_host(self, fqdn: str) -> dict | None:
        """The host answering on `fqdn`, or None. Matched on `domain_names`, exactly."""
        status, hosts = self.call("/api/nginx/proxy-hosts")
        if status != 200 or not isinstance(hosts, list):
            sys.exit(f"Could not list NPM proxy hosts (HTTP {status}): {hosts}")
        wanted = fqdn.rstrip(".").lower()
        for host in hosts:
            names = [str(name).rstrip(".").lower() for name in host.get("domain_names") or []]
            if wanted in names:
                return host
        return None

    def set_advanced_config(self, host: dict, advanced_config: str, dry_run: bool = False) -> str:
        """Write `advanced_config` onto a host, leaving every other field as it was."""
        host_id = int(host["id"])
        fqdn = (host.get("domain_names") or ["?"])[0]
        if (host.get("advanced_config") or "").strip() == advanced_config.strip():
            return f"{fqdn} (host #{host_id}) already {refused_paths(advanced_config)}"
        if dry_run:
            return f"would set advanced_config on NPM host #{host_id} {fqdn}: {refused_paths(advanced_config)}"

        status, payload = self.call(f"/api/nginx/proxy-hosts/{host_id}", "PUT", payload_fields(host, advanced_config))
        if status not in (200, 201, 204):
            sys.exit(f"Could not set advanced_config on NPM host #{host_id} (HTTP {status}): {payload}")

        # Read it back. The write reported success; that is a statement about the
        # request, not about the edge, and this file exists because the two came apart.
        after = self.proxy_host(fqdn) or {}
        if (after.get("advanced_config") or "").strip() != advanced_config.strip():
            sys.exit(
                f"NPM accepted the update to host #{host_id} but read back "
                f"{after.get('advanced_config')!r} — the rule is not on the edge."
            )
        return f"NPM host #{host_id} {fqdn} {refused_paths(advanced_config)}"

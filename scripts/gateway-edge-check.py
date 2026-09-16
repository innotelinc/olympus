#!/usr/bin/env python3
"""Is `gateway.olympus.innotel.us` actually reachable? Answer with the broken link.

WHY THIS EXISTS. "It's not resolving" is what an operator reports and it is almost
never what happened. The name is four separate things wired in series, and each one
fails with the same sentence in a browser:

    DNS        gateway.olympus.innotel.us  CNAME  innotel.us  → A  73.68.203.71
    edge       NPM (192.168.1.46) :443     →  http://172.17.0.1:20129
    proxy      oauth2-proxy                →  Authentik (302) for anything but /ping
    session    oauth2-proxy                →  redis at 127.0.0.1:16379
    gateway    omniroute                   →  127.0.0.1:20128

A resolver that does not answer produces a DNS error; an edge that is down produces
a connection timeout; a lapsed certificate produces a TLS warning; a stopped proxy
produces a 502. All four get reported by a user as "not resolving", and three of
them are not the name at all.

So this walks the chain in order and stops at the first link that is broken, saying
which one it was and what that means. It is deliberately not a smoke test that
returns 0/1 — the diagnosis is the product.

THE SESSION LINK IS HERE BECAUSE OF ONE PARTICULAR 502, and it is the odd one out:
it is the only link that cannot be reached with `curl` at all. The proxy keeps its
sessions in redis rather than in a cookie, and the reason is the login callback. A
cookie session carries the email, the ID token and every group the identity claims;
an account in a few dozen groups overflows the 4KB cookie limit, oauth2-proxy splits
the session across several `Set-Cookie` headers, and the edge — whose
`proxy_buffer_size` is smaller than that — answers the request that would have
FINISHED the login with `502 Bad Gateway`. The site is perfectly reachable at the
front door and cannot be logged into, which is why this walked the chain, found
nothing wrong, and reported "ok" while the name was unusable. It cannot be probed
from outside, because the whole point is that the answer comes only after you
authenticate. So it checks the two things that make it work — the store answers,
and the proxy is pointed at it — and says so rather than implying more.

A WORTHWHILE THING IT TURNS UP: the record is a CNAME to the zone apex, and the
zone's own authoritative servers are both on the same host as the edge, Authentik
and Cerulean. When that host is down, **every** name under `innotel.us` stops
resolving at once — so the failure looks like a DNS fault in one name when it is an
outage of the platform. The report says which resolver failed and which answered,
because that is the difference between "renew the certificate" and "wait for the
box to come back".

    scripts/gateway-edge-check.py
    scripts/gateway-edge-check.py --json
    scripts/gateway-edge-check.py --host studio.olympus.innotel.us
    scripts/gateway-edge-check.py --no-sso        # a name that serves directly

Exit codes:
    0  every link answered
    1  a link is broken — the report names it
    2  the check cannot run (nothing configured to check, or a library missing)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- configuration ------------------------------------------------------------

DEFAULT_SSO_PORT = 20129
DEFAULT_REDIS_PORT = 16379
DEFAULT_HOST = "gateway.olympus.innotel.us"
SSO_CONTAINER = "olympus-gateway-sso"


def load_env(path: Path) -> dict[str, str]:
    """The repo `.env`, read the way the rest of the stack reads it."""
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
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


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- DNS ----------------------------------------------------------------------
#
# No `dig`, no `nslookup`, and no third-party module: this host has none of the
# three, and a check that needs a package installed to run is a check that does not
# run. A UDP query for one A record is a hundred lines and no dependencies.
#
# The parser is defensive on purpose. A malformed or hostile answer must produce a
# diagnosis, never a traceback — the whole point of this script is to report what
# is wrong rather than to die trying.

RCODE_NAMES = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}


def rdn(certificate: dict, field: str, key: str) -> str | None:
    """One value out of a certificate's subject/issuer.

    `ssl` hands those back as a tuple of RDNs, each a tuple of name/value pairs —
    not a mapping, so `dict(...)` on it raises rather than doing the obvious thing.
    """
    for entry in certificate.get(field) or ():
        for name, value in entry:
            if name == key:
                return value
    return None


class DnsError(Exception):
    """The answer could not be read. Not the same as 'the name does not exist'."""


def read_name(data: bytes, offset: int) -> tuple[str, int]:
    """A name at `offset`, following compression pointers. Returns (name, next)."""
    labels: list[str] = []
    jumped = False
    consumed = offset
    hops = 0

    while True:
        if offset >= len(data):
            raise DnsError("truncated name")
        length = data[offset]

        if length == 0:
            offset += 1
            if not jumped:
                consumed = offset
            break

        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise DnsError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                consumed = offset + 2
            jumped = True
            hops += 1
            # A pointer that loops would spin forever, and a response is untrusted
            # input; 32 hops is more than any real name needs.
            if hops > 32:
                raise DnsError("compression pointer loop")
            offset = pointer
            continue

        if length > 63:
            raise DnsError(f"label length {length} out of range")
        end = offset + 1 + length
        if end > len(data):
            raise DnsError("truncated label")
        labels.append(data[offset + 1 : end].decode("ascii", "replace"))
        offset = end

    return ".".join(labels), consumed


def parse_response(data: bytes) -> dict:
    """rcode and the A/AAAA answers from a DNS response packet."""
    if len(data) < 12:
        raise DnsError(f"response is {len(data)} bytes — too short to be DNS")

    _, flags, questions, answers, _, _ = struct.unpack(">HHHHHH", data[:12])
    rcode = flags & 0xF
    result: dict = {"rcode": rcode, "name": RCODE_NAMES.get(rcode, f"rcode {rcode}"), "addresses": []}

    offset = 12
    for _ in range(questions):
        _, offset = read_name(data, offset)
        offset += 4  # qtype + qclass
        # A question section that runs past the packet is malformed, and accepting
        # it would report a broken resolver as a name that simply did not resolve —
        # two different fixes wearing the same sentence.
        if offset > len(data):
            raise DnsError("truncated question section")

    cnames: list[str] = []
    for _ in range(answers):
        _, offset = read_name(data, offset)
        if offset + 10 > len(data):
            raise DnsError("truncated record header")
        rtype, _, _, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        if len(rdata) != rdlength:
            raise DnsError("truncated record data")
        offset += rdlength

        if rtype == 1 and rdlength == 4:
            result["addresses"].append(socket.inet_ntoa(rdata))
        elif rtype == 28 and rdlength == 16:
            result["addresses"].append(socket.inet_ntop(socket.AF_INET6, rdata))
        elif rtype == 5:
            try:
                target, _ = read_name(data, offset - rdlength)
            except DnsError:
                target = "?"
            cnames.append(target)

    if cnames:
        result["cname"] = cnames[0]

    return result


def query(server: str, name: str, timeout: float = 5.0) -> dict:
    """Ask one resolver. Raises `DnsError` when it does not answer or the packet
    is unreadable — both of which are findings, and are reported as such."""
    transaction = 0x2A2A
    header = struct.pack(">HHHHHH", transaction, 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".")) + b"\x00"
    packet = header + question + struct.pack(">HH", 1, 1)  # A, IN

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    except socket.timeout as error:
        raise DnsError(f"no answer within {timeout:g}s") from error
    except OSError as error:
        raise DnsError(str(error)) from error
    finally:
        sock.close()

    return parse_response(data)


def system_resolver() -> str:
    """The first nameserver from /etc/resolv.conf, for the report's wording."""
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                return fields[1]
    except OSError:
        pass
    return "(system)"


def configured_resolver(env: dict[str, str]) -> str:
    """The resolver this stack is *supposed* to use.

    `CERULEAN_DNS_API_URL` is the platform that owns the zone, so its host is the
    one whose answer matters. Reported alongside the system resolver rather than
    instead of it: "the LAN resolver is down" and "the world cannot see the name"
    are different problems with different fixes.
    """
    raw = env.get("CERULEAN_DNS_API_URL", "")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    host = raw.split("/", 1)[0].split(":", 1)[0].strip()
    return host


# --- the links ----------------------------------------------------------------


def check_dns(host: str, env: dict[str, str]) -> dict:
    """Both resolvers, reported separately.

    The system resolver is asked first because it is what this host would use; the
    platform resolver is asked second because it is what *should* be authoritative
    for the zone. Agreement is the healthy case.
    """
    servers = [("system", system_resolver())]
    platform = configured_resolver(env)
    if platform:
        servers.append(("cerulean", platform))

    results: list[dict] = []
    for label, server in servers:
        entry = {"resolver": label, "server": server}
        try:
            answer = query(server, host)
            entry.update(answer)
        except DnsError as error:
            entry["error"] = str(error)
        results.append(entry)

    resolved = any(entry.get("addresses") for entry in results)
    payload: dict = {"link": "dns", "ok": resolved, "answers": results}

    # A resolver that failed while another answered is not a failure — the name is
    # reachable, and this check exists to answer that question. It is not nothing
    # either: the resolver that did not answer is the one the zone belongs to, and
    # its absence is what turns into "it's not resolving" for everyone who uses it.
    # Reported as a warning rather than passed over in silence.
    unanswered = [entry for entry in results if entry.get("error")]
    if resolved and unanswered:
        names = ", ".join(f"{entry['server']} ({entry['error']})" for entry in unanswered)
        payload["warning"] = (
            f"{names} did not answer. The name is reachable from here, but anything "
            "pointed at that resolver will fail every name under this zone while it is down."
        )

    return payload


def check_tls(host: str, port: int = 443, timeout: float = 10.0) -> dict:
    """Connect with SNI and read the certificate.

    Separate from the HTTP check on purpose: a name that resolves but serves a
    lapsed or wrong-name certificate is reachable and unusable, and a browser says
    something about privacy rather than about DNS.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                certificate = tls.getpeercert()
    except ssl.SSLCertVerificationError as error:
        return {"link": "tls", "ok": False, "error": f"certificate rejected: {error.verify_message or error}"}
    except ssl.SSLError as error:
        return {"link": "tls", "ok": False, "error": f"TLS handshake failed: {error}"}
    except OSError as error:
        return {"link": "tls", "ok": False, "error": f"could not connect to {host}:{port}: {error}"}

    not_after = certificate.get("notAfter")
    days_left = None
    if not_after:
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (expires - datetime.now(timezone.utc)).days
        except ValueError:
            days_left = None

    names = [value for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS"]
    return {
        "link": "tls",
        # Verifying already happened inside `wrap_socket`; reaching here means the
        # chain validated against the system trust store for this exact name.
        "ok": True,
        "issuer": rdn(certificate, "issuer", "commonName"),
        "names": names[:6],
        "expires": not_after,
        "days_left": days_left,
    }


def edge_verdict(status: int, location: str, expect_sso: bool) -> tuple[bool, str, str | None]:
    """Whether one response is the expected one. Pure, so it can be asserted.

    The gateway is fronted by the identity-aware proxy, so a *redirect to the IdP* is
    the healthy answer — and a 200 is not a lesser pass but the failure this whole
    deployment exists to prevent: it means the proxy is not in the path and the
    dashboard is answering unauthenticated. Returning a reason rather than a bare
    boolean is what lets the report say that instead of "unexpected status".
    """
    if expect_sso:
        if 300 <= status < 400 and "authorize" in location:
            return True, "redirects to the identity provider, as the SSO proxy should", None
        if status == 200:
            return (
                False,
                "",
                "served 200 without a redirect — the identity-aware proxy is not in the "
                "path, so the dashboard is answering unauthenticated",
            )
        return False, "", f"expected a redirect to the identity provider, got HTTP {status}"

    if 200 <= status < 300:
        return True, "serving", None

    # A redirect here is not a weaker pass — with `--no-sso` the name is supposed to
    # serve, so anything that is not a 2xx is a different answer than the one asked for.
    return False, "", f"expected the name to serve, got HTTP {status}"


def v1_verdict(public_status: int, lan_status: int) -> tuple[bool, str, str | None]:
    """Whether `/v1` is where it is supposed to be. Pure, so it can be asserted.

    THE POLICY, IN TWO REQUESTS. `/v1` is the inference API. It cannot sit behind an
    interactive login (every client sends a bearer key, not a cookie) and the gateway
    itself validates nothing on this deployment — measured: no key, a bogus key and
    the real key all answered 200, and an unauthenticated POST on the public name
    returned a completion. So the *public* name must refuse it and the *LAN* door —
    the proxy's own 0.0.0.0 listener, which is the documented path for another
    machine — must keep carrying it.

    Both halves are asserted, because either one alone is a different deployment than
    the one that was asked for: a 403 everywhere breaks the API clients, and a 200 on
    the public name hands the internet the provider credentials behind the gateway.
    """
    if public_status != 403:
        return (
            False,
            "",
            f"https://<host>/v1/models answered {public_status}, not 403 — the public name is "
            "still carrying the inference API. `make gateway-edge` sets the edge rule that "
            "closes it (closed_paths_config in scripts/cerulean_api.py).",
        )
    if lan_status == 403:
        return (
            False,
            "",
            "the LAN door refuses /v1 as well — the edge rule has been applied somewhere that "
            "also serves 127.0.0.1:20129, which breaks every API client.",
        )
    return True, f"closed on the public name (403), open on the LAN door ({lan_status})", None


def check_v1(host: str, port: int, timeout: float = 15.0) -> dict:
    """Is `/v1` closed at the edge and still carried on the LAN door?"""
    payload: dict = {"link": "v1"}
    opener = urllib.request.build_opener(NoRedirect())

    for label, url in (("public", f"https://{host}/v1/models"), ("lan", f"http://127.0.0.1:{port}/v1/models")):
        try:
            with opener.open(url, timeout=timeout) as response:  # noqa: S310 - both URLs are built here
                payload[label] = response.status
        except urllib.error.HTTPError as error:
            payload[label] = error.code
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            payload.update({"ok": False, "error": f"no answer from {url}: {reason}"})
            return payload

    ok, note, failure = v1_verdict(payload["public"], payload["lan"])
    payload["ok"] = ok
    if ok:
        payload["note"] = note
    else:
        payload["error"] = failure
    return payload


def check_edge(host: str, expect_sso: bool, timeout: float = 15.0) -> dict:
    """One request at the public name, and what came back."""
    request = urllib.request.Request(f"https://{host}/", method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            location = response.headers.get("location", "")
            server = response.headers.get("server", "")
    except urllib.error.HTTPError as error:
        status = error.code
        location = error.headers.get("location", "") if error.headers else ""
        server = error.headers.get("server", "") if error.headers else ""
    except urllib.error.URLError as error:
        return {"link": "edge", "ok": False, "error": f"no answer from https://{host}/: {error.reason}"}
    except OSError as error:
        return {"link": "edge", "ok": False, "error": f"connection failed: {error}"}

    ok, note, failure = edge_verdict(status, location, expect_sso)
    payload = {"link": "edge", "ok": ok, "status": status, "server": server, "location": location}
    if ok:
        payload["note"] = note
    else:
        payload["error"] = failure
    return payload


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the answer being checked, not something to follow."""

    def redirect_request(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


def check_proxy(port: int, timeout: float = 5.0) -> dict:
    """The SSO proxy on its own port, by its own ping endpoint.

    Checked even when the name fails, because it is local and it splits the fault
    in half: a healthy proxy with a broken name is a DNS or edge problem, and a
    dead proxy explains the name on its own.
    """
    url = f"http://127.0.0.1:{port}/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
            body = response.read(200).decode("utf-8", "replace").strip()
            status = response.status
    except urllib.error.HTTPError as error:
        return {"link": "proxy", "ok": False, "error": f"{url} answered HTTP {error.code}"}
    except urllib.error.URLError as error:
        return {"link": "proxy", "ok": False, "error": f"{url} did not answer: {error.reason}"}
    except OSError as error:
        return {"link": "proxy", "ok": False, "error": f"{url} did not answer: {error}"}

    if status != 200:
        return {"link": "proxy", "ok": False, "error": f"{url} answered HTTP {status}"}

    return {"link": "proxy", "ok": True, "status": status, "body": body}


def redis_encode(*parts: str) -> bytes:
    """One RESP command. A `*N` array is ONE command, not a line-up of them.

    This is the trap worth naming: `AUTH <password> PING` sent as a single
    three-element array is not "authenticate, then ping" — it is one command
    called AUTH carrying two arguments, and redis answers `-WRONGPASS`. Which
    reads exactly like a wrong password, and cost an afternoon. Commands go in
    separate arrays, one per command.
    """
    payload = f"*{len(parts)}\r\n".encode()
    for part in parts:
        raw = part.encode()
        payload += b"$%d\r\n%s\r\n" % (len(raw), raw)
    return payload


def redis_read_line(connection: socket.socket) -> str:
    """One reply line, which is all the simple-string replies here need."""
    buffer = b""
    while b"\r\n" not in buffer:
        chunk = connection.recv(1024)
        if not chunk:
            break
        buffer += chunk
    return buffer.split(b"\r\n", 1)[0].decode("utf-8", "replace")


def redis_command(port: int, password: str, command: str, arguments: list[str] | None = None, timeout: float = 5.0) -> str:
    """Run one command, authenticating first if a password was given.

    Hand-rolled rather than imported for the same reason as the DNS client above:
    this host has no redis client and no third-party module, and a check that needs
    a package installed to run is a check that does not run.

    AUTH is sent as its own command and its reply is read before the real one, so
    the caller gets the *last* reply — an authentication failure must be reported as
    one, not mistaken for a failed PING.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        if password:
            connection.sendall(redis_encode("AUTH", password))
            reply = redis_read_line(connection)
            if not reply.startswith("+"):
                return reply or "no reply to AUTH"
        connection.sendall(redis_encode(command, *(arguments or [])))
        return redis_read_line(connection)


def proxy_session_store() -> tuple[str | None, str]:
    """The session store the running proxy was actually started with.

    Returns (store, note). `store` is None when it cannot be determined — which is a
    finding about this check, not about the proxy, so it is reported as such rather
    than counted as a pass. The configuration is read from the container because
    there is no route that reports it: an unauthenticated request never reaches a
    session, which is exactly the blind spot this link exists to cover.
    """
    if not shutil.which("docker"):
        return None, "not verified — no docker on this host"

    try:
        result = subprocess.run(
            ["docker", "inspect", SSO_CONTAINER, "--format", "{{json .Config.Cmd}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"not verified — {error}"

    # `the container is not there` is a different finding from `the store is
    # unreadable`, and on this host it is usually "make gateway-sso-up was never run".
    if result.returncode != 0:
        return None, f"{SSO_CONTAINER} is not running"

    command = result.stdout
    for store in ("redis", "cookie"):
        if f"--session-store-type={store}" in command:
            return store, ""
    return None, "the proxy's session store could not be read from its command"


def check_session(env: dict[str, str]) -> dict:
    """Can a login be completed? The store is the half of that a probe can reach.

    Split from the proxy check because the two fail apart: a proxy on a dead store
    answers /ping exactly like a healthy one, right up until somebody tries to sign
    in — and then fails, on the callback, with a 502 from the edge that names no
    component at all.
    """
    port = int(env.get("GATEWAY_SSO_REDIS_PORT") or DEFAULT_REDIS_PORT)
    password = env.get("GATEWAY_SSO_REDIS_PASSWORD", "")
    store, note = proxy_session_store()
    payload: dict = {"link": "session", "port": port, "store": store}

    try:
        reply = redis_command(port, password, "PING")
    except OSError as error:
        payload["ok"] = False
        payload["error"] = (
            f"nothing answered on 127.0.0.1:{port}: {error}. The proxy keeps its sessions "
            "there rather than in a cookie, so logins cannot complete without it."
        )
        return payload

    if not reply.startswith("+PONG"):
        # Redis reports a wrong password as an error reply, not a dropped
        # connection, so this is the case where the two ends disagree about the
        # secret rather than one of them being down.
        payload["ok"] = False
        payload["error"] = (
            f"127.0.0.1:{port} answered {reply[:60]!r} — the store is up, but "
            "GATEWAY_SSO_REDIS_PASSWORD in .env does not match its --requirepass."
        )
        return payload

    if store == "cookie":
        # The regression this link exists for. Not a crash and not a 502 at the
        # front door, so nothing else would have said anything.
        payload["ok"] = False
        payload["error"] = (
            "the proxy is on `--session-store-type=cookie`, which cannot hold a "
            "session for an identity in many Authentik groups: it splits the session "
            "across several cookies, the edge's proxy buffer is smaller than those "
            "headers, and the login callback gets `502` from the edge. Set it to "
            "redis in compose.gateway-sso.yml."
        )
        return payload

    payload["ok"] = True
    payload["note"] = "up and answering PING"
    if note:
        payload["warning"] = f"the proxy's session store was {note}, so only the store itself was checked"
    return payload


# --- diagnosis ----------------------------------------------------------------


def diagnose(links: dict[str, dict], host: str) -> str:
    """The sentence an operator needs: which link broke, and what it means.

    Written for the *first* broken link, because repairing a chain from the far end
    is guesswork. The wording of the DNS case is the important one: a resolver that
    does not answer says nothing about this name, and everything about the host
    answering for the zone.
    """
    dns, tls, edge, proxy = links["dns"], links["tls"], links["edge"], links["proxy"]
    session = links.get("session")

    if not dns["ok"]:
        failed = [entry for entry in dns["answers"] if entry.get("error")]
        nxdomain = [entry for entry in dns["answers"] if entry.get("rcode") == 3]
        if nxdomain:
            return (
                f"{host} does not exist in the zone. The resolvers that answered say "
                "NXDOMAIN, so the record is missing — create it with `make gateway-edge`."
            )
        if failed:
            servers = ", ".join(f"{entry['server']} ({entry['error']})" for entry in failed)
            return (
                f"no resolver answered for {host}: {servers}. The name itself may be "
                "fine — a resolver that does not answer fails every name it is asked "
                "for, and this zone's own servers live on the same host as the edge. "
                "Try the name from another network before changing anything here."
            )
        return f"{host} resolved to no address at all."

    if not tls["ok"]:
        return f"{host} resolves but its TLS is unusable: {tls['error']}"

    if not edge["ok"]:
        return (
            f"{host} resolves and its certificate is valid, so DNS is not the problem "
            f"— the edge is: {edge.get('error')}"
        )

    # Before the proxy, because a wrong answer here is not a broken link: every step
    # above succeeded and the deployment is still not the one that was asked for.
    if links.get("v1") is not None and not links["v1"].get("ok"):
        return f"{host} is reachable and gated, but `/v1` is not where it should be: {links['v1'].get('error')}"

    if not proxy["ok"]:
        return (
            f"{host} answers from the edge but the SSO proxy behind it does not: "
            f"{proxy['error']}. The name is fine; the container is not."
        )

    # Checked after the proxy and worded differently from it on purpose: everything
    # above this line is reachable, so the only thing left that can be wrong is
    # whether somebody can actually sign in.
    if session is not None and not session.get("ok"):
        return (
            f"{host} is reachable and its proxy is up, but a login would not complete: "
            f"{session.get('error') or 'the session store did not answer'}"
        )

    return f"{host} is reachable and gated as expected."


def exit_code(links: dict[str, dict]) -> int:
    """0 when every link answered. Separate and tiny on purpose: a check that cannot
    fail is not a check, and this is the function the tests hold to that."""
    return 0 if all(link.get("ok") for link in links.values()) else 1


def report(links: dict[str, dict], host: str) -> None:
    dns = links["dns"]
    print(f"{host}")
    print("  1. dns")
    for entry in dns["answers"]:
        if entry.get("error"):
            print(f"     {entry['resolver']:<9} {entry['server']:<16} no answer — {entry['error']}")
        else:
            address = ", ".join(entry.get("addresses") or []) or "no address"
            chain = f" via {entry['cname']}" if entry.get("cname") else ""
            print(f"     {entry['resolver']:<9} {entry['server']:<16} {entry['name']}{chain} {address}")

    if dns.get("warning"):
        print(f"     warning:  {dns['warning']}")

    for key, label in (("tls", "tls"), ("edge", "edge"), ("v1", "v1"), ("proxy", "proxy"), ("session", "session")):
        payload = links.get(key)
        if payload is None:
            continue
        detail = ""
        if key == "tls" and payload.get("ok"):
            detail = f"{payload.get('issuer')} · expires {payload.get('expires')} ({payload.get('days_left')}d)"
        elif key == "edge" and payload.get("ok"):
            detail = f"HTTP {payload.get('status')} · {payload.get('server')} · {payload.get('note') or 'serving'}"
        elif key == "v1" and payload.get("ok"):
            detail = f"public HTTP {payload.get('public')} · lan HTTP {payload.get('lan')} · {payload.get('note')}"
        elif key == "proxy" and payload.get("ok"):
            detail = f"HTTP {payload.get('status')} · {payload.get('body')}"
        elif key == "session" and payload.get("ok"):
            store = payload.get("store") or "unreadable"
            detail = f"127.0.0.1:{payload.get('port')} · sessions in {store} · {payload.get('note')}"
        else:
            detail = payload.get("error") or ""
        mark = "ok  " if payload.get("ok") else "FAIL"
        print(f"  {mark} {label:<6} {detail}")
        if payload.get("warning"):
            print(f"     warning:  {payload['warning']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the gateway's public name, link by link.")
    parser.add_argument("--host", default="", help=f"the name to check (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=0, help="the SSO proxy's port (default GATEWAY_SSO_PORT)")
    parser.add_argument("--resolver", default="", help="also ask this resolver (default CERULEAN_DNS_API_URL's host)")
    parser.add_argument("--no-sso", action="store_true", help="expect a 2xx rather than a redirect to the IdP")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args(argv)

    root = repo_root()
    env = load_env(Path(args.env_file) if args.env_file else root / ".env")
    # The process environment wins for the GATEWAY_* keys, so a caller can point
    # this at another name without editing the file — the same precedence the rest
    # of the stack uses.
    env.update({key: value for key, value in os.environ.items() if key.startswith("GATEWAY_")})

    host = args.host or env.get("GATEWAY_PUBLIC_HOST", "") or DEFAULT_HOST
    port = args.port or int(env.get("GATEWAY_SSO_PORT") or DEFAULT_SSO_PORT)
    if args.resolver:
        env["CERULEAN_DNS_API_URL"] = args.resolver

    if "." not in host:
        print(f"gateway-edge-check: {host!r} is not a hostname to check", file=sys.stderr)
        return 2

    links = {
        "dns": check_dns(host, env),
        "tls": check_tls(host),
        "edge": check_edge(host, expect_sso=not args.no_sso),
        "proxy": check_proxy(port),
    }
    # A published site has no proxy and no session store; asking about them there
    # would report a page's name as broken for not having a login. `/v1` is asked
    # about for the same reason — it is the gateway's route, and a site's name does
    # not serve it either.
    if not args.no_sso:
        links["session"] = check_session(env)
        links["v1"] = check_v1(host, port)

    verdict = diagnose(links, host)
    code = exit_code(links)

    if args.json:
        print(
            json.dumps(
                {"host": host, "ok": code == 0, "verdict": verdict, "links": links},
                indent=2,
                default=str,
            )
        )
        return code

    report(links, host)
    print()
    print(("ok: " if code == 0 else "FAILED: ") + verdict)
    return code


if __name__ == "__main__":
    sys.exit(main())

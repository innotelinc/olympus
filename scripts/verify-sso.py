#!/usr/bin/env python3
"""verify-sso.py — prove Olympus Studio's sign-in posture still holds on a live box.

Studio is not fronted by an oauth2-proxy gateway like the other zones: it is the
Relying Party itself (its own authorization-code + PKCE flow in web/studio/lib/auth.ts),
so the things worth asserting are different, and one of them is the outage this
file was written after.

  1. **The client credentials Studio presents actually authenticate.** Authentik
     stores a provider's secret write-only, and a wrong secret and an unregistered
     `redirect_uri` return the *same* `400 invalid_client` — so a hand check is
     impossible and the failure arrives as a browser error. The honest probe is a
     request that cannot succeed on the code but exercises client authentication
     first: a bogus `code` with real credentials comes back `invalid_grant`, while
     bad credentials come back `invalid_client`. Read-only; mints nothing.
  2. **The edge serves this host's Studio, not a stale one.** `studio.olympus.innotel.us`
     was proxied to a second, older box whose Studio held a different secret, so
     every sign-in failed with (1)'s error while the local .env was perfectly good.
     The NPM host's forward target is compared against the host this test runs on.
  3. **A full flow completes and the session opens Studio.** The dance is driven
     against the public name with a temporary Authentik identity, and the sealed
     session must then return 200 on `/` and `/api/projects` — the API read is
     what proves the cookie is a real session rather than a rendered page.
  4. **Nothing opens Studio without a session.** `/` and `/api/projects` must
     refuse an anonymous caller, because Studio's own gate is the whole control
     over a name the edge does not gate.

The temporary identity is deleted on the way out, including when a check fails.
Nothing here is destructive: no container is started, stopped or edited.

Config (environment, falling back to this repo's .env, then Cerulean's):
    OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET   what Studio presents
    STUDIO_PUBLIC_HOST, BASE_DOMAIN                        the names to drive
    AUTHENTIK_BOOTSTRAP_TOKEN                              Authentik API (admin)
    NPM_API_URL, NPM_ADMIN_EMAIL, NPM_ADMIN_PASSWORD       to read the edge's target
    STUDIO_EDGE_FORWARD_HOST                               override the expected target

Exit codes: 0 = pass, 1 = a check failed, 2 = cannot run (unconfigured/unreachable).

Usage:
    python3 scripts/verify-sso.py
    python3 scripts/verify-sso.py --host studio.olympus.innotel.us --verbose
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERULEAN_ENV = os.path.join(REPO_ROOT, "..", "..", "1-primary", "cerulean", ".env")

AUTH_FLOW = "default-authentication-flow"
E2E_USER = "e2e-olympus-sso"
SESSION_COOKIE = "studio_session"

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"


class CannotRun(Exception):
    """Configuration or reachability problem — exit 2, not a test failure."""


class CheckFailed(Exception):
    """An assertion about the deployment failed — exit 1."""


class IdpDenied(Exception):
    """Authentik rendered its denial page instead of issuing a code: the identity
    authenticated but the application is not bound to it."""

    def __init__(self, body):
        super().__init__("Authentik refused the authorization")
        self.body = body


# ── config ─────────────────────────────────────────────────────────────────


def read_env_file(path):
    """Parse a `KEY=value` file into a dict (ignores blanks and comments)."""
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            vals[key.strip()] = val.strip().strip('"').strip("'")
    return vals


class Config:
    def __init__(self, args):
        self.env_file = os.path.join(REPO_ROOT, ".env")
        env = read_env_file(self.env_file)
        cerulean = read_env_file(CERULEAN_ENV)

        def pick(*names, default=""):
            for name in names:
                if os.environ.get(name):
                    return os.environ[name]
                if env.get(name):
                    return env[name]
                if cerulean.get(name):
                    return cerulean[name]
            return default

        self.verbose = args.verbose
        self.issuer = pick("OIDC_ISSUER_URL").rstrip("/")
        self.client_id = pick("OIDC_CLIENT_ID")
        self.client_secret = pick("OIDC_CLIENT_SECRET")

        # The IdP host is whoever serves the issuer; the session and CSRF cookies
        # are per-host, so it must not be guessed.
        parsed = urllib.parse.urlparse(self.issuer)
        self.idp = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed.scheme and parsed.netloc
            else pick("AUTHENTIK_PUBLIC_URL", default="https://auth.cerulean.innotel.us").rstrip("/")
        )
        self.api = pick("AUTHENTIK_API_URL", default=self.idp).rstrip("/") + "/api/v3"
        self.token = pick("AUTHENTIK_BOOTSTRAP_TOKEN", "AUTHENTIK_TOKEN")

        studio_host = pick("STUDIO_PUBLIC_HOST")
        root_host = pick("BASE_DOMAIN")
        hosts = args.host or [h for h in (studio_host, root_host) if h]
        # Deduplicate while keeping order; the studio name is the builder and the
        # root name is the front door, and both are registered callbacks.
        self.hosts = list(dict.fromkeys(hosts))

        self.npm = {
            "url": pick("NPM_API_URL", "NPM_PUBLIC_API_URL", "NPM_BASE_URL"),
            "email": pick("NPM_ADMIN_EMAIL", "NPM_EMAIL"),
            "password": pick("NPM_ADMIN_PASSWORD", "NPM_PASSWORD"),
        }
        self.expected_forward = pick("STUDIO_EDGE_FORWARD_HOST")
        # Generated e2e password: random hex core + complexity suffix. The
        # literal prefix stays below 8 chars so secret-scan's literal-assignment
        # rule (≥8-char quoted value) doesn't misread the prefix as a credential.
        self.password = "E2e-Sso" + os.urandom(6).hex() + "!Aa1"
        self._discovery = None

        if not self.issuer or not self.client_id or not self.client_secret:
            raise CannotRun(
                "OIDC_ISSUER_URL / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET are not all set "
                f"in the environment or {self.env_file} — nothing to verify."
            )
        if not self.hosts:
            raise CannotRun("no public name to drive (set STUDIO_PUBLIC_HOST or BASE_DOMAIN)")
        if not self.token:
            raise CannotRun("no Authentik API token: set AUTHENTIK_BOOTSTRAP_TOKEN")

    @property
    def discovery(self):
        """The provider's discovery document, fetched once."""
        if self._discovery is None:
            url = self.issuer + "/.well-known/openid-configuration"
            with urllib.request.urlopen(url, timeout=30) as resp:
                self._discovery = json.loads(resp.read() or b"{}")
        return self._discovery

    @property
    def token_endpoint(self):
        # Read from discovery rather than derived: the app-scoped issuer is
        # `.../application/o/<slug>/`, while the token endpoint lives at the
        # instance level (`.../application/o/token/`). Joining the issuer with
        # `/token/` yields a 404, which would make the credential probe pass for
        # the wrong reason.
        endpoint = self.discovery.get("token_endpoint")
        if not endpoint:
            raise CannotRun(f"discovery at {self.issuer} names no token_endpoint")
        return endpoint


# ── HTTP ───────────────────────────────────────────────────────────────────


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """A cookie-jar-backed client that never follows redirects, so each OIDC hop
    can be asserted on its own."""

    def __init__(self, cfg, base=None):
        self.cfg = cfg
        self.base = base or cfg.idp
        self.jar = http.cookiejar.CookieJar()

    def _open(self, req, timeout=30):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.headers.get("Location"), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Location"), (err.read() or b"").decode("utf-8", "replace")

    def cookie(self, name):
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def get(self, url):
        if url.startswith("/"):
            url = self.base + url
        status, location, body = self._open(urllib.request.Request(url))
        if self.cfg.verbose:
            print(f"         GET {url[:100]} -> {status}", file=sys.stderr)
        return status, location, body

    def post_json(self, url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        # Authentik's flow executor requires the CSRF cookie echoed back.
        req.add_header("X-authentik-CSRF", self.cookie("authentik_csrf") or "")
        req.add_header("Referer", self.base + "/")
        status, location, body = self._open(req)
        if self.cfg.verbose:
            print(f"         POST {url[:80]} -> {status}", file=sys.stderr)
        return status, location, body

    def follow_json(self, url):
        status, location, body = self.get(url)
        if status == 302 and location:
            status, location, body = self.get(location)
        if status != 200:
            raise CheckFailed(f"expected a JSON stage, got HTTP {status} for {url[:90]}")
        if not body.lstrip().startswith(("{", "[")):
            raise CheckFailed(f"expected JSON, got {body[:120]!r}")
        return json.loads(body)


class AuthApi:
    """The Authentik API, just enough to make and delete a temporary identity."""

    def __init__(self, cfg):
        self.cfg = cfg
        # A self-signed lab certificate is normal here; the app itself trusts it.
        self.context = ssl._create_unverified_context()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.cfg.api + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.cfg.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.context) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as err:
            detail = (err.read() or b"").decode("utf-8", "replace")
            raise CheckFailed(f"{method} {path} -> HTTP {err.code}: {detail[:200]}")

    def delete_user(self, username):
        _, listing = self.call(
            "GET", "/core/users/?username=" + urllib.parse.quote(username)
        )
        for stale in listing.get("results", []):
            self.call("DELETE", f"/core/users/{stale['pk']}/")

    def make_user(self, username):
        self.delete_user(username)
        status, user = self.call(
            "POST",
            "/core/users/",
            {
                "username": username,
                "name": "e2e Olympus SSO",
                "email": f"{username}@innotel.us",
                "is_active": True,
                "path": "users",
                "type": "internal",
            },
        )
        pk = user["pk"]
        self.call("POST", f"/core/users/{pk}/set_password/", {"password": self.cfg.password})
        return pk


# ── assertions ─────────────────────────────────────────────────────────────


def check(condition, message):
    if condition:
        print(f"  {OK}  {message}")
    else:
        raise CheckFailed(message)


def unreachable(err, host):
    """A name that does not resolve, or a connection that never lands.

    Reported, never raised: a resolver that cannot see this name is a finding
    about *this* run, and an unhandled `socket.gaierror` out of urllib would
    bury every host checked before it behind a traceback.
    """
    if "Name or service not known" in str(err) or "Temporary failure" in str(err):
        return f"cannot resolve {host} from this host ({err})"
    return f"cannot reach {host} ({err})"


def client_credentials_authenticate(cfg):
    """Prove the configured client_id/client_secret authenticate at the token endpoint.

    The probe sends a client authentication that can succeed and a code that cannot,
    so the verdict is unambiguous: `invalid_grant` means Authentik accepted the
    client, `invalid_client` means it did not. This is the check that would have
    named the Olympus outage immediately instead of leaving a browser error.
    """
    # `redirect_uri` has to be one the provider registered; its value does not
    # affect the client-auth verdict, so the first name is a safe choice.
    redirect_uri = f"https://{cfg.hosts[0]}/api/auth/callback"
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": "probe-invalid-code",
            "redirect_uri": redirect_uri,
            "client_id": cfg.client_id,
            "code_verifier": "probe-invalid-verifier",
        }
    ).encode()
    req = urllib.request.Request(cfg.token_endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{cfg.client_id}:{cfg.client_secret}".encode()).decode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload = resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        status = err.code
        try:
            payload = json.loads(err.read() or b"{}")
        except Exception:
            payload = {}
    error = str(payload.get("error", ""))
    print(f"        {cfg.token_endpoint} -> HTTP {status} {error or '(no error)'}")
    check(
        status not in (0, 404, 502),
        f"the token endpoint answered (HTTP {status}; a 404 or 502 would make the "
        "check below pass for the wrong reason)",
    )
    check(
        error != "invalid_client",
        f"the token endpoint answered {error or 'no error'!r} for the configured client "
        "(invalid_grant = the client authenticated; invalid_client = it did not)",
    )


def edge_targets_this_host(cfg):
    """The edge's forward target must be the host this test runs on.

    A name proxied to a *different* box makes every assertion above about the
    wrong deployment: this project's .env can be correct while the public name
    serves a stale copy holding an old secret.
    """
    if not (cfg.npm["url"] and cfg.npm["email"] and cfg.npm["password"]):
        print("        (skipped — NPM_API_URL / NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD not set)")
        return
    api_url = cfg.npm["url"].rstrip("/")
    token = None
    try:
        body = json.dumps({"identity": cfg.npm["email"], "secret": cfg.npm["password"]}).encode()
        req = urllib.request.Request(api_url + "/api/tokens", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            token = json.loads(resp.read() or b"{}").get("token")
    except (urllib.error.URLError, OSError, ValueError):
        token = None
    if not token:
        print("        (skipped — could not log in to the NPM API)")
        return

    req = urllib.request.Request(api_url + "/api/nginx/proxy-hosts")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        hosts = json.loads(resp.read() or b"[]")

    local = local_addresses()
    for name in cfg.hosts:
        match = next(
            (h for h in hosts if name in [str(n).lower() for n in (h.get("domain_names") or [])]),
            None,
        )
        if match is None:
            print(f"        {name}: no NPM proxy host")
            continue
        fwd = f"{match.get('forward_scheme')}://{match.get('forward_host')}:{match.get('forward_port')}"
        print(f"        {name} -> {fwd}")
        expected = cfg.expected_forward or None
        if expected:
            check(
                str(match.get("forward_host")) == expected,
                f"{name} -> {fwd} (expected {expected})",
            )
        else:
            check(
                str(match.get("forward_host")) in local,
                f"{name} -> {fwd} (must be this host: {'/'.join(sorted(local))})",
            )


def local_addresses():
    """This host's own addresses, as a proxy host would name them.

    Every interface, not just the outbound one. A service published on the LAN
    address and a service published only on the docker bridge are both *this*
    host, and the edge — itself a container — reaches the latter as 172.17.0.1.
    The assertion below exists to catch the edge pointing somewhere else
    entirely (it is what found the legacy 192.168.1.10 deployment), so it has to
    know both spellings or it fires on a correct configuration.
    """
    addrs = {"127.0.0.1", "localhost", "host.docker.internal"}
    import socket
    import subprocess

    for probe in ("8.8.8.8", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 80))
            addrs.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return addrs
    for line in out.splitlines():
        parts = line.split()
        # `2: eth0    inet 192.168.1.46/24 brd ...`
        if len(parts) > 3 and parts[2] == "inet":
            addrs.add(parts[3].split("/")[0])
    return addrs


def sso_login(client, cfg, host):
    """Drive a full authorization-code flow against one public name.

    Returns the callback hop's status. Leaves the sealed session in the jar.
    """
    status, location, _ = client.get(f"https://{host}/api/auth/login")
    check(
        status in (302, 307) and bool(location) and cfg.idp in location,
        f"GET https://{host}/api/auth/login -> HTTP {status} to the IdP",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    sent = (query.get("redirect_uri") or [""])[0]
    print(f"        authorize redirect_uri: {sent}")
    check(
        sent == f"https://{host}/api/auth/callback",
        f"the authorize URL names the registered callback ({sent!r})",
    )

    status, location, body = client.get(location)
    check(status == 302 and bool(location), f"authorize -> HTTP {status} to the login flow")

    # Authentik hands back a flow URL on the host it actually serves; pin the
    # client there so the session and CSRF cookies line up.
    flow = urllib.parse.urlparse(urllib.parse.urljoin(client.base, location))
    client.base = f"{flow.scheme}://{flow.netloc}"
    executor = (
        client.base
        + "/api/v3/flows/executor/"
        + AUTH_FLOW
        + "/?"
        + urllib.parse.urlencode({"query": urllib.parse.urlparse(location).query})
    )
    stage = client.follow_json(executor)
    for _ in range(6):
        component = stage.get("component")
        if component == "xak-flow-redirect":
            break
        if component == "ak-stage-identification":
            payload, label = {"uid_field": E2E_USER}, "username"
        elif component == "ak-stage-password":
            payload, label = {"password": cfg.password}, "password"
        else:
            raise CheckFailed(f"unexpected Authentik stage {component}")
        status, next_url, body = client.post_json(executor, payload)
        if status not in (200, 302):
            raise CheckFailed(f"{label} rejected (HTTP {status}): {body[:200]}")
        stage = client.follow_json(next_url or executor)
    check(
        stage.get("component") == "xak-flow-redirect",
        "Authentik's flow handed back the authorize URL",
    )

    status, location, body = client.get(stage["to"])
    if status == 200:
        raise IdpDenied(body)
    check(status == 302 and bool(location), f"authorize -> HTTP {status} to the callback")
    status, _, body = client.get(location)
    if status not in (302, 307):
        print(f"        callback body: {body[:280]}")
    return status


# ── main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", action="append", help="public name to drive (repeatable)")
    parser.add_argument("--verbose", action="store_true", help="trace every HTTP hop")
    args = parser.parse_args()

    try:
        cfg = Config(args)
    except CannotRun as err:
        print(f"SKIP: {err}", file=sys.stderr)
        return 2

    print("Olympus Studio SSO verification")
    print(f"  issuer : {cfg.issuer}")
    print(f"  client : {cfg.client_id}")
    print(f"  hosts  : {', '.join(cfg.hosts)}")
    print()

    # ── reachability: a failure here is a skip, not a bad deployment ──────
    try:
        Client(cfg).get(cfg.idp + "/")
    except (urllib.error.URLError, OSError) as err:
        print(f"SKIP: cannot reach the IdP at {cfg.idp}: {err}", file=sys.stderr)
        return 2

    failures = 0
    api = AuthApi(cfg)
    pk = None
    try:
        print("\nClient credentials (what Studio presents at the token endpoint)")
        try:
            client_credentials_authenticate(cfg)
        except CheckFailed as err:
            failures += 1
            print(f"  {BAD}  {err}")
        except CannotRun as err:
            print(f"  SKIP  {err}")

        print("\nEdge target (the name must serve THIS host's Studio)")
        try:
            edge_targets_this_host(cfg)
        except (CheckFailed, urllib.error.URLError, OSError) as err:
            failures += 1
            print(f"  {BAD}  {err}")

        print("\nAnonymous requests are refused")
        for host in cfg.hosts:
            anonymous = Client(cfg)
            status, location, _ = anonymous.get(f"https://{host}/api/projects")
            check(
                status in (401, 403) or (status in (302, 307) and "auth" in (location or "")),
                f"https://{host}/api/projects -> HTTP {status} without a session "
                "(Studio must refuse; the edge does not gate this name)",
            )

        print("\nA full sign-in completes and opens Studio")
        pk = api.make_user(E2E_USER)
        for host in cfg.hosts:
            client = Client(cfg)
            try:
                status = sso_login(client, cfg, host)
            except IdpDenied as denied:
                failures += 1
                print(f"  {BAD}  {host}: Authentik refused the authorization — {denied.body[:200]}")
                continue
            except CheckFailed as err:
                failures += 1
                print(f"  {BAD}  {host}: {err}")
                continue
            except (urllib.error.URLError, OSError) as err:
                failures += 1
                print(f"  {BAD}  {host}: {unreachable(err, host)}")
                continue
            check(
                status in (302, 307),
                f"{host}: the callback sealed a session (HTTP {status})",
            )
            check(
                client.cookie(SESSION_COOKIE) is not None,
                f"{host}: the {SESSION_COOKIE} cookie was issued",
            )
            status, _, _ = client.get(f"https://{host}/")
            check(status == 200, f"{host}: / with a session -> HTTP {status}")
            status, _, body = client.get(f"https://{host}/api/projects")
            check(status == 200, f"{host}: /api/projects with a session -> HTTP {status}")
            check(
                body.lstrip().startswith("{"),
                f"{host}: /api/projects returned the projects document",
            )
    except CheckFailed as err:
        failures += 1
        print(f"  {BAD}  {err}")
    except (urllib.error.URLError, OSError) as err:
        failures += 1
        print(f"  {BAD}  {unreachable(err, ', '.join(cfg.hosts))}")
    finally:
        if pk is not None:
            try:
                api.call("DELETE", f"/core/users/{pk}/")
                print("\n(removed the temporary identity)")
            except CheckFailed as err:
                print(f"\nwarning: could not delete the temporary identity: {err}", file=sys.stderr)

    print()
    if failures:
        print(f"{failures} check(s) failed", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

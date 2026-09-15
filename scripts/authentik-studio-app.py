#!/usr/bin/env python3
"""Register an OIDC provider + application in Authentik.

Written for Studio, but the only Studio-specific things are its default slug and
its default redirect URIs: `--slug`, `--client-id`, `--name` and `--redirect-uri`
register any other client (the OmniRoute gateway dashboard among them, via
`make gateway-oidc`).

Authentik's API requires a Bearer token — a username/password will not work
(the API answers 403 to basic auth). Create one in the Authentik UI under
Directory -> Tokens (or via the `ak` CLI), then:

    export AUTHENTIK_URL=https://auth.cerulean.innotel.us
    export AUTHENTIK_TOKEN=<token>
    python3 scripts/authentik-studio-app.py --dry-run   # show what would happen
    python3 scripts/authentik-studio-app.py             # create it

Options:
    --redirect-uri URL   what the client will be reachable at (repeatable)
    --client-id ID       default: OIDC_CLIENT_ID from .env, else the slug
    --name NAME          provider + application name (default: capitalised slug)
    --slug SLUG          application slug (default: from OIDC_ISSUER_URL, else studio)
    --env-prefix PFX     names to print as the client's settings (default OIDC_)
    --env-file PATH      configuration to read (default: repo-root .env)
    --insecure           skip TLS verification (self-signed lab certificates)

Configuration falls back to the repo-root `.env` — the same file Studio reads —
so `make studio-oidc` needs no exports. Real process env wins over the file, so
CI can drive it without a file on disk — with one exception, because that rule is
what makes a malformed export dangerous: a `vault://` value *without* its `#key`
fragment is unusable by definition, and if the environment supplied one it would
shadow a perfectly good `.env` and blind this script. Such a value is skipped and
said so, which is the same repair `scripts/studio-token-alert.sh` makes.

`AUTHENTIK_TOKEN` may also be a `vault://<mount>/<path>#<key>` reference: on the
platform the credential lives in Cerulean Vault and `.env` carries only the
reference, exactly as it does for the gateway password. Pass VAULT_ADDR and a
token via VAULT_TOKEN or VAULT_TOKEN_FILE (both usually already in `.env`).

The default redirect URIs are the local dev callback plus one HTTPS callback per
public name: `STUDIO_PUBLIC_HOST` (the builder) and `BASE_DOMAIN` (the root host,
which shows a landing screen with a sign-in button). Studio derives its callback
from the incoming request, so every name it is served on has to be registered
here or `/authorize` rejects the sign-in.

The script is idempotent: if the provider or application already exists it
reports that instead of creating a duplicate, and it PATCHes what is missing —
an empty `grant_types` and any unregistered redirect URI — rather than skipping
a provider that is not actually usable. It resolves the authorization flow,
invalidation flow, scope mappings, and signing key from the instance rather than
assuming fixed identifiers.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

DEFAULT_SCOPES = ["openid", "email", "profile"]
DEFAULT_STUDIO_PORT = "3001"
AUTHORIZATION_FLOW_SLUGS = [
    "default-provider-authorization-implicit-consent",
    "default-provider-authorization-explicit-consent",
]
INVALIDATION_FLOW_SLUGS = ["default-provider-invalidation-flow", "default-invalidation-flow"]


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
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")

    return values


def is_usable(value: str) -> bool:
    """Whether a configured value can actually be used.

    The one shape that cannot is a `vault://` reference with no `#key` fragment: the
    fragment names the field to read, so the reference is malformed, and the
    process environment beating `.env` (which is the rule everywhere in this
    stack) would then make a malformed export blind a checkout whose file is
    fine. A malformed value is therefore skipped rather than preferred.
    """
    if not value.startswith("vault://"):
        return True
    return "#" in value


def resolve_vault_reference(value: str, setting, env_path: Path) -> str:
    """Resolve `vault://<mount>/<path>#<key>` through Cerulean Vault.

    The registration credential is a secret, so `.env` is allowed to carry a
    reference instead of the value — the convention the rest of this stack uses
    (`OMNIROUTE_INITIAL_PASSWORD=vault://…`) and that scripts/omniroute-vault.sh
    already resolves for the gateway. Without this the tool would send the
    literal `vault://…` string as a bearer token and fail as an opaque 403.

    Only references are touched: a plain value is returned unchanged, so a
    checkout with no Vault behaves exactly as before.
    """
    if not value.startswith("vault://"):
        return value

    location, _, key = value[len("vault://"):].partition("#")
    parts = [segment for segment in location.split("/") if segment]
    if len(parts) < 2 or not key:
        sys.exit(
            f"AUTHENTIK_TOKEN is not a usable Vault reference: {value}\n"
            "Expected vault://<mount>/<path>#<KEY>, e.g. "
            "vault://cerulean/olympus/authentik#AUTHENTIK_TOKEN"
        )
    mount, secret_path = parts[0], "/".join(parts[1:])

    address = setting("VAULT_ADDR").rstrip("/")
    if not address:
        sys.exit(
            "AUTHENTIK_TOKEN is a Vault reference but VAULT_ADDR is not set.\n"
            "Set VAULT_ADDR, or put the token itself in AUTHENTIK_TOKEN."
        )

    token = setting("VAULT_TOKEN")
    if not token:
        token_file = setting("VAULT_TOKEN_FILE")
        if not token_file:
            sys.exit(
                "AUTHENTIK_TOKEN is a Vault reference but no Vault token is available.\n"
                "Set VAULT_TOKEN or VAULT_TOKEN_FILE."
            )
        # Relative paths in .env are relative to the repository root, and the
        # operator may run this from anywhere.
        candidate = Path(token_file)
        if not candidate.is_absolute():
            candidate = env_path.parent / candidate
        try:
            token = candidate.read_text(encoding="utf-8").strip()
        except OSError as error:
            sys.exit(f"Could not read VAULT_TOKEN_FILE ({candidate}): {error.strerror}")
        if not token:
            sys.exit(f"VAULT_TOKEN_FILE ({candidate}) is empty.")

    context = None
    if setting("VAULT_SKIP_VERIFY") in ("1", "true", "yes"):
        context = ssl._create_unverified_context()
    elif setting("VAULT_CACERT"):
        context = ssl.create_default_context(cafile=setting("VAULT_CACERT"))

    request = urllib.request.Request(f"{address}/v1/{mount}/data/{secret_path}")
    request.add_header("X-Vault-Token", token)
    request.add_header("Accept", "application/json")
    if setting("VAULT_NAMESPACE"):
        request.add_header("X-Vault-Namespace", setting("VAULT_NAMESPACE"))

    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        sys.exit(
            f"Could not read {value} from Vault at {address}: HTTP {error.code}.\n"
            "Check that the mount/path are right and the token is allowed to read them."
        )
    except (urllib.error.URLError, OSError) as error:
        sys.exit(f"Could not reach Vault at {address}: {getattr(error, 'reason', error)}")

    # KV v2 nests under data.data; KV v1 stops at data.
    outer = payload.get("data") if isinstance(payload, dict) else None
    inner = outer.get("data") if isinstance(outer, dict) else None
    source = inner if isinstance(inner, dict) else (outer if isinstance(outer, dict) else {})
    resolved = source.get(key)
    if not isinstance(resolved, str) or not resolved.strip():
        sys.exit(f"Vault returned no '{key}' at {mount}/{secret_path}.")

    print(f"  token:      resolved from Vault ({mount}/{secret_path}#{key})")
    return resolved.strip()


def verify_client(setting, env_path: Path) -> int:
    """Prove the configured client_id/client_secret actually authenticate.

    A login failure at the token endpoint is opaque from the client side: both a
    wrong secret and a public client return the same `400 invalid_client`, and
    Authentik stores the secret write-only, so nothing can be diffed by hand.

    The one honest probe is a request that CANNOT succeed on the code but does
    exercise client authentication first. A bogus `code` with a real
    client_id/client_secret comes back `invalid_grant` (the client was accepted,
    the code was not) — while bad credentials come back `invalid_client`. The two
    are therefore distinguishable without a browser, and this is read-only: it
    mints nothing and changes nothing.
    """
    import base64

    issuer = setting("OIDC_ISSUER_URL").rstrip("/")
    client_id = setting("OIDC_CLIENT_ID")
    client_secret = setting("OIDC_CLIENT_SECRET")

    if not issuer:
        print("verify-client: OIDC_ISSUER_URL is not set in the environment or " + str(env_path), file=sys.stderr)
        return 2
    if not client_id or not client_secret:
        print(
            "verify-client: OIDC_CLIENT_ID / OIDC_CLIENT_SECRET are not both set — "
            "nothing to verify. Put the values the client presents in .env (or export them).",
            file=sys.stderr,
        )
        return 2

    discovery_url = f"{issuer}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(discovery_url, timeout=30) as response:
            document = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        print(f"verify-client: discovery at {discovery_url} answered HTTP {error.code}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as error:
        print(f"verify-client: could not reach {discovery_url}: {getattr(error, 'reason', error)}", file=sys.stderr)
        return 2

    token_endpoint = document.get("token_endpoint")
    if not token_endpoint:
        print(f"verify-client: discovery at {discovery_url} names no token_endpoint", file=sys.stderr)
        return 2

    # `redirect_uri` must be one the provider knows; the first configured one is
    # a safe default and its value does not affect the client-auth verdict.
    redirect_uri = (default_redirect_uris(setting) or ["http://localhost/callback"])[0]

    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": "probe-invalid-code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": "probe-invalid-verifier",
        }
    ).encode()
    request = urllib.request.Request(token_endpoint, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    request.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode(),
    )

    payload: dict = {}
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            payload = json.loads(error.read() or b"{}")
        except Exception:
            payload = {}
    except (urllib.error.URLError, OSError) as error:
        print(f"verify-client: could not reach {token_endpoint}: {getattr(error, 'reason', error)}", file=sys.stderr)
        return 2

    error_code = str(payload.get("error", ""))
    print(f"verify-client: {token_endpoint} answered HTTP {status} {error_code or '(no error)'}")

    if error_code == "invalid_client":
        print(
            "  FAIL: Authentik rejected the client credentials (invalid_client).\n"
            "  The OIDC_CLIENT_ID / OIDC_CLIENT_SECRET do not authenticate this provider.\n"
            "  Repair:  make studio-oidc ARGS=--rotate-secret   # prints the new secret once\n"
            "  then set OIDC_CLIENT_SECRET (Cerulean Vault) to that value and restart Studio.\n"
            "  A provider left as a PUBLIC client also lands here — this same command\n"
            "  patches client_type back to 'confidential'.",
            file=sys.stderr,
        )
        return 1

    print(
        "  ok: the client authenticated (this probe fails only on the fake code, "
        "which is the point) — the credentials Studio presents are valid."
    )
    return 0


def default_redirect_uris(setting) -> list[str]:
    """Local callback; the public host's callback; the stack's root host too.

    The app answers on two public names: `STUDIO_PUBLIC_HOST` (the builder) and
    `BASE_DOMAIN` (the front door, which shows a landing screen with a
    sign-in button). Studio derives its callback from the incoming request, so
    both names must be registered or sign-in from that host is refused at
    /authorize.
    """
    port = setting("STUDIO_PORT", DEFAULT_STUDIO_PORT)
    uris = [f"http://localhost:{port}/api/auth/callback"]

    host = setting("STUDIO_PUBLIC_HOST")
    if host:
        uris.append(f"https://{host}/api/auth/callback")

    root = setting("BASE_DOMAIN")
    if root and f"https://{root}/api/auth/callback" not in uris:
        uris.append(f"https://{root}/api/auth/callback")

    return uris


class Api:
    def __init__(self, base: str, token: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.context = ssl._create_unverified_context() if insecure else None

    def request(self, method: str, path: str, body: dict | None = None, optional: bool = False) -> object | None:
        url = f"{self.base}/api/v3{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                sys.exit(
                    f"Authentik rejected the token (HTTP {error.code}) for {method} {path}.\n"
                    "Create an API token in Directory -> Tokens and export AUTHENTIK_TOKEN."
                )
            if optional and error.code == 404:
                return None
            # Endpoint paths move between Authentik releases; keep the body short.
            detail = error.read().decode(errors="replace").strip()
            if "<" in detail:
                detail = "(endpoint not found on this release)"
            sys.exit(f"{method} {path} failed: HTTP {error.code} — {detail[:200]}")

    def results(self, path: str, optional: bool = False, **params: str) -> list[dict]:
        query = f"?{urlencode(params)}" if params else ""
        payload = self.request("GET", f"{path}{query}", optional=optional)
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return payload["results"]
        return payload if isinstance(payload, list) else []


def pick_flow(api: Api, slugs: list[str], label: str) -> dict:
    for slug in slugs:
        found = api.results("/flows/instances/", slug=slug)
        if found:
            return found[0]
    sys.exit(
        f"Could not find the {label} flow (tried {', '.join(slugs)}).\n"
        "List them with: curl -H \"Authorization: Bearer $AUTHENTIK_TOKEN\" "
        f"{api.base}/api/v3/flows/instances/"
    )


def pick_scopes(api: Api, names: list[str]) -> list[dict]:
    available = api.results("/propertymappings/provider/scope/")
    by_name = {item.get("scope_name"): item for item in available}
    chosen = [by_name[name] for name in names if name in by_name]

    if "openid" not in {item.get("scope_name") for item in chosen}:
        sys.exit("The instance is missing the 'openid' scope mapping; cannot create a provider.")

    missing = [name for name in names if name not in by_name]
    if missing:
        print(f"  note: scope mapping(s) not present, skipping: {', '.join(missing)}")
    return chosen


def pick_signing_key(api: Api) -> dict | None:
    """The signing key moved from /core/ to /crypto/ in newer Authentik releases."""
    keys: list[dict] = []
    for path in ("/crypto/certificatekeypairs/", "/core/certificatekeypairs/"):
        keys = api.results(path, optional=True)
        if keys:
            break

    if not keys:
        print("  note: no certificate keypair found — Authentik will use its default signer")
        return None

    preferred = next((k for k in keys if "self-signed" in str(k.get("name", "")).lower()), keys[0])
    return preferred


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the Studio OIDC provider in Authentik.")
    parser.add_argument("--redirect-uri", action="append", default=[], help="repeatable")
    parser.add_argument("--client-id", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    # Authentik stores the secret write-only: once a provider exists, its value
    # cannot be read back, so a NEW client that has to share it (the identity-aware
    # proxy in front of the gateway dashboard) cannot be configured from an
    # existing registration. Rotating is the only way to obtain a known value, and
    # it is the safe direction — the previous secret stops working, so this is
    # stated plainly rather than done quietly.
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help="replace an existing provider's client secret and print the new value once",
    )
    parser.add_argument("--api-base", default="")
    parser.add_argument("--token", default="")
    parser.add_argument(
        "--verify-client",
        action="store_true",
        help="probe the token endpoint to prove the configured client_id/client_secret authenticate (read-only)",
    )
    parser.add_argument("--env-file", default="")
    parser.add_argument(
        "--env-prefix",
        default="OIDC_",
        help="prefix to print the client's settings under (default OIDC_ for Studio)",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    file_values = load_env_file(env_path)

    skipped: set[str] = set()

    def setting(name: str, fallback: str = "") -> str:
        """Process env, then the file, then the fallback — unusable values skipped.

        Skipping is the only deviation from "the environment wins", and it exists
        because a fragment-less `vault://` export is not a decision, it is a typo
        — preferring it would fail with the file's good value sitting right there.
        The skip is reported once per variable so it is not silent.
        """
        for candidate in (os.environ.get(name), file_values.get(name), fallback):
            if candidate and is_usable(candidate):
                return candidate
            if candidate and name not in skipped:
                skipped.add(name)
                print(
                    f"  note:       ignoring {name} from the process environment — it is a "
                    "vault:// reference with no #key fragment, so it cannot be resolved; "
                    "using the value from " + str(env_path),
                    file=sys.stderr,
                )
        return ""

    # The client-credential probe needs no Authentik API token, so it answers
    # before this script demands one — a checkout that can reach the issuer but
    # has no API token can still prove whether sign-in will work.
    if args.verify_client:
        return verify_client(setting, env_path)

    api_base = args.api_base or setting("AUTHENTIK_URL")
    token = args.token or resolve_vault_reference(setting("AUTHENTIK_TOKEN"), setting, env_path)

    if not api_base:
        sys.exit("Set AUTHENTIK_URL (e.g. https://auth.cerulean.innotel.us) or pass --api-base.")
    if not token:
        sys.exit("Set AUTHENTIK_TOKEN or pass --token. Create it in Authentik: Directory -> Tokens.")

    # The issuer path IS the application slug, so it is the honest default, and
    # the client id defaults to the slug too — that is how the app is registered
    # for a fresh deployment. These are resolved back onto `args` because the
    # rest of this function reads them by that name.
    configured_issuer = setting("OIDC_ISSUER_URL").rstrip("/")
    args.slug = args.slug or (configured_issuer.rsplit("/", 1)[-1] if configured_issuer else "studio")
    args.client_id = args.client_id or setting("OIDC_CLIENT_ID", args.slug)
    args.name = args.name or args.slug.capitalize()

    redirect_uris = args.redirect_uri or default_redirect_uris(setting)
    api = Api(api_base, token, args.insecure)

    print(f"Authentik: {api.base}")
    if file_values:
        print(f"  env file:  {env_path}")
    print(f"  provider/application name: {args.name} (slug: {args.slug})")
    print(f"  client id: {args.client_id}")
    print("  redirect URIs:")
    for uri in redirect_uris:
        print(f"    {uri}")

    existing_providers = [p for p in api.results("/providers/oauth2/") if p.get("name") == args.name]
    existing_apps = [a for a in api.results("/core/applications/", slug=args.slug) if a.get("slug") == args.slug]

    authorization_flow = pick_flow(api, AUTHORIZATION_FLOW_SLUGS, "authorization")
    invalidation_flow = pick_flow(api, INVALIDATION_FLOW_SLUGS, "invalidation")
    scopes = pick_scopes(api, DEFAULT_SCOPES)
    signing_key = pick_signing_key(api)

    print(f"  authorization flow: {authorization_flow.get('slug')}")
    print(f"  invalidation flow:  {invalidation_flow.get('slug')}")
    print(f"  scope mappings:     {', '.join(str(s.get('scope_name')) for s in scopes)}")
    print(f"  signing key:        {signing_key.get('name') if signing_key else '(default)'}")

    client_secret = secrets.token_urlsafe(48)

    provider_body: dict = {
        "name": args.name,
        "authorization_flow": authorization_flow["pk"],
        "invalidation_flow": invalidation_flow["pk"],
        "client_type": "confidential",
        # Authentik does NOT default this to anything useful — an omitted
        # grant_types lands as an empty list and /authorize then fails with
        # "Invalid grant_type for provider". It must be set explicitly.
        "grant_types": ["authorization_code", "refresh_token"],
        "client_id": args.client_id,
        "client_secret": client_secret,
        "redirect_uris": [{"matching_mode": "strict", "url": uri} for uri in redirect_uris],
        "property_mappings": [s["pk"] for s in scopes],
        "include_claims_in_id_token": True,
        "sub_mode": "hashed_user_id",
        "access_code_validity": "minutes=1",
        "access_token_validity": "minutes=5",
        "refresh_token_validity": "days=30",
    }
    if signing_key:
        provider_body["signing_key"] = signing_key["pk"]

    if existing_providers:
        provider = existing_providers[0]
        current_grants = set(provider.get("grant_types") or [])
        current_uris = [
            entry["url"]
            for entry in (provider.get("redirect_uris") or [])
            if isinstance(entry, dict) and entry.get("url")
        ]
        missing_uris = [uri for uri in redirect_uris if uri not in current_uris]

        # Two repairs, because both fail silently at /authorize: an omitted
        # grant_types lands as [] ("Invalid grant_type for provider"), and a
        # redirect_uri that was never registered is refused the moment Studio is
        # reachable under a new host — which is exactly what happens at launch.
        # An existing-but-unusable provider is worse than a missing one, so a
        # re-run repairs it instead of skipping it.
        patch: dict = {}
        rotated_secret = ""
        if "authorization_code" not in current_grants:
            patch["grant_types"] = ["authorization_code", "refresh_token"]
        if missing_uris:
            patch["redirect_uris"] = [
                {"matching_mode": "strict", "url": uri} for uri in [*current_uris, *missing_uris]
            ]
        # A provider left as a PUBLIC client cannot authenticate at the token
        # endpoint at all: the client sends its secret and Authentik answers
        # `400 invalid_client` ("unsupported authentication method"). None of the
        # repairs above look at HOW the client authenticates, so an app that
        # exists but was created as public stays broken forever while every
        # re-run reports it "fully configured". Confidential is what a
        # server-side client like Studio must be.
        if provider.get("client_type") != "confidential":
            patch["client_type"] = "confidential"
        # Same class of gap, same symptom: a provider registered under a
        # different client_id than the one this client presents is unknown to
        # Authentik, which also answers `invalid_client`. Re-point it instead of
        # leaving a duplicate to be discovered by hand.
        if provider.get("client_id") and provider.get("client_id") != args.client_id:
            patch["client_id"] = args.client_id
        if args.rotate_secret:
            rotated_secret = secrets.token_urlsafe(48)
            patch["client_secret"] = rotated_secret

        if not patch:
            print(f"\n  provider already exists (pk {provider['pk']}) and is fully configured")
            print("  NOTE: its client_secret is not retrievable. Rotate it in the UI if you need it.")
        elif args.dry_run:
            print(f"\n  provider exists (pk {provider['pk']}) — would PATCH:")
            for key, value in patch.items():
                print(f"    {key}: {'<new secret, 64 chars>' if key == 'client_secret' else value}")
        else:
            repaired = api.request("PATCH", f"/providers/oauth2/{provider['pk']}/", patch)
            if "grant_types" in patch:
                print(f"\n  grant_types was {sorted(current_grants)} — patched to {repaired.get('grant_types')}")
                print("  (without this, Authentik answers 'Invalid grant_type for provider' at /authorize)")
            if "client_type" in patch:
                print(f"  client_type was {provider.get('client_type')!r} — patched to 'confidential'")
                print("  (without this, Authentik answers 'invalid_client' at the token endpoint)")
            if "client_id" in patch:
                print(f"  client_id was {provider.get('client_id')!r} — patched to {args.client_id!r}")
                print("  (the client presented a client_id Authentik did not know)")
            if missing_uris:
                print(f"  registered redirect URI(s): {', '.join(missing_uris)}")
            if rotated_secret:
                print(f"  client_id:     {args.client_id}")
                print(f"  client_secret: {rotated_secret}")
                print("  ^ the PREVIOUS secret stopped working — update every client of this")
                print("    provider now; Authentik will not show this value again.")
    elif args.dry_run:
        provider = None
        print("\n  would create provider with redirect_uris:")
        for uri in redirect_uris:
            print(f"    {uri}")
    else:
        provider = api.request("POST", "/providers/oauth2/", provider_body)
        print(f"\n  created provider (pk {provider.get('pk')})")
        print(f"  client_id:     {args.client_id}")
        print(f"  client_secret: {client_secret}")
        print("  ^ store this in Cerulean Vault / .env now — Authentik will not show it again.")

    if existing_apps:
        print(f"  application already exists (slug {args.slug}) — leaving it untouched")
    elif args.dry_run:
        print(f"  would create application '{args.name}' (slug {args.slug})")
    else:
        if provider is None:
            sys.exit("No provider to attach — cannot create the application.")
        api.request(
            "POST",
            "/core/applications/",
            {
                "name": args.name,
                "slug": args.slug,
                "provider": provider["pk"],
                "meta_launch_url": "",
                "policy_engine_mode": "any",
            },
        )
        print(f"  created application '{args.name}' (slug {args.slug})")

    issuer = f"{api.base}/application/o/{args.slug}/"
    prefix = args.env_prefix
    # The names differ per client because the two clients are configured in
    # different places: Studio reads OIDC_* from its own environment, while the
    # gateway dashboard's GATEWAY_OIDC_* pair with the identity-aware proxy in
    # compose.gateway-sso.yml (and with the settings API, if OmniRoute's own OIDC
    # is ever enabled — see docs/gateway-sso.md).
    print(f"\nSet these for this client (prefix {prefix!r}):")
    print(f"  {prefix}ISSUER_URL={issuer}")
    print(f"  {prefix}CLIENT_ID={args.client_id}")
    print(f"  {prefix}CLIENT_SECRET=<the value above, or the one already configured>")
    if prefix == "OIDC_":
        print("  OIDC_REDIRECT_URI=            # empty — Studio derives the callback per request")
    print(f"\nVerify discovery: {issuer}.well-known/openid-configuration")

    if args.dry_run:
        print("\ndry run — nothing was created")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Register the Studio OIDC provider + application in Authentik.

Authentik's API requires a Bearer token — a username/password will not work
(the API answers 403 to basic auth). Create one in the Authentik UI under
Directory -> Tokens (or via the `ak` CLI), then:

    export AUTHENTIK_URL=https://auth.cerulean.innotel.us
    export AUTHENTIK_TOKEN=<token>
    python3 scripts/authentik-studio-app.py --dry-run   # show what would happen
    python3 scripts/authentik-studio-app.py             # create it

Options:
    --redirect-uri URL   what Studio will be reachable at (repeatable)
    --client-id ID       default: OIDC_CLIENT_ID from .env, else the slug
    --name NAME          provider + application name (default: capitalised slug)
    --slug SLUG          application slug (default: from OIDC_ISSUER_URL, else studio)
    --env-file PATH      configuration to read (default: repo-root .env)
    --insecure           skip TLS verification (self-signed lab certificates)

Configuration falls back to the repo-root `.env` — the same file Studio reads —
so `make studio-oidc` needs no exports. Real process env wins over the file, so
CI can drive it without a file on disk.

The default redirect URIs are the local dev callback plus, when
`STUDIO_PUBLIC_HOST` is set, that host's HTTPS callback. Studio derives its
callback from the incoming request, so every name it is served on has to be
registered here or `/authorize` rejects the sign-in.

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


def default_redirect_uris(setting) -> list[str]:
    """Local callback always; the public host's callback when one is configured."""
    port = setting("STUDIO_PORT", DEFAULT_STUDIO_PORT)
    uris = [f"http://localhost:{port}/api/auth/callback"]

    host = setting("STUDIO_PUBLIC_HOST")
    if host:
        uris.append(f"https://{host}/api/auth/callback")

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
    parser.add_argument("--api-base", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    file_values = load_env_file(env_path)

    def setting(name: str, fallback: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or fallback

    api_base = args.api_base or setting("AUTHENTIK_URL")
    token = args.token or setting("AUTHENTIK_TOKEN")

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
        if "authorization_code" not in current_grants:
            patch["grant_types"] = ["authorization_code", "refresh_token"]
        if missing_uris:
            patch["redirect_uris"] = [
                {"matching_mode": "strict", "url": uri} for uri in [*current_uris, *missing_uris]
            ]

        if not patch:
            print(f"\n  provider already exists (pk {provider['pk']}) and is fully configured")
            print("  NOTE: its client_secret is not retrievable. Rotate it in the UI if you need it.")
        elif args.dry_run:
            print(f"\n  provider exists (pk {provider['pk']}) — would PATCH:")
            for key, value in patch.items():
                print(f"    {key}: {value}")
        else:
            repaired = api.request("PATCH", f"/providers/oauth2/{provider['pk']}/", patch)
            if "grant_types" in patch:
                print(f"\n  grant_types was {sorted(current_grants)} — patched to {repaired.get('grant_types')}")
                print("  (without this, Authentik answers 'Invalid grant_type for provider' at /authorize)")
            if missing_uris:
                print(f"  registered redirect URI(s): {', '.join(missing_uris)}")
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
    print("\nSet these in .env for Studio:")
    print(f"  OIDC_ISSUER_URL={issuer}")
    print(f"  OIDC_CLIENT_ID={args.client_id}")
    print("  OIDC_CLIENT_SECRET=<the value above, or the one already configured>")
    print("  OIDC_REDIRECT_URI=            # empty — Studio derives the callback per request")
    print(f"\nVerify discovery: {issuer}.well-known/openid-configuration")

    if args.dry_run:
        print("\ndry run — nothing was created")

    return 0


if __name__ == "__main__":
    sys.exit(main())

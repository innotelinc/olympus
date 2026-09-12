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
    --client-id ID       default: studio
    --name NAME          provider + application name (default: Studio)
    --slug SLUG          application slug (default: studio)
    --insecure           skip TLS verification (self-signed lab certificates)

The script is idempotent: if the provider or application already exists it
reports that instead of creating a duplicate. It resolves the authorization
flow, invalidation flow, scope mappings, and signing key from the instance
rather than assuming fixed identifiers.
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
from urllib.parse import urlencode

DEFAULT_SCOPES = ["openid", "email", "profile"]
AUTHORIZATION_FLOW_SLUGS = [
    "default-provider-authorization-implicit-consent",
    "default-provider-authorization-explicit-consent",
]
INVALIDATION_FLOW_SLUGS = ["default-provider-invalidation-flow", "default-invalidation-flow"]


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
    parser.add_argument("--client-id", default="studio")
    parser.add_argument("--name", default="Studio")
    parser.add_argument("--slug", default="studio")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-base", default=os.environ.get("AUTHENTIK_URL", ""))
    parser.add_argument("--token", default=os.environ.get("AUTHENTIK_TOKEN", ""))
    args = parser.parse_args()

    if not args.api_base:
        sys.exit("Set AUTHENTIK_URL (e.g. https://auth.cerulean.innotel.us) or pass --api-base.")
    if not args.token:
        sys.exit("Set AUTHENTIK_TOKEN or pass --token. Create it in Authentik: Directory -> Tokens.")

    redirect_uris = args.redirect_uri or ["http://localhost:3001/api/auth/callback"]
    api = Api(args.api_base, args.token, args.insecure)

    print(f"Authentik: {api.base}")
    print(f"  provider/application name: {args.name} (slug: {args.slug})")

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
        current = set(provider.get("grant_types") or [])

        if "authorization_code" in current:
            print(f"\n  provider already exists (pk {provider['pk']}) and is correctly configured")
            print("  NOTE: its client_secret is not retrievable. Rotate it in the UI if you need it.")
        elif args.dry_run:
            print(f"\n  provider exists (pk {provider['pk']}) but grant_types={sorted(current)}")
            print("  would PATCH grant_types -> ['authorization_code', 'refresh_token']")
        else:
            # An existing-but-broken provider is worse than a missing one, so a
            # re-run repairs it instead of skipping it.
            repaired = api.request(
                "PATCH",
                f"/providers/oauth2/{provider['pk']}/",
                {"grant_types": ["authorization_code", "refresh_token"]},
            )
            print(f"\n  provider existed with grant_types={sorted(current)} — patched to {repaired.get('grant_types')}")
            print("  (without this, Authentik answers 'Invalid grant_type for provider' at /authorize)")
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
        print("  ^ store this in Infisical / .env now — Authentik will not show it again.")

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
    print("\nSet these for Studio:")
    print(f"  OIDC_ISSUER_URL={issuer}")
    print(f"  OIDC_CLIENT_ID={args.client_id}")
    print("  OIDC_CLIENT_SECRET=<the value above>")
    print(f"  OIDC_REDIRECT_URI={redirect_uris[0]}")
    print(f"\nVerify discovery: {issuer}.well-known/openid-configuration")

    if args.dry_run:
        print("\ndry run — nothing was created")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Mint, rotate and inspect Studio's Authentik registration credential.

`scripts/authentik-studio-app.py` registers Studio's OIDC application and
authenticates with a *scoped* service-account credential. This script owns that
credential: it writes it to Cerulean Vault, dates it, and can rebuild the whole
grant — service account, role, permissions, token — from nothing.

    make studio-token-check      # expiry report; no host access, no admin rights
    make studio-token-rotate     # replace the credential (see the flow below)

Why rotation goes through the Authentik shell rather than the API
---------------------------------------------------------------
All three of these are behaviour of Authentik 2026.8, read out of the running
instance and confirmed against it:

* **REST-created API tokens can't be dated.** `TokenSerializer.validate()` ends
  with `attrs["expires"] = default_token_duration()` for `intent=api`, so the
  tenant's `default_token_duration` wins — `minutes=30` on Cerulean. Asking for
  anything longer is accepted and then discarded. A registration credential that
  dies every half hour is useless, so the token is created out-of-band.
* **`PATCH`ing a token re-parents it to the caller.** `validate()` also runs
  `attrs.setdefault("user", request.user)`, and DRF only calls `validate_user`
  for fields present in the payload — so a partial PATCH that never mentions
  `user` silently reassigns the token to whoever authenticated the request. A
  PATCH that set an expiry on this stack's token turned a service-account
  credential into an administrator one. Never PATCH a token here.
* **`set_key` is safe.** `TokenViewSet.set_key` assigns `token.key` and saves;
  it touches nothing else, so a key can be replaced in place without changing
  who the token belongs to or when it expires.

So the credential is created the way the platform creates its own long-lived
tokens: through the Authentik shell, which can set `expires` freely. `--snippet`
prints the program to run there, `--store-stdin` takes its output and puts the
key in Vault. `make studio-token-rotate` wires the two together, and no host
name, password or key is stored in this repository — the host is a parameter.

The credential
--------------
* `olympus-studio-oidc`, owned by the service account `olympus-studio`, carrying
  the single role `olympus-studio-registration`: the reads and provider writes
  registration needs, and nothing else — no users, groups, roles, outposts, and
  no deletes. It expires (`--ttl-days`, default 180) so a leaked copy stops
  working on its own; `--check` reports the remaining days and exits non-zero
  once it is inside `--warn-days`, so monitoring catches it before `make
  studio-oidc` does.
* No administrator credential is involved anywhere. Rebuilding the grant goes
  through the shell, which is already root-equivalent on the host — an admin API
  token would add a second, weaker copy of that authority for nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Identifiers of the platform objects this script owns; every one is a flag.
DEFAULT_IDENTIFIER = "olympus-studio-oidc"
DEFAULT_SERVICE_ACCOUNT = "olympus-studio"
DEFAULT_ROLE = "olympus-studio-registration"
DEFAULT_TTL_DAYS = 180
DEFAULT_WARN_DAYS = 14
# What the token says it is, so the next operator to open Authentik's UI knows
# why it exists. Deliberately not named `*_TOKEN_*`: it is a label, and a
# constant shaped like a credential is indistinguishable from one to a scanner.
REGISTRATION_DESCRIPTION = "Olympus Studio: OIDC app registration (make studio-oidc)"
# The key length Authentik generates for itself; matching it keeps a minted key
# indistinguishable from one the platform made.
TOKEN_KEY_BYTES = 45

# The role's entire grant. An explicit list rather than a prefix match, because
# this is the thing that has to be *re-checked* on every rebuild: anything wider
# would let this stack read or rewrite identities it has no business touching.
ROLE_PERMISSIONS = [
    "authentik_flows.view_flow",
    "authentik_crypto.view_certificatekeypair",
    "authentik_providers_oauth2.view_scopemapping",
    "authentik_providers_oauth2.view_oauth2provider",
    "authentik_providers_oauth2.add_oauth2provider",
    "authentik_providers_oauth2.change_oauth2provider",
    "authentik_core.view_application",
    "authentik_core.add_application",
]

# Vault layout — the same sub-path `scripts/vault-bootstrap.py` writes and
# `scripts/authentik-studio-app.py` reads, under this stack's own KV path so the
# scoped `olympus` policy covers it.
DEFAULT_PREFIX = "cerulean"
DEFAULT_PATH = "olympus"
AUTHENTIK_SUBPATH = "authentik"

# Labels the shell program writes its three answers under, and `--store-stdin`
# reads back. Deliberately unlikely strings: `ak shell` also emits a wall of JSON
# log lines, so the reader keys off these prefixes rather than parsing the
# stream. They are field names, not values — the `=` belongs to the wire format.
MARKER_KEY = "OLYMPUS_STUDIO_KEY"
MARKER_ACCOUNT = "OLYMPUS_STUDIO_ACCOUNT_PK"
MARKER_EXPIRES = "OLYMPUS_STUDIO_EXPIRES"


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


class Setting:
    """`KEY` lookup with process env winning over the .env file."""

    def __init__(self, values: dict[str, str], env_path: Path) -> None:
        self.values = values
        self.env_path = env_path

    def __call__(self, name: str, fallback: str = "") -> str:
        return os.environ.get(name) or self.values.get(name) or fallback


def vault_context(setting: Setting) -> ssl.SSLContext | bool:
    if setting("VAULT_SKIP_VERIFY") in ("1", "true", "yes"):
        return ssl._create_unverified_context()
    if setting("VAULT_CACERT"):
        return ssl.create_default_context(cafile=setting("VAULT_CACERT"))
    return True


def vault_token(setting: Setting) -> str:
    """VAULT_TOKEN, else VAULT_TOKEN_FILE — the order the Vault CLI uses."""
    token = setting("VAULT_TOKEN")
    if token:
        return token

    token_file = setting("VAULT_TOKEN_FILE")
    if not token_file:
        sys.exit(
            "A Vault credential is required but neither VAULT_TOKEN nor VAULT_TOKEN_FILE "
            "is set.\nSee the Secrets section of docs/stack.md."
        )

    # A relative path in .env is relative to the repository root, and this script
    # may be run from anywhere.
    candidate = Path(token_file)
    if not candidate.is_absolute():
        candidate = setting.env_path.parent / candidate
    try:
        token = candidate.read_text(encoding="utf-8").strip()
    except OSError as error:
        sys.exit(f"Could not read VAULT_TOKEN_FILE ({candidate}): {error.strerror}")
    if not token:
        sys.exit(f"VAULT_TOKEN_FILE ({candidate}) is empty.")
    return token


def vault_request(setting: Setting, path: str, key: str) -> urllib.request.Request:
    request = urllib.request.Request(f"{setting('VAULT_ADDR').rstrip('/')}/v1/{path}")
    request.add_header("X-Vault-Token", vault_token(setting))
    request.add_header("Accept", "application/json")
    if setting("VAULT_NAMESPACE"):
        request.add_header("X-Vault-Namespace", setting("VAULT_NAMESPACE"))
    return request


def resolve_vault_reference(value: str, setting: Setting, label: str) -> str:
    """Resolve `vault://<mount>/<path>#<key>`; a plain value is returned as-is.

    `.env` carries a reference rather than the credential, the convention the
    rest of this stack uses for the gateway password. Without this the tool
    would send the literal `vault://…` string as a bearer token.
    """
    if not value.startswith("vault://"):
        return value

    location, _, key = value[len("vault://"):].partition("#")
    parts = [segment for segment in location.split("/") if segment]
    if len(parts) < 2 or not key:
        sys.exit(
            f"{label} is not a usable Vault reference: {value}\n"
            f"Expected vault://<mount>/<path>#<KEY>, e.g. vault://cerulean/olympus/authentik#{label}"
        )
    mount, secret_path = parts[0], "/".join(parts[1:])

    address = setting("VAULT_ADDR").rstrip("/")
    if not address:
        sys.exit(f"{label} is a Vault reference but VAULT_ADDR is not set.")

    try:
        with urllib.request.urlopen(
            vault_request(setting, f"{mount}/data/{secret_path}", key), timeout=30, context=vault_context(setting)
        ) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        sys.exit(f"Could not read {label} from Vault at {address}: HTTP {error.code}.")
    except (urllib.error.URLError, OSError) as error:
        sys.exit(f"Could not reach Vault at {address}: {getattr(error, 'reason', error)}")

    # KV v2 nests under data.data; KV v1 stops at data.
    outer = payload.get("data") if isinstance(payload, dict) else None
    inner = outer.get("data") if isinstance(outer, dict) else None
    source = inner if isinstance(inner, dict) else (outer if isinstance(outer, dict) else {})
    resolved = source.get(key)
    if not isinstance(resolved, str) or not resolved.strip():
        sys.exit(f"Vault returned no '{key}' at {mount}/{secret_path}.")

    return resolved.strip()


def store_in_vault(setting: Setting, url: str, token: str) -> str:
    """Write both halves of the credential; returns the path for the reference."""
    address = setting("VAULT_ADDR").rstrip("/")
    if not address:
        sys.exit(
            "VAULT_ADDR is not set, so the new credential has nowhere durable to go.\n"
            "Set VAULT_ADDR (and VAULT_TOKEN / VAULT_TOKEN_FILE), then re-run."
        )

    prefix = setting("VAULT_PREFIX", DEFAULT_PREFIX).strip("/")
    path = setting("VAULT_PATH", DEFAULT_PATH).strip("/")
    secret_path = f"{path}/{AUTHENTIK_SUBPATH}"

    request = urllib.request.Request(
        f"{address}/v1/{prefix}/data/{secret_path}",
        data=json.dumps({"data": {"AUTHENTIK_URL": url, "AUTHENTIK_TOKEN": token}}).encode(),
        method="POST",
    )
    request.add_header("X-Vault-Token", vault_token(setting))
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    if setting("VAULT_NAMESPACE"):
        request.add_header("X-Vault-Namespace", setting("VAULT_NAMESPACE"))

    try:
        with urllib.request.urlopen(request, timeout=30, context=vault_context(setting)) as response:
            written = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()[:200]
        sys.exit(f"Writing the credential to {prefix}/{secret_path} failed: HTTP {error.code} — {detail}")
    except (urllib.error.URLError, OSError) as error:
        sys.exit(f"Could not reach Vault at {address}: {getattr(error, 'reason', error)}")

    version = ((written.get("data") or {}) if isinstance(written, dict) else {}).get("version")
    if version is None:
        sys.exit(
            f"{prefix}/ did not answer as KV v2 (the write returned no version).\n"
            "KV v1 has no versioning and cannot hold secrets the way this stack expects."
        )

    # Read it straight back. This is the one moment the value exists nowhere
    # else, so an unverified write is a credential nobody can rely on.
    try:
        with urllib.request.urlopen(
            vault_request(setting, f"{prefix}/data/{secret_path}", "AUTHENTIK_TOKEN"),
            timeout=30,
            context=vault_context(setting),
        ) as response:
            read_back = json.loads(response.read() or b"{}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
        sys.exit(f"Could not read the credential back from {prefix}/{secret_path}: {error}")

    got = ((read_back.get("data") or {}).get("data") or {})
    if got.get("AUTHENTIK_TOKEN") != token or got.get("AUTHENTIK_URL") != url:
        sys.exit("The credential did not read back identically — not trusting it.")

    print(f"  stored at {prefix}/{secret_path} (version {version}), values not printed")
    return f"vault://{prefix}/{secret_path}#AUTHENTIK_TOKEN"


class Authentik:
    """Minimal API client. Never logs the token."""

    def __init__(self, base: str, token: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.context = ssl._create_unverified_context() if insecure else None

    def call(self, method: str, path: str) -> tuple[int, object]:
        """Returns (status, parsed-body); the caller decides what is fatal."""
        request = urllib.request.Request(f"{self.base}/api/v3{path}", method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                payload = response.read()
                return response.status, (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(errors="replace").strip()[:200]
        except (urllib.error.URLError, OSError) as error:
            sys.exit(f"Could not reach Authentik at {self.base}: {getattr(error, 'reason', error)}")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check(setting: Setting, args: argparse.Namespace) -> int:
    """Expiry report. 0 healthy, 1 unusable, 2 lapsing (or never expiring)."""
    base = args.api_base or setting("AUTHENTIK_URL")
    if not base:
        sys.exit("Set AUTHENTIK_URL (e.g. https://auth.cerulean.innotel.us) or pass --api-base.")

    token = args.token or setting("AUTHENTIK_TOKEN")
    if not token:
        sys.exit(
            "No credential to check: set AUTHENTIK_TOKEN (or the vault:// reference "
            "`make vault-bootstrap` writes) and try again."
        )
    token = resolve_vault_reference(token, setting, "AUTHENTIK_TOKEN")

    status, body = Authentik(base, token, args.insecure).call("GET", f"/core/tokens/{args.identifier}/")

    if status in (401, 403):
        print(f"credential: {args.identifier} was REJECTED (HTTP {status})", file=sys.stderr)
        print("  expired or revoked — re-mint it: make studio-token-rotate", file=sys.stderr)
        return 1
    if status == 404:
        print(f"credential: no token named '{args.identifier}' is visible to this credential", file=sys.stderr)
        return 1
    if status != 200 or not isinstance(body, dict):
        print(f"credential: could not read '{args.identifier}' (HTTP {status}) — {body}", file=sys.stderr)
        return 1

    expiring = bool(body.get("expiring"))
    expires = parse_iso(body.get("expires"))
    owner = body.get("user")

    if not expiring or expires is None:
        print(f"credential: {args.identifier} is valid but NEVER EXPIRES (owner pk {owner})", file=sys.stderr)
        print("  rotate it to a dated one: make studio-token-rotate", file=sys.stderr)
        return 2

    remaining = expires - datetime.now(timezone.utc)
    print(f"credential: {args.identifier} valid until {iso(expires)} ({remaining.days} day(s) left, owner pk {owner})")

    if remaining.total_seconds() <= 0:
        print("  already expired — re-mint it: make studio-token-rotate", file=sys.stderr)
        return 1
    if remaining.days <= args.warn_days:
        print(f"  lapsing within {args.warn_days} day(s) — rotate it: make studio-token-rotate", file=sys.stderr)
        return 2
    return 0


def snippet(args: argparse.Namespace) -> str:
    """The program to run in the Authentik shell.

    Rebuilds the whole grant and then rotates the key *in place*, so there is no
    moment where the stack has no working credential: the token keeps its
    identity and only its key and expiry change. Running it twice is harmless.

    It prints three `NAME=value` lines and nothing else that matters — `ak
    shell` buries them in log output, which is why `--store-stdin` filters by
    prefix rather than trying to parse the whole stream.
    """
    permissions = ",\n    ".join(f'"{name}"' for name in ROLE_PERMISSIONS)
    return f'''\
# Olympus Studio: rebuild the OIDC registration credential (generated by
# scripts/authentik-studio-token.py — edit that file, not this program).
import secrets
from datetime import timedelta

from django.contrib.auth.models import Permission
from django.utils import timezone

from authentik.core.models import USER_ATTRIBUTE_TOKEN_EXPIRING, Token, User
from authentik.rbac.models import Role

IDENTIFIER = {args.identifier!r}
SERVICE_ACCOUNT = {args.service_account!r}
ROLE = {args.role!r}
TTL_DAYS = {args.ttl_days}
DESCRIPTION = {REGISTRATION_DESCRIPTION!r}
PERMISSIONS = [
    {permissions},
]

account, _ = User.objects.get_or_create(
    username=SERVICE_ACCOUNT, defaults={{"type": "service_account", "is_active": True}}
)
if account.type != "service_account":
    raise SystemExit(f"{{SERVICE_ACCOUNT}} exists but is a {{account.type}}, not a service account")

# The platform records on the account whether its tokens are meant to expire.
# Ours are, so say so rather than leaving it claiming otherwise.
attributes = dict(account.attributes or {{}})
attributes[USER_ATTRIBUTE_TOKEN_EXPIRING] = True
account.attributes = attributes
account.save()

role, _ = Role.objects.get_or_create(name=ROLE)
found, missing = [], []
for name in PERMISSIONS:
    app_label, _, codename = name.partition(".")
    permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
    (found if permission else missing).append(permission or name)
if missing:
    raise SystemExit(f"permissions not present on this instance: {{missing}}")
role.assign_perms(found)  # idempotent
role.users.add(account)

expires = timezone.now() + timedelta(days=TTL_DAYS)
key = secrets.token_urlsafe({TOKEN_KEY_BYTES})
token = Token.objects.filter(identifier=IDENTIFIER).first()
if token is None:
    token = Token(identifier=IDENTIFIER, user=account, intent="api", description=DESCRIPTION)
# Assign the owner explicitly, every time. A token found here may have been
# re-parented by an API PATCH — the failure this script exists to prevent — and
# an unowned token would otherwise be rotated into an administrator credential.
token.user = account
token.key = key
token.expiring = True
token.expires = expires
token.save()

print("{MARKER_KEY}=" + key)
print("{MARKER_ACCOUNT}=%s" % account.pk)
print("{MARKER_EXPIRES}=%s" % expires.isoformat())
'''


def read_snippet_output(text: str) -> dict[str, str]:
    """Pull the markers out of the shell's output, which is otherwise all logs."""
    found: dict[str, str] = {}
    markers = (("key", MARKER_KEY), ("account", MARKER_ACCOUNT), ("expires", MARKER_EXPIRES))
    for line in text.splitlines():
        for name, marker in markers:
            if line.startswith(f"{marker}="):
                found[name] = line[len(marker) + 1:].strip()
    return found


def store_from_stdin(setting: Setting, args: argparse.Namespace) -> int:
    """Take the shell program's output, store the credential, prove it works."""
    base = args.api_base or setting("AUTHENTIK_URL")
    if not base:
        sys.exit("Set AUTHENTIK_URL (e.g. https://auth.cerulean.innotel.us) or pass --api-base.")

    found = read_snippet_output(sys.stdin.read())
    key = found.get("key", "")
    if not key:
        sys.exit(
            "No credential on stdin.\n"
            "This mode reads the output of the program `--snippet` prints, so run it as:\n"
            "  make studio-token-rotate AUTHENTIK_HOST=<host running cerulean-authentik>\n"
            "or pipe it yourself:\n"
            f"  python3 scripts/authentik-studio-token.py --snippet | ssh <host> "
            f"'docker exec -i cerulean-authentik ak shell' | "
            "python3 scripts/authentik-studio-token.py --store-stdin"
        )

    expires = parse_iso(found.get("expires"))
    owner = found.get("account", "")
    if expires is None:
        # Not fatal, but the expiry is the reason this credential is safe to
        # hold, so a missing date is worth shouting about.
        print("warning: the shell program reported no expiry — check it before relying on --check", file=sys.stderr)

    print("--- storing in Cerulean Vault ---")
    reference = store_in_vault(setting, base, key)

    print("--- exercising the new credential ---")
    scoped = Authentik(base, key, args.insecure)

    status, _ = scoped.call("GET", "/providers/oauth2/")
    if status != 200:
        sys.exit(
            f"The new credential cannot list OAuth2 providers (HTTP {status}) — registration would fail.\n"
            "Re-run make studio-token-rotate; the previous credential is already replaced."
        )
    print("  verified: can read OAuth2 providers (200)")

    # The check that matters most. A credential that can read users is wider than
    # the role allows — which is exactly what a re-parented token looks like.
    status, _ = scoped.call("GET", "/core/users/")
    if status != 403:
        sys.exit(
            f"The new credential can read users (HTTP {status}) — it is wider than the role allows.\n"
            "Something re-parented or re-granted it; do not use this credential. "
            "Inspect it with: make studio-token-check"
        )
    print("  verified: denied on users (403) — least privilege holds")

    status, token_body = scoped.call("GET", f"/core/tokens/{args.identifier}/")
    if status != 200 or not isinstance(token_body, dict):
        sys.exit(f"The new credential cannot read its own token (HTTP {status}) — expiry checks would break.")
    if owner and str(token_body.get("user")) != owner:
        sys.exit(
            f"The credential belongs to user pk {token_body.get('user')}, not the service account (pk {owner}).\n"
            "Refusing to keep it."
        )
    print(f"  verified: owns token '{args.identifier}' as service account pk {owner or token_body.get('user')}")

    print("\n.env needs only the reference:")
    print(f"  AUTHENTIK_URL={base}")
    print(f"  AUTHENTIK_TOKEN={reference}")
    if expires is not None:
        print(f"  expires {iso(expires)} — before then: make studio-token-rotate")
    print("\nNext: make studio-oidc   # re-register with the new credential")
    print("      make studio-oidc-check   # discovery + credential expiry")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint, rotate or inspect Studio's Authentik registration credential."
    )
    parser.add_argument("--check", action="store_true", help="report expiry only; no host or admin access")
    parser.add_argument("--snippet", action="store_true", help="print the program to run in the Authentik shell")
    parser.add_argument("--store-stdin", action="store_true", help="store the credential that program printed")
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS, help=f"default {DEFAULT_TTL_DAYS}")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS, help=f"default {DEFAULT_WARN_DAYS}")
    parser.add_argument("--identifier", default=DEFAULT_IDENTIFIER)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--token", default="", help="the scoped credential, for --check")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    env_path = Path(args.env_file) if args.env_file else Path(__file__).resolve().parent.parent / ".env"
    setting = Setting(load_env_file(env_path), env_path)

    modes = [name for name in ("check", "snippet", "store_stdin") if getattr(args, name)]
    if len(modes) > 1:
        sys.exit("Choose one of --check, --snippet or --store-stdin.")

    if args.check:
        return check(setting, args)
    if args.snippet:
        if args.ttl_days < 1:
            sys.exit("--ttl-days must be at least 1.")
        print(snippet(args), end="")
        return 0
    if args.store_stdin:
        return store_from_stdin(setting, args)

    parser.print_usage(sys.stderr)
    print(
        "\nnothing to do: pick a mode.\n"
        "  --check       report the credential's remaining life (safe to run from monitoring)\n"
        "  --snippet     print the program that rebuilds the grant in the Authentik shell\n"
        "  --store-stdin read that program's output and store the credential in Vault\n"
        "\nUsually you want `make studio-token-check` or `make studio-token-rotate`.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

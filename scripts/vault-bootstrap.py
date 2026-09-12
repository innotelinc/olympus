#!/usr/bin/env python3
"""Store this stack's secrets in Cerulean Vault (HashiCorp Vault, KV v2).

Cerulean is the platform's SecretOps layer: a durable, file-backed Vault with
KV v2 mounted at ``VAULT_PREFIX`` and a periodic token scoped to that prefix.
This script writes the stack's generated secret there, so it stops living in a
plaintext ``.env``.

Every address and credential comes from the environment — nothing is hardcoded
here, because this file is tracked in a public repository.

Required:
    VAULT_ADDR          e.g. http://vault:8200
    VAULT_TOKEN         a token with write access to VAULT_PREFIX
                        (or VAULT_TOKEN_FILE, pointing at one; Vault's own
                        convention, so `vault login` output can be reused)
    VAULT_PREFIX        KV v2 mount point (Cerulean default: cerulean)

Optional:
    VAULT_PATH          path under the mount (default: olympus)
    VAULT_NAMESPACE     Enterprise namespaces; unused on OSS Vault
    VAULT_SKIP_VERIFY   "1" to accept a self-signed certificate
    VAULT_CACERT        CA bundle for TLS
    OMNIROUTE_PASSWORD_FILE
                        also drop the generated password here (0600).
                        Unset = leave it only in Vault.

Once written, ``.env`` may carry a reference instead of the value:

    OMNIROUTE_INITIAL_PASSWORD=vault://cerulean/olympus#INITIAL_PASSWORD

which is exactly the ``vault://<mount>/<path>#<key>`` convention Cerulean
resolves at startup, so the stack and the platform agree on one format.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE_MODE = 0o600
DEFAULT_PREFIX = "cerulean"
DEFAULT_PATH = "olympus"
SECRET_KEY = "INITIAL_PASSWORD"


def fail(message: str) -> "None":
    sys.exit(f"{message}\n")


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(
            f"{name} is not set. Export it (or load your .env) before running this script.\n"
            "See the module docstring for the full list."
        )
    return value


def read_token() -> str:
    """VAULT_TOKEN, else VAULT_TOKEN_FILE — the same order the Vault CLI uses."""
    direct = os.environ.get("VAULT_TOKEN", "").strip()
    if direct:
        return direct

    token_file = os.environ.get("VAULT_TOKEN_FILE", "").strip()
    if not token_file:
        fail(
            "No Vault token. Set VAULT_TOKEN, or VAULT_TOKEN_FILE to a file containing one.\n"
            "On the Cerulean platform the scoped token is written to\n"
            "./data/vault/token/cerulean.token by scripts/vault-entrypoint.sh."
        )

    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        fail(f"Could not read VAULT_TOKEN_FILE ({token_file}): {error.strerror}")

    if not token:
        fail(f"VAULT_TOKEN_FILE ({token_file}) is empty.")
    return token


ADDR = require("VAULT_ADDR").rstrip("/")
TOKEN = read_token()
PREFIX = (os.environ.get("VAULT_PREFIX", "").strip() or DEFAULT_PREFIX).strip("/")
SECRET_PATH = (os.environ.get("VAULT_PATH", "").strip() or DEFAULT_PATH).strip("/")
NAMESPACE = os.environ.get("VAULT_NAMESPACE", "").strip()

_verify: ssl.SSLContext | bool = True
if os.environ.get("VAULT_SKIP_VERIFY", "").strip() in ("1", "true", "yes"):
    _verify = ssl._create_unverified_context()
elif os.environ.get("VAULT_CACERT", "").strip():
    _verify = ssl.create_default_context(cafile=os.environ["VAULT_CACERT"].strip())


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """Call Vault. Returns (status, parsed-body); never logs the token."""
    url = f"{ADDR}/v1/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Vault-Token", TOKEN)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if NAMESPACE:
        request.add_header("X-Vault-Namespace", NAMESPACE)

    try:
        with urllib.request.urlopen(request, timeout=30, context=_verify) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        try:
            detail = json.dumps(json.loads(detail).get("errors", detail))[:200]
        except Exception:
            detail = detail[:200]
        return error.code, {"errors": detail}
    except urllib.error.URLError as error:
        fail(
            f"Could not reach Vault at {ADDR}: {error.reason}\n"
            "Check VAULT_ADDR, and that the server is unsealed."
        )


def probe_kv2() -> None:
    """Confirm PREFIX is a KV v2 mount without reading sys/mounts.

    The platform's scoped token usually cannot read sys/mounts — that endpoint is
    cluster-wide, while the token is scoped to one prefix. A 403 there is expected
    and must not be fatal: the mount's own metadata endpoint is authoritative and
    sits inside the scope.
    """
    status, listing = api("LIST", f"{PREFIX}/metadata")
    if status == 200:
        print(f"--- {PREFIX}/ answers as KV v2 (metadata listing) ---")
        return
    if status == 404:
        fail(
            f"{PREFIX}/ has no metadata endpoint, so it is not a KV v2 mount.\n"
            "Point VAULT_PREFIX at the KV v2 mount (Cerulean's default is `cerulean`)."
        )
    fail(f"Could not verify the KV mount at {PREFIX}/: HTTP {status} — {listing.get('errors')}")


def ensure_kv2() -> None:
    """KV v2 must be mounted at PREFIX. Create it if absent, verify if present."""
    status, mounts = api("GET", "sys/mounts")
    if status == 403:
        # Scoped token: cannot see the cluster's mount table. Verify instead
        # through the prefix we were actually granted.
        print(f"--- sys/mounts is not readable with this token (scoped) — probing {PREFIX}/ ---")
        probe_kv2()
        return
    if status != 200:
        fail(f"Reading sys/mounts failed: HTTP {status} — {mounts.get('errors')}")

    existing = (mounts.get("data") or mounts).get(f"{PREFIX}/")
    if existing is None:
        print(f"--- enabling KV v2 at {PREFIX}/ ---")
        status, created = api(
            "POST", f"sys/mounts/{PREFIX}", {"type": "kv", "options": {"version": "2"}}
        )
        if status not in (200, 204):
            fail(f"Could not enable KV v2 at {PREFIX}/: HTTP {status} — {created.get('errors')}")
        print(f"    enabled {PREFIX}/ as KV v2")
        return

    version = str((existing.get("options") or {}).get("version", "1"))
    if version != "2":
        fail(
            f"{PREFIX}/ is mounted as KV v{version}, not v2.\n"
            "KV v1 has no versioning or metadata; enable KV v2 at a different prefix\n"
            f"(VAULT_PREFIX) or migrate the mount."
        )
    print(f"--- {PREFIX}/ is KV v2 ---")


def main() -> int:
    print(f"Vault:  {ADDR}")
    print(f"  mount: {PREFIX}/  path: {SECRET_PATH}  token: {len(TOKEN)} chars")

    ensure_kv2()

    # /v1/status is unauthenticated and 200 only while unsealed.
    status, sealed = api("GET", "sys/seal-status")
    if status == 200 and sealed.get("sealed"):
        fail("Vault is sealed. Unseal it first (scripts/vault-entrypoint.sh does this on start).")

    omni_password = "OR-" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(24)
    )

    print(f"--- writing {SECRET_KEY} to {PREFIX}/{SECRET_PATH} ---")
    status, written = api(
        "POST",
        f"{PREFIX}/data/{SECRET_PATH}",
        {"data": {SECRET_KEY: omni_password, "STACK": "olympus"}},
    )
    if status not in (200, 204):
        fail(f"Writing the secret failed: HTTP {status} — {written.get('errors')}")
    version = ((written.get("data") or {}).get("version")) if isinstance(written, dict) else None
    print(f"    stored (version {version}), value not printed")

    # Prove it round-trips before claiming success.
    status, read = api("GET", f"{PREFIX}/data/{SECRET_PATH}")
    if status != 200:
        fail(f"Could not read the secret back: HTTP {status} — {read.get('errors')}")
    got = ((read.get("data") or {}).get("data") or {})
    if got.get(SECRET_KEY) != omni_password:
        fail("The secret did not read back identically.")
    print(f"    verified: keys present = {sorted(got)}")

    password_file = os.environ.get("OMNIROUTE_PASSWORD_FILE", "").strip()
    if password_file:
        # Explicit opt-in only: a predictable /tmp path is world-readable.
        path = Path(password_file)
        path.write_text(omni_password + "\n")
        path.chmod(ENV_FILE_MODE)
        print(f"    also wrote {path} (0600)")
    else:
        print("    password left in Vault only (set OMNIROUTE_PASSWORD_FILE to also drop it)")

    print("\nReference it from .env instead of pasting the value:")
    print(f"  OMNIROUTE_INITIAL_PASSWORD=vault://{PREFIX}/{SECRET_PATH}#{SECRET_KEY}")
    print(f"\nVerify: curl -H \"X-Vault-Token: $VAULT_TOKEN\" {ADDR}/v1/{PREFIX}/data/{SECRET_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

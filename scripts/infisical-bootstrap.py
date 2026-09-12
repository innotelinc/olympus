#!/usr/bin/env python3
"""Bootstrap Infisical: create project, secrets, identity, and token.

Every credential and address comes from the environment — nothing is hardcoded
here, because this file is tracked in a public repository. Set at least:

    INFISICAL_ADDR             e.g. http://infisical.internal:8088
    INFISICAL_ADMIN_EMAIL
    INFISICAL_ADMIN_PASSWORD
    INFISICAL_ORG_ID

Optional:
    INFISICAL_WORKSPACE_NAME   default: olympus
    INFISICAL_ENVIRONMENT      default: dev
    OMNIROUTE_PASSWORD_FILE    where to drop the generated OmniRoute password
                               (0600). Unset = do not write it anywhere.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path

ENV_FILE_MODE = 0o600


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"{name} is not set. Export it (or load your .env) before running this script.\n"
            "See the module docstring for the full list."
        )
    return value


INFISICAL = require("INFISICAL_ADDR").rstrip("/")
ADMIN_EMAIL = require("INFISICAL_ADMIN_EMAIL")
ADMIN_PASSWORD = require("INFISICAL_ADMIN_PASSWORD")
ORG_ID = require("INFISICAL_ORG_ID")

WORKSPACE_NAME = os.environ.get("INFISICAL_WORKSPACE_NAME", "olympus").strip() or "olympus"
ENVIRONMENT = os.environ.get("INFISICAL_ENVIRONMENT", "dev").strip() or "dev"


def api(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    cmd = [
        "curl",
        "-sS",
        "-X",
        method,
        f"{INFISICAL}{path}",
        "-H",
        "Content-Type: application/json",
    ]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if body:
        cmd += ["-d", json.dumps(body)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    # Never echo request headers or bodies that may carry credentials.
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"raw_len": len(result.stdout), "err": result.stderr.strip()[:200]}


def main() -> None:
    print(f"--- 1. login to {INFISICAL} as {ADMIN_EMAIL} ---")
    login = subprocess.run(
        [
            "infisical",
            "login",
            "--method",
            "user",
            "--email",
            ADMIN_EMAIL,
            "--password",
            ADMIN_PASSWORD,
            "--organization-id",
            ORG_ID,
            "--domain",
            INFISICAL,
            "--plain",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    token = login.stdout.strip()
    if not token or len(token) < 50:
        # Deliberately does not print login.stderr: it can echo the password back.
        sys.exit("Login failed. Check INFISICAL_ADMIN_EMAIL / INFISICAL_ADMIN_PASSWORD / INFISICAL_ORG_ID.")

    print(f"    authenticated (token length {len(token)})")

    print(f"--- 2. create workspace '{WORKSPACE_NAME}' ---")
    workspace = api("POST", "/api/v2/workspace", token, {"projectName": WORKSPACE_NAME})
    workspace_id = (
        workspace.get("project", {}).get("id", "")
        or workspace.get("workspace", {}).get("id", "")
    )
    if not workspace_id:
        sys.exit(f"Could not create the workspace: {json.dumps(workspace)[:300]}")
    print(f"    workspace_id={workspace_id}")

    omni_password = "OR-" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))

    print("--- 3. store the OmniRoute initial password ---")
    created = api(
        "POST",
        "/api/v3/secrets/batch-create",
        token,
        {
            "workspaceId": workspace_id,
            "environment": ENVIRONMENT,
            "secretPath": "/",
            "secrets": [
                {"secretName": "INITIAL_PASSWORD", "secretValue": omni_password, "type": "shared"}
            ],
        },
    )
    if created.get("raw_len") is not None or created.get("error"):
        sys.exit(f"Could not store the secret: {json.dumps(created)[:300]}")
    print("    INITIAL_PASSWORD stored (value not printed)")

    print("--- 4. create the machine identity ---")
    identity = api(
        "POST",
        f"/api/v1/workspace/{workspace_id}/identities",
        token,
        {"name": "olympus-factory", "role": "member"},
    )
    identity_id = identity.get("identity", {}).get("id", "") or identity.get("id", "")
    if not identity_id:
        sys.exit(f"Could not create the identity: {json.dumps(identity)[:300]}")
    print(f"    identity_id={identity_id}")

    print("--- 5. attach secrets to the identity ---")
    api(
        "POST",
        f"/api/v1/identities/{identity_id}/secrets",
        token,
        {"environment": ENVIRONMENT, "secretPath": "/", "secrets": ["INITIAL_PASSWORD"]},
    )

    print("--- 6. mint a universal-auth token ---")
    identity_token = api(
        "POST",
        f"/api/v1/identities/{identity_id}/auth/universal-auth",
        token,
        {"description": "factory-launcher"},
    )
    infisical_token = identity_token.get("accessToken", "")
    if not infisical_token:
        sys.exit(f"Could not mint a token: {json.dumps(identity_token)[:300]}")
    print(f"    token minted (length {len(infisical_token)})")

    print("--- 7. write .env (0600) ---")
    env_path = Path(".env")
    env_path.write_text(
        "\n".join(
            [
                f"INFISICAL_ADDR={INFISICAL}",
                f"INFISICAL_PROJECT_ID={workspace_id}",
                f"INFISICAL_TOKEN={infisical_token}",
                f"INFISICAL_ENVIRONMENT={ENVIRONMENT}",
                "INFISICAL_PATH=/",
                "OMNIROUTE_HOST=0.0.0.0",
                "OMNIROUTE_PORT=20128",
                "",
            ]
        )
    )
    env_path.chmod(ENV_FILE_MODE)
    print("    wrote .env")

    password_file = os.environ.get("OMNIROUTE_PASSWORD_FILE", "").strip()
    if password_file:
        # Explicit opt-in only: a predictable /tmp path is world-readable.
        path = Path(password_file)
        path.write_text(omni_password + "\n")
        path.chmod(ENV_FILE_MODE)
        print(f"    wrote the OmniRoute password to {path} (0600)")
    else:
        print("    OmniRoute password left in Infisical as INITIAL_PASSWORD (set")
        print("    OMNIROUTE_PASSWORD_FILE to also drop it on disk).")

    print("\nDONE")


if __name__ == "__main__":
    main()

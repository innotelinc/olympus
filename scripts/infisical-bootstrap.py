#!/usr/bin/env python3
"""Bootstrap Infisical: create project, secrets, identity, and token."""
import json, subprocess, sys, secrets, string

INFISICAL = "http://192.168.1.10:8088"
ADMIN_EMAIL = "REDACTED"
ADMIN_PASS = "REDACTED"

def api(method, path, token=None, body=None):
    cmd = ["curl", "-sS", "-X", method, f"{INFISICAL}{path}",
           "-H", "Content-Type: application/json"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if body:
        cmd += ["-d", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"raw": r.stdout, "err": r.stderr}

# 1. Login via CLI to get JWT
print("--- 1. login ---")
r = subprocess.run(
    ["infisical", "login", "--method", "user",
     "--email", ADMIN_EMAIL, "--password", ADMIN_PASS,
     "--organization-id", "87722b0f-3b23-482a-8bf3-8fddf1c9d3a6",
     "--domain", INFISICAL, "--plain"],
    capture_output=True, text=True, timeout=30
)
TOKEN = r.stdout.strip()
if not TOKEN or len(TOKEN) < 50:
    print(f"Login failed: {r.stderr}")
    sys.exit(1)
print(f"token_len={len(TOKEN)}")

# 2. Create workspace
print("\n--- 2. create workspace ---")
ws = api("POST", "/api/v2/workspace", TOKEN, {"projectName": "olympus"})
print(json.dumps(ws, indent=2)[:500])
WS_ID = ws.get("project", {}).get("id", "") or ws.get("workspace", {}).get("id", "")
if not WS_ID:
    print("FAILED to create workspace")
    sys.exit(1)
print(f"workspace_id={WS_ID}")

# 3. Generate OmniRoute password
omni_pass = "OR-" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
print(f"\nomniroute_password={omni_pass}")

# 4. Create secret via batch endpoint
print("\n--- 3. create secret ---")
sec = api("POST", "/api/v3/secrets/batch-create", TOKEN, {
    "workspaceId": WS_ID,
    "environment": "dev",
    "secretPath": "/",
    "secrets": [{
        "secretName": "INITIAL_PASSWORD",
        "secretValue": omni_pass,
        "type": "shared"
    }]
})
print(json.dumps(sec, indent=2)[:500])

# 5. Create machine identity
print("\n--- 4. create identity ---")
ident = api("POST", f"/api/v1/workspace/{WS_ID}/identities", TOKEN, {
    "name": "olympus-factory",
    "role": "member"
})
print(json.dumps(ident, indent=2)[:500])
IDENT_ID = ident.get("identity", {}).get("id", "") or ident.get("id", "")
if not IDENT_ID:
    print("FAILED to create identity")
    sys.exit(1)
print(f"identity_id={IDENT_ID}")

# 6. Attach secrets
print("\n--- 5. attach secrets ---")
attach = api("POST", f"/api/v1/identities/{IDENT_ID}/secrets", TOKEN, {
    "environment": "dev",
    "secretPath": "/",
    "secrets": ["INITIAL_PASSWORD"]
})
print(json.dumps(attach, indent=2)[:300])

# 7. Create universal auth token
print("\n--- 6. create identity token ---")
idtoken = api("POST", f"/api/v1/identities/{IDENT_ID}/auth/universal-auth", TOKEN, {
    "description": "factory-launcher"
})
print(json.dumps(idtoken, indent=2)[:300])
INFISICAL_TOKEN = idtoken.get("accessToken", "")
print(f"infisical_token_len={len(INFISICAL_TOKEN)}")

if not INFISICAL_TOKEN:
    print("FAILED to create token")
    sys.exit(1)

# 8. Write .env
print("\n--- 7. writing .env ---")
with open(".env", "w") as f:
    f.write(f"""INFISICAL_DOMAIN={INFISICAL}
INFISICAL_PROJECT_ID={WS_ID}
INFISICAL_TOKEN={INFISICAL_TOKEN}
INFISICAL_ENV=dev
INFISICAL_PATH=/
OMNIROUTE_PORT=20128
OMNIROUTE_HOST=0.0.0.0
""")
print("wrote .env")

with open("/tmp/omniroute-password.txt", "w") as f:
    f.write(omni_pass)
print(f"omniroute_password={omni_pass}")
print("\nDONE")

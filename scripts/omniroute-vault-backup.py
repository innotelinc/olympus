#!/usr/bin/env python3
"""Back the gateway's state up to Cerulean Vault, and restore it.

WHY THIS EXISTS. Everything that makes the gateway *this* gateway lives in one
Docker volume: the provider connections, the settings, the dashboard password
hash — and `server.env`, whose `STORAGE_ENCRYPTION_KEY` is what decrypts those
connections. The key therefore sits in the same box as the data it protects, so
losing the volume does not lose the connections in the sense of "they are gone
from a list" — it loses them the way a locked box loses its contents. Measured
during the move into this repository: all ten connections came across only
because `server.env` came with them.

`scripts/omniroute-restore-providers.py` already answers "how do I move
connections between gateways". This answers the other half — "what do I hold on
to, so that a gateway can be rebuilt at all" — by putting both halves of that
pair in the platform's secret store rather than on the host that is at risk.

    scripts/omniroute-vault-backup.py                # back up (default)
    scripts/omniroute-vault-backup.py --check        # what is stored, and does it match?
    scripts/omniroute-vault-backup.py --restore      # put it back (asks for --force)
    scripts/omniroute-vault-backup.py --dry-run      # say what would be written

What is stored, under this stack's own path (`<VAULT_PREFIX>/<VAULT_PATH>/omniroute`,
which the scoped `olympus` policy already covers):

    SERVER_ENV       the data dir's `server.env`, verbatim — the decryption keys
    PROVIDERS_JSON   `omniroute auth export` output: every connection, decrypted
    BACKED_UP_AT     when this ran, UTC
    PROVIDER_COUNT   how many connections that JSON held
    SOURCE           the data dir the backup was read from

Nothing is ever printed: the report carries counts, sizes and version numbers.
Both halves are needed and neither is sufficient — a fresh volume with the
connections restored is a gateway that cannot decrypt them, and a fresh volume
with only `server.env` is a gateway with nothing to decrypt.

Exit codes, kept apart because they send you to different places:

    0  done (or, under --check, stored state matches the live gateway)
    1  the backup or restore failed
    2  the checkout or the gateway is not in a state where this means anything

Reads configuration from the repo-root `.env`, the same file the stack reads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "omniroute-restore-providers.py"

DEFAULT_PREFIX = "cerulean"
DEFAULT_PATH = "olympus"
VAULT_ENTRY = "omniroute"
DEFAULT_CONTAINER = "olympus-omniroute"
DEFAULT_HOST_DATA_DIR = Path("/root/.omniroute")

KEY_SERVER_ENV = "SERVER_ENV"
KEY_PROVIDERS = "PROVIDERS_JSON"
KEY_BACKED_UP_AT = "BACKED_UP_AT"
KEY_PROVIDER_COUNT = "PROVIDER_COUNT"
KEY_SOURCE = "SOURCE"

# Values shipped in .env.example as "put something here". Same guard as
# scripts/cerulean-edge.py: a placeholder that shadows the real value turns a
# working checkout into an opaque failure, and the environment is the usual way
# that happens.
TEMPLATE_PLACEHOLDERS = {"", "change-me", "changeme", "example", "placeholder"}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2


class Failure(RuntimeError):
    """Something the operator has to fix; the message is safe to print."""


# --- configuration ------------------------------------------------------------


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


def is_usable(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate not in TEMPLATE_PLACEHOLDERS and not candidate.startswith("change-me")


def setting(file_values: dict[str, str], key: str, default: str = "") -> str:
    """`.env` first, then the process environment.

    Deliberately the opposite order to the rest of the tooling. Every value this
    script needs (`VAULT_*`) is one a harness may export as a *template*
    placeholder; measured on this platform, `CERULEAN_ADMIN_PASSWORD` arrived
    that way and the environment's value shadowed the real one in `.env`. A file
    that exists is the operator's own statement of intent, so it wins here, and a
    placeholder never does.
    """
    from_file = (file_values.get(key) or "").strip()
    if from_file and is_usable(from_file):
        return from_file
    from_env = (os.environ.get(key) or "").strip()
    return from_env if is_usable(from_env) else ""


# --- vault --------------------------------------------------------------------


class Vault:
    def __init__(self, addr: str, token: str, prefix: str, path: str) -> None:
        self.addr = addr.rstrip("/")
        self.token = token
        self.prefix = prefix.strip("/")
        self.path = path.strip("/")
        self.secret_path = f"{self.prefix}/data/{self.path}/{VAULT_ENTRY}"
        self.metadata_path = f"{self.prefix}/metadata/{self.path}/{VAULT_ENTRY}"

    def call(self, method: str, api_path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.addr}/v1/{api_path.lstrip('/')}", data=data, method=method)
        request.add_header("X-Vault-Token", self.token)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
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
            raise Failure(f"could not reach Vault at {self.addr}: {error.reason}") from None

    def write(self, values: dict[str, object]) -> int:
        status, body = self.call("POST", self.secret_path, {"data": values})
        if status not in (200, 204):
            if status == 403:
                raise Failure(
                    f"the Vault token cannot write {self.prefix}/data/{self.path}/{VAULT_ENTRY}.\n"
                    "The `olympus` policy covers this stack's own path; check VAULT_PATH."
                )
            raise Failure(f"writing {self.secret_path} failed: HTTP {status} — {body.get('errors')}")
        version = ((body.get("data") or {}).get("version")) if isinstance(body, dict) else None
        return int(version or 0)

    def read(self) -> tuple[dict, int | None, str]:
        """Returns (values, version, updated_at). Absent is not an error: it is the
        state this script exists to change."""
        status, body = self.call("GET", self.secret_path)
        if status == 404:
            return {}, None, ""
        if status == 403:
            raise Failure(
                f"the Vault token cannot read {self.prefix}/data/{self.path}/{VAULT_ENTRY}."
            )
        if status != 200:
            raise Failure(f"reading {self.secret_path} failed: HTTP {status} — {body.get('errors')}")
        data = body.get("data") or {}
        values = data.get("data") or {}
        metadata = data.get("metadata") or {}
        if not isinstance(values, dict):
            raise Failure(f"{self.secret_path} is not a KV v2 secret (no data.data nesting).")
        return values, metadata.get("version"), str(metadata.get("updated_time") or "")

    def probe_write(self) -> None:
        """Fail before doing any work if the grant is wrong."""
        status, _ = self.call("GET", f"{self.prefix}/data/{self.path}")
        if status == 403:
            raise Failure(f"the Vault token cannot read {self.prefix}/data/{self.path}.")

    def dashboard_password(self) -> str:
        """The stack's gateway password, from the secret `.env` already references.

        Restore used to hand the restore script `OMNIROUTE_API_KEY` from `.env` as a
        management token, and it is not one — measured: "the API key was rejected as a
        management token". The key `.env` carries is a *client* key for inference; the
        dashboard password is the credential that can write providers, and it is
        already in this stack's Vault path (`OMNIROUTE_INITIAL_PASSWORD=vault://…`).
        """
        status, body = self.call("GET", f"{self.prefix}/data/{self.path}")
        if status != 200:
            return ""
        values = (body.get("data") or {}).get("data") or {}
        return str(values.get("INITIAL_PASSWORD") or "")


# --- the gateway's state ------------------------------------------------------


def detect_data_dir(explicit: str, container: str) -> Path:
    """Where the gateway's state lives.

    An explicit path wins; then `OMNIROUTE_DATA_DIR`; then the running container's
    mount of `/app/data` (the deployment in this repository); then the host-run
    data dir setup.sh uses. The container is asked rather than assumed, because
    the whole point of this script is that the location is easy to get wrong.
    """
    if explicit:
        return Path(explicit).expanduser()
    env_dir = (os.environ.get("OMNIROUTE_DATA_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser()

    docker = shutil.which("docker")
    if docker:
        try:
            result = subprocess.run(
                [docker, "inspect", container, "--format", "{{json .Mounts}}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            try:
                for mount in json.loads(result.stdout):
                    if mount.get("Destination") == "/app/data" and mount.get("Source"):
                        return Path(str(mount["Source"]))
            except ValueError:
                pass
    return DEFAULT_HOST_DATA_DIR


def read_server_env(data_dir: Path) -> str:
    path = data_dir / "server.env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise Failure(
            f"could not read {path} ({error.strerror}).\n"
            "That file holds STORAGE_ENCRYPTION_KEY, which is what makes the gateway's\n"
            "stored connections readable. Point --data-dir at the gateway's data dir."
        ) from None
    if "STORAGE_ENCRYPTION_KEY" not in text:
        raise Failure(f"{path} has no STORAGE_ENCRYPTION_KEY — that is not the gateway's data dir.")
    return text


def load_restore_module():
    """Reuse the one implementation of the export/import path.

    `omniroute-restore-providers.py` already knows how to read a data dir's
    connections with `omniroute auth export` and how to write them into a
    gateway. Importing it keeps this script from growing a second, subtly
    different copy of that logic.
    """
    spec = importlib.util.spec_from_file_location("restore_providers", RESTORE_SCRIPT)
    if not spec or not spec.loader:
        raise Failure(f"could not load {RESTORE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export_connections(data_dir: Path) -> list[dict]:
    restore = load_restore_module()
    try:
        entries = restore.export_credentials(str(data_dir), None)
    except restore.GatewayError as error:
        raise Failure(str(error)) from None
    return entries


def count_providers(values: dict) -> int:
    raw = values.get(KEY_PROVIDERS)
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        parsed = json.loads(raw)
    except ValueError:
        return 0
    entries = parsed if isinstance(parsed, list) else parsed.get("connections", [])
    return len([entry for entry in entries if isinstance(entry, dict)])


def redact(value: object) -> str:
    """A fingerprint, never a value: lets the report prove two things match."""
    import hashlib

    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# --- modes --------------------------------------------------------------------


def do_backup(vault: Vault, data_dir: Path, dry_run: bool, as_json: bool) -> int:
    server_env = read_server_env(data_dir)
    entries = export_connections(data_dir)
    providers_json = json.dumps(entries, indent=2)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = {
        KEY_SERVER_ENV: server_env,
        KEY_PROVIDERS: providers_json,
        KEY_BACKED_UP_AT: stamp,
        KEY_PROVIDER_COUNT: len(entries),
        KEY_SOURCE: str(data_dir),
    }

    report = {
        "vault": f"{vault.addr} {vault.secret_path}",
        "data_dir": str(data_dir),
        "providers": len(entries),
        "providers_sha": redact(entries),
        "server_env_bytes": len(server_env),
        "server_env_sha": redact(server_env),
        "backed_up_at": stamp,
    }

    if dry_run:
        report["written"] = False
        version = None
    else:
        version = vault.write(values)
        report["written"] = True
        report["version"] = version

    if as_json:
        print(json.dumps(report, indent=2))
        return EXIT_OK

    print(f"vault      {vault.addr}  {vault.secret_path}")
    print(f"data dir   {data_dir}")
    print(f"providers  {len(entries)} connection(s), sha {report['providers_sha']}")
    print(f"server.env {len(server_env)} bytes, sha {report['server_env_sha']} (keys stored, not shown)")
    if dry_run:
        print("dry run — nothing written")
    else:
        print(f"stored     version {version}, at {stamp}")
    if not entries:
        print("warning: no connections were exported — back this up only if that is correct")
    return EXIT_OK


def do_check(vault: Vault, data_dir: Path, as_json: bool) -> int:
    values, version, updated = vault.read()
    if not values:
        print(f"nothing stored at {vault.secret_path} — run this script with no arguments")
        return EXIT_FAILED

    stored_count = count_providers(values)
    report = {
        "vault": f"{vault.addr} {vault.secret_path}",
        "version": version,
        "updated": updated,
        "backed_up_at": values.get(KEY_BACKED_UP_AT),
        "source": values.get(KEY_SOURCE),
        "providers": stored_count,
        "has_server_env": bool(values.get(KEY_SERVER_ENV)),
        "has_providers": bool(values.get(KEY_PROVIDERS)),
    }

    live: int | None = None
    try:
        live = len(export_connections(data_dir))
    except Failure:
        live = None
    report["live_providers"] = live

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"vault      {vault.addr}  {vault.secret_path}")
        print(f"stored     version {version}, backup taken {values.get(KEY_BACKED_UP_AT) or 'unknown'}")
        print(f"           from {values.get(KEY_SOURCE) or 'unknown'}")
        print(f"           {stored_count} connection(s); server.env {'present' if report['has_server_env'] else 'MISSING'}")
        if live is None:
            print("live       could not read the gateway's data dir — drift not compared")
        elif live == stored_count:
            print(f"live       {live} connection(s) — matches")
        else:
            print(f"live       {live} connection(s) — does NOT match the backup ({stored_count})")

    if not report["has_server_env"] or not report["has_providers"]:
        print("the stored secret is incomplete: a restore would not rebuild a working gateway", file=sys.stderr)
        return EXIT_FAILED
    if live is not None and live != stored_count:
        return EXIT_FAILED
    return EXIT_OK


def do_restore(
    vault: Vault,
    data_dir: Path,
    force: bool,
    password: str,
    api_key: str,
    as_json: bool,
) -> int:
    """Keys first, then connections — the order a fresh volume needs.

    `server.env` has to exist before the gateway is started on that volume, and the
    connections can only be imported into a gateway that is already running. So a
    restore that stops between the two is not a corrupt state, it is an unfinished
    one: the keys are in place and re-running after `make gateway-up` finishes the
    job. That is why the failure says so rather than only exiting non-zero.
    """
    values, version, updated = vault.read()
    if not values:
        raise Failure(f"nothing stored at {vault.secret_path} — nothing to restore")
    if not values.get(KEY_PROVIDERS) and not values.get(KEY_SERVER_ENV):
        raise Failure(f"{vault.secret_path} holds neither a providers export nor server.env")

    report: dict[str, object] = {"vault_version": version, "updated": updated, "data_dir": str(data_dir)}

    # 1. server.env — the keys. Refuse to clobber a different set of keys unless
    #    asked: `STORAGE_ENCRYPTION_KEY` decides whether the connections beside it
    #    in storage.sqlite can be read at all.
    server_env = values.get(KEY_SERVER_ENV)
    if isinstance(server_env, str) and server_env.strip():
        target = data_dir / "server.env"
        existing = ""
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing and existing != server_env and not force:
            report["server_env"] = "refused — a different server.env is already there (use --force)"
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(server_env, encoding="utf-8")
            os.chmod(target, 0o600)
            report["server_env"] = "written" if existing != server_env else "already current"
    else:
        report["server_env"] = "not in the backup"

    # 2. The connections — through the script that already owns that write path.
    providers_json = values.get(KEY_PROVIDERS)
    restored = 0
    if isinstance(providers_json, str) and providers_json.strip():
        handle, temp_path = tempfile.mkstemp(prefix="omniroute-vault-", suffix=".json")
        os.close(handle)
        os.chmod(temp_path, 0o600)
        try:
            Path(temp_path).write_text(providers_json, encoding="utf-8")
            argv = [sys.executable, str(RESTORE_SCRIPT), "--creds", temp_path, "--json"]
            if password:
                argv += ["--password", password]
            if api_key:
                argv += ["--api-key", api_key]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[:400]
                raise Failure(
                    f"restoring connections failed ({result.returncode}): {detail}\n"
                    f"server.env was {report['server_env']}, so this is resumable: start the\n"
                    "gateway, then re-run --restore."
                )
            try:
                summary = json.loads(result.stdout or "{}")
                restored = len(summary.get("added") or [])
                report["skipped"] = len(summary.get("skipped") or [])
            except ValueError:
                restored = 0
            report["connections_added"] = restored
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    else:
        report["connections_added"] = "not in the backup"

    if as_json:
        print(json.dumps(report, indent=2))
        return EXIT_OK

    print(f"vault      {vault.addr}  {vault.secret_path} (version {version})")
    print(f"data dir   {data_dir}")
    print(f"server.env {report['server_env']}")
    print(f"providers  {restored} connection(s) added")
    print("\nNext: start the gateway and check it can decrypt what it was given —")
    print("  make gateway-up && make build-model-check")
    return EXIT_OK


# --- entry point --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository root holding .env")
    parser.add_argument("--env-file", default="", help="read configuration from this file instead")
    parser.add_argument("--data-dir", default="", help="the gateway's data dir (default: ask the container)")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help=f"container to ask (default {DEFAULT_CONTAINER})")
    parser.add_argument("--vault-prefix", default="", help=f"KV v2 mount (default VAULT_PREFIX, else {DEFAULT_PREFIX})")
    parser.add_argument("--vault-path", default="", help=f"path under the mount (default VAULT_PATH, else {DEFAULT_PATH})")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report what is stored, and compare with the live gateway")
    mode.add_argument("--restore", action="store_true", help="write the stored state back")
    parser.add_argument("--dry-run", action="store_true", help="with a backup: report without writing")
    parser.add_argument("--force", action="store_true", help="with --restore: overwrite an existing server.env")
    parser.add_argument("--password", default="", help="with --restore: dashboard password for the target gateway")
    parser.add_argument("--api-key", default="", help="with --restore: management key for the target gateway")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    env_path = Path(args.env_file) if args.env_file else repo / ".env"
    file_values = load_env_file(env_path)

    addr = setting(file_values, "VAULT_ADDR")
    prefix = args.vault_prefix or setting(file_values, "VAULT_PREFIX", DEFAULT_PREFIX) or DEFAULT_PREFIX
    path = args.vault_path or setting(file_values, "VAULT_PATH", DEFAULT_PATH) or DEFAULT_PATH
    if not addr:
        print(f"VAULT_ADDR is not set (looked in {env_path} and the environment).", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    token = setting(file_values, "VAULT_TOKEN")
    if not token:
        token_file = setting(file_values, "VAULT_TOKEN_FILE")
        if token_file:
            candidate = Path(token_file) if Path(token_file).is_absolute() else repo / token_file
            try:
                token = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
    if not token:
        print(
            f"no Vault token: set VAULT_TOKEN or VAULT_TOKEN_FILE in {env_path}.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    vault = Vault(addr, token, prefix, path)
    data_dir = detect_data_dir(args.data_dir, args.container)

    try:
        vault.probe_write()
        if args.check:
            return do_check(vault, data_dir, args.json)
        if args.restore:
            # The dashboard password, not the client API key in `.env`: only the
            # former can write providers. Falls back to the value this stack
            # already stores in Vault.
            password = args.password or setting(file_values, "OMNIROUTE_DASHBOARD_PASSWORD") or vault.dashboard_password()
            return do_restore(vault, data_dir, args.force, password, args.api_key, args.json)
        return do_backup(vault, data_dir, args.dry_run, args.json)
    except Failure as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

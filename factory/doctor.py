#!/usr/bin/env python3
"""Olympus doctor — deployment readiness for this checkout.

Reports what is present, what is configured, and what is blocked.

Scope note: the SDLC scheduler and the workflow engine live in the source repo
(see README), so this audits the deployment surface of *this* checkout — files,
configuration, gateway reachability, and whether the vendor tools have been
cloned. It deliberately does not invent factory state.

Exit codes: 0 ready (warnings allowed), 1 blocked.

Usage:
    python3 factory/doctor.py
    python3 factory/doctor.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = ["README.md", ".env.example", "Makefile", "docker-compose.yml"]
OPTIONAL_FILES = ["compose.infisical.yml", "UPSTREAMS.md", "docs/stack.md", "web/studio/package.json"]
VENDOR_DIRS = ["omniroute", "archon", "ai-software-factory"]

PLACEHOLDER_PREFIXES = ("change-me", "changeme", "your-", "xxx", "todo", "paste_")
DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return lowered.startswith(PLACEHOLDER_PREFIXES)


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, status: str, label: str, detail: str = "") -> None:
        self.rows.append({"status": status, "label": label, "detail": detail})

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row["status"] == status)

    @property
    def ready(self) -> bool:
        return self.count("block") == 0


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader so doctor sees the same values the factory will."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def probe_gateway(base_url: str, api_key: str) -> tuple[str, str]:
    """Return (status, detail) for the gateway /models endpoint."""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(request, timeout=2.5) as response:
            return "ok", f"reachable at {base_url} (HTTP {response.status})"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return "warn", f"reachable at {base_url} but rejected the key (HTTP {error.code})"
        return "warn", f"answered HTTP {error.code} at {base_url}"
    except Exception as error:  # noqa: BLE001 - any transport failure is just not-reachable
        return "warn", f"not reachable at {base_url} ({type(error).__name__})"


def build_report() -> Report:
    report = Report()

    report.add("ok", "repository root", str(ROOT))

    for name in REQUIRED_FILES:
        if (ROOT / name).is_file():
            report.add("ok", f"required file {name}")
        else:
            report.add("block", f"required file {name}", "missing")

    for name in OPTIONAL_FILES:
        if (ROOT / name).is_file():
            report.add("ok", f"optional file {name}")
        else:
            report.add("warn", f"optional file {name}", "missing")

    env_path = ROOT / ".env"
    file_env = read_dotenv(env_path)

    if env_path.is_file():
        report.add("ok", ".env present", "gitignored, never commit it")
    else:
        report.add("warn", ".env present", "copy .env.example to .env")

    def value_of(key: str, default: str = "") -> str:
        # Real process env wins, then the .env file.
        return os.environ.get(key) or file_env.get(key, default) or default

    base_url = value_of("OMNIROUTE_BASE_URL", DEFAULT_BASE_URL)
    api_key = value_of("OMNIROUTE_API_KEY")
    model = value_of("OMNIROUTE_MODEL", "auto/coding")

    report.add("ok", "OMNIROUTE_BASE_URL", base_url)
    report.add("ok", "OMNIROUTE_MODEL", model)

    if is_placeholder(api_key):
        report.add("block", "OMNIROUTE_API_KEY", "not configured (or still a template value)")
    else:
        report.add("ok", "OMNIROUTE_API_KEY", "configured")

    gateway_status, gateway_detail = probe_gateway(base_url, api_key)
    if gateway_status != "ok" and not is_placeholder(api_key):
        # A key is configured, so the gateway is expected to answer.
        report.add("block", "gateway reachability", gateway_detail)
    else:
        report.add(gateway_status, "gateway reachability", gateway_detail)

    missing_vendor = [name for name in VENDOR_DIRS if not (ROOT / "core-modules" / name).is_dir()]
    if missing_vendor:
        report.add(
            "warn",
            "vendored tools",
            f"not cloned: {', '.join(missing_vendor)} — run ./setup.sh",
        )
    else:
        report.add("ok", "vendored tools", "core-modules populated")

    studio = ROOT / "web" / "studio"
    if (studio / "package.json").is_file():
        report.add("ok", "Studio app", "web/studio present")
        if (studio / "node_modules").is_dir():
            report.add("ok", "Studio dependencies", "installed")
        else:
            report.add("warn", "Studio dependencies", "run: make studio-install")
        if (studio / ".next").is_dir():
            report.add("ok", "Studio build", "production build present")
        else:
            report.add("warn", "Studio build", "run: make studio-build")
    else:
        report.add("warn", "Studio app", "web/studio not present")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Olympus deployment readiness audit.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    report = build_report()

    if args.json:
        print(
            json.dumps(
                {
                    "ready": report.ready,
                    "blockers": report.count("block"),
                    "warnings": report.count("warn"),
                    "checks": report.rows,
                },
                indent=2,
            )
        )
        return 0 if report.ready else 1

    symbols = {"ok": "[ ok ]", "warn": "[warn]", "block": "[FAIL]"}
    print("Olympus doctor — deployment readiness")
    print("")

    for row in report.rows:
        line = f"  {symbols[row['status']]} {row['label']}"
        if row["detail"]:
            line += f" — {row['detail']}"
        print(line)

    blockers = report.count("block")
    warnings = report.count("warn")
    print("")

    if report.ready:
        suffix = f" ({warnings} warning{'s' if warnings != 1 else ''})" if warnings else ""
        print(f"Status: READY{suffix}")
        return 0

    print(f"Status: BLOCKED — {blockers} blocker{'s' if blockers != 1 else ''}, {warnings} warning(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

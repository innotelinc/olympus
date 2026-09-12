#!/usr/bin/env python3
"""Olympus scheduler trigger — status reporter.

The unattended scheduler lives with the factory machinery in the source repo
(see README). This checkout ships the deployment surface only, so the trigger
here is always NOT_ARMED and reports why.

Autonomy is expected to be level 0 in this checkout: nothing runs unattended
until a human has watched a real issue-to-PR lap.

Usage:
    python3 factory/trigger.py --status
    python3 factory/trigger.py --status --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Markers the source repo's scheduler would create when armed.
ARMED_MARKERS = [".factory/trigger.json", ".factory/loop.pid"]
STOP_MARKER = ".factory/STOP"  # this one is a runtime state file, not a trigger


def marker_states() -> dict[str, bool]:
    return {name: (ROOT / name).exists() for name in ARMED_MARKERS}


def stop_requested() -> bool:
    return (ROOT / STOP_MARKER).exists()


def status_payload() -> dict[str, object]:
    markers = marker_states()
    armed = any(markers.values())

    return {
        "state": "ARMED" if armed else "NOT_ARMED",
        "autonomy_level": 0,
        "reason": (
            "trigger markers present"
            if armed
            else "no scheduler in this checkout — the factory consumer lives in the source repo"
        ),
        "stop_requested": stop_requested(),
        "markers": markers,
        "expected": "NOT_ARMED for autonomy level 0",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Olympus scheduler trigger status.")
    parser.add_argument("--status", action="store_true", help="print the current trigger state")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    if not args.status:
        print("This checkout reports trigger status only.", file=sys.stderr)
        print("Try: python3 factory/trigger.py --status", file=sys.stderr)
        return 2

    payload = status_payload()

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"trigger: {payload['state']}")
    print(f"autonomy level: {payload['autonomy_level']}  (expected: {payload['expected']})")
    print(f"reason: {payload['reason']}")
    if payload["stop_requested"]:
        print("stop control: STOP marker present — the scheduler must not run")

    return 0


if __name__ == "__main__":
    sys.exit(main())

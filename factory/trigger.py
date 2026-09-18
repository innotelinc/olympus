#!/usr/bin/env python3
"""Olympus scheduler trigger — autonomy and dispatch status.

This file used to answer the same question from a guess: it reported
`NOT_ARMED` for the reason *"no scheduler in this checkout — the factory consumer
lives in the source repo"*. That was true of the file and false of the
deployment. The consumer is installed by `scripts/factory-pin.sh` (the vendor
installer), which had simply never been run here — so the honest answer was
*"nothing is armed, and nothing could be"*, and the two states were reported
identically.

The measurements that changed between those two sentences are worth keeping,
because they are what makes an armed loop checkable (`docs/roadmap.md`,
"Autonomy ladder" — a rung is earned with evidence, never configured):

* the **integration pin** is the ground truth for what an unattended loop would
  run: `.factory/consumer.json` names an Archon source directory whose name is
  its own revision, and the deployment is pinned to `29f6a73d…`, which exposes
  **18** shared workflows;
* **`factory tick`** submits exactly one of them, chosen by
  `.factory/schedule.json` — so the schedule *is* the dispatcher's policy, and a
  schedule naming a workflow the pin does not carry is a loop that fails every
  tick;
* `.factory/loop.sh` repeats that tick until `.factory/STOP` appears (what
  `factory halt` writes), and the `factory-timer` unit keeps the loop alive.

None of that is a scheduler this repo runs; all of it is state a human arms and
this file reports. The gate itself lives in `scripts/factory-dispatch.py`, which
is also the only thing that writes the schedule and installs the unit — this file
is a *reader*, so a status surface can never arm anything by accident. It is
present in the deployment because the vendor installer would otherwise replace it
with its own stub; `scripts/factory-pin.sh` restores it after an install for
exactly that reason (and because the parallel `doctor.py` backs the compose
healthcheck).

Usage:
    python3 factory/trigger.py --status
    python3 factory/trigger.py --status --json

Exit codes: 0 reported; 2 when the dispatch script is missing (the checkout is
not the deployment) — never a made-up state.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCH = ROOT / "scripts" / "factory-dispatch.py"


def dispatch_status() -> dict | None:
    """Ask the gate. Returns None when it is not there to ask."""
    if not DISPATCH.is_file():
        return None
    try:
        out = subprocess.run([sys.executable, str(DISPATCH), "--status", "--json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def unavailable_payload() -> dict:
    return {
        "state": "UNKNOWN",
        "autonomy_level": None,
        "expected": "NOT_ARMED for autonomy level 0",
        "reason": f"{DISPATCH.relative_to(ROOT)} is missing — this checkout is not the deployment",
        "stop_requested": (ROOT / ".factory/STOP").exists(),
        "markers": {name: (ROOT / name).exists()
                    for name in (".factory/consumer.json", ".factory/schedule.json",
                                 ".factory/trigger.json")},
        "blockers": [],
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

    payload = dispatch_status()
    if payload is None:
        payload = unavailable_payload()
        if args.json:
            print(json.dumps(payload, indent=2))
            return 2

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"trigger: {payload['state']}")
    print(f"autonomy level: {payload.get('autonomy_level')}  (expected: {payload['expected']})")
    print(f"reason: {payload['reason']}")
    pin = payload.get("pin") or {}
    if pin.get("present"):
        print(f"pin: {pin['revision'][:12]}  workflows: {len(payload.get('workflow_names', []))}")
    if payload.get("workflow"):
        print(f"schedule: {payload['workflow']}")
    laps = payload.get("laps") or {}
    if laps:
        print(f"evidence: {laps.get('passing', 0)} passing lap(s) of {laps.get('total', 0)}")
    for blocker in payload.get("blockers") or []:
        print(f"blocked: {blocker['gate']} — {blocker['detail']}")
        print(f"         fix: {blocker['fix']}")
    if payload["stop_requested"]:
        print("stop control: STOP marker present — the scheduler must not run")

    return 0


if __name__ == "__main__":
    sys.exit(main())

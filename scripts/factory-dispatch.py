#!/usr/bin/env python3
"""Olympus dispatch — the arming gate for autonomy level 1.

WHY THIS EXISTS
---------------
The factory runs at **L0**: a human starts every lap. L1 is one step further —
`factory tick` reads `.factory/schedule.json` and submits exactly one shared
workflow, `.factory/loop.sh` repeats it, and a systemd unit keeps the loop alive.
Arming that loop is a single file and a single `systemctl`, and it is the change
that lets the deployment do work nobody is watching. So the level is **earned,
not configured** (`docs/roadmap.md`, "Autonomy ladder": *each rung needs a
watched lap and explicit evidence before it is turned on*). This script is both
halves of that sentence: it records the lap as evidence, and it refuses to arm
without one.

It is also the deployment surface the published `factory/trigger.py` reports
from. Until 2026-09-18 that file said *"no scheduler in this checkout — the
factory consumer lives in the source repo"*, which was true of the file and
false of the deployment: the vendor installer (`scripts/factory-pin.sh`) had
simply never been run, so nothing could be armed even by hand. It has been, and
what stands between this deployment and L1 is the step a script cannot take for
anyone — watching a lap — which is the point.

THE GATE
--------
Every blocker names the command that clears it; nothing is armed by default.

1. **Integration pin.** `.factory/consumer.json` must name a source directory
   whose name is its own full revision, and that revision must be a 40-character
   SHA. Cleared by `scripts/factory-pin.sh`.
2. **Installed runtime.** `.factory/loop.sh` and a *real* `factory/consumer.py`
   (the deck's placeholder cannot drive anything). Cleared by the same install.
3. **A valid schedule.** The workflow named must exist in the pinned source —
   `factory tick` refuses a workflow the source does not carry, and a schedule
   naming a retired one is a loop that fails every tick.
4. **A provider login.** `$HOME/.factory-env`, mode 600, which the unit sources.
   The native doctor reports `"authentication not live-tested"` until a lap has
   run against it, so a live laps is the only proof that exists.
5. **Evidence.** At least one recorded lap with `result=pass`. A failed lap is
   evidence too — it just does not clear the gate.
6. **The stop control.** `.factory/STOP` (what `factory halt` writes) must be
   absent; a loop armed over a halt is armed against the operator.

`tick` itself is unchanged and still enforces its own `--cwd` /
`--workflow-source` ownership: this script writes configuration and never
invents a stage policy.

USAGE
-----
    scripts/factory-dispatch.py --status [--json]
    scripts/factory-dispatch.py --record-lap --workflow archon-triage \\
        --run <run-id> --result pass --watched-by dhunter [--notes "..."]
    scripts/factory-dispatch.py --arm --workflow archon-lifecycle [--interval 900]
    scripts/factory-dispatch.py --arm --workflow archon-lifecycle --dry-run
    scripts/factory-dispatch.py --disarm
    scripts/factory-dispatch.py --check        # exit 1 when armed without evidence

State lives under `.factory/`, which is machine-local and gitignored — the pin
records an absolute cache path, and the rest is loop/ledger/evidence state that
must never be committed (`.gitignore` says the same).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REVISION_RE = re.compile(r"[0-9a-f]{40}")
PLACEHOLDER_MARKERS = ("placeholder", "not pretend otherwise")
PIN_MARKERS = ("Integration pin required",)

UNIT_NAME = "factory-timer.service"
DEFAULT_WORKFLOW = "archon-lifecycle"
DEFAULT_INTERVAL = 900
UNIT_USER = "root"


# ── Paths and small helpers ──────────────────────────────────────────────────

def repo_root(start: Path | None = None) -> Path:
    """The repository the factory belongs to — `git rev-parse --show-toplevel`."""
    here = (start or Path(__file__).resolve().parent).resolve()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return here.parent


def state_dir(root: Path) -> Path:
    return root / ".factory"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def systemctl(*args: str) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 127, "systemctl not found"
    try:
        out = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return out.returncode, (out.stdout or out.stderr).strip()


# ── The state this gate reads ────────────────────────────────────────────────

def read_pin(root: Path) -> dict:
    """The integration pin, with the checks `consumer.py` is about to make."""
    pin = read_json(state_dir(root) / "consumer.json")
    if not pin:
        return {"present": False, "reason": "no .factory/consumer.json"}
    revision = pin.get("revision", "")
    source = Path(pin.get("source", ""))
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        return {"present": False, "reason": "revision is not a full lowercase commit SHA"}
    if source.name != revision:
        return {"present": False, "reason": "source directory is not named by its revision"}
    if not source.is_dir():
        return {"present": False, "reason": f"pinned source is missing: {source}"}
    return {"present": True, "revision": revision, "source": str(source),
            "repository": pin.get("repository", ""), "bun": pin.get("bun", "bun")}


def read_consumer(root: Path) -> dict:
    """Whether the thing that would be scheduled is the deck's stub or the runtime."""
    path = root / "factory" / "consumer.py"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"installed": False, "reason": "factory/consumer.py is missing"}
    if any(marker in text for marker in PLACEHOLDER_MARKERS):
        return {"installed": False, "reason": "factory/consumer.py is the placeholder"}
    if not any(marker in text for marker in PIN_MARKERS):
        return {"installed": False, "reason": "factory/consumer.py does not read the pin"}
    return {"installed": True, "path": str(path)}


def source_workflows(pin: dict) -> list[str]:
    """Workflow names the pinned source carries — offline, from its own pack.json."""
    if not pin.get("present"):
        return []
    source = Path(pin["source"])
    manifest = read_json(source / "factory" / "pack.json") or read_json(source / "pack.json")
    directory = manifest.get("source_directory", ".archon/workflows/sdlc")
    found: list[str] = []
    for path in (source / directory).rglob("*"):
        if path.suffix not in (".yaml", ".yml"):
            continue
        match = re.search(r"^name:\s*['\"]?([a-z][a-z0-9-]*)['\"]?\s*$",
                          path.read_text(encoding="utf-8", errors="replace"), re.M)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return sorted(found)


def read_laps(root: Path) -> tuple[list[dict], int]:
    """Recorded laps, plus the count of lines that were not readable."""
    path = state_dir(root) / "laps.jsonl"
    if not path.is_file():
        return [], 0
    laps, malformed = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lap = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(lap, dict):
            laps.append(lap)
        else:
            malformed += 1
    return laps, malformed


def unit_state() -> dict:
    enabled_rc, enabled = systemctl("is-enabled", UNIT_NAME)
    active_rc, active = systemctl("is-active", UNIT_NAME)
    installed = Path("/etc/systemd/system") / UNIT_NAME
    return {
        "installed": installed.is_file(),
        "enabled": enabled if enabled_rc in (0, 1) else "unknown",
        "active": active if active_rc in (0, 1, 3) else "unknown",
    }


def loop_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", ".factory/loop.sh"], capture_output=True, text=True, timeout=30)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def snapshot(root: Path) -> dict:
    """Everything the gate and the report need, read once."""
    pin = read_pin(root)
    consumer = read_consumer(root)
    state = state_dir(root)
    schedule = read_json(state / "schedule.json")
    trigger = read_json(state / "trigger.json")
    laps, malformed = read_laps(root)
    passing = [lap for lap in laps if lap.get("result") == "pass"]
    workflows = source_workflows(pin)
    workflow = schedule.get("workflow") or trigger.get("workflow") or ""
    return {
        "root": str(root),
        "state_dir": str(state),
        "pin": pin,
        "consumer": consumer,
        "loop": {"present": (state / "loop.sh").is_file(), "running": loop_running()},
        "provider_env": Path(os.path.expanduser("~/.factory-env")),
        "schedule": schedule,
        "trigger": trigger,
        "workflow_names": workflows,
        "workflow": workflow,
        "laps": {"total": len(laps), "passing": len(passing), "malformed": malformed,
                 "last": laps[-1] if laps else None},
        "stop": (state / "STOP").is_file(),
        "unit": unit_state(),
    }


def blockers(state: dict) -> list[dict]:
    """Each blocker: what is wrong, and the command a human runs to clear it."""
    out: list[dict] = []
    if not state["pin"].get("present"):
        out.append({"gate": "pin", "detail": state["pin"].get("reason", "no pin"),
                    "fix": "scripts/factory-pin.sh"})
    if not state["consumer"].get("installed"):
        out.append({"gate": "runtime", "detail": state["consumer"].get("reason", "no runtime"),
                    "fix": "scripts/factory-pin.sh"})
    if not state["loop"]["present"]:
        out.append({"gate": "loop", "detail": "no .factory/loop.sh", "fix": "scripts/factory-pin.sh"})
    if not state["provider_env"].is_file():
        out.append({"gate": "provider login",
                    "detail": f"{state['provider_env']} is missing (the unit sources it)",
                    "fix": "write the provider token to ~/.factory-env, mode 600"})
    elif state["provider_env"].stat().st_mode & 0o077:
        out.append({"gate": "provider login",
                    "detail": f"{state['provider_env']} is readable beyond its owner",
                    "fix": f"chmod 600 {state['provider_env']}"})
    if state["laps"]["passing"] < 1:
        out.append({"gate": "evidence",
                    "detail": "no recorded lap with result=pass",
                    "fix": "run one lap, then: scripts/factory-dispatch.py --record-lap ..."})
    if state["stop"]:
        out.append({"gate": "stop control", "detail": ".factory/STOP is set (factory halt)",
                    "fix": "python3 factory/consumer.py unhalt"})
    if state["workflow"] and state["workflow_names"] and state["workflow"] not in state["workflow_names"]:
        out.append({"gate": "schedule",
                    "detail": f"'{state['workflow']}' is not in the pinned source",
                    "fix": f"pick one of: {', '.join(state['workflow_names'][:6])} …"})
    return out


def level(state: dict) -> tuple[str, str]:
    """`trigger.json` is the arm record; the level follows it, nothing else."""
    if state["trigger"].get("armed"):
        return "ARMED", "1"
    return "NOT_ARMED", "0"


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    state = snapshot(root)
    status, autonomy = level(state)
    reasons = blockers(state)
    payload = {
        "state": status,
        "autonomy_level": int(autonomy),
        "expected": "NOT_ARMED for autonomy level 0",
        "reason": ("trigger.json records an armed dispatcher" if status == "ARMED"
                   else f"{len(reasons)} gate blocker(s)" if reasons
                   else "gates clear — a watched lap is all that remains"),
        "stop_requested": state["stop"],
        "markers": {".factory/trigger.json": (state_dir(root) / "trigger.json").is_file(),
                    ".factory/loop.pid": (state_dir(root) / "loop.pid").is_file(),
                    ".factory/schedule.json": (state_dir(root) / "schedule.json").is_file()},
        "pin": state["pin"],
        "runtime": state["consumer"],
        "loop": state["loop"],
        "provider_env": {"path": str(state["provider_env"]),
                         "present": state["provider_env"].is_file(),
                         "mode_600": state["provider_env"].is_file()
                         and not state["provider_env"].stat().st_mode & 0o077},
        "schedule": state["schedule"],
        "workflow": state["workflow"],
        "workflow_names": state["workflow_names"],
        "laps": state["laps"],
        "unit": state["unit"],
        "blockers": reasons,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"dispatch: {status}   autonomy level: {autonomy}")
    if state["pin"].get("present"):
        print(f"pin: {state['pin']['revision'][:12]}  source={state['pin']['source']}")
        print(f"workflows: {len(state['workflow_names'])} in the pinned source")
    else:
        print(f"pin: absent — {state['pin'].get('reason')}")
    print(f"loop: {'present' if state['loop']['present'] else 'absent'}"
          f"{', running' if state['loop']['running'] else ''}")
    unit = state["unit"]
    print(f"timer: installed={unit['installed']} enabled={unit['enabled']} active={unit['active']}")
    laps = state["laps"]
    print(f"evidence: {laps['passing']} passing lap(s) of {laps['total']}")
    if state["workflow"]:
        print(f"schedule: {state['workflow']}")
    if state["stop"]:
        print("stop control: .factory/STOP is set — the loop must not run")
    if reasons:
        print("\nblockers:")
        for item in reasons:
            print(f"  - {item['gate']}: {item['detail']}")
            print(f"    fix: {item['fix']}")
    else:
        print("\ngates clear — record a watched lap, then --arm")
    return 0


def cmd_record_lap(args: argparse.Namespace) -> int:
    root = repo_root()
    state = snapshot(root)
    if not args.workflow:
        print("--record-lap needs --workflow <name>", file=sys.stderr)
        return 2
    if args.result not in ("pass", "fail"):
        print("--result must be pass or fail", file=sys.stderr)
        return 2
    if not state["pin"].get("present") or not state["consumer"].get("installed"):
        # Only the installed runtime can run a factory lap; recording one here
        # would be evidence for something that never happened.
        print("refusing: no installed factory runtime, so no factory lap could have run",
              file=sys.stderr)
        return 2
    if state["workflow_names"] and args.workflow not in state["workflow_names"]:
        print(f"'{args.workflow}' is not in the pinned source — a lap of it proves nothing",
              file=sys.stderr)
        return 2
    lap = {"ts": now(), "workflow": args.workflow, "run": args.run or "",
           "result": args.result, "watched_by": args.watched_by or "",
           "notes": args.notes or ""}
    path = state_dir(root) / "laps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(lap) + "\n")
    print(f"lap recorded: {args.workflow} {args.result}"
          f"{f' run={args.run}' if args.run else ''} -> {path}")
    after = snapshot(root)
    remaining = [item for item in blockers(after) if item["gate"] != "evidence"]
    if args.result == "pass" and not remaining:
        print("gates clear — arm with: scripts/factory-dispatch.py --arm --workflow "
              f"{args.workflow}")
    elif remaining:
        print("still blocked:")
        for item in remaining:
            print(f"  - {item['gate']}: {item['detail']}  (fix: {item['fix']})")
    return 0


def render_unit(root: Path, interval: int, home: str, path_env: str, env_file: Path) -> str:
    return f"""# Rendered by scripts/factory-dispatch.py — do not edit here.
#
# One scheduled shared workflow per tick, from the pinned Archon source. The
# provider login is sourced from {env_file} (mode 600), which is why HOME and
# PATH are set explicitly: systemd gives a root service neither, and git's
# credential helper, `bun` and Archon's ~/.archon are all found through them.
[Unit]
Description=Olympus factory timer: one scheduled shared workflow per tick
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
Environment=HOME={home}
Environment=PATH={path_env}
Environment=IS_SANDBOX=1
Environment=FACTORY_INTERVAL_SECONDS={interval}
ExecStart=/bin/bash -lc 'source "{env_file}" && exec bash .factory/loop.sh'
Restart=on-failure
RestartSec=600
KillMode=control-group

[Install]
WantedBy=multi-user.target
"""


def cmd_arm(args: argparse.Namespace) -> int:
    root = repo_root()
    state = snapshot(root)
    workflow = args.workflow or state["workflow"] or DEFAULT_WORKFLOW
    interval = args.interval or DEFAULT_INTERVAL
    if interval <= 0:
        print("--interval must be positive", file=sys.stderr)
        return 2

    # The named workflow is part of the gate even before it is written down.
    if state["workflow_names"] and workflow not in state["workflow_names"]:
        print(f"refusing: '{workflow}' is not in the pinned source", file=sys.stderr)
        print(f"  available: {', '.join(state['workflow_names'])}", file=sys.stderr)
        return 1

    reasons = blockers(state)
    if reasons and not args.force:
        print("refusing to arm — the gate is not clear:", file=sys.stderr)
        for item in reasons:
            print(f"  - {item['gate']}: {item['detail']}")
            print(f"    fix: {item['fix']}", file=sys.stderr)
        print("\nA dispatcher armed without evidence is an unattended loop nobody "
              "watched. Watch one lap, record it, then arm.", file=sys.stderr)
        return 1

    home = os.path.expanduser("~")
    env_file = Path(os.environ.get("FACTORY_ENV_FILE", f"{home}/.factory-env"))
    unit_body = render_unit(root, interval, home, os.environ.get("PATH", ""), env_file)
    schedule = {"workflow": workflow, "inputs": {}}
    for pair in args.input or []:
        key, _, value = pair.partition("=")
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"refusing: bad --input name in '{pair}'", file=sys.stderr)
            return 2
        schedule["inputs"][key] = value
    trigger = {"armed": True, "level": 1, "since": now(), "workflow": workflow,
               "interval_seconds": interval, "unit": UNIT_NAME,
               "evidence": {"passing_laps": state["laps"]["passing"],
                            "last": state["laps"]["last"]},
               "armed_by": args.watched_by or os.environ.get("USER", "")}
    if reasons and args.force:
        # An override is recorded, not hidden: the point of `--check` downstream
        # is that an armed dispatcher without evidence is visible in writing.
        trigger["gate_overridden"] = [item["gate"] for item in reasons]
        print("warning: arming with the gate overridden — recorded in .factory/trigger.json",
              file=sys.stderr)

    if args.dry_run:
        print("dry run — nothing written")
        print(f"  would write {state_dir(root) / 'schedule.json'}: "
              f"{json.dumps(schedule, sort_keys=True)}")
        print(f"  would write {state_dir(root) / 'trigger.json'}: "
              f"{json.dumps({k: trigger[k] for k in ('armed', 'level', 'workflow', 'interval_seconds')}, sort_keys=True)}")
        print(f"  would write {state_dir(root) / UNIT_NAME} and enable it")
        return 0

    write_json(state_dir(root) / "schedule.json", schedule)
    write_json(state_dir(root) / "trigger.json", trigger)
    (state_dir(root) / UNIT_NAME).write_text(unit_body, encoding="utf-8")
    print(f"armed: {workflow} every {interval}s (level 1)")

    if args.render_only:
        print(f"unit rendered to {state_dir(root) / UNIT_NAME} — install it on a host with systemd")
        return 0
    if not shutil.which("systemctl"):
        print(f"no systemd here — the unit is at {state_dir(root) / UNIT_NAME}", file=sys.stderr)
        return 0

    target = Path("/etc/systemd/system") / UNIT_NAME
    try:
        target.write_text(unit_body, encoding="utf-8")
    except OSError as exc:
        print(f"cannot write {target}: {exc} — re-run as root", file=sys.stderr)
        return 1
    systemctl("daemon-reload")
    rc, out = systemctl("enable", "--now", UNIT_NAME)
    print(f"systemctl enable --now {UNIT_NAME}: {'ok' if rc == 0 else out}")
    return 0 if rc == 0 else 1


def cmd_disarm(args: argparse.Namespace) -> int:
    root = repo_root()
    state = state_dir(root)
    if shutil.which("systemctl") and Path("/etc/systemd/system", UNIT_NAME).is_file():
        rc, out = systemctl("disable", "--now", UNIT_NAME)
        print(f"systemctl disable --now {UNIT_NAME}: {'ok' if rc == 0 else out}")
        Path("/etc/systemd/system", UNIT_NAME).unlink(missing_ok=True)
        systemctl("daemon-reload")
    for name in (UNIT_NAME, "trigger.json", "schedule.json"):
        (state / name).unlink(missing_ok=True)
    if loop_running():
        print("note: a .factory/loop.sh process is still running — stop it before it ticks again")
    print("disarmed: the dispatcher will not schedule anything (level 0)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """For CI and drift checks: an armed dispatcher must have evidence behind it."""
    root = repo_root()
    state = snapshot(root)
    armed = bool(state["trigger"].get("armed"))
    laps = state["laps"].get("passing", 0) if isinstance(state["laps"], dict) else 0
    if armed and not laps:
        print("armed without evidence: .factory/trigger.json exists but no lap passed",
              file=sys.stderr)
        return 1
    if armed:
        print(f"armed (level 1) behind {laps} passing lap(s); "
              f"workflow={state['workflow']} unit={state['unit']['active']}")
    else:
        print("not armed (level 0) — expected by default")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arm, inspect or disarm Olympus's L1 dispatch loop.")
    parser.add_argument("--status", action="store_true", help="report the gate and the level")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--record-lap", action="store_true", help="append evidence for a watched lap")
    parser.add_argument("--arm", action="store_true", help="arm the dispatcher (writes the schedule, installs the timer)")
    parser.add_argument("--disarm", action="store_true", help="disarm and stop the timer")
    parser.add_argument("--check", action="store_true", help="fail when armed without evidence")
    parser.add_argument("--workflow", help=f"shared workflow to schedule (default {DEFAULT_WORKFLOW})")
    parser.add_argument("--interval", type=int, help=f"seconds between ticks (default {DEFAULT_INTERVAL})")
    parser.add_argument("--input", action="append", help="scheduled workflow input as key=value (repeatable)")
    parser.add_argument("--run", help="native run id the lap refers to")
    parser.add_argument("--result", help="pass or fail")
    parser.add_argument("--watched-by", help="who watched the lap")
    parser.add_argument("--notes", help="what the lap showed")
    parser.add_argument("--dry-run", action="store_true", help="print what --arm would change")
    parser.add_argument("--render-only", action="store_true", help="write the unit, do not install it")
    parser.add_argument("--force", action="store_true",
                        help="arm despite blockers (records that the gate was overridden)")
    args = parser.parse_args(argv)

    chosen = [name for name in ("status", "record_lap", "arm", "disarm", "check")
              if getattr(args, name)]
    if len(chosen) != 1:
        parser.print_help()
        if not chosen:
            print("\nPick one of --status, --record-lap, --arm, --disarm, --check.",
                  file=sys.stderr)
            return 2
        print("\nPick exactly one action.", file=sys.stderr)
        return 2

    try:
        return {"status": cmd_status, "record_lap": cmd_record_lap, "arm": cmd_arm,
                "disarm": cmd_disarm, "check": cmd_check}[chosen[0]](args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"factory-dispatch refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

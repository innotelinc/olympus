#!/usr/bin/env python3
"""Prune built apps and finished queue artefacts, so a slug can be rebuilt.

WHY THIS EXISTS. The greenfield workflow refuses to overwrite an existing app —
a deliberate guard, because silently replacing someone's build is worse than
failing. The cost is that `builds/<slug>/` from a previous run blocks every
rebuild of that slug, and the only fix was remembering to `rm -rf` first.

The queue accumulates too: every build leaves a `.status.json` and a `.log`
behind after it finishes, and nothing ever removed them.

    scripts/prune-builds.py                      # report only (the default)
    scripts/prune-builds.py --yes                # delete what it reported
    scripts/prune-builds.py --older-than 0 --yes # everything, age ignored
    scripts/prune-builds.py --specs --yes        # also drop consumed specs
    scripts/prune-builds.py --queue-only --older-than 0 --yes   # just the queue

`--queue-only` exists because the two halves age differently. A queue entry is
finished the moment its job ended, so "everything I have already seen fail" is a
sane request within the hour. A build directory is the *source* of an app that may
be running right now — `builds/<slug>` is what a rebuild and a re-package read,
and the container keeps serving from its image after the tree is gone, so deleting
one is not visibly undone until the next build. The default age filter hides that;
`--older-than 0` would not. This flag makes the safe half of a full sweep
reachable without the other half.

Deleting is opt-in. A build the runner is working on is never selected — that
comes from the runner's own heartbeat, so a build started by hand has no
protection here beyond the age filter — and a spec that git tracks is only
removed when `--specs` is given too: the repo tracks `build-requests/`, so
removing a spec locally is a change to working-tree content, not a cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_AGE_DAYS = 7
STATUS_SUFFIX = ".status.json"
LOG_SUFFIX = ".log"
REQUEST_SUFFIX = ".request.json"
RUNNING_SUFFIX = ".running.json"
CANCEL_SUFFIX = ".cancel.json"
HEARTBEAT_NAME = "runner.heartbeat.json"


def lock_name() -> str:
    return "runner.lock"


def age_days(path: Path, now: float | None = None) -> float:
    reference = time.time() if now is None else now
    try:
        return (reference - path.stat().st_mtime) / 86400.0
    except OSError:
        return 0.0


def busy_slugs(queue: Path) -> set[str]:
    """Slugs a build is working on right now, from the runner's own heartbeat.

    The runner rewrites this file every few seconds while it is busy, so it is
    the one place that knows. `busy_with` holds the slug; if the heartbeat is
    missing or stale the build is not running and nothing needs protecting.
    """
    heartbeat = queue / HEARTBEAT_NAME
    if not heartbeat.is_file():
        return set()
    try:
        payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    if age_days(heartbeat) * 86400 > 120:
        return set()
    busy = payload.get("busy_with")
    return {str(busy)} if busy else set()


def select_builds(builds_dir: Path, older_than: float, now: float | None = None) -> list[Path]:
    """Build directories old enough to remove, newest last."""
    if not builds_dir.is_dir():
        return []
    candidates = [entry for entry in builds_dir.iterdir() if entry.is_dir()]
    fresh = [entry for entry in candidates if age_days(entry, now) < older_than]
    return sorted(
        (entry for entry in candidates if entry not in fresh),
        key=lambda entry: age_days(entry, now),
        reverse=True,
    )


def select_queue_artefacts(queue: Path, older_than: float, now: float | None = None) -> list[Path]:
    """A finished job's status and log, once they are old.

    The files the runner stops caring about when a job ends: its status and log,
    plus any cancel marker the job left behind. A `.request.json` is work still
    waiting to be claimed and a `.running.json` is work in progress — the runner
    renames between those two, so moving either would drop a build the operator
    asked for, and `recover()` already fails the stale ones. The lock and
    heartbeat belong to the running process.

    A marker is included only once it is old. The runner consumes its own, but a
    marker written for a job an older runner never cancelled stays forever — and
    at any age beyond a build's own timeout it cannot belong to a live job.
    """
    if not queue.is_dir():
        return []
    selected = []
    for entry in sorted(queue.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name == HEARTBEAT_NAME or name == lock_name():
            continue
        if name.endswith(RUNNING_SUFFIX):
            continue
        finished = (
            name.endswith(STATUS_SUFFIX)
            or name.endswith(LOG_SUFFIX)
            or name.endswith(CANCEL_SUFFIX)
        )
        if not finished:
            continue
        if age_days(entry, now) >= older_than:
            selected.append(entry)
    return selected


def tracked_specs(repo: Path) -> set[str]:
    """Specs git knows about, as repo-relative paths — these are content, not litter."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--", "build-requests/*.md"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def select_specs(
    repo: Path, older_than: float, now: float | None = None, include_tracked: bool = False
) -> list[Path]:
    spec_dir = repo / "build-requests"
    if not spec_dir.is_dir():
        return []
    tracked = tracked_specs(repo)
    selected = []
    for entry in sorted(spec_dir.glob("*.md")):
        if entry.name == "README.md":
            continue
        rel = str(entry.relative_to(repo))
        if rel in tracked and not include_tracked:
            continue
        if age_days(entry, now) >= older_than:
            selected.append(entry)
    return selected


def human(path: Path, repo: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    """`argv` is for tests; the CLI passes nothing and argparse reads `sys.argv`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", default=str(Path(__file__).resolve().parent.parent), help="repository root"
    )
    parser.add_argument(
        "--older-than",
        type=float,
        default=DEFAULT_AGE_DAYS,
        help=f"only touch entries at least this many days old (default {DEFAULT_AGE_DAYS}; 0 = all)",
    )
    parser.add_argument("--specs", action="store_true", help="also consider build-requests/*.md")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="never consider build directories, however old (use with --older-than 0)",
    )
    parser.add_argument(
        "--include-tracked",
        action="store_true",
        help="with --specs: also remove specs tracked by git (working-tree change)",
    )
    parser.add_argument("--yes", action="store_true", help="actually delete (default: report)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    builds_dir = repo / "builds"
    queue = Path(os.environ.get("BUILD_QUEUE_DIR") or repo / ".factory" / "build-queue")

    protected = busy_slugs(queue)
    # Not merely "skip the ones that look busy": with `--queue-only` no build
    # directory is a candidate at all, so the answer does not depend on a heartbeat
    # that a build started by hand does not write.
    builds = (
        []
        if args.queue_only
        else [entry for entry in select_builds(builds_dir, args.older_than) if entry.name not in protected]
    )
    artefacts = select_queue_artefacts(queue, args.older_than)
    specs = select_specs(repo, args.older_than, include_tracked=args.include_tracked) if args.specs else []

    report = {
        "repo": str(repo),
        "older_than_days": args.older_than,
        "queue_only": args.queue_only,
        "protected": sorted(protected),
        "builds": [human(entry, repo) for entry in builds],
        "queue": [human(entry, repo) for entry in artefacts],
        "specs": [human(entry, repo) for entry in specs],
        "deleted": args.yes,
    }

    if not args.yes:
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"repo        {repo}")
            print(f"older-than  {args.older_than:g} day(s)")
            for label, entries in (
                ("builds", report["builds"]),
                ("queue", report["queue"]),
                ("specs", report["specs"]),
            ):
                for entry in entries:
                    print(f"  {label:<6} {entry}")
            for slug in report["protected"]:
                print(f"  held   {slug} (a build is running)")
            total = len(report["builds"]) + len(report["queue"]) + len(report["specs"])
            print(f"would remove {total} entr{'y' if total == 1 else 'ies'} — re-run with --yes")
        return 0

    removed = 0
    for entry in builds:
        if entry.is_dir():
            import shutil

            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed += 1
    for entry in artefacts + specs:
        entry.unlink(missing_ok=True)
        removed += 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"removed {removed} entr{'y' if removed == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

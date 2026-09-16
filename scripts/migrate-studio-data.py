#!/usr/bin/env python3
"""migrate-studio-data.py — move Studio's state from one host to another.

Studio keeps its state in a **named docker volume** (`olympus_studio-data`,
mounted at `/app/data`), not in the repository, so it does not travel with a
checkout: a fresh host comes up with an empty Studio, which is indistinguishable
from "the sign-in is broken" when nobody says which one happened. This moves it.

    # on the OLD host
    python3 scripts/migrate-studio-data.py export --out /tmp/olympus-handover
    scp /tmp/olympus-handover/*.tgz <new-host>:/tmp/olympus-handover/

    # on the NEW host
    python3 scripts/migrate-studio-data.py import --in /tmp/olympus-handover

Three things are outside this volume and are called out rather than moved, because
each has its own pipeline and its own doc:

  * the repository's `builds/` directory (a bind mount, read by the archive route)
    — `export` bundles it when it has content;
  * the **published sites** — one container and one nginx vhost per site, put in
    place by `make site-publish` (see docs/site-publishing.md);
  * the per-app databases, which live with the app containers, not with Studio.

The volume is read-only during export and the import refuses to write into a
volume that already has data unless `--force` is given, so neither direction can
quietly overwrite a deployment.

Usage:
    migrate-studio-data.py status
    migrate-studio-data.py export [--out DIR] [--volume NAME]
    migrate-studio-data.py import --in PATH [--volume NAME] [--force]

`--in` takes either the handover directory or the tarball inside it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

DEFAULT_VOLUME = "olympus_studio-data"
ARCHIVE = "studio-data.tgz"
BUILDS_ARCHIVE = "builds.tgz"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER_IMAGE = "alpine:latest"


class Failed(Exception):
    """A step failed; the message is what the operator needs to read."""


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising `Failed` with its stderr rather than a traceback."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise Failed(f"{' '.join(cmd[:3])}… failed:\n{(result.stderr or result.stdout).strip()[:600]}")
    return result


def volume_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "volume", "inspect", name], capture_output=True, text=True
    )
    return result.returncode == 0


def volume_entries(name: str) -> list[str]:
    """Top-level entries in a volume, or [] when it does not exist."""
    if not volume_exists(name):
        return []
    result = run(
        ["docker", "run", "--rm", "-v", f"{name}:/src:ro", HELPER_IMAGE, "ls", "-A", "/src"]
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def volume_bytes(name: str) -> str:
    result = run(
        ["docker", "run", "--rm", "-v", f"{name}:/src:ro", HELPER_IMAGE, "du", "-sh", "/src"]
    )
    return result.stdout.split()[0] if result.stdout.strip() else "?"


def cmd_status(args) -> int:
    entries = volume_entries(args.volume)
    print(f"volume : {args.volume}")
    if not volume_exists(args.volume):
        print("         does not exist — this host has never run Studio")
        return 0
    if not entries:
        print("         exists but is EMPTY (a fresh Studio: no saved projects)")
    else:
        print(f"         {len(entries)} entries, {volume_bytes(args.volume)}")
        for entry in entries[:10]:
            print(f"           {entry}")
        if len(entries) > 10:
            print(f"           … and {len(entries) - 10} more")
    builds = os.path.join(REPO_ROOT, "builds")
    packaged = sorted(os.listdir(builds)) if os.path.isdir(builds) else []
    print(f"builds/: {len(packaged)} packaged build(s) in {builds}")
    return 0


def cmd_export(args) -> int:
    if not volume_exists(args.volume):
        raise Failed(f"no docker volume named {args.volume} on this host — nothing to export.")

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    print(f"exporting {args.volume} -> {out}")

    # Read-only bind, so an export can never write back into the live volume.
    run(
        [
            "docker", "run", "--rm",
            "-v", f"{args.volume}:/src:ro",
            "-v", f"{out}:/out",
            HELPER_IMAGE, "tar", "czf", f"/out/{ARCHIVE}", "-C", "/src", ".",
        ]
    )
    size = os.path.getsize(os.path.join(out, ARCHIVE))
    print(f"  {ARCHIVE}: {size / 1024 / 1024:.1f} MiB")

    builds = os.path.join(REPO_ROOT, "builds")
    if os.path.isdir(builds) and os.listdir(builds):
        run(
            [
                "docker", "run", "--rm",
                "-v", f"{builds}:/src:ro",
                "-v", f"{out}:/out",
                HELPER_IMAGE, "tar", "czf", f"/out/{BUILDS_ARCHIVE}", "-C", "/src", ".",
            ]
        )
        print(f"  {BUILDS_ARCHIVE}: {os.path.getsize(os.path.join(out, BUILDS_ARCHIVE)) / 1024 / 1024:.1f} MiB")
    else:
        print("  builds/ is empty — nothing to bundle (packaged builds are regenerated)")

    print(
        "\nNext, on the NEW host:\n"
        f"  scp {out}/*.tgz <new-host>:{out}/\n"
        f"  python3 scripts/migrate-studio-data.py import --in {out}\n\n"
        "Still to move by hand, each with its own pipeline:\n"
        "  * the published sites   — make site-publish per site (docs/site-publishing.md)\n"
        "  * the per-app databases — they live with the app containers, not with Studio\n"
        "  * the edge's proxy hosts — re-point them at the new host (make site-publish does it)"
    )
    return 0


def cmd_import(args) -> int:
    source = os.path.abspath(args.input)
    directory = source if os.path.isdir(source) else os.path.dirname(source)
    archive = os.path.join(directory, ARCHIVE)
    if not os.path.exists(archive):
        raise Failed(f"no {ARCHIVE} in {directory} — export on the old host first.")

    entries = volume_entries(args.volume)
    if entries and not args.force:
        raise Failed(
            f"{args.volume} already has {len(entries)} entries on this host.\n"
            "Restoring over it would mix two deployments. Inspect it with\n"
            f"  python3 scripts/migrate-studio-data.py status\n"
            "and pass --force if you are certain the existing data is disposable."
        )

    if not volume_exists(args.volume):
        print(f"creating volume {args.volume}")
        run(["docker", "volume", "create", args.volume])

    print(f"restoring {archive} -> {args.volume}")
    run(
        [
            "docker", "run", "--rm",
            "-v", f"{args.volume}:/dst",
            "-v", f"{directory}:/in:ro",
            HELPER_IMAGE, "tar", "xzf", f"/in/{ARCHIVE}", "-C", "/dst",
        ]
    )
    print(f"  {len(volume_entries(args.volume))} entries in place, {volume_bytes(args.volume)}")

    builds_archive = os.path.join(directory, BUILDS_ARCHIVE)
    builds = os.path.join(REPO_ROOT, "builds")
    if os.path.exists(builds_archive):
        os.makedirs(builds, exist_ok=True)
        run(
            [
                "docker", "run", "--rm",
                "-v", f"{builds}:/dst",
                "-v", f"{directory}:/in:ro",
                HELPER_IMAGE, "tar", "xzf", f"/in/{BUILDS_ARCHIVE}", "-C", "/dst",
            ]
        )
        print(f"  builds/ restored ({len(os.listdir(builds))} entries)")

    print(
        "\nStudio reads the volume at start-up, so restart it and then confirm:\n"
        "  docker compose up -d studio\n"
        "  python3 scripts/verify-sso.py          # sign-in still holds\n"
        "  curl -s -o /dev/null -w '%{http_code}\\n' https://$STUDIO_PUBLIC_HOST/api/projects"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--volume", default=os.environ.get("STUDIO_VOLUME", DEFAULT_VOLUME))
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="what this host's Studio volume holds")
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export", help="bundle the volume (run on the OLD host)")
    p_export.add_argument("--out", default="/tmp/olympus-handover")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="restore the bundle (run on the NEW host)")
    p_import.add_argument("--in", dest="input", required=True)
    p_import.add_argument("--force", action="store_true", help="overwrite a non-empty volume")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Failed as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""env-sync.py — keep `.env` level with the surface `.env.example` documents.

WHY THIS EXISTS. `.env.example` is the documented surface: every knob the stack
reads, grouped and commented. `.env` is what a deployment actually has, and nothing
kept the two level. Measured on this host: **nine** documented keys were absent from
`.env` — including `STUDIO_DATA_DIR` and `STUDIO_RATE_LIMIT_PER_MIN` — while the
README claimed `make setup` seeds new keys on upgrade. It does not: `setup.sh` never
opens `.env`. So a knob added in one release is invisible in a deployment until
someone reads the example and notices, and the symptom of that is a feature quietly
running on its built-in default.

The cost of being wrong is asymmetric, which is why this exists rather than a note in
the README. An absent key and a key set to the default behave identically — until the
default changes, and then the deployment that never had the key moves with it while
the operator believes nothing changed.

WHAT IT WILL NOT DO. It never edits, reorders or removes a line already in `.env`. It
appends only keys the example documents and `.env` does not have, as whole blocks with
the example's own comments, under a heading that says where they came from. A key that
is present but **commented out** counts as present: `# STUDIO_DATA_DIR=…` is a
deliberate "not this one", and appending an active line would overrule a decision
somebody made on purpose.

    scripts/env-sync.py                     # report drift (exit 2 when there is any)
    scripts/env-sync.py --write             # append what it reported
    scripts/env-sync.py --example .env.example --env .env
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# `KEY=`, and `# KEY=` — the second form is how this file treats an intentional
# "not set": still present, so still the operator's decision.
ASSIGNMENT = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
ACTIVE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")

HEADER = (
    "# --- Documented in .env.example but missing here -----------------------------",
    "# Added by scripts/env-sync.py. These are the documented defaults. The stack",
    "# behaved the same without them — the compose files and the scripts fall back to",
    "# these same values — which is exactly why they went unnoticed: what changed when",
    "# they were added is that the knob is now visible where you would go to change it.",
)


class EnvBlock:
    """One documented knob: its comments, its line, and the key they belong to."""

    __slots__ = ("key", "lines")

    def __init__(self, key: str, lines: list[str]) -> None:
        self.key = key
        self.lines = lines

    def render(self) -> str:
        return "\n".join(self.lines)


def example_blocks(text: str) -> list[EnvBlock]:
    """The example, as ordered blocks — comments carried with the key they precede.

    A blank line ends the pending comment run, so a section header four lines up does
    not get glued to the first key under it and re-appended as if it described that
    key alone.
    """
    blocks: list[EnvBlock] = []
    pending: list[str] = []

    for line in text.splitlines():
        match = ACTIVE.match(line)
        if match:
            blocks.append(EnvBlock(match.group(1), [*pending, line]))
            pending = []
            continue
        if line.strip().startswith("#"):
            pending.append(line)
            continue
        if not line.strip():
            pending = []

    return blocks


def declared_keys(text: str) -> set[str]:
    """Keys the file mentions at all — set, or commented out on purpose."""
    keys: set[str] = set()
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def active_keys(text: str) -> set[str]:
    """Keys that are set, as opposed to present-but-commented."""
    keys: set[str] = set()
    for line in text.splitlines():
        match = ACTIVE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def missing_blocks(example: str, env: str) -> tuple[list[EnvBlock], list[EnvBlock]]:
    """`(to_add, commented)` — what the example documents and `.env` has not declared.

    The second list is the ones that *are* there, commented out. They are not added,
    and they are reported separately, because "you have this and turned it off" is a
    different fact from "you have never had this" and only one of them wants action.
    """
    declared = declared_keys(env)
    live = active_keys(env)

    to_add: list[EnvBlock] = []
    commented: list[EnvBlock] = []

    for block in example_blocks(example):
        if block.key in live:
            continue
        if block.key in declared:
            commented.append(block)
            continue
        to_add.append(block)

    return to_add, commented


def sync(example_path: Path, env_path: Path, *, write: bool) -> int:
    if not example_path.is_file():
        print(f"env-sync: no {example_path} — nothing to level against", file=sys.stderr)
        return 1

    # Absent rather than empty: creating one here would invent a `.env` with every
    # secret blank, which is `make setup`'s job and not a thing to do by accident.
    if not env_path.is_file():
        print(
            f"env-sync: no {env_path} — create it first (make setup, or cp "
            f"{example_path.name} {env_path.name})",
            file=sys.stderr,
        )
        return 1

    example = example_path.read_text(encoding="utf-8")
    env = env_path.read_text(encoding="utf-8")
    to_add, commented = missing_blocks(example, env)

    for block in commented:
        print(f"present but commented out: {block.key} — left as it is")

    if not to_add:
        print(f"env-sync: {env_path.name} has every key {example_path.name} documents")
        return 0

    if not write:
        for block in to_add:
            print(f"missing: {block.key}")
        print(
            f"\n{len(to_add)} key(s) documented in {example_path.name} are absent from "
            f"{env_path.name} — re-run with --write to append them"
        )
        return 2

    addition = "\n\n".join(block.render() for block in to_add)
    # Keep the file's own trailing newline out of the middle of the addition.
    body = env.rstrip("\n")
    # Same reason as project_plan.py: a backslash inside an f-string expression only
    # parses on 3.12+, and the nodes that run these scripts use 3.11.
    header = "\n".join(HEADER)
    env_path.write_text(f"{body}\n\n{header}\n\n{addition}\n", encoding="utf-8")

    for block in to_add:
        print(f"added: {block.key}")
    print(f"\n{env_path.name}: appended {len(to_add)} documented key(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--example", default=str(REPO_ROOT / ".env.example"))
    parser.add_argument("--env", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--write", action="store_true", help="append what is missing (default: report)")
    args = parser.parse_args(argv)

    return sync(Path(args.example), Path(args.env), write=args.write)


if __name__ == "__main__":
    sys.exit(main())

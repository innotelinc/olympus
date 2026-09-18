"""Resolve the build-request spec into a target directory, deterministically.

A model asked to "find the spec" will occasionally settle on a plausible file that
was never written for this run, and the factory then manufactures something nobody
asked for. Everything here is therefore code, and every refusal is explicit.

Reads (env, because a caller-controlled value is never substituted into script source):
    INPUTS_DECLARED   the spec path passed as --input spec=<path>
    INPUTS_REPLACE    "true" to rebuild over a previous manufacture (--input replace=true)

Emits {slug, title, spec_path, spec_sha, output_dir, app_dir}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

MAX_SPEC_BYTES = 400_000
# Files written by workflow nodes, not by the coding agent. A directory that
# contains only these is an interrupted pre-build and is safe to resume; any
# project file remains protected by the overwrite guard.
WORKFLOW_CONTROL_FILES = {"plan.json"}
# The same set of spellings `scripts/build-runner.py` accepts from a request, so a
# retry means the same thing whether it arrives through Studio or through `make app`.
TRUTHY = {"1", "true", "yes", "on"}


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str) -> "None":
    note(f"LOAD_FAILED: {message}")
    raise SystemExit(1)


def repo_root() -> Path:
    """The workflow's scripts run with the checkout as the working directory."""
    root = Path.cwd().resolve()
    if not (root / ".archon").is_dir():
        fail(f"working directory {root} is not the Olympus checkout (no .archon/)")
    return root


def output_dir_from_config(root: Path) -> str:
    """`factory_settings.output_dir` from .archon/config.yaml, default ./builds.

    Parsed narrowly and deliberately without a YAML dependency: this is one scalar
    under one block, and a workflow that cannot start because PyYAML is missing on a
    runner is a worse failure than a strict parse of the one key we read.
    """
    config = root / ".archon" / "config.yaml"
    if not config.is_file():
        return "./builds"

    inside = False
    for raw in config.read_text(encoding="utf-8").splitlines():
        if re.match(r"^factory_settings\s*:", raw):
            inside = True
            continue
        if inside:
            if raw.strip() and not raw[0].isspace():
                break  # left the block
            match = re.match(r"\s+output_dir\s*:\s*(.+?)\s*$", raw)
            if match:
                return match.group(1).strip().strip('"').strip("'")
    return "./builds"


def slug_for(stem: str) -> str:
    """A directory name derived from the spec's filename — never from its contents."""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60].strip("-")
    return slug or "app"


def title_for(text: str, slug: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+Application Specification:\s*(.+?)\s*$", line)
        if match and match.group(1).strip():
            return match.group(1).strip()[:120]
    return " ".join(word.capitalize() for word in slug.split("-"))


def main() -> int:
    root = repo_root()

    declared = (os.environ.get("INPUTS_DECLARED") or "").strip()
    if not declared:
        fail(
            "no spec given. Pass one: --input spec=build-requests/<name>.md . "
            "Refusing to guess which spec to manufacture."
        )

    spec_path = Path(declared)
    if not spec_path.is_absolute():
        spec_path = (root / spec_path).resolve()
    else:
        spec_path = spec_path.resolve()

    # A spec is an input document from outside this workflow. It is never allowed to
    # name a path that leaves the checkout, even to read.
    if not spec_path.is_relative_to(root):
        fail(f"spec {spec_path} is outside the checkout ({root}); refusing to read it")
    if not spec_path.is_file():
        fail(f"spec {declared!r} not found (looked at {spec_path})")
    if spec_path.suffix.lower() != ".md":
        fail(f"spec {spec_path.name} is not a .md file")

    raw = spec_path.read_bytes()
    if len(raw) > MAX_SPEC_BYTES:
        fail(f"spec is {len(raw)} bytes; the limit is {MAX_SPEC_BYTES}")
    text = raw.decode("utf-8", errors="replace")

    slug = slug_for(spec_path.stem)
    output_dir = output_dir_from_config(root)
    out = Path(output_dir)
    if not out.is_absolute():
        out = (root / out).resolve()
    app_dir = out / slug

    # A previous manufacture is evidence someone was looking at it. Replacing it
    # silently loses the comparison they were about to make, so it only happens when
    # the caller says so — `replace=true`, the same consent `scripts/build-runner.py`
    # takes from Studio's own confirm (`{ "replace": true }`).
    #
    # That consent is what a retry needs. Studio's "fix it" exports a spec whose own
    # purpose is to rebuild a project that already has output, and `builds/<slug>`
    # is exactly what a previous attempt left behind — so a guard with no way to say
    # "yes, replace it" refused every retry of a project that had got far enough to
    # produce files, which is every retry worth making.
    #
    # The one thing that is never a reason to stop is an interrupted plan node:
    # `plan.json` is workflow control state, not an app file, and leaving it behind
    # used to block every retry after a planning/build failure. Remove only those
    # control files and preserve the guard for any real project output.
    replace = (os.environ.get("INPUTS_REPLACE") or "").strip().lower() in TRUTHY
    if app_dir.is_dir():
        entries = list(app_dir.iterdir())
        if replace and entries:
            shutil.rmtree(app_dir)
            note(f"replaced    {app_dir} (a rebuild was requested; the previous one is gone)")
            app_dir.mkdir(parents=True, exist_ok=True)
        else:
            project_entries = [
                entry for entry in entries if entry.name not in WORKFLOW_CONTROL_FILES
            ]
            if not project_entries:
                for entry in entries:
                    if entry.name in WORKFLOW_CONTROL_FILES:
                        entry.unlink(missing_ok=True)
            elif entries:
                fail(
                    f"{app_dir} already exists and is not empty. Rebuild it on purpose "
                    f"(make app SPEC=… REPLACE=1, or --input replace=true), or export "
                    f"the app under a different title. It is left exactly as it was."
                )

    note(f"spec        {spec_path}")
    note(f"app         {app_dir}")
    note(f"output_dir  {out}")

    print(
        json.dumps(
            {
                "slug": slug,
                "title": title_for(text, slug),
                "spec_path": str(spec_path),
                "spec_sha": hashlib.sha256(raw).hexdigest(),
                "output_dir": str(out),
                "app_dir": str(app_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

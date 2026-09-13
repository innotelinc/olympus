"""Plan the specification before the agent builds it.

WHY THIS NODE EXISTS. The build command used to be the first opinion in the
pipeline: the agent read the spec's "Tech Stack" and decided, in prose, what to
write — and the packager downstream had its own opinion, a fixed React client plus
a generated Node/SQLite API. A spec that asked for anything else was built to
neither, and the mismatch surfaced at packaging, after the model had spent its
turn. Measured: a spec whose stack says "React 19 + Node HTTP API + SQLite" left an
app directory with no server at all, and the packager refused it for a missing data
model it had invented rather than one the spec asked for.

So the stack is decided here, in one turn, and written to `plan.json` in the
directory the build will fill. Three readers then agree on it: the builder (which is
told the plan and builds to it), the packager (which writes the Dockerfile from the
plan's language and commands) and the runtime (which runs the container on the
plan's port with the plan's healthcheck).

WHAT IT REFUSES TO DO: guess. A planning turn that produces no usable plan fails the
workflow, because the alternative is an agent run — minutes, and the model's quota —
against a stack nobody chose, ending in an app that cannot be packaged. The spec is
the authority and there is nobody in this path to ask, so the only honest outcomes
are a plan or a stop.

Reads (env): INPUTS_SPEC_PATH, INPUTS_APP_DIR, INPUTS_TITLE, INPUTS_KIND
Emits {language, port, start, files, summary, detail}.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str) -> "None":
    note(f"PLAN_FAILED: {message}")
    raise SystemExit(1)


def checkout() -> Path:
    """The Olympus checkout, which is this node's working directory.

    `load-spec` already depends on that and refuses when it is not true — the
    workflow's nodes run from the repository root, not from the copy of the workflow
    under `artifacts/runs/`. The scripts are read from the checkout rather than from
    beside this file for the same reason: the copy is not the deployment.
    """
    root = Path.cwd().resolve()
    if not (root / "scripts" / "project_plan.py").is_file():
        fail(f"working directory {root} is not the Olympus checkout (no scripts/project_plan.py)")
    return root


def main() -> int:
    spec_path = (os.environ.get("INPUTS_SPEC_PATH") or "").strip()
    app_dir = (os.environ.get("INPUTS_APP_DIR") or "").strip()
    title = (os.environ.get("INPUTS_TITLE") or "").strip()
    kind = (os.environ.get("INPUTS_KIND") or "app").strip() or "app"

    if not spec_path:
        fail("no spec path was passed in")
    if not app_dir:
        fail("no app directory was passed in")
    if kind not in ("app", "website"):
        fail(f"unknown kind {kind!r} (expected 'app' or 'website')")

    root = checkout()
    sys.path.insert(0, str(root / "scripts"))

    import project_plan  # noqa: E402 - the path insert above is what makes this importable

    try:
        plan = project_plan.plan_for_spec(spec_path, title, kind, root)
    except project_plan.PlanError as error:
        fail(str(error))

    try:
        written = project_plan.write_plan_file(app_dir, plan)
    except OSError as error:
        fail(f"could not write the plan to {app_dir}: {error}")

    note(f"planned    {project_plan.describe(plan)}")
    note(f"plan       {written}")
    for entry in plan["files"]:
        note(f"  file     {entry['path']}")

    print(json.dumps({
        "language": plan["runtime"]["language"],
        "port": plan["run"]["port"] or 0,
        "start": plan["run"]["start"],
        "files": len(plan["files"]),
        "summary": plan["summary"],
        "detail": project_plan.describe(plan),
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

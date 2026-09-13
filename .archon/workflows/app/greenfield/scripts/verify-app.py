"""Assert that an app was actually built.

This node exists because the alternative is a green run over an empty directory. The
obs-node it is modelled on, from the factory's own notes: *a node that was denied a
tool exits 0 having changed nothing, and "the run succeeded" is then true and
useless.* Here that failure has a second cause — the model call is the one step whose
exit code means the least.

So the assertion is on the ARTIFACT: an app directory containing files, an entry
point when the app has one, and — when the spec declares a check this node can run
safely — that check actually passing.

WHAT "VERIFIED" CAN MEAN, stated plainly rather than blurred:
    passed      a command the spec declared ran here and exited 0
    structural  no declared command was safely runnable; the artifact was asserted
                and the criteria are echoed for a human. Not a claim of correctness.

Reads (env): INPUTS_APP_DIR, INPUTS_SPEC_PATH, INPUTS_TITLE
Emits {files, bytes, entry, verification, command, detail}.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

CHECK_TIMEOUT_SECONDS = 600
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist"}
# The spec is an input document. A command that chains, redirects, elevates or
# fetches is refused rather than executed: none of those are a verification step, and
# "run whatever the file said" is how a document becomes a shell.
REFUSED = ("sudo", "rm ", "curl", "wget", "|", ">", "&&", ";", "$(", "`")


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str) -> "None":
    note(f"VERIFY_FAILED: {message}")
    raise SystemExit(1)


@functools.lru_cache(maxsize=1)
def plan_contract():
    """`scripts/project_plan.py` from the checkout, or None when it is not there.

    Loaded by path because this node runs from a copy of the workflow: the script
    beside it is the copy and the checkout's is the deployment's. The alternative to
    these ten lines is a shared module the workflow cannot import — it is copied to
    `artifacts/runs/<id>/`, away from `scripts/`.

    The app directory is tried first because the app and the spec both live in the
    real checkout; this file's own directory is the fallback, which is what makes the
    node testable from the checkout it is checked into.
    """
    hints = [os.environ.get("INPUTS_APP_DIR") or "", os.environ.get("INPUTS_SPEC_PATH") or ""]
    starts: list[Path] = []
    for hint in hints:
        if not hint:
            continue
        path = Path(hint)
        starts.append(path if path.is_dir() else path.parent)
    starts.append(Path(__file__).resolve().parent)

    for start in starts:
        for parent in (start, *start.parents):
            candidate = parent / "scripts" / "project_plan.py"
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location("project_plan_under_test", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    return None


def is_workflow_file(app_dir: Path, path: Path) -> bool:
    """Whether a path in the app directory is the workflow's rather than the app's."""
    module = plan_contract()
    relative = str(path.relative_to(app_dir))
    if module is None:
        return Path(relative).name == "plan.json"
    return bool(module.is_control_file(relative))


def inventory(app_dir: Path) -> list[tuple[str, int]]:
    """The app's own files.

    The workflow's records are skipped, and not as a tidy-up: `plan.json` is written
    into this directory before the agent runs, so counting it would report a
    directory of nothing as a directory of one file — which is precisely the failure
    this node exists to catch.
    """
    found: list[tuple[str, int]] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(app_dir).parts):
            continue
        if is_workflow_file(app_dir, path):
            continue
        found.append((str(path.relative_to(app_dir)), path.stat().st_size))
    return found


def verification_section(spec_text: str) -> str:
    """The spec's 'Verification Criteria' block, or "" when it has none."""
    lines = spec_text.splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        if re.match(r"^##\s+.*Verification Criteria", line, re.I):
            inside = True
            continue
        if inside:
            if line.startswith("## "):
                break
            collected.append(line)
    return "\n".join(collected).strip()


def declared_commands(section: str) -> list[str]:
    return [c.strip() for c in re.findall(r"`([^`]+)`", section) if c.strip()]


def runnable(app_dir: Path, candidates: list[str]) -> str:
    """The first declared command this node can justify running, or "".

    Deliberately conservative. "Open index.html in a browser" is a real verification
    criterion and not a command; pretending otherwise would either fail every run or
    pass every run.
    """
    has_deps = (app_dir / "node_modules").is_dir()
    has_manifest = (app_dir / "package.json").is_file()
    tests = list(app_dir.rglob("test_*.py")) + list(app_dir.rglob("*_test.py"))

    for candidate in candidates:
        low = candidate.lower()
        if any(token in candidate for token in REFUSED):
            note(f"skipping declared command (not a simple check): {candidate}")
            continue
        if len(candidate) > 300:
            continue
        if low.startswith(("npm ", "npx ")):
            if has_manifest and has_deps:
                return candidate
            note(f"skipping {candidate!r}: dependencies are not installed here")
            continue
        if "unittest" in low and tests:
            return candidate
        if low.startswith("pytest") and tests:
            return candidate
    return ""


def main() -> int:
    app_dir = Path((os.environ.get("INPUTS_APP_DIR") or "").strip())
    spec_path = Path((os.environ.get("INPUTS_SPEC_PATH") or "").strip())

    if not app_dir.is_dir():
        fail(f"the agent left no app directory at {app_dir}")

    files = inventory(app_dir)
    if not files:
        fail(
            f"the agent wrote nothing into {app_dir}. The build node records its exit "
            f"code and log — check them: an agent that exits 0 having written nothing "
            f"is exactly what this node refuses to call a success."
        )

    total = sum(size for _, size in files)
    entry = ""
    for name, _ in files:
        if name == "index.html":
            entry = name
            break
    if not entry:
        for name, _ in files:
            if name.endswith((".html", ".htm")):
                entry = name
                break

    section = verification_section(spec_path.read_text(encoding="utf-8", errors="replace"))
    candidates = declared_commands(section)
    command = runnable(app_dir, candidates)

    if command:
        note(f"running declared verification: {command}")
        try:
            completed = subprocess.run(
                shlex.split(command),
                cwd=str(app_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CHECK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            fail(f"declared verification {command!r} timed out after {CHECK_TIMEOUT_SECONDS}s")
        except OSError as error:
            fail(f"could not run declared verification {command!r}: {error}")

        tail = f"{completed.stdout or ''}{completed.stderr or ''}".strip()[-1200:]
        if completed.returncode != 0:
            fail(
                f"declared verification {command!r} exited {completed.returncode}:\n{tail}"
            )
        verification, detail = "passed", f"{command} exited 0"
    else:
        criteria = " ".join(line.strip("- ").strip() for line in section.splitlines() if line.strip())
        if not criteria:
            criteria = "the spec declares no verification criteria"
        verification = "structural"
        detail = f"no declared command was runnable here; criteria to check by hand: {criteria}"[:1200]
        note(detail)

    note(f"{len(files)} file(s), {total} bytes, entry={entry or 'none'}")

    print(
        json.dumps(
            {
                "files": len(files),
                "bytes": total,
                "entry": entry,
                "verification": verification,
                "command": command,
                "detail": detail,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

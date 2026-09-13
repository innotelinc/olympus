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


def inventory(app_dir: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(app_dir).parts):
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

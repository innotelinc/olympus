"""Record which spec and which model produced this app.

A directory of generated apps with no provenance is un-auditable: "which spec is
this from, and did anything check it?" is the first question anyone asks of one, and
the answer cannot be recovered afterwards from the files themselves.

Written only after `verify-app` has passed, so the record never describes an app that
was not built.

Reads (env): INPUTS_APP_DIR, INPUTS_SLUG, INPUTS_TITLE, INPUTS_SPEC_PATH,
             INPUTS_SPEC_SHA, INPUTS_MODEL, INPUTS_EXIT_CODE, INPUTS_FILES,
             INPUTS_BYTES, INPUTS_ENTRY, INPUTS_VERIFICATION, INPUTS_COMMAND,
             INPUTS_DETAIL
Emits {app_dir, manifest, files, bytes, verification}.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist"}


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str) -> "None":
    note(f"RECORD_FAILED: {message}")
    raise SystemExit(1)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(f"INPUTS_{name}") or default).strip()


def as_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def inventory(app_dir: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(app_dir).parts):
            continue
        found.append((str(path.relative_to(app_dir)), path.stat().st_size))
    return found


def main() -> int:
    app_dir = Path(env("APP_DIR"))
    if not app_dir.is_dir():
        fail(f"no app directory at {app_dir}")

    slug = env("SLUG") or app_dir.name
    title = env("TITLE") or slug
    spec_path = env("SPEC_PATH")
    spec_sha = env("SPEC_SHA")
    model = env("MODEL")
    exit_code = as_int(env("EXIT_CODE"), -1)
    entry = env("ENTRY")
    verification = env("VERIFICATION") or "structural"
    command = env("COMMAND")
    detail = env("DETAIL")

    # THE ARTIFACT IS THE SOURCE OF TRUTH, not the numbers handed in. `verify-app`
    # already asserted it; re-deriving here means the record cannot describe a
    # directory that has since changed, and a disagreement is worth printing rather
    # than silently resolving in the wrong direction.
    entries = inventory(app_dir)
    files = len(entries)
    total = sum(size for _, size in entries)
    claimed_files, claimed_bytes = as_int(env("FILES"), files), as_int(env("BYTES"), total)
    if (claimed_files, claimed_bytes) != (files, total):
        note(
            f"note: verify reported {claimed_files} file(s)/{claimed_bytes} B, "
            f"the directory now holds {files}/{total} B — recording the directory"
        )

    # A path the operator can paste, rather than an absolute one from this machine.
    try:
        spec_display = str(Path(spec_path).resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        spec_display = spec_path

    manifest = {
        "app": slug,
        "title": title,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spec": {"path": spec_path, "sha256": spec_sha},
        "builder": {"workflow": "archon-greenfield", "agent": "codex", "model": model,
                    "agent_exit_code": exit_code},
        "artifact": {"dir": str(app_dir), "files": files, "bytes": total, "entry": entry},
        "verification": {"result": verification, "command": command, "detail": detail},
        "inventory": [{"path": name, "bytes": size} for name, size in entries],
    }

    manifest_path = app_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    listing = "\n".join(f"- `{name}` — {size} B" for name, size in entries) or "- (none)"

    readme = f"""# {title}

Manufactured from a build-request spec by `archon-greenfield`.

| | |
| --- | --- |
| Spec | `{spec_display}` |
| Spec SHA-256 | `{spec_sha}` |
| Agent | codex (`{model}`), exit `{exit_code}` |
| Files | {files} ({total} B) |
| Entry point | {entry or "none"} |
| Verification | **{verification}** — {detail} |

## Files

{listing}

## Rebuilding

```bash
make app SPEC={spec_display}
```

This directory is generated output (`builds/` is gitignored). Delete it to rebuild:
the workflow refuses to overwrite an existing app, so a rebuild is always explicit.
"""

    (app_dir / "README.md").write_text(readme, encoding="utf-8")
    note(f"recorded {manifest_path}")

    print(
        json.dumps(
            {
                "app_dir": str(app_dir),
                "manifest": str(manifest_path),
                "files": files,
                "bytes": total,
                "verification": verification,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

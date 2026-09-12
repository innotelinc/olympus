#!/usr/bin/env python3
"""Credential scan for tracked files — fails closed on literal secrets.

This exists because a hardcoded secret-manager admin password once shipped in this
public repository. It is intentionally narrow: it looks for *literal* secrets,
not for the word "password", so it stays quiet on templates, env references,
and documentation.

It flags:

  1. Known credential shapes (provider key prefixes, private-key blocks).
  2. A literal string assigned to a sensitive-looking name, e.g.
         ADMIN_PASS = "hunter2"
     Anything that is a placeholder, an interpolation (``${VAR}``, ``$VAR``,
     ``%s``), a template (``<...>``), an ellipsis, or a regex fragment is left
     alone — those are configuration, not credentials.

Matched values are masked in the output, so a finding never re-prints the
secret it found.

Usage:
    python3 scripts/secret-scan.py                 # scan tracked files
    python3 scripts/secret-scan.py path...         # scan specific paths
    python3 scripts/secret-scan.py --stdin         # scan piped content
    python3 scripts/secret-scan.py --history       # scan every blob in history

Exit codes: 0 clean, 1 findings.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SENSITIVE_NAME = re.compile(
    r"(password|passwd|pass|pwd|secret|token|api_?key|credential|admin_pass|admin_email)",
    re.IGNORECASE,
)

ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<quote>[\"'])(?P<value>[^\"'\n]{8,})(?P=quote)"
)

# A <something>_KEY holding a dotted lowercase identifier is naming a storage or
# registry key, not carrying a credential — e.g. STORAGE_KEY = "studio.token".
# Real credentials essentially never take this shape, so the exemption is kept
# this narrow on purpose.
NAMESPACED_IDENTIFIER_KEY = re.compile(r"_key$", re.IGNORECASE)
NAMESPACED_IDENTIFIER_VALUE = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")

# A value that is itself a SCREAMING_SNAKE_CASE name is naming a field or env var,
# not carrying a credential — e.g. SECRET_KEY = "INITIAL_PASSWORD", which says
# *which* Vault field to read. Two or more all-uppercase words joined by
# underscores, and nothing else: real secrets are mixed-case and include digits
# or symbols, so this cannot swallow one.
FIELD_NAME_VALUE = re.compile(r"^[A-Z]+(_[A-Z]+)+$")

# Values that look like configuration rather than credentials.
PLACEHOLDER_HINTS = (
    "change-me",
    "change_me",
    "changeme",
    "your-",
    "your_",
    "xxx",
    "todo",
    "paste_your",
    "placeholder",
    "example.com",
    "redacted",
    "dummy",
    "fake",
    "none",
)

INTERPOLATION_CHARS = ("$", "{", "}", "%s", "<", ">", "…", "\\n")
REGEX_HINTS = ("|", "\\", "(?:", "[[:", ".*", "+?", "?:")

SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("provider-api-key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
]

# Test fixtures legitimately hold fake credentials, so the literal-assignment
# rule is skipped for them. The shape rules still apply there, so a real
# provider key committed into a test is caught regardless.
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures?)/|\.(test|spec)\.[A-Za-z]+$", re.IGNORECASE
)

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".webm", ".wasm", ".node",
}

Finding = tuple[str, int, str, str]


def mask(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * min(len(value) - 4, 12)}{value[-2:]}"


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if any(hint in lowered for hint in PLACEHOLDER_HINTS):
        return True
    if any(token in value for token in INTERPOLATION_CHARS):
        return True
    if any(token in value for token in REGEX_HINTS):
        return True
    return False


def scan_text(label: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    relaxed = bool(TEST_PATH.search(label))

    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in SHAPES:
            match = pattern.search(line)
            if match:
                findings.append((label, number, rule, mask(match.group(0))))

        if relaxed:
            continue

        for match in ASSIGNMENT.finditer(line):
            name = match.group("name")
            value = match.group("value")
            if not SENSITIVE_NAME.search(name):
                continue
            if is_placeholder(value):
                continue
            if NAMESPACED_IDENTIFIER_KEY.search(name) and NAMESPACED_IDENTIFIER_VALUE.match(value):
                continue
            if FIELD_NAME_VALUE.match(value):
                continue
            findings.append((label, number, f"literal-secret ({name})", mask(value)))

    return findings


def tracked_paths() -> list[str]:
    """Tracked files, plus untracked-but-not-ignored ones.

    Including untracked files matters: scanning only `git ls-files` silently
    skips work-in-progress, which is exactly when a stray credential is easiest
    to introduce.
    """
    names: list[str] = []
    for args in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        names.extend(line for line in result.stdout.splitlines() if line.strip())

    # Stable, de-duplicated order.
    return sorted(set(names))


def history_blobs() -> list[tuple[str, str]]:
    """Every version of every file ever committed, as (label, text)."""
    revisions = subprocess.run(
        ["git", "rev-list", "--all"], capture_output=True, text=True, check=True
    ).stdout.split()

    blobs: list[tuple[str, str]] = []
    for revision in revisions:
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", revision],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for path in listing:
            if Path(path).suffix.lower() in BINARY_SUFFIXES:
                continue
            content = subprocess.run(
                ["git", "show", f"{revision}:{path}"],
                capture_output=True,
                text=True,
                errors="replace",
            )
            if content.returncode == 0:
                blobs.append((f"{revision[:8]}:{path}", content.stdout))

    return blobs


def main() -> int:
    args = sys.argv[1:]
    use_stdin = "--stdin" in args
    use_history = "--history" in args
    explicit = [a for a in args if not a.startswith("--")]

    findings: list[Finding] = []

    if use_stdin:
        findings.extend(scan_text("<stdin>", sys.stdin.read()))
    elif use_history:
        for label, text in history_blobs():
            findings.extend(scan_text(label, text))
    else:
        paths = explicit or tracked_paths()
        for path in paths:
            file = Path(path)
            if not file.is_file() or file.suffix.lower() in BINARY_SUFFIXES:
                continue
            findings.extend(scan_text(path, file.read_text(errors="replace")))

    if not findings:
        print("secret-scan: clean")
        return 0

    print(f"secret-scan: {len(findings)} finding(s)", file=sys.stderr)
    for label, number, rule, masked in findings:
        print(f"  {label}:{number}  {rule}  {masked}", file=sys.stderr)
    print("\nNothing credential-shaped may be committed. Move it to the environment.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

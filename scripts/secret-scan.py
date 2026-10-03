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

     A name that appears *inside* a string literal is not an assignment either.
     The rule is a regex over source text, so without that distinction
     ``line.startswith("VAULT_TOKEN_FILE=")`` reads a name out of the literal and
     takes its closing quote as the opening quote of a value — which is how a
     marker constant came to be reported as a credential in this repository.

     A mode name from the Fetch API is a mode, not a credential. Minified
     bundles carry ``credentials = "same-origin"`` verbatim, which is what
     blocked the dashboard's rebuilt chunk; telling it apart from a real pair
     stored under that name takes both halves — see FETCH_CREDENTIALS_MODES.

     A value the line *builds* at run time is not stored either. When the literal
     is one operand of a concatenation and the same line draws on a random
     source (``os.urandom``, ``secrets``, ``token_hex`` …), no committed text is
     the credential: ``3-media/monarch/scripts/verify-sso.py``'s throwaway
     ``"E2e-Sso-" + os.urandom(6).hex() + "!Aa1"`` is the case this spares. Both
     halves are required, so a secret merely split across two literals
     (``"hunter2" + "hunter2"``) still reads as a stored credential.

     A value Next.js inlines into the client bundle is published, not stored:
     the ``NEXT_PUBLIC_`` prefix is that compiler's own marker for "this ships
     to every browser", so the only alternative to publishing it is not having
     the feature. The dograh patch carried in this repository sets its Chatwoot
     *website* token that way, and a widget token is public by design. The
     prefix is the whole test — nothing is secret under a name the framework
     advertises — and without it the whole-tree scan can never be green, which
     is how a gate gets bypassed rather than satisfied.

Matched values are masked in the output, so a finding never re-prints the
secret it found.

Usage:
    python3 scripts/secret-scan.py                 # scan tracked files
    python3 scripts/secret-scan.py path...         # scan specific paths
    python3 scripts/secret-scan.py --stdin [label]  # scan piped content,
                                  # labelled as `label` (e.g. the file name)
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

# A <something>_MOUNT/_PATH/_DIR/_FILE holding an absolute path is pointing at
# where a secret lives, not carrying one — e.g. SECRETS_MOUNT = "/run/ontrak",
# the volume the first run writes the shared keys into. Both halves are required:
# the name has to say "location" *and* the value has to be a path. Either alone
# would be loose enough to hide a credential behind a name like SECRET_PATH.
LOCATION_NAME = re.compile(
    r"(_mount|_mountpoint|_path|_dir|_file|_socket|_endpoint|_url|_host|_ref)$",
    re.IGNORECASE,
)
LOCATION_VALUE = re.compile(r"^/[A-Za-z0-9._~/-]+$")

# A value that is itself a SCREAMING_SNAKE_CASE name is naming a field or env var,
# not carrying a credential — e.g. SECRET_KEY = "INITIAL_PASSWORD", which says
# *which* Vault field to read. Two or more all-uppercase words joined by
# underscores, and nothing else: real secrets are mixed-case and include digits
# or symbols, so this cannot swallow one.
FIELD_NAME_VALUE = re.compile(r"^[A-Z]+(_[A-Z]+)+$")

# The Fetch API's RequestCredentials enum. ``credentials = "same-origin"`` is a
# mode, not a credential, and a minified bundle carries it verbatim: the
# dashboard's dist chunk holds ``p.credentials="omit"`` and its ``"same-origin"``
# twin inside the fetch wrapper, which blocked every rebuild of that bundle (the
# whole file is re-added when the chunk hash changes, so the hook rescans it).
# The name alone cannot exempt it — a variable called `credentials` can hold a
# real pair such as "user:hunter2" — so both halves are required: the name *is*
# `credentials`, and the value is one of the three modes the spec defines.
# Nothing else is a mode name.
FETCH_CREDENTIALS_NAME = re.compile(r"^credentials$", re.IGNORECASE)
FETCH_CREDENTIALS_MODES = frozenset({"omit", "same-origin", "include"})

# A value Next.js *requires* to be inlined into the client bundle is not a stored
# credential: the `NEXT_PUBLIC_` prefix is the compiler's own marker that the
# value ships to every browser, so publishing it is the point rather than the
# mistake. dograh's carried upstream patch sets its Chatwoot *website* token this
# way (`NEXT_PUBLIC_CHATWOOT_TOKEN`), and a widget token is public by design —
# without the exemption the whole tracked tree can never be clean, and a gate
# that always fails is one people learn to bypass. The prefix is the whole test:
# nothing is secret under a name the framework advertises as client-visible.
NEXT_PUBLIC_NAME = re.compile(r"^NEXT_PUBLIC_", re.IGNORECASE)

# A vault:// reference names the secret to fetch at runtime (scheme, path,
# optional #field) — the value in the file is a pointer, not the secret.
VAULT_REFERENCE = re.compile(r"^vault://", re.IGNORECASE)

# A value built at run time: the literal is one operand of a concatenation *and*
# the same line draws on a random source. Both halves are required (see the
# module docstring): each alone would be loose enough to hide a real secret —
# concatenation alone excuses a value split across literals, and a random source
# alone excuses any line that happens to mention one.
RUNTIME_SECRET_SOURCE = re.compile(
    r"\b(os\.urandom|secrets\.\w+|random\.\w+|uuid\.uuid4|token_hex|token_urlsafe|getrandom|Crypto\.Random)\b"
)
CONCATENATION_AFTER = re.compile(r"^\s*\+")
CONCATENATION_BEFORE = re.compile(r"\+\s*$")

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


def string_spans(line: str) -> list[tuple[int, int]]:
    """Character ranges covered by the string literals on a line.

    Deliberately simple: quotes, and a backslash escapes the next character.
    That is enough for the languages this scans (Python, shell, JS/TS, YAML
    values), and it answers the only question the assignment rule asks — does
    this name start inside a literal?
    """
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(line)
    while index < length:
        if line[index] not in "\"'":
            index += 1
            continue
        quote = line[index]
        start = index
        index += 1
        while index < length:
            if line[index] == "\\":
                index += 2
                continue
            if line[index] == quote:
                index += 1
                break
            index += 1
        spans.append((start, index))
    return spans


def inside_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in spans)


def is_runtime_composed(line: str, match: re.Match[str]) -> bool:
    """Is this literal one operand of a value the line generates at run time?

    The assignment rule sees a password literal such as ``"E2e-Sso-"`` inside a
    longer expression; if the literal is joined to something else and the line
    reaches for a random source, the committed text is a prefix, not the
    credential. (Written without the assignment shape on purpose: this file is
    scanned by its own hook, and ``name = "literal"`` in a docstring is exactly
    the false positive rule 2 exists to avoid.)
    """
    if not RUNTIME_SECRET_SOURCE.search(line):
        return False
    after = line[match.end("value") + 1:]        # skip the closing quote
    before = line[:match.start("value") - 1]     # drop the opening quote
    return bool(CONCATENATION_AFTER.match(after) or CONCATENATION_BEFORE.search(before))


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if any(hint in lowered for hint in PLACEHOLDER_HINTS):
        return True
    if any(token in value for token in INTERPOLATION_CHARS):
        return True
    return any(token in value for token in REGEX_HINTS)


def scan_text(label: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    relaxed = bool(TEST_PATH.search(label))

    for number, line in enumerate(text.splitlines(), start=1):
        # Shape rules run on the raw line on purpose: a provider key sitting
        # inside a string literal is exactly what they are for.
        for rule, pattern in SHAPES:
            match = pattern.search(line)
            if match:
                findings.append((label, number, rule, mask(match.group(0))))

        if relaxed:
            continue

        spans = string_spans(line)
        for match in ASSIGNMENT.finditer(line):
            # A "name" quoted in a literal is code or a marker string, not an
            # assignment: see string_spans.
            if inside_spans(match.start("name"), spans):
                continue
            name = match.group("name")
            value = match.group("value")
            if not SENSITIVE_NAME.search(name):
                continue
            if NEXT_PUBLIC_NAME.match(name):
                continue
            if is_placeholder(value):
                continue
            if NAMESPACED_IDENTIFIER_KEY.search(name) and NAMESPACED_IDENTIFIER_VALUE.match(value):
                continue
            if FIELD_NAME_VALUE.match(value):
                continue
            if FETCH_CREDENTIALS_NAME.match(name) and value.strip().lower() in FETCH_CREDENTIALS_MODES:
                continue
            if LOCATION_NAME.search(name) and LOCATION_VALUE.match(value):
                continue
            if VAULT_REFERENCE.match(value):
                continue
            if is_runtime_composed(line, match):
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
        # `--stdin [label]` — a label names the file the content came from (used
        # by the pre-commit hook), which also activates the test-path relaxation
        # for that file, matching the path-based behaviour exactly.
        label_args = [a for a in args if a != "--stdin" and not a.startswith("-")]
        label = label_args[0] if label_args else "<stdin>"
        findings.extend(scan_text(label, sys.stdin.read()))
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

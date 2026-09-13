"""Run the coding agent over the spec. The one step code cannot verify.

The agent is Codex, wired to the OmniRoute gateway — the platform's designated
coding brain (see docs/stack.md). The invocation is composed here rather than read
from a machine's `~/.codex/config.toml`, so the workflow behaves the same on a
developer's box and on a runner: gateway URL, model and key all come from the
environment the stack already exports.

WHAT THIS NODE DOES NOT DO: decide success. Its exit code is RECORDED and passed on.
Measured on this stack, the gateway can answer 503 on a request that arrives after
the files were already written, and an agent cut off mid-lap can still exit 0 having
written nothing — so the one thing that cannot be inferred from here is whether an
app exists. `verify-app` owns that question.

RETRIES ONLY WHILE THE DIRECTORY IS EMPTY, AND CHANGES MODEL BETWEEN ATTEMPTS. The
retry is driven by the artifact, never by the exit code: the moment any file exists this
node stops and hands the directory to verify, because re-running an agent over a
half-written app is how you end up with a directory that is neither build.

Why the model changes: `auto/coding` — the stack's designated coding model, and what
OMNIROUTE_MODEL points at — is a gateway *combo*, and the gateway pins a native Codex
turn to whichever combo member served the first turn. On this deployment the free
provider has no credentials rows at all (`provider_connections` is empty), and the
pinned path requires them, so every turn after the first answers 503 "No credentials for
opencode" and the agent ends having written nothing. Measured: `/v1/responses | 16
tools` through the combo -> 503, while the same request naming `oc/big-pickle` directly
-> 200, because a concrete model never enters the combo path and so is never pinned.
So attempt 1 uses the configured model and later attempts use a concrete one. If the
gateway is ever given real credentials for the combo's members, remove this and the
primary model serves every attempt.

Reads (env):
    INPUTS_SPEC_PATH            the resolved spec
    INPUTS_APP_DIR              where the app must be written
    INPUTS_TITLE                the app's title
    OMNIROUTE_MODEL             primary model (default "auto/coding")
    OMNIROUTE_MODEL_FALLBACK    concrete model for retries (default "oc/big-pickle")

Emits {exit_code, model, attempts, log}.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUN_TIMEOUT_SECONDS = 3_300
MAX_LOG_CHARS = 4_000
INLINE_SPEC_LIMIT = 40_000

# Bounded, and low on purpose: each attempt is a full agent run, and the point of the
# retry is to cover a rate-limited free tier, not to argue with a task the agent cannot
# do. A third attempt that also writes nothing is evidence, not bad luck.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 20

DEFAULT_MODEL = "auto/coding"
# A CONCRETE model, not a combo. Combos pin a native Codex turn to one member, and the
# pinned path demands provider credentials this deployment does not have — see the
# module docstring. Naming the model directly keeps the turn off the combo path.
DEFAULT_FALLBACK_MODEL = "oc/big-pickle"


def note(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def fail(message: str) -> "None":
    note(f"BUILD_FAILED: {message}")
    raise SystemExit(1)


def prompt_template() -> str:
    """The prompt lives in commands/build.md, so a reviewer edits prose, not code."""
    path = Path(__file__).resolve().parent.parent / "commands" / "build.md"
    if not path.is_file():
        fail(f"prompt template missing: {path}")

    text = path.read_text(encoding="utf-8")
    # Drop the YAML frontmatter — it is metadata for the workflow, not instructions.
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


DEFAULT_GATEWAY = "http://127.0.0.1:20128/v1"

# What the agent is allowed to inherit. Deliberately NOT the whole environment:
# a workflow node runs inside whatever the caller had, and the caller's variables
# decided the outcome here. Measured on this stack, the agent received
# `OPENAI_BASE_URL=http://192.168…` (a LAN address, not this gateway) alongside
# `ANTHROPIC_BASE_URL` and a set of archon-issued Codex tokens — and then sat at zero
# CPU for five minutes with a thread parked in `unix_stream_data_wait`, having never
# sent a request. The explicit provider config below cannot win against an endpoint
# the process is told to use by its environment.
INHERIT = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL",
           "CODEX_HOME", "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Variables that redirect the agent somewhere other than the configured provider.
# Stripped even when present, because inheriting them is never intended.
HIJACKS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL",
           "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def agent_env(gateway: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in INHERIT}
    for key in HIJACKS:
        env.pop(key, None)
    env.setdefault("HOME", os.path.expanduser("~"))
    # The gateway key, when the stack exports one. The agent's own credentials live in
    # CODEX_HOME/auth.json, which is what the operator's working setup uses.
    key = (os.environ.get("OMNIROUTE_API_KEY") or "").strip()
    if key:
        env["OPENAI_API_KEY"] = key
    return env


def model_chain() -> list[str]:
    """The models to try, in order, across attempts.

    Primary first so the deployment's configured model gets its chance, then the
    concrete fallback. Deduplicated, because retrying the same failing model twice is
    just waiting longer for the same answer.
    """
    primary = (os.environ.get("OMNIROUTE_MODEL") or "").strip() or DEFAULT_MODEL
    fallback = (os.environ.get("OMNIROUTE_MODEL_FALLBACK") or "").strip() \
        or DEFAULT_FALLBACK_MODEL
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def gateway_overrides() -> list[str]:
    """Point the agent at the stack's gateway, self-contained.

    These are ALWAYS passed, and that is the fix for this node's earlier failures
    rather than a stylistic preference:

      * Archon gives each run its own `CODEX_HOME` holding `auth.json` and **no
        `config.toml`**, and it strips the stack's own variables out of the child
        environment ("stripped 42 keys from .../.env"). So a `config.toml` read or an
        `OMNIROUTE_BASE_URL` lookup finds nothing, the agent falls back to its built-in
        provider, and the node sits at zero CPU for the whole run — measured: two
        processes, 9 minutes, `futex_do_wait`, no request ever reaching the gateway.
      * A `model_providers` table assembled from dotted `-c` flags is rejected unless
        it carries `name`: "provider name must not be empty".

    `wire_api` must be `"responses"`: codex 0.153.4 removed `"chat"` outright —
    "`wire_api = \"chat\"` is no longer supported" — so the Responses wire is the only
    one available, not a preference.

    The default is the endpoint this stack publishes the gateway on, so a runner needs
    no codex config at all.
    """
    base_url = (os.environ.get("OMNIROUTE_BASE_URL") or "").strip() or DEFAULT_GATEWAY
    return [
        "-c", 'model_provider="omniroute"',
        "-c", 'model_providers.omniroute.name="OmniRoute"',
        "-c", f'model_providers.omniroute.base_url="{base_url}"',
        "-c", 'model_providers.omniroute.wire_api="responses"',
        "-c", 'model_providers.omniroute.requires_openai_auth=true',
    ]


def has_artifact(app_dir: Path) -> bool:
    """Whether the agent has produced anything at all.

    Any file counts, nested or not. Deliberately not a quality judgement — verify owns
    that. This answers one narrow question: would another attempt be repeating a no-op,
    or destroying work?
    """
    try:
        return any(path.is_file() for path in app_dir.rglob("*"))
    except OSError:
        return False


def run_agent(argv: list[str], app_dir: Path, child_env: dict[str, str]) -> tuple[int, str]:
    """One attempt. Returns the agent's exit code and its (bounded) output."""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(app_dir),
            env=child_env,
            # Not the inherited stdin. A node's stdin is a socket shared with the
            # runner's plumbing, and an agent that blocks reading it never starts.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
        )
        exit_code = completed.returncode
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
    except subprocess.TimeoutExpired as expired:
        exit_code = 124
        captured = b""
        for stream in (expired.stdout, expired.stderr):
            if isinstance(stream, bytes):
                captured += stream
            elif isinstance(stream, str):
                captured += stream.encode("utf-8", errors="replace")
        output = captured.decode("utf-8", errors="replace").strip()
        note(f"agent timed out after {RUN_TIMEOUT_SECONDS}s")
    except OSError as error:
        fail(f"could not run the coding agent ({argv[0]}): {error}")

    if len(output) > MAX_LOG_CHARS:
        output = "… (truncated)\n" + output[-MAX_LOG_CHARS:]
    return exit_code, output


def main() -> int:
    spec_path = Path((os.environ.get("INPUTS_SPEC_PATH") or "").strip())
    app_dir = Path((os.environ.get("INPUTS_APP_DIR") or "").strip())
    title = (os.environ.get("INPUTS_TITLE") or "").strip() or "the application"

    if not spec_path.is_file():
        fail(f"spec not readable: {spec_path}")
    if not str(app_dir):
        fail("no app directory given")

    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
    if len(spec_text) > INLINE_SPEC_LIMIT:
        # Keep the prompt bounded; the agent can read the file itself.
        spec_body = (
            f"The specification is {len(spec_text)} characters, too long to inline. "
            f"Read it from `{spec_path}` before you start, in full."
        )
    else:
        spec_body = spec_text.strip()

    prompt = (
        prompt_template()
        .replace("{{TITLE}}", title)
        .replace("{{APP_DIR}}", str(app_dir))
        .replace("{{SPEC_BODY}}", spec_body)
    )

    app_dir.mkdir(parents=True, exist_ok=True)

    codex = (os.environ.get("CODEX_BIN") or "").strip() or shutil.which("codex") or "codex"

    gateway = (os.environ.get("OMNIROUTE_BASE_URL") or "").strip() or DEFAULT_GATEWAY
    child_env = agent_env(gateway)
    chain = model_chain()

    note(f"agent       {codex}")
    note(f"models      {' -> '.join(chain)}")
    note(f"workspace   {app_dir}")

    exit_code = 0
    output = ""
    model = chain[0]
    attempt = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Attempt 1 gets the configured model; later attempts get the concrete one. If
        # the chain is a single entry, every attempt uses it.
        model = chain[min(attempt - 1, len(chain) - 1)]
        if attempt > 1:
            note(
                f"retry {attempt}/{MAX_ATTEMPTS} on {model} — nothing written yet, "
                f"waiting {RETRY_DELAY_SECONDS}s"
            )
            time.sleep(RETRY_DELAY_SECONDS)

        argv = [
            codex, "exec",
            "-C", str(app_dir),
            "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "-m", model,
            *gateway_overrides(),
            prompt,
        ]

        exit_code, output = run_agent(argv, app_dir, child_env)
        note(f"agent exit={exit_code} attempt={attempt} model={model}")

        if has_artifact(app_dir):
            # Stop here even when the exit code is non-zero: an agent whose last
            # request 503'd *after* it had written the app is a success this node must
            # not throw away. Whether that app is any good is verify's question.
            break

    # Reported, not judged. verify-app decides whether an app exists.
    print(json.dumps({
        "exit_code": exit_code,
        "model": model,
        "attempts": attempt,
        "log": output,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

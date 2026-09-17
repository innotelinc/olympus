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

Why the model changes: `auto/coding` is a gateway *combo*, and the gateway pins a native
Codex turn to whichever combo member served the first turn — so the model that finishes a
build is decided by which member happened to answer it, not by what the caller asked for.
Two measured outcomes on this deployment, same spec, same node: routed to `oc/big-pickle`
the agent wrote the file contents out as *chat text* with no function call and the app
directory stayed empty for eight minutes; pinned to a concrete tool-calling model the same
spec built in 2m18s with real `exec_command` calls. The combo path also 503s outright
("No credentials for opencode") when its pinned member has no credentials rows, which is
what the earlier empty `provider_connections` produced.

So attempt 1 uses the configured model, attempt 2 the configured fallback, and attempt 3
the combo as a last resort — three different strategies over three bounded attempts,
because the configured models here run on free tiers that go into cooldown and two of
them cooling down at once used to end a run that the wider catalogue could have served.
Pin OMNIROUTE_MODEL to a concrete tool-calling model (see .env.example) to make this
deterministic rather than a coin toss.

A TREE THAT IS WRITTEN AND BROKEN GOES BACK TO THE AGENT THAT WROTE IT. The retry above
stops the moment a file exists, which is right — re-running an agent over a half-written
app produces something that is neither build — but it also means a project that installs
and then fails to compile reached packaging untouched, and the failure surfaced there as
a compiler message with nobody left to act on it. Nothing in this workflow ran the
*plan's* own build: `verify-app` runs a check the spec declares, and the packager runs
the plan's commands in the image, after this node is gone. So the plan's install and
build run here once, on the tree the agent just wrote, and a non-zero exit is handed
back to the agent — which is the only party that can fix it and is still in the room.

Reads (env):
    INPUTS_SPEC_PATH            the resolved spec
    INPUTS_APP_DIR              where the app must be written
    INPUTS_TITLE                the app's title
    OMNIROUTE_MODEL             primary model (default "auto/coding")
    OMNIROUTE_MODEL_FALLBACK    second attempt (default "oc/big-pickle")
    OMNIROUTE_BASE_URL          the gateway (default "http://192.168.1.46:20129/v1",
                                the SSO proxy in front of it — `:20128` is the
                                gateway's own port, published on its host's
                                loopback and bridge alone)
    OMNIROUTE_API_KEY           the gateway's key, passed to the agent as
                                OPENAI_API_KEY (the agent never sees the
                                `.env` variable itself)

Each `OMNIROUTE_*` is read from this node's environment and then from the checkout's
`.env` — Archon strips the repo `.env` keys before the node runs, so the file is the copy
that survives (see `repo_omniroute`). That includes the key: read from `os.environ` alone
it is always empty under Archon, and the gateway then refuses every attempt with
"Missing environment variable: `OMNIROUTE_API_KEY`" while this node reports that the
agent wrote nothing. Set the model to a concrete id that can call tools: the auto policy
can land on a free member that answers in prose and writes nothing.

Emits {exit_code, model, attempts, log}.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import shlex
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
# The wait before each retry grows, because the thing being waited out is a rate-limit
# window and those are measured in minutes, not seconds. Measured on this deployment: a
# run whose three attempts were 20s apart failed all three with `429 Too Many Requests`,
# and an identical invocation succeeded four minutes later. A flat 20s retry spent three
# agent runs learning nothing.
RETRY_DELAY_SECONDS = 30
RETRY_BACKOFF = 4  # 30s, then 120s
# Said out loud when every attempt was refused rather than empty: "the agent wrote
# nothing" is true of a rate limit and of a model that cannot use tools, and they need
# different answers from whoever reads the failure.
RATE_LIMIT_MARKERS = (" 429 ", "429 too many requests", "too many requests", "exceeded retry limit")

# How many times a written-but-broken tree goes back to the agent. One: the agent is
# still in the room with the exact command and the compiler's own message, and a tree
# that fails a second repair is evidence about the task rather than bad luck.
MAX_REPAIRS = 1
# The plan's build, run here before packaging runs it in the image. Bounded well under
# RUN_TIMEOUT_SECONDS: this is one install and one compile, not an agent turn.
BUILD_TIMEOUT_SECONDS = 600
# Enough of the compiler's output to name the file and the reason. The agent gets this;
# the build log keeps the whole thing.
BUILD_ERROR_CHARS = 4_000
# Languages whose install step writes *inside the project*: `npm install` goes to
# ./node_modules, `composer install` to ./vendor. Only those are built here, because the
# node runs on the host and a `pip install -r requirements.txt` would install into
# whatever interpreter is on PATH — a change outside the project that this node has no
# business making. A plan in any other language is still built and repaired in its image,
# where the install is the container's business and cannot reach the host.
BUILDABLE_HERE = frozenset({"node", "go", "php", "rust"})

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


# The door, not the gateway's own port: this node runs inside a container, where
# `127.0.0.1:20128` is the container itself. See `.env.example`.
DEFAULT_GATEWAY = "http://192.168.1.46:20129/v1"

# What the agent is allowed to inherit. Deliberately NOT the whole environment:
# a workflow node runs inside whatever the caller had, and the caller's variables
# decided the outcome here. Measured on this stack, the agent received
# `OPENAI_BASE_URL=http://192.168…` (a LAN address, not this gateway) alongside
# `ANTHROPIC_BASE_URL` and a set of archon-issued Codex tokens — and then sat at zero
# CPU for five minutes with a thread parked in `unix_stream_data_wait`, having never
# sent a request. The explicit provider config below cannot win against an endpoint
# the process is told to use by its environment.
INHERIT = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL",
           "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Variables that redirect the agent somewhere other than the configured provider.
# Stripped even when present, because inheriting them is never intended.
HIJACKS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL",
           "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def _search_roots() -> list[Path]:
    """Where to look for the checkout that owns this build.

    NOT this file's own directory. Archon copies the workflow into
    `artifacts/runs/<id>/workflow-source/project/…` and runs it from there, so walking
    up from `__file__` walks up a copy that has no `.env` — the first version of this
    did exactly that and silently kept the code defaults. The app directory and the
    spec are the two paths the workflow already resolved for us, and both live inside
    the real checkout (`<repo>/builds/<slug>`, `<repo>/build-requests/<slug>.md`).
    """
    roots: list[Path] = []
    for var in ("INPUTS_APP_DIR", "INPUTS_SPEC_PATH"):
        value = (os.environ.get(var) or "").strip()
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        roots.append(path if path.is_dir() else path.parent)
    roots.append(Path(__file__).resolve().parent)
    return roots


@functools.lru_cache(maxsize=1)
def repo_omniroute() -> dict[str, str]:
    """`OMNIROUTE_*` from the checkout's `.env` — the copy Archon takes away from here.

    Archon loads the target repo's `.env` and strips those keys before a script node
    runs ("stripped 42 keys from /…/olympus (.env)"), so `os.environ` in this node
    carries the *defaults*, not the deployment's configuration. Measured: a node
    configured with `OMNIROUTE_MODEL=gemini/gemini-3-flash-preview` reported
    `models auto/coding -> oc/big-pickle`, asked the gateway for `auto/coding`, and the
    manifest recorded a model that was never configured — the combo then walked 1109
    fallbacks before answering. The runner and CI hand the same values in through the
    environment and one of the two always survives, so this reads the file rather than
    assuming which layer won.

    Only `OMNIROUTE_*` is read: this node needs a model and a gateway, not the stack's
    tokens. Authentication is left to `CODEX_HOME/auth.json` on purpose.
    """
    for start in _search_roots():
        for parent in (start, *start.parents):
            candidate = parent / ".env"
            if not candidate.is_file():
                continue
            parsed: dict[str, str] = {}
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("OMNIROUTE_"):
                    parsed[key] = value.strip().strip('"').strip("'")
            return parsed
    return {}


def setting(key: str) -> str:
    """A configured value: this node's environment first, then the checkout's `.env`."""
    return (os.environ.get(key) or "").strip() or repo_omniroute().get(key, "")


@functools.lru_cache(maxsize=1)
def plan_contract():
    """`scripts/project_plan.py` from the checkout, or None when it is not there.

    Loaded by path rather than imported by name: this node runs from a copy of the
    workflow under `artifacts/runs/`, where the script beside it is the copy and the
    checkout's copy is the deployment's. Returns None rather than failing so a
    checkout without the module builds the way it did before it existed — a missing
    contract is not a reason to stop a build that has a spec.
    """
    for start in _search_roots():
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
    if module is None:
        return path.name == "plan.json"
    try:
        return bool(module.is_control_file(str(path.relative_to(app_dir))))
    except ValueError:
        return False


def plan_section(app_dir: Path) -> str:
    """The plan section of the prompt: the contract, or a sentence saying there is none.

    A build with no plan is not refused — the spec is still a spec — but it is said
    out loud instead of leaving `{{PLAN}}` in the prompt as prose the agent has to
    interpret, which is how a placeholder becomes an instruction.
    """
    module = plan_contract()
    plan = module.read_plan_file(app_dir) if module is not None else None
    if plan is None:
        return (
            "## The stack is not decided for you\n\n"
            "No plan was written for this build, so the specification's tech stack decides\n"
            "the shape. Match it, and keep the project runnable on its own: nothing will be\n"
            "installed for you, and the app has to start without help."
        )
    return module.plan_prompt_block(plan)


def agent_env(gateway: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in INHERIT}
    for key in HIJACKS:
        env.pop(key, None)
    env.setdefault("HOME", os.path.expanduser("~"))
    # The gateway key: this node's environment first, then the checkout's `.env`, for
    # the same reason the model and the base URL are read that way — Archon strips the
    # stack's `.env` keys out of the node's environment, so `os.environ` alone finds
    # nothing. Measured: with the key unset the agent exited 1 on all three attempts
    # having written nothing, printing only "ERROR: Missing environment variable:
    # `OMNIROUTE_API_KEY`." — which `verify` then reported as "the agent wrote
    # nothing", reading as a model failure when the model was never reached.
    #
    # BOTH NAMES, because two different layers read them. `OMNIROUTE_API_KEY` is the
    # name in the provider's `env_key` in the operator's `$CODEX_HOME/config.toml`
    # (`[model_providers.omniroute] env_key = "OMNIROUTE_API_KEY"`), and the `-c`
    # overrides below *merge* with that file rather than replacing it — so a runner
    # invoked against such a config, as on this host, needs the name it declares.
    # `OPENAI_API_KEY` is what an Archon-supplied CODEX_HOME falls back to (it gets
    # an `auth.json` and no `config.toml`). Neither is ever wrong: it is the same
    # gateway credential.
    key = setting("OMNIROUTE_API_KEY")
    if key:
        env["OMNIROUTE_API_KEY"] = key
        env["OPENAI_API_KEY"] = key
    return env


def model_chain() -> list[str]:
    """The models to try, in order, across attempts.

    Primary first so the deployment's configured model gets its chance, then the
    concrete fallback, then the composed route as a last resort. Three strategies
    rather than a repeat, and deduplicated, because retrying the same failing model
    twice is just waiting longer for the same answer.
    """
    primary = setting("OMNIROUTE_MODEL") or DEFAULT_MODEL
    fallback = setting("OMNIROUTE_MODEL_FALLBACK") or DEFAULT_FALLBACK_MODEL
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    # Last, the composed route: it walks every member with credentials until one
    # answers. Not the first or second choice — which member serves a turn, and whether
    # it can call tools, is luck — but by the last attempt a coin toss beats stopping.
    # Measured: three attempts against two cooling-down free models wrote nothing while
    # the wider catalogue was answering.
    if DEFAULT_MODEL not in chain:
        chain.append(DEFAULT_MODEL)
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
    base_url = setting("OMNIROUTE_BASE_URL") or DEFAULT_GATEWAY
    return [
        "-c", 'model_provider="omniroute"',
        "-c", 'model_providers.omniroute.name="OmniRoute"',
        "-c", f'model_providers.omniroute.base_url="{base_url}"',
        "-c", 'model_providers.omniroute.wire_api="responses"',
        "-c", 'model_providers.omniroute.requires_openai_auth=true',
    ]


def retry_delay(attempt: int) -> int:
    """How long to wait before this attempt. Grows, because a rate limit does.

    `attempt` is 1-based, and the first attempt never waits.
    """
    if attempt <= 1:
        return 0
    return RETRY_DELAY_SECONDS * (RETRY_BACKOFF ** (attempt - 2))


def looked_rate_limited(output: str) -> bool:
    """Whether what the agent said was a refusal to answer rather than an empty reply."""
    lowered = output.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def has_artifact(app_dir: Path) -> bool:
    """Whether the agent has produced anything at all.

    Any file counts, nested or not — except the workflow's own. `plan.json` is written
    into this directory by the planning node *before* the agent runs, so counting it
    would end the retry before the first attempt had a chance to write anything: the
    node reads "something exists, stop here" and hands an empty app to verify.

    Deliberately not a quality judgement — verify owns that. This answers one narrow
    question: would another attempt be repeating a no-op, or destroying work?
    """
    try:
        return any(
            path.is_file() and not is_workflow_file(app_dir, path) for path in app_dir.rglob("*")
        )
    except OSError:
        return False


def plan_build_commands(app_dir: Path) -> list[str]:
    """The commands the image will run for this plan, as the plan declares them.

    Read from the plan on disk rather than rebuilt from the spec: the plan is what the
    packager writes into the Dockerfile and what the runtime starts, so running it here
    is running the same thing packaging would, only early enough for the agent that
    wrote the tree to answer for it.

    An absent module or an absent plan is not an error — a project built before the
    planner existed has no plan, and it packaged fine before this existed. A plan in a
    language whose install would reach outside the project is refused for the reason in
    `BUILDABLE_HERE`; that is a limitation of running this on a host, not a statement
    about the project.
    """
    module = plan_contract()
    if module is None:
        return []
    plan = module.read_plan_file(app_dir) if hasattr(module, "read_plan_file") else None
    if not plan:
        return []
    runtime = plan.get("runtime") if isinstance(plan.get("runtime"), dict) else {}
    language = str(runtime.get("language") or "").strip().lower()
    if language not in BUILDABLE_HERE:
        return []
    run = plan.get("run") if isinstance(plan.get("run"), dict) else {}
    commands = [str(run.get("install") or "").strip(), str(run.get("build") or "").strip()]
    return [command for command in commands if command]


def build_program(commands: list[str]) -> str:
    """The program the first command needs, or "" when it cannot be read."""
    if not commands:
        return ""
    try:
        parts = shlex.split(commands[0])
    except ValueError:
        return ""
    return parts[0] if parts else ""


def run_plan_build(
    app_dir: Path, commands: list[str], child_env: dict[str, str]
) -> tuple[int, str]:
    """Install and build the project exactly as the Dockerfile will, and report it.

    Through `bash -lc` because that is what a Dockerfile `RUN` is: a shell. Passing the
    commands as arguments to a shell rather than a shell string keeps `shlex` out of the
    security story — the plan's commands are already what the image executes as root,
    and this is the same string in the same order, not a new capability.
    """
    joined = " && ".join(commands)
    try:
        completed = subprocess.run(
            ["bash", "-lc", joined],
            cwd=str(app_dir),
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, f"the plan's build did not finish within {BUILD_TIMEOUT_SECONDS}s"
    except OSError as error:
        return 127, f"could not run the plan's build: {error}"

    output = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
    if len(output) > BUILD_ERROR_CHARS:
        output = "… (truncated)\n" + output[-BUILD_ERROR_CHARS:]
    return completed.returncode, output


def repair_prompt(base_prompt: str, commands: list[str], output: str) -> str:
    """The build's own failure, appended to the instruction that produced the tree.

    The whole original prompt is kept, so the repair is a correction to the same task
    rather than a new one: an agent handed only the error tends to rewrite the project
    around it, and the plan and the spec are what stop that.
    """
    return (
        base_prompt
        + "\n\n## The tree you just wrote does not build\n\n"
        + f"Running the plan's own commands — `{' && '.join(commands)}` — in the working "
        + "directory exited non-zero:\n\n```\n"
        + (output or "(no output)")
        + "\n```\n\n"
        + "Fix the cause in the files you wrote. Do not remove a feature, stub a module, "
        + "or change the stack to make the error go away, and do not run a packager — "
        + "packaging runs after you, from the same plan. When the tree builds, stop."
    )


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
        .replace("{{PLAN}}", plan_section(app_dir))
    )

    app_dir.mkdir(parents=True, exist_ok=True)

    codex = (os.environ.get("CODEX_BIN") or "").strip() or shutil.which("codex") or "codex"

    gateway = setting("OMNIROUTE_BASE_URL") or DEFAULT_GATEWAY
    child_env = agent_env(gateway)
    # Archon runs this node with its own provider environment. Do not let that
    # environment's CODEX_HOME override the explicit OmniRoute configuration above:
    # it can contain an Archon/Claude config that makes Codex exit cleanly without
    # ever reaching the coding model. The agent gets only the gateway key and the
    # explicit -c provider settings, which is also how the verified runner smoke
    # invocation behaves.
    child_env.pop("CODEX_HOME", None)
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
            delay = retry_delay(attempt)
            note(
                f"retry {attempt}/{MAX_ATTEMPTS} on {model} — nothing written yet, "
                f"waiting {delay}s"
            )
            time.sleep(delay)

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
        if not has_artifact(app_dir) and output:
            note(f"agent output (no artifact yet): {output[-1200:]}")

        if has_artifact(app_dir):
            # Stop here even when the exit code is non-zero: an agent whose last
            # request 503'd *after* it had written the app is a success this node must
            # not throw away. Whether that app is any good is verify's question.
            break

    # Reported, not judged — verify-app decides whether an app exists — but named when
    # nothing exists and the reason is a rate limit, because the run's failure will say
    # "the agent wrote nothing" and the operator would otherwise have to read the log to
    # learn that the gateway refused every turn rather than that the model failed.
    if not has_artifact(app_dir):
        if looked_rate_limited(output):
            note(
                "every attempt was rate-limited (HTTP 429) — the free pool was busy. This is "
                "not a fault in the spec or the prompt: re-run the workflow, or pin a model "
                "with quota in OMNIROUTE_MODEL."
            )
        else:
            note(
                "all agent attempts produced no project files; the final agent exit/log "
                "is recorded above. Verify will refuse this empty artifact. Check model "
                "tool-call capability, gateway credentials, and the captured agent output."
            )

    # Written, but does it build? The retry above stopped the moment a file existed, so
    # this is the first and only point where the plan's own commands run against the tree
    # the agent wrote. Skipped unless the toolchain the plan asks for is here: a build
    # this node cannot run would produce an error about a missing program and hand that
    # to the agent as if the project were broken, and the packager installs its own
    # toolchain in the image anyway.
    commands = plan_build_commands(app_dir)
    build_exit = 0
    repairs = 0
    if has_artifact(app_dir) and commands:
        program = build_program(commands)
        if not program or shutil.which(program) is None:
            note(
                f"skipping the plan's build: {program or 'its first command'} is not on "
                "PATH here — packaging runs it in the image"
            )
        else:
            build_exit, build_output = run_plan_build(app_dir, commands, child_env)
            note(f"plan build exit={build_exit}")
            while build_exit != 0 and repairs < MAX_REPAIRS:
                repairs += 1
                note(
                    f"repair {repairs}/{MAX_REPAIRS}: the build failed, handing its output "
                    f"back to {model}"
                )
                repair_argv = [
                    codex, "exec",
                    "-C", str(app_dir),
                    "--skip-git-repo-check",
                    "--sandbox", "workspace-write",
                    "-m", model,
                    *gateway_overrides(),
                    repair_prompt(prompt, commands, build_output),
                ]
                exit_code, output = run_agent(repair_argv, app_dir, child_env)
                note(f"agent exit={exit_code} repair={repairs} model={model}")
                build_exit, build_output = run_plan_build(app_dir, commands, child_env)
                note(f"plan build exit={build_exit} after repair {repairs}")

    print(json.dumps({
        "exit_code": exit_code,
        "model": model,
        "attempts": attempt,
        # Recorded so a reader can tell "the agent gave up" from "the agent's tree does
        # not compile": a run whose build_exit is non-zero is one verify will report as
        # written and unbuildable, and the number says whether it was ever retried.
        "build_exit": build_exit,
        "repairs": repairs,
        "log": output,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        fail(str(error))

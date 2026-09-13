#!/usr/bin/env python3
"""The plan contract, in one place.

WHY THIS IS A MODULE. A plan is what turns "build me a weight tracker" into a
stack, a port and three commands. Three readers have to agree on it and they are
not the same program: the runner validates the plan that came back from a browser,
the packager and the runtime read the one on disk, and the greenfield workflow now
plans a spec before it builds it. Two implementations of "what is a valid plan"
drift exactly where drift costs the most — a plan one side accepts and the other
refuses, or a port one side takes and the other silently replaces.

`web/studio/lib/plan.ts` is the same contract for the browser surface, and the
rules here are a port of it. The *prose* is duplicated on purpose: the Studio
prompt talks about what the person confirming it will see, and this one talks
about a spec with nobody to ask, so sharing it would mean writing neither well.

WHAT A PLAN IS NOT. It is not a scaffold. The model names a language and the
commands that install, build and start the project; the packager writes the
Dockerfile around them and the container runs them. Anything the project needs has
to be declared in its own files — a requirements.txt, a package.json — because
nothing is installed for it.

Read by: `scripts/build-runner.py` (validation at the queue boundary),
`scripts/package-project.py` and `scripts/app-runtime.py` (the plan on disk), and
`.archon/workflows/app/greenfield/scripts/plan-app.py`, which plans a spec before
the agent builds it. By hand: `python3 scripts/project_plan.py --spec
build-requests/x.md`.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request

# The plan's filename inside the project directory. One name, because three
# scripts resolve it and a typo in one of them is a project that cannot run.
PLAN_NAME = "plan.json"

# Files the *workflow* writes into the app directory rather than the model. They
# are not part of the app: counting one as a built file would report a directory
# of nothing as a directory of something, and treating one as "the agent has
# started writing" would turn the retry off before the first attempt.
CONTROL_FILES = (PLAN_NAME,)

MIN_PORT = 1024
MAX_PORT = 49151
DEFAULT_PORT = 3000

MAX_COMMAND_CHARS = 500
MAX_NAME_CHARS = 80
MAX_SUMMARY_CHARS = 400
MAX_FILES = 60
MAX_PATH_CHARS = 200
MAX_PURPOSE_CHARS = 200
MAX_NOTES_CHARS = 1_200
MAX_LANGUAGE_CHARS = 40
MAX_FRAMEWORKS = 8

# The languages the packager has a base image for. Not a restriction on what a
# plan may say — `package-project.py` is the authority and it refuses by name —
# but the planner is asked for one of these, because a plan the packager cannot
# build is a build that fails after the model has already spent its turn.
LANGUAGES = ("node", "python", "go", "php", "ruby", "static")


class PlanError(Exception):
    """A refusal with a sentence, rather than a traceback."""


def is_control_file(path: str) -> bool:
    """Whether a path inside an app directory is the workflow's, not the app's."""
    return path.replace("\\", "/").lstrip("./") in CONTROL_FILES


def slugify(value: str, fallback: str = "app") -> str:
    """A name as a hostname label.

    This is the one part of a plan that reaches the network, so it is normalised
    here rather than trusted: lowercase ASCII letters, digits and single hyphens,
    no leading or trailing hyphen, never empty. "Café & Bar / v2" becomes
    `cafe-bar-v2`, and a name that normalises to nothing falls back to the kind,
    because an empty label is not a hostname.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    # Strip combining marks so "café" folds to "cafe" rather than losing the e.
    text = "".join(char for char in text if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return slug or fallback


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:limit]


def _command(value: object, label: str) -> str:
    """A command is one line, and it is bounded.

    One line because a newline in it is either a model wrapping its output or an
    attempt at a second command, and neither is something to run: in a Dockerfile
    it is a line continuation that silently joins the next instruction on.

    Refused rather than truncated when it is too long. Cutting one at the limit can
    leave a command that still runs and does something else — `npm install &&
    npm run build` losing its second half is a build that reports success over an
    output directory nothing wrote.
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise PlanError(f"the plan's {label} command is not a string")
    text = " ".join(value.split()).strip()
    if len(text) > MAX_COMMAND_CHARS:
        raise PlanError(f"the plan's {label} command is longer than {MAX_COMMAND_CHARS} characters")
    return text


def _port(value: object, default: int | None, coerce: bool) -> int | None:
    """The port, or the default.

    `coerce` is the difference between the two sources of a plan. A reply from a
    model says `"8000"` often enough that reading the number out of it is right. A
    plan that has been through the browser and back has already been typed, so a
    string there means the contract was broken, and recording it as absent is
    better than treating `"8000"` as 8000.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        port = value
    elif coerce:
        text = _text(value, 10)
        if not text.isdigit():
            return default
        port = int(text)
    else:
        return default
    return port if MIN_PORT <= port <= MAX_PORT else default


def _health_path(value: object) -> str:
    """A same-origin path, never a URL — the runtime fetches this, not a browser."""
    text = _text(value, 200)
    if not text.startswith("/"):
        return "/"
    return text.split("?")[0] or "/"


def _file_path(value: object) -> str:
    text = re.sub(r"^\.?/", "", _text(value, MAX_PATH_CHARS))
    if not text or ".." in text or text.startswith("/"):
        return ""
    return text


def _string_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        text = _text(entry, 60)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _files(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        path = _file_path(entry.get("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "purpose": _text(entry.get("purpose"), MAX_PURPOSE_CHARS)})
        if len(out) >= MAX_FILES:
            break
    return out


def extract_json_object(text: str) -> str | None:
    """The first JSON object in a reply.

    Scanned rather than regex-matched, because a brace inside a string literal —
    in a note, or a file's purpose — would end a regex early. Braces inside
    strings are tracked, and the object ends at the matching close brace.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def normalize_plan(
    payload: object,
    kind: str = "app",
    *,
    default_port: int | None = None,
    coerce: bool = False,
) -> dict:
    """A plan as an object, checked field by field, or a `PlanError` naming what is missing.

    `kind` is passed in rather than read from the payload: a person chose it, and a
    model reply changing what "app" means would make the choice decorative.

    The two keywords are the rules the readers genuinely disagree on, so they are
    arguments rather than hidden differences:

    * `default_port` — `None` leaves an absent port absent and lets the runtime
      decide, which is what the runner does for a plan the browser already filled
      in. A number is for the planner, whose plan is about to run: a plan with no
      port is a container serving on a port nobody chose.
    * `coerce` — whether to read the loose forms a model writes, like a quoted
      port. A plan arriving from the browser was already typed there.
    """
    if not isinstance(payload, dict):
        raise PlanError("the plan is not a JSON object")

    run = payload.get("run")
    if not isinstance(run, dict):
        raise PlanError("the plan has no `run` section, so there is nothing to start")

    start = _command(run.get("start"), "start")
    if not start:
        raise PlanError("the plan has no start command, so nothing would run")

    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    name = _text(payload.get("name"), MAX_NAME_CHARS)
    resolved_kind = "website" if kind == "website" or payload.get("kind") == "website" else "app"

    return {
        "v": 1,
        "name": name or ("New website" if resolved_kind == "website" else "New app"),
        "slug": slugify(name, resolved_kind),
        "kind": resolved_kind,
        "summary": _text(payload.get("summary"), MAX_SUMMARY_CHARS) or "No summary was given.",
        "runtime": {
            "language": _text(runtime.get("language"), MAX_LANGUAGE_CHARS),
            "frameworks": _string_list(runtime.get("frameworks"), MAX_FRAMEWORKS),
            "database": _text(runtime.get("database"), MAX_LANGUAGE_CHARS) or None,
        },
        "run": {
            "install": _command(run.get("install"), "install"),
            "build": _command(run.get("build"), "build"),
            "start": start,
            "port": _port(run.get("port"), default_port, coerce),
            "healthcheck": _health_path(run.get("healthcheck")),
        },
        "files": _files(payload.get("files")),
        "notes": _text(payload.get("notes"), MAX_NOTES_CHARS) or None,
    }


def parse_plan_text(
    text: str,
    kind: str = "app",
    *,
    default_port: int | None = None,
    coerce: bool = False,
) -> dict:
    """A plan out of a model reply, which may be wrapped in prose or a fence."""
    raw = extract_json_object(text)
    if raw is None:
        raise PlanError("the planner did not return a JSON plan")

    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise PlanError(f"the planner's JSON could not be parsed: {error}") from error

    return normalize_plan(payload, kind, default_port=default_port, coerce=coerce)


def read_plan_file(directory) -> dict | None:
    """The `plan.json` in a project directory, normalised, or None when absent.

    Validated on the way in, not trusted: this file has been written by one
    process and is read by another, and it is a plan whose commands end up in a
    Dockerfile. A plan that does not validate is not a plan, it is a corruption —
    so it reads as absent, and packaging falls back to the project's own packager
    rather than executing half a file.
    """
    from pathlib import Path

    path = Path(directory) / PLAN_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return normalize_plan(payload)
    except PlanError:
        return None


def describe(plan: dict) -> str:
    """One line an operator can read in a log: what it is and what runs it."""
    language = plan["runtime"]["language"] or "unknown language"
    frameworks = plan["runtime"]["frameworks"]
    stack = f"{language} ({', '.join(frameworks)})" if frameworks else language
    database = plan["runtime"]["database"]
    kind = "website" if plan["kind"] == "website" else "app"
    return (
        f"{plan['name']} — {kind}, {stack}"
        + (f", {database}" if database else "")
        + f", start `{plan['run']['start']}` on port {plan['run']['port'] or 'the runtime\'s choice'}"
    )


def plan_prompt_block(plan: dict) -> str:
    """The contract appended to the builder's prompt when a plan exists.

    This is the half of the plan that reaches the model. Without it the plan would
    only describe the container the project ends up in, and the agent would still
    be guessing at the stack — which is how a spec whose Tech Stack says "React 19
    + Node HTTP API + SQLite" turned into a client-only directory that could not
    be packaged.
    """
    run = plan["run"]
    runtime = plan["runtime"]
    stack = runtime["language"] or "unspecified"
    if runtime["frameworks"]:
        stack += f" ({', '.join(runtime['frameworks'])})"

    lines = [
        "## The stack is decided",
        "",
        "This project was planned before the build started, and the plan is the contract:",
        "it is what gets packaged and what gets run. Build to it. Do not substitute a",
        "different language, framework or database because it would be easier.",
        "",
        f"    language     {stack}",
        f"    install      {run['install'] or '(nothing to install)'}",
        f"    build        {run['build'] or '(nothing to compile)'}",
        f"    start        {run['start']}",
        f"    port         {run['port']}",
        f"    healthcheck  {run['healthcheck']}",
    ]
    if runtime["database"]:
        lines.append(f"    database     {runtime['database']}")
    lines.append("")

    if plan["files"]:
        lines += ["Write exactly these files, each for the purpose given:", ""]
        width = max(len(entry["path"]) for entry in plan["files"])
        lines += [f"    {entry['path']:<{width}}  {entry['purpose']}" for entry in plan["files"]]
        lines.append("")

    lines += [
        "Rules that follow from the plan:",
        "",
        f"- **The server must listen on `0.0.0.0`, on port {run['port']}.** Not `127.0.0.1`: the",
        "  process runs in its own container, so a server bound to loopback is unreachable",
        "  from outside it, and the failure is invisible from inside.",
        f"- `{run['healthcheck']}` must answer 2xx with no authentication once the app is up.",
        f"- Those install and build commands are run for you, in that order, before the",
        f"  container starts. Anything they need — `requirements.txt`, `package.json`, `go.mod`",
        f"  — is a file you must write. Nothing is installed for you, and there is no network",
        f"  at runtime beyond what the project ships.",
        "- Anything worth keeping between restarts goes under the directory in `$DATA_DIR`,",
        "  which is the one path that survives a rebuild. Do not write state anywhere else.",
    ]
    if plan["notes"]:
        lines += ["", f"Noted when the plan was confirmed: {plan['notes']}"]

    return "\n".join(lines)


def plan_system_prompt(kind: str = "app") -> str:
    """The planning turn's instructions.

    Ported from `web/studio/lib/plan.ts` so the two surfaces ask for the same
    thing, with one difference that is the point of this path: there is no person
    to confirm the plan before it runs, so the spec is the authority and the
    summary is written for the log rather than for a confirm button.
    """
    subject = "WEBSITE" if kind == "website" else "FULL-STACK APPLICATION"
    languages = ", ".join(f'"{language}"' for language in LANGUAGES)

    return f"""You are the planning step of Olympus's build pipeline. A written specification is about to be built by a coding agent, and you decide what it is built *with*: the language, the commands that install it, build it and start it, the port it listens on, and the files it will contain. You reply with JSON, and nothing else.

Choose the stack from the specification, not from habit. Its "Tech Stack" section is a decision someone already made — use it, and do not quietly replace it with something easier. Where the spec is silent, pick what is genuinely best for the job and say why in the summary.

HOW IT WILL BE RUN. This plan is executed on a build host, in a container built from `runtime.language`:
- `run.install` runs first, with network access, to install dependencies.
- `run.build` then produces the deployable output. Leave it empty if there is nothing to compile.
- `run.start` starts the project and must stay in the foreground. It is served at `run.port`, which is the port inside the container. Pick the port your start command actually listens on.
- Anything the project needs must be declared in the project's own files — a package.json, requirements.txt, Gemfile, go.mod, composer.json. Nothing is installed for you.

This is being planned for {subject}.

Reply with exactly this JSON shape and no other keys:
{{
  "name": "Weight Tracker",
  "summary": "One or two sentences: what it does and the stack you chose.",
  "runtime": {{
    "language": "node",
    "frameworks": ["react", "express"],
    "database": "sqlite"
  }},
  "run": {{
    "install": "npm install",
    "build": "npm run build",
    "start": "npm start",
    "port": 3000,
    "healthcheck": "/"
  }},
  "files": [
    {{ "path": "package.json", "purpose": "Dependencies and scripts" }}
  ],
  "notes": "Anything a person should know — a limit, or a choice worth a second look, or null."
}}

Rules:
- `runtime.language` is the base the container needs, one of {languages}.
- `runtime.database` is a datastore name, or null when the project keeps no data. Say "sqlite" rather than naming a client library, and prefer a file-backed database for a single-container project — there is no second service to connect to.
- `files` is every file you intend to write, with a short purpose each. List real paths, not directories.
- "static" means there is no toolchain and no process to start — plain HTML, CSS and JavaScript that nginx serves. Leave `install` and `build` empty and make `start` exactly `nginx -g 'daemon off;'`. A React or Vite site is "node", because something has to bundle it.
- A website must not have a database unless the specification needs one.
- No markdown fences, no prose before or after the JSON. The object is the whole reply."""


def plan_messages(spec_text: str, title: str, kind: str = "app") -> list[dict[str, str]]:
    """The planning turn: the instructions, then the specification itself."""
    return [
        {"role": "system", "content": plan_system_prompt(kind)},
        {
            "role": "user",
            "content": (
                f"Plan the build of “{title}”.\n\n"
                "The specification, in full:\n\n"
                f"{spec_text.strip()}\n\n"
                "Reply with the JSON plan only."
            ),
        },
    ]


def find_repo_root(*hints: str) -> "Path | None":
    """The checkout that owns these paths.

    A workflow node runs from a COPY of the workflow, under
    `artifacts/runs/<id>/workflow-source/`, so walking up from the node's own file
    walks up a tree with no `.env` in it. The paths the workflow resolved for the
    node — the app directory it will build, the spec it was given — do live in the
    real checkout, so they are what this walks from.
    """
    from pathlib import Path

    for hint in hints:
        if not hint:
            continue
        start = Path(hint)
        if not start.is_absolute():
            start = Path.cwd() / start
        start = start if start.is_dir() else start.parent
        for candidate in (start, *start.parents):
            if (candidate / "scripts" / "project_plan.py").is_file():
                return candidate
    return None


def gateway_settings(root, prefix: str = "OMNIROUTE_") -> dict[str, str]:
    """`OMNIROUTE_*`: this process's environment first, then the checkout's `.env`.

    Both layers are needed. Archon loads the target repo's `.env` and strips those
    keys before a script node runs, so the environment alone carries the defaults
    rather than the deployment's configuration — measured: a node configured with
    one model asked the gateway for `auto/coding`. Pointing this at a gateway that
    is not the configured one is the failure mode, and it is silent.
    """
    import os
    from pathlib import Path

    values: dict[str, str] = {}
    dotenv = Path(root) / ".env" if root else None
    if dotenv is not None and dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith(prefix):
                values[key] = value.strip().strip('"').strip("'")

    for key, value in os.environ.items():
        if key.startswith(prefix) and value.strip():
            values[key] = value.strip()
    return values


def request_plan(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: int = 180,
    temperature: float = 0.2,
) -> dict:
    """Ask the gateway for a plan, and return the validated one.

    The same endpoint, body and JSON envelope as Studio's planning turn — one
    gateway, one shape. Failures carry what the gateway said: a plan that could not
    be produced is a build that must not start, and "no plan" on its own would send
    the operator looking at the spec rather than at the gateway.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()[:400]
        raise PlanError(f"the gateway refused the planning turn (HTTP {error.code}): {detail}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise PlanError(f"could not reach the gateway at {base_url} ({error})") from error

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise PlanError(f"the gateway's reply had no message in it: {str(payload)[:300]}") from error

    return parse_plan_text(str(content or ""), default_port=DEFAULT_PORT, coerce=True)


# The gateway's composed route. It walks every member that has credentials until one
# answers, which is exactly what is wanted as a *last* attempt: the pinned models here
# run on free tiers that go into cooldown, and when both are cooling down the choice is
# between no plan and a route that tries the rest of the catalogue.
DEFAULT_MODEL = "auto/coding"


def model_chain(settings: dict[str, str]) -> list[str]:
    """The models to ask, in order, deduplicated.

    The same keys the build uses, in the same order, so a deployment that pinned a
    concrete model plans with what it builds with. Each entry is a different strategy
    rather than a repeat: the configured model, then the concrete fallback, then the
    composed route.

    The last entry is measured rather than theoretical. A run whose two configured
    models were both unavailable — `oc/mimo-v2.5-free` timing out upstream and
    `gemini/gemini-3-flash-preview` reporting `model_cooldown` with a 23-minute reset —
    failed three attempts in a row while a third of the catalogue was answering. The
    composed route is not the *first* choice, because which member serves a turn is
    luck; it is the last one, because by then luck is better than nothing.
    """
    chain: list[str] = []
    for key in ("OMNIROUTE_MODEL", "OMNIROUTE_MODEL_FALLBACK"):
        model = (settings.get(key) or "").strip()
        if model and model not in chain:
            chain.append(model)
    if not chain:
        return [DEFAULT_MODEL]
    if DEFAULT_MODEL not in chain:
        chain.append(DEFAULT_MODEL)
    return chain


def plan_for_spec(
    spec_path,
    title: str = "",
    kind: str = "app",
    root=None,
    *,
    timeout: int = 180,
) -> dict:
    """Plan a written specification: one turn, validated, or a `PlanError`.

    The spec is the authority here rather than the person who would confirm a plan in
    Studio, so this only ever *asks*: what comes back is either a plan the packager
    can build or a refusal naming why not.

    Every model in the chain is tried once. A refusal is worth a second ask when the
    next entry is a different model — a rate-limited member and a model that answers
    in prose are different faults — and there is no third ask, because an agent run
    is about to spend far more than another planning turn on a plan nobody can read.
    """
    from pathlib import Path

    path = Path(spec_path)
    if not path.is_file():
        raise PlanError(f"spec not readable: {path}")

    spec_text = path.read_text(encoding="utf-8", errors="replace")
    settings = gateway_settings(root)
    base_url = settings.get("OMNIROUTE_BASE_URL") or "http://127.0.0.1:20128/v1"
    api_key = settings.get("OMNIROUTE_API_KEY") or ""
    messages = plan_messages(spec_text, title or path.stem, kind)

    failure: PlanError | None = None
    for model in model_chain(settings):
        try:
            return request_plan(base_url, api_key, model, messages, timeout=timeout)
        except PlanError as error:
            failure = error
            print(f"planning with {model} did not produce a plan: {error}", file=sys.stderr, flush=True)

    raise failure or PlanError("no model could produce a plan")


def write_plan_file(directory, plan: dict):
    """Write `plan.json` where the packager and the runtime read it."""
    from pathlib import Path

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / PLAN_NAME).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return target / PLAN_NAME


def main(argv: list[str] | None = None) -> int:
    """`make plan SPEC=…` — the planning step, runnable by hand.

    Prints the plan as JSON on stdout, so it can be piped, and the one-line
    description on stderr, where a person reads it.
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Plan a build-request spec before it is built.")
    parser.add_argument("--spec", required=True, help="the spec to plan (build-requests/<name>.md)")
    parser.add_argument("--app-dir", default="", help="where to write plan.json (default: builds/<slug>)")
    parser.add_argument("--title", default="", help="the app's title (default: the spec's filename)")
    parser.add_argument("--kind", default="app", choices=("app", "website"))
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for the planning turn")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and print, but write no plan.json",
    )
    args = parser.parse_args(argv)

    root = find_repo_root(args.spec, args.app_dir)
    try:
        plan = plan_for_spec(args.spec, args.title, args.kind, root, timeout=args.timeout)
    except PlanError as error:
        print(f"PLAN_FAILED: {error}", file=sys.stderr)
        return 1

    app_dir = Path(args.app_dir) if args.app_dir else (Path(args.spec).parent.parent / "builds" / plan["slug"])
    if not args.dry_run:
        write_plan_file(app_dir, plan)

    print(f"{describe(plan)}" + ("" if args.dry_run else f"  →  {app_dir / PLAN_NAME}"), file=sys.stderr)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

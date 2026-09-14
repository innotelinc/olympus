#!/usr/bin/env python3
"""olympus-tui.py — the builder, in a terminal.

WHY THIS EXISTS. Olympus has had two front ends for one builder and only one of
them had a UI. Studio is the browser surface: it queues a build, then polls a
status file and a log. Everything the terminal needs was already there — the
queue, the status, the log, the runner — but reaching it by hand meant `make app`,
then `make build-runner-list`, then following the log with `tail`. That is one
job spread over three windows. This is the same three things in one, streaming.

WHAT THIS IS NOT. It does not plan, and it does not generate. `scripts/project_plan.py`
exists because two implementations of "what is a valid plan" drift exactly where drift
costs the most, and the generation prompt is the same argument one layer down. So the
instruction typed here becomes a *spec*, and a spec is the builder's own input: the
runner walks the identical path from the identical file that `make app` and the
browser's Build It walk. What this adds is the view, not a second builder.

  olympus-tui.py                       interactive
  olympus-tui.py --once "a weight tracker with a weekly chart"
  olympus-tui.py --list                what is queued, running and finished
  olympus-tui.py --repo /path/to/olympus --queue /var/lib/olympus/build-queue

KEYS. Enter submits the instruction. `/` opens the command line: `/publish`,
`/preview`, `/open`, `/list`, `/help`, `/quit`. Ctrl-C stops watching (it does not
cancel the build — builds run under the runner's systemd unit, not under this
process, which is the point of queueing them).
"""

from __future__ import annotations

import argparse
import curses
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent


def _load_module(name: str, filename: str):
    """Import one of the sibling scripts, which have hyphens in their names.

    Same technique `scripts/tests/` uses: an importlib spec, so these stay
    standalone scripts you can run directly *and* importable without renaming them.
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_runner = _load_module("olympus_build_runner", "build-runner.py")
project_plan = _load_module("olympus_project_plan", "project_plan.py")

REQUEST_SUFFIX = build_runner.REQUEST_SUFFIX
RUNNING_SUFFIX = build_runner.RUNNING_SUFFIX
STATUS_SUFFIX = build_runner.STATUS_SUFFIX
HEARTBEAT_NAME = build_runner.HEARTBEAT_NAME

# How often the status file and the log are re-read while a build runs. The runner
# writes its status on the same order, so polling faster mostly re-reads the same
# bytes; polling slower makes the window feel dead, which is the complaint this
# whole UI exists to answer.
POLL_SECONDS = 0.5

MAX_TITLE_CHARS = 60
MAX_LOG_LINES = 400


# --- the instruction, as a spec ---------------------------------------------
#
# The builder's input is a spec. A person in a terminal types an instruction. The
# gap is filled here, and only here — the words the planner reads are the words that
# were typed. Headings are added because the factory's spec shape is markdown with a
# stated purpose, and *nothing else* is added, because inventing a tech stack or a
# feature list on the user's behalf is how a build stops being what they asked for.

SPEC_TEMPLATE = """# {title}

{instruction}

## What to build

The instruction above is the whole request. Choose the stack from it.

## Verification

The build is proved by the plan's own install, build and start commands running
against the finished tree, and by the healthcheck the plan declares answering.
"""


def title_for(instruction: str) -> str:
    """A one-line name for the app, from the first line of the instruction.

    Titles become hostnames, so this is deliberately narrow: the first line, with
    its whitespace collapsed, trimmed of the punctuation a sentence ends with, and
    capped. A title is not validated here beyond that — `project_plan.slugify` is
    what turns it into the label that reaches the network, and it is the authority
    on what a label may be.
    """
    first = ""
    for line in instruction.strip().splitlines():
        if line.strip():
            first = " ".join(line.split())
            break
    trimmed = first[:MAX_TITLE_CHARS].rstrip(" .,:;!?-")
    return trimmed or "Untitled build"


def slug_for(instruction: str) -> str:
    """The app's slug — `project_plan`'s rule, not a second copy of it."""
    return project_plan.slugify(title_for(instruction), "app")


def spec_text(instruction: str, title: str) -> str:
    return SPEC_TEMPLATE.format(title=title, instruction=instruction.strip())


def write_spec(repo: Path, slug: str, text: str, *, replace: bool) -> Path:
    """Write `build-requests/<slug>.md`, or refuse to clobber one.

    Refusing is the default because the spec is what the build is: overwriting one
    silently would replace a document someone may have written by hand with a
    one-line instruction. `--replace` and `/rebuild` are how that is asked for.
    """
    requests = repo / "build-requests"
    requests.mkdir(parents=True, exist_ok=True)
    target = requests / f"{slug}.md"
    if target.exists() and not replace:
        raise FileExistsError(
            f"build-requests/{slug}.md already exists — `/rebuild` to build over it"
        )
    target.write_text(text, encoding="utf-8")
    return target


# --- what is happening -------------------------------------------------------
#
# Every stage below is a fact read out of the status file or the log, never a timer
# and never an estimate. The markers are filenames the pipeline actually writes:
# `plan.json` before the agent runs, a manifest after it, and a name registration
# at the end. A stage that has no marker yet is simply not done.

PLAN_MARKERS = ("plan.json", "planned by", "PLAN_FAILED")
WRITTEN_MARKERS = ("MANIFEST.json", "project.manifest.json", "site.manifest.json", "BUILD_FAILED")
RUNNING_MARKERS = ("app-runtime.py", "app_up", "listening on", "RUN_FAILED")
NAME_MARKERS = ("studio-sites.py", "proxy host", "published", "NPM proxy")


def _seen(log: str, markers: tuple[str, ...]) -> bool:
    return any(marker in log for marker in markers)


def stages(action: str, status: dict | None, log: str) -> list[tuple[str, str]]:
    """The outline: `(label, state)` with state one of done/active/todo.

    `action` is the runner's own word for what was asked — build, preview or
    publish — because the last two steps genuinely differ between them and a
    generic "finish" would hide the difference the user chose.
    """
    state = str((status or {}).get("state") or "")
    finished = state in ("succeeded", "failed")
    picked_up = bool(status) and (finished or state in ("running", "queued", "starting"))

    steps: list[tuple[str, bool]] = [
        ("Queued for the runner", bool(status) or bool(log)),
        ("Runner picked it up", picked_up),
        ("Stack planned", _seen(log, PLAN_MARKERS)),
        ("Files written and built", _seen(log, WRITTEN_MARKERS)),
    ]

    if action == "build":
        steps.append(("Packaged", bool((status or {}).get("site")) or _seen(log, RUNNING_MARKERS)))
    elif action == "preview":
        steps.append(("Running, preview name registered", bool((status or {}).get("previewUrl"))))
    else:
        steps.append(("Running and published", bool((status or {}).get("publishedUrl"))))

    # The first step without its marker is the one in flight — unless the run is
    # over, in which case nothing is in flight and every unfinished step is simply
    # unfinished. Reporting a live step on a finished run is the same lie as a
    # progress bar that keeps moving after the work stopped.
    active = None
    if not finished:
        for index, (_label, done) in enumerate(steps):
            if not done:
                active = index
                break

    outlined: list[tuple[str, str]] = []
    for index, (label, done) in enumerate(steps):
        outlined.append((label, "done" if done else "active" if index == active else "todo"))
    return outlined


def outcome_line(status: dict | None, action: str, slug: str, suffix: str) -> str:
    """The one sentence that says where the build got to.

    The runner's own message when it has one, because it was written by the code
    that knew what failed; otherwise a sentence naming what this action produces.
    """
    if not status:
        return "Waiting for the runner to pick this up."
    message = str(status.get("message") or "").strip()
    if message:
        return message

    if action == "preview":
        return f"Previewing {slug}" + (f" on https://{slug}-preview.{suffix}" if suffix else "")
    if action == "publish":
        return f"Publishing {slug}" + (f" to https://{slug}.{suffix}" if suffix else "")
    return f"Building {slug}"


def files_written(log: str) -> list[str]:
    """Paths the log shows being written, newest last, deduplicated.

    Read from the log rather than from disk so the list appears while the build is
    still running. A path that scrolls past twice is one file.
    """
    found: list[str] = []
    for match in re.finditer(r"(?:wrote|writing|created|→)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", log):
        path = match.group(1)
        if path not in found:
            found.append(path)
    return found


def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


# --- delivering a build that already exists ----------------------------------
#
# Preview and publish are queued, not run here, for the same reason a build is:
# they take minutes, they need the container runtime and the checkout, and the
# runner is the thing that has both. This window stays a view onto that.
#
# The request carries the *files*, because the runner cannot read this host's
# `builds/` — the browser sends the same thing from the same reason. So what is
# read out here is the app's source, and caches, the packager's own outputs and the
# workflow's control files are left behind: they are regenerated, and a stale
# `dist/` inside a payload is how a publish serves code that no longer exists.

DELIVERY_SKIP_PREFIXES = (
    "node_modules/",
    ".git/",
    "dist/",
    ".next/",
    "__pycache__/",
    ".venv/",
    "venv/",
)
DELIVERY_SKIP_NAMES = (
    "project.zip",
    "site.zip",
    "MANIFEST.json",
    "project.manifest.json",
    "site.manifest.json",
    project_plan.PLAN_NAME,
)
MAX_DELIVERY_FILES = 200


def delivery_files(build_dir: Path) -> list[dict[str, str]]:
    """The app's own files, as a publish or preview request carries them."""
    if not build_dir.is_dir():
        return []

    found: list[dict[str, str]] = []
    for path in sorted(build_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(build_dir).as_posix()
        if relative.startswith(DELIVERY_SKIP_PREFIXES) or relative in DELIVERY_SKIP_NAMES:
            continue
        if path.is_symlink():
            continue
        found.append({"path": relative, "contents": read_text(path, 200_000)})
        if len(found) >= MAX_DELIVERY_FILES:
            break
    return found


def submit_delivery(queue: Path, slug: str, action: str, files: list[dict[str, str]]) -> str:
    """Queue a preview or a publish for an app whose files we have.

    Written with the runner's own constants and its atomic writer, so the entry is
    the same protocol the browser's queue entry is; the runner validates it on the
    way in either way, and a second definition of the request shape is a second
    thing that can disagree about a protocol version.
    """
    job = os.urandom(8).hex()
    payload = {
        "v": build_runner.PROTOCOL_VERSION,
        "job": job,
        "action": action,
        "slug": slug,
        "title": slug,
        # A spec exists to tell the factory what to manufacture, and neither of
        # these manufactures anything — writing one would leave factory input behind
        # that a later bare `make app` would build.
        "spec": "",
        "publish": action == "publish",
        "files": files,
        "requested_by": "tui",
        "requested_at": build_runner.now_iso(),
        "replace": False,
    }
    queue.mkdir(parents=True, exist_ok=True)
    build_runner.write_json_atomic(queue / f"{job}{REQUEST_SUFFIX}", payload)
    return job


# --- the queue, from the reading side ---------------------------------------


class Session:
    """One build, from the instruction to the status file it ends in."""

    def __init__(self, repo: Path, queue: Path) -> None:
        self.repo = repo
        self.queue = queue
        self.instruction = ""
        self.slug = ""
        self.job = ""
        self.action = "build"
        self.error: str | None = None

    def submit(self, instruction: str, *, replace: bool = False) -> None:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("Say what to build.")

        slug = slug_for(instruction)
        write_spec(self.repo, slug, spec_text(instruction, title_for(instruction)), replace=replace)

        # The runner's own submitter, so the queue entry this writes is byte-for-byte
        # the one the CLI and the browser write. A second implementation of the
        # request shape is a second thing that can disagree about a protocol version.
        code, payload = build_runner.submit(
            self.queue, self.repo, f"build-requests/{slug}.md", replace=replace
        )
        if code != 0 or not payload:
            raise RuntimeError(f"the queue refused {slug}")

        self.instruction = instruction
        self.slug = slug
        self.job = str(payload["job"])
        self.action = "build"
        self.error = None

    def status(self) -> dict | None:
        if not self.job:
            return None
        path = self.queue / f"{self.job}{STATUS_SUFFIX}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def queued(self) -> bool:
        if not self.job:
            return False
        return (self.queue / f"{self.job}{REQUEST_SUFFIX}").exists() or (
            self.queue / f"{self.job}{RUNNING_SUFFIX}"
        ).exists()

    def log(self) -> str:
        if not self.job:
            return ""
        return read_text(self.queue / f"{self.job}.log")

    def action_for(self, status: dict | None) -> str:
        value = str((status or {}).get("action") or self.action)
        return value if value in ("build", "preview", "publish") else self.action


# --- the window -------------------------------------------------------------


def render(
    session: Session,
    status: dict | None,
    log: str,
    width: int,
    *,
    message: str = "",
    suffix: str = "",
) -> list[tuple[str, str]]:
    """The whole screen as `(style, text)` pairs.

    Pure and separate from curses so the layout is testable without a terminal,
    and so the same lines can be printed by `--once`.
    """
    action = session.action_for(status)
    lines: list[tuple[str, str]] = []

    lines.append(("brand", f" Olympus TUI  ·  {action}  ·  runner " + (
        "live" if session.queued() else "queued"
    )))
    lines.append(("rule", "─" * max(1, width)))

    if not session.job:
        lines.append(("dim", " Type what you want built, then press Enter."))
        lines.append(("dim", " It becomes a spec, and the runner builds it — the same"))
        lines.append(("dim", " path Build It takes in the browser."))
        lines.append(("blank", ""))
        for example in (
            "A weight tracker: log a weight each morning, weekly trend against a goal",
            "A recipe box with search by ingredient",
            "A shift rota shown as a week grid",
        ):
            lines.append(("example", f"   {example}"))
        lines.append(("blank", ""))
        lines.append(("dim", " /list  what is queued, running and finished     /help  keys"))
        if message:
            lines.append(("blank", ""))
            lines.append(("error", f" {message}"))
        return lines

    lines.append(("label", f" {session.slug}"))
    lines.append(("dim", f"   {session.instruction.strip().splitlines()[0]}"))
    lines.append(("blank", ""))

    for label, state in stages(action, status, log):
        mark = "✓" if state == "done" else "•" if state == "active" else "·"
        lines.append((f"step-{state}", f"   {mark}  {label}"))

    lines.append(("blank", ""))
    state_word = str((status or {}).get("state") or "queued")
    style = "error" if state_word == "failed" else "ok" if state_word == "succeeded" else "dim"
    lines.append((style, f" {outcome_line(status, action, session.slug, suffix)}"))

    written = files_written(log)
    if written:
        lines.append(("blank", ""))
        lines.append(("label", f" Files ({len(written)})"))
        for path in written[-8:]:
            lines.append(("dim", f"   {path}"))

    lines.append(("blank", ""))
    lines.append(("label", " Log"))
    tail = [line for line in log.splitlines() if line.strip()][-14:]
    if not tail:
        lines.append(("dim", "   nothing yet — the runner writes here as it goes"))
    for line in tail:
        lines.append(("log", f"   {line[: max(1, width - 4)]}"))

    if message:
        lines.append(("blank", ""))
        lines.append(("error", f" {message}"))
    return lines


HELP_LINES = (
    "Enter        submit the instruction in the box",
    "/preview <slug>   package those files, run them, register a preview-only name",
    "/publish <slug>   the same, then put the app on its own name",
    "/rebuild          build again over an existing spec",
    "/list             what is queued, running and finished",
    "/help             this",
    "q                 leave (a running build keeps running)",
)


class Screen:
    """The curses half. Everything it draws comes from `render`."""

    STYLES = {
        "brand": ("Olympus", "BOLD"),
        "rule": ("", ""),
        "label": ("", "BOLD"),
        "dim": ("", ""),
        "blank": ("", ""),
        "example": ("", ""),
        "log": ("", "DIM"),
        "ok": ("", "BOLD"),
        "error": ("", "BOLD"),
        "step-done": ("", ""),
        "step-active": ("", "BOLD"),
        "step-todo": ("", "DIM"),
        "input": ("", "BOLD"),
    }

    def __init__(self, session: Session, suffix: str) -> None:
        self.session = session
        self.suffix = suffix

    def run(self, stdscr) -> int:
        curses.curs_set(1)
        stdscr.nodelay(False)
        stdscr.timeout(int(POLL_SECONDS * 1000))
        self._init_colors()

        typed = ""
        message = ""
        command_mode = False
        offset = 0

        while True:
            status = self.session.status()
            log = self.session.log()
            height, width = stdscr.getmaxyx()
            rows = render(self.session, status, log, width, message=message, suffix=self.suffix)

            when = str((status or {}).get("updated_at") or "")
            footer = f" {when}  enter=submit  /=commands  q=quit"
            rows = rows[: max(1, height - 2)] + [("dim", footer)]

            stdscr.erase()
            for index, (style, text) in enumerate(rows):
                if index >= height - 1:
                    break
                try:
                    stdscr.addstr(index, 0, text[: max(1, width - 1)], self._attr(style))
                except curses.error:  # the last column of the last line
                    pass

            # The prompt line, and it is the only line that takes input.
            label = "/" if command_mode else ">"
            prompt = f"{label} {typed}"
            try:
                stdscr.addstr(height - 1, 0, prompt[: max(1, width - 1)], self._attr("input"))
            except curses.error:
                pass
            stdscr.move(height - 1, min(len(prompt), max(0, width - 2)))
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue
            if key in (curses.KEY_ENTER, 10, 13):
                text, typed = typed.strip(), ""
                if command_mode:
                    command_mode = False
                    if text in ("q", "quit", "exit"):
                        return 0
                    message = self._command(text, offset)
                elif text:
                    message = self._submit(text)
            elif key == 27:  # Esc
                typed, command_mode = "", False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                typed = typed[:-1]
            elif key == 21:  # Ctrl-U
                typed = ""
            elif key == ord("/") and not typed:
                command_mode = True
            elif key == ord("q") and not typed and not command_mode:
                return 0
            elif 32 <= key < 127:
                typed += chr(key)
            elif command_mode:
                command_mode = False

    def _attr(self, style: str) -> int:
        name, flag = self.STYLES.get(style, ("", ""))
        return curses.color_pair(self._pairs.get(name, 0)) | (getattr(curses, f"A_{flag}") if flag else 0)

    def _init_colors(self) -> None:
        self._pairs: dict[str, int] = {"": 0}
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        self._pairs["Olympus"] = 1
        curses.init_pair(1, curses.COLOR_CYAN, -1)

    def _submit(self, text: str) -> str:
        try:
            self.session.submit(text)
        except FileExistsError as error:
            return str(error)
        except (ValueError, RuntimeError, build_runner.RequestError) as error:
            return str(error)
        return ""

    def _command(self, text: str, _offset: int) -> str:
        parts = text.split()
        name, args = (parts[0] if parts else ""), parts[1:]
        slug = args[0] if args else self.session.slug

        if name in ("help", "h"):
            return " | ".join(line.split("  ")[0].strip() for line in HELP_LINES)
        if name in ("quit", "q", "exit"):
            return "use q to leave"
        if name == "list":
            return listing(self.session.repo, self.session.queue)
        if name == "rebuild":
            if not self.session.instruction:
                return "nothing to rebuild yet"
            return self._submit(self.session.instruction)
        if name in ("publish", "preview", "build"):
            if not slug:
                return "no app yet — type an instruction first"
            return self._action_queued(slug, name)
        return f"unknown command: /{name} — try /help"

    def _action_queued(self, slug: str, action: str) -> str:
        """Queue a delivery for an app that has already been built, and follow it."""
        build_dir = self.session.repo / "builds" / slug
        files = delivery_files(build_dir)
        if not files:
            return f"nothing built at builds/{slug} — build it first"

        try:
            job = submit_delivery(self.session.queue, slug, action, files)
        except OSError as error:
            return f"could not queue that: {error}"

        self.session.job = job
        self.session.action = action
        self.session.slug = slug
        return f"queued {action} — {len(files)} file(s)"


def listing(repo: Path, queue: Path) -> str:
    """One line for the queue's contents, for the command line and `--list`."""
    if not queue.is_dir():
        return "nothing has been queued yet"
    counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
    for path in queue.iterdir():
        if path.name.endswith(REQUEST_SUFFIX):
            counts["queued"] += 1
        elif path.name.endswith(RUNNING_SUFFIX):
            counts["running"] += 1
        elif path.name.endswith(STATUS_SUFFIX):
            state = str((json.loads(read_text(path) or "{}") or {}).get("state") or "")
            counts[state] = counts.get(state, 0) + 1
    heartbeat = read_text(queue / HEARTBEAT_NAME)
    live = bool(heartbeat) and "beat_at" in heartbeat
    summary = ", ".join(f"{value} {key}" for key, value in counts.items() if value)
    return f"runner {'live' if live else 'NOT RUNNING'} — {summary or 'queue empty'}"


def once(repo: Path, queue: Path, instruction: str, suffix: str, timeout: float) -> int:
    """Submit one build, follow it to the end, and print where it got to.

    The non-interactive path exists so the same code can be driven from a script or
    a test — a curses window is not assertable, and the parts that decide what the
    screen says are the parts worth testing.
    """
    session = Session(repo, queue)
    try:
        session.submit(instruction, replace=False)
    except FileExistsError as error:
        # Already built under that name: reuse it, which is what a person would do.
        print(f"{error} — following it instead", file=sys.stderr)
        session.slug = slug_for(instruction)
        session.instruction = instruction
        for path in queue.glob(f"*{STATUS_SUFFIX}"):
            payload = json.loads(read_text(path) or "{}")
            if isinstance(payload, dict) and payload.get("slug") == session.slug:
                session.job = str(payload.get("job") or "")
                break
        if not session.job:
            print("no job for that app; pass --rebuild", file=sys.stderr)
            return 2

    deadline = time.monotonic() + timeout
    status: dict | None = None
    while time.monotonic() < deadline:
        status = session.status()
        if status and str(status.get("state")) in ("succeeded", "failed"):
            break
        time.sleep(POLL_SECONDS)

    log = session.log()
    for style, text in render(session, status, log, width=88, suffix=suffix):
        if text:
            print(text)
    state = str((status or {}).get("state") or "unknown")
    return 0 if state == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository root")
    parser.add_argument("--queue", default="", help="queue directory (default: .factory/build-queue)")
    parser.add_argument("--once", metavar="INSTRUCTION", help="submit one build and follow it, then exit")
    parser.add_argument("--rebuild", action="store_true", help="with --once: build over an existing spec")
    parser.add_argument("--list", action="store_true", help="print the queue and exit")
    parser.add_argument("--timeout", type=float, default=0, help="with --once: how long to follow it")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    queue = Path(args.queue).expanduser().resolve() if args.queue else build_runner.default_queue_dir(repo)
    suffix = env_suffix(repo)

    if args.list:
        print(listing(repo, queue))
        return 0

    if args.once:
        timeout = args.timeout or float(os.environ.get("BUILD_TIMEOUT_SECONDS") or 1800)
        if args.rebuild:
            write_spec(
                repo,
                slug_for(args.once),
                spec_text(args.once, title_for(args.once)),
                replace=True,
            )
        return once(repo, queue, args.once, suffix, timeout)

    session = Session(repo, queue)
    # A terminal is the thing this is for; without one, say so and stop rather than
    # emitting escape codes into a pipe.
    if not sys.stdout.isatty():
        print("not a terminal — use --once or --list", file=sys.stderr)
        return 2
    curses.wrapper(Screen(session, suffix).run)
    return 0


def env_suffix(repo: Path) -> str:
    """`SITE_HOST_SUFFIX` from the repo `.env`, for naming what a delivery produces."""
    path = repo / ".env"
    if not path.is_file():
        return ""
    for line in read_text(path).splitlines():
        if line.strip().startswith("SITE_HOST_SUFFIX="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


if __name__ == "__main__":
    sys.exit(main())

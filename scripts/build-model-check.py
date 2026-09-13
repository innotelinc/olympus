#!/usr/bin/env python3
"""Can the gateway's configured build model actually call a tool?

WHY THIS EXISTS. Every way this stack's build model fails is silent. A model with
no quota, a model that answers in prose, and a model that is simply not there all
produce the same observable: Codex exits **0**, the app directory stays empty, and
nothing in the log says why. Measured here, a build ran for eight minutes against
`auto/coding` and wrote nothing, while the ten restored provider connections
answered `402 budget pool quota exhausted`, `401 credits exhausted` and `429 no
credits remaining` — only the gemini accounts had quota at all.

None of that is visible from a connection count, a 200, or an exit code. What is
visible is whether the model reaches for a tool, and whether it can do so **twice**
— a build is a multi-turn loop, and the failure that cost the most time here was
on the *second* turn: the combo pins a native Codex turn to the member that served
the first one, and where that member has no credentials every turn after the first
answers

    503  No credentials for opencode

So this script runs two turns and reports on both:

    scripts/build-model-check.py                    # the configured chain
    scripts/build-model-check.py --json             # machine-readable
    scripts/build-model-check.py --model auto/coding

Exit codes, and the point of the whole script:

    0  a model in the chain called a tool and carried the conversation to a
       second turn — a build can produce something
    1  none did — a build would run, exit 0, and write nothing to disk
    2  the check could not tell (config missing, or the gateway did not answer)

1 and 2 are different claims and are kept apart: 1 means the deployment is
broken, 2 means this script does not know whether it is.

WHAT A PASS DOES NOT PROVE. A single-shot tool call is not the same as a working
build, and this check was written after getting that wrong once: `oc/big-pickle`
was recorded here as never calling a tool, and it calls one readily — with a
one-line prompt, a three-tool prompt, and on a follow-up turn. Whatever made that
earlier build write nothing was the multi-turn path, not the model's ability to
call a function. A pass here means "worth trying"; the measured outcome that
matters is whether `builds/<slug>` gains files, which is what the runner's
`artifact` counts in `.factory/build-queue/*.status.json` record.

The chain is `OMNIROUTE_MODEL` then `OMNIROUTE_MODEL_FALLBACK`, read from the repo
`.env` (process environment wins where it says anything, matching the rest of the
tooling) — the same two names `build-app.py` retries through, so this reports on
the models a build actually uses rather than on a list kept in two places.

Nothing the model proposes is executed. The tool is described to the model and its
arguments are read; they are never run here — the check must be safe to run on a
timer against production. The tool *result* fed back is the literal string "ok".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --- what we ask --------------------------------------------------------------

# Cheap and unambiguous. A model that cannot call a tool answers this in prose,
# which is precisely the failure being looked for.
PROBE_INPUT = "Create a file named probe.txt containing the word ok. Use the shell tool."

# The follow-up. Its content does not matter much; what matters is that the
# gateway has to serve a second turn on the same conversation.
PROBE_FOLLOW_UP = "Now run `ls -la probe.txt` with the shell tool to confirm it exists."

# The smallest tool that still looks like the one Codex sends: a named function
# taking a single `command` string. Deliberately not the real Codex payload — a
# model that cannot handle this one cannot handle that one either, and the
# narrower shape is the same for every model we compare.
PROBE_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run a shell command.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The command to run."}},
        "required": ["command"],
    },
}

ENV_KEYS = ("OMNIROUTE_BASE_URL", "OMNIROUTE_API_KEY", "OMNIROUTE_MODEL", "OMNIROUTE_MODEL_FALLBACK")

# A combo walks the gateway's catalogue — measured at 1397 routing decisions and
# 70s for one request — so the ceiling has to be generous enough not to call a
# slow-but-working model broken. It applies per request, and the probe makes two.
DEFAULT_TIMEOUT = 180

OUTCOME_TOOL = "tool-call"
OUTCOME_FIRST_TURN_ONLY = "first-turn-only"
OUTCOME_PROSE = "prose-only"
OUTCOME_EMPTY = "no-output"
OUTCOME_ERROR = "error"
OUTCOME_UNREACHABLE = "unreachable"

EXIT_OK = 0
EXIT_NO_MODEL = 1
EXIT_UNVERIFIED = 2

# What each outcome means for the operator, kept next to the outcome so a new one
# cannot be added without saying what it implies.
OUTCOME_MEANING = {
    OUTCOME_TOOL: "calls the tool, and the next turn works — usable as a build model",
    OUTCOME_FIRST_TURN_ONLY: "calls the tool once, then the conversation breaks — a build would write nothing after the first turn",
    OUTCOME_PROSE: "answers in prose instead of calling the tool — writes nothing",
    OUTCOME_EMPTY: "answered without calling the tool or saying anything",
    OUTCOME_ERROR: "the gateway refused the request",
    OUTCOME_UNREACHABLE: "the gateway did not answer",
}

# Outcomes that mean a build will produce nothing. Kept as a set rather than
# inferred so a new outcome has to be classified deliberately.
BROKEN_OUTCOMES = {OUTCOME_FIRST_TURN_ONLY, OUTCOME_PROSE, OUTCOME_EMPTY, OUTCOME_ERROR}


@dataclass
class Result:
    model: str
    outcome: str
    detail: str = ""
    http_status: int | None = None
    elapsed: float = 0.0
    turns: int = 0
    first_turn: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome == OUTCOME_TOOL

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "outcome": self.outcome,
            "meaning": OUTCOME_MEANING.get(self.outcome, "unknown outcome"),
            "detail": self.detail,
            "first_turn": self.first_turn,
            "turns_completed": self.turns,
            "http_status": self.http_status,
            "elapsed_seconds": round(self.elapsed, 2),
        }


@dataclass
class Report:
    base_url: str
    results: list[Result] = field(default_factory=list)

    @property
    def usable(self) -> list[Result]:
        return [r for r in self.results if r.usable]

    @property
    def verdict(self) -> str:
        if self.usable:
            return "ok"
        if self.results and all(r.outcome == OUTCOME_UNREACHABLE for r in self.results):
            return "unverified"
        return "no-usable-model"


class ConfigError(RuntimeError):
    """The check cannot run as configured; the message is the operator's fix."""


# --- configuration ------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """The `.env` subset the tooling reads: KEY=value, comments, optional quoting.

    Handles the same shapes as the runner's loader because a `.env` that one
    reader understands and another does not is a bug waiting for the wrong day.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        # An inline comment only starts a comment after whitespace, so a value
        # containing '#' (a vault:// reference's fragment) survives.
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def repo_env(repo: Path, environ: dict[str, str]) -> dict[str, str]:
    """`.env` merged under the process environment — the environment wins.

    Same precedence as `build-runner.py` and `manufacture.sh`: an explicit export
    is a deliberate override, and this process is frequently run by hand.
    """
    from_file: dict[str, str] = {}
    for name in (".env", ".env.local"):
        path = repo / name
        if path.is_file():
            try:
                from_file.update(parse_env_file(path.read_text(encoding="utf-8")))
            except OSError:
                continue
    merged = {**from_file, **{k: v for k, v in environ.items() if k in ENV_KEYS and v}}
    return merged


def model_chain(env: dict[str, str], override: list[str] | None = None) -> list[str]:
    """The models a build would try, in order, deduplicated.

    `auto/coding` appears in both positions on a default install; trying the same
    model twice would report the same failure twice and read like two problems.
    """
    models = override if override else [env.get("OMNIROUTE_MODEL", ""), env.get("OMNIROUTE_MODEL_FALLBACK", "")]
    chain: list[str] = []
    for model in models:
        model = (model or "").strip()
        if model and model not in chain:
            chain.append(model)
    return chain


# --- reading a response -------------------------------------------------------


def find_tool_call(payload: object) -> dict | None:
    """The first `function_call` in a Responses payload, or None."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("output")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            return item
    return None


def describe_tool_call(item: dict) -> str:
    """A one-line rendering of what the model asked to run.

    `name` is optional on the wire: the gateway this stack uses omits it on some
    providers, so the arguments are the part worth showing.
    """
    name = str(item.get("name") or "").strip()
    arguments = str(item.get("arguments") or "").strip()
    if name and arguments:
        return f"{name}({arguments[:120]})"
    return (arguments or name or "tool call with no arguments")[:120]


def classify(http_status: int, payload: object) -> tuple[str, str]:
    """What a gateway response means for a build. Pure, so it can be tested.

    A 200 is not the check — the response body has to contain a tool call. This
    is separated from the request deliberately: the classification is the part
    that must be right, and it is the part worth asserting on.
    """
    if http_status >= 400:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")[:200]
            elif isinstance(error, str):
                detail = error[:200]
        return OUTCOME_ERROR, f"HTTP {http_status}" + (f" — {detail}" if detail else "")

    if not isinstance(payload, dict):
        return OUTCOME_ERROR, "the gateway answered with something that is not a JSON object"

    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        return OUTCOME_ERROR, f"the gateway reported an error: {str(message)[:200]}"

    items = payload.get("output")
    if not isinstance(items, list) or not items:
        return OUTCOME_EMPTY, "no output items at all"

    call = find_tool_call(payload)
    if call is not None:
        return OUTCOME_TOOL, describe_tool_call(call)

    # Answered, but never reached for a tool — the failure that looks like work.
    for item in items:
        if isinstance(item, dict) and item.get("type") == "message":
            text = " ".join(
                str(part.get("text") or "")
                for part in item.get("content") or []
                if isinstance(part, dict)
            ).strip()
            return OUTCOME_PROSE, text[:200] or "(empty message)"
    return OUTCOME_EMPTY, f"{len(items)} output item(s), none of them a tool call"


# --- the probe ----------------------------------------------------------------


class GatewayUnreachable(RuntimeError):
    """No HTTP answer at all — distinct from an HTTP answer that says no."""


def post(base_url: str, api_key: str, body: dict, timeout: int) -> tuple[int, object]:
    """One non-streaming request. A refusal is data; only no answer raises."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise GatewayUnreachable(str(getattr(error, "reason", error))) from None

    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")[:400]


def follow_up_input(call: dict) -> list[dict]:
    """Echo the model's own tool call back, with a result, so turn 2 is a real turn.

    The `call_id` is what the API correlates on; a provider that omits it still
    needs a stable one, and the probe's result is a constant either way.
    """
    call_id = str(call.get("call_id") or call.get("id") or "call_probe")
    return [
        {
            "type": "function_call",
            "call_id": call_id,
            "name": str(call.get("name") or PROBE_TOOL["name"]),
            "arguments": str(call.get("arguments") or "{}"),
        },
        {"type": "function_call_output", "call_id": call_id, "output": "ok"},
    ]


def probe(base_url: str, api_key: str, model: str, timeout: int = DEFAULT_TIMEOUT) -> Result:
    """Two turns against one model: does it call a tool, and can it be asked twice?"""
    started = time.monotonic()
    base = {"model": model, "stream": False, "tools": [PROBE_TOOL]}

    try:
        status, payload = post(base_url, api_key, {**base, "input": PROBE_INPUT}, timeout)
    except GatewayUnreachable as error:
        return Result(model=model, outcome=OUTCOME_UNREACHABLE, detail=str(error), elapsed=time.monotonic() - started)

    outcome, detail = classify(status, payload)
    if outcome != OUTCOME_TOOL:
        return Result(
            model=model,
            outcome=outcome,
            detail=detail,
            http_status=status,
            elapsed=time.monotonic() - started,
            turns=1,
        )

    call = find_tool_call(payload) or {}
    first = describe_tool_call(call)

    # The turn that matters. A model that calls a tool once and then cannot be
    # asked again is exactly the build that writes one file and stalls.
    try:
        status2, payload2 = post(
            base_url,
            api_key,
            {
                **base,
                "input": follow_up_input(call)
                + [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROBE_FOLLOW_UP}]}],
            },
            timeout,
        )
    except GatewayUnreachable as error:
        return Result(
            model=model,
            outcome=OUTCOME_UNREACHABLE,
            detail=f"the follow-up turn did not answer: {error}",
            http_status=status,
            elapsed=time.monotonic() - started,
            turns=1,
            first_turn=first,
        )

    outcome2, detail2 = classify(status2, payload2)
    if outcome2 == OUTCOME_TOOL:
        return Result(
            model=model,
            outcome=OUTCOME_TOOL,
            detail=f"turn 1 {first}; turn 2 {detail2}",
            http_status=status2,
            elapsed=time.monotonic() - started,
            turns=2,
            first_turn=first,
        )

    # The failure this script exists for. Named separately from a plain error so
    # the report says which turn broke.
    return Result(
        model=model,
        outcome=OUTCOME_FIRST_TURN_ONLY,
        detail=f"the follow-up turn failed: {detail2}",
        http_status=status2,
        elapsed=time.monotonic() - started,
        turns=1,
        first_turn=first,
    )


def run_chain(base_url: str, api_key: str, models: list[str], timeout: int) -> Report:
    """Probe every model, stopping at the first usable one.

    Stopping early is not just an optimisation: once something in the chain works
    the verdict is settled, and a later model's failure would only invite the
    reader to fix a model that does not need fixing.
    """
    report = Report(base_url=base_url)
    for model in models:
        result = probe(base_url, api_key, model, timeout=timeout)
        report.results.append(result)
        if result.usable:
            break
    return report


# --- reporting ----------------------------------------------------------------


def render(report: Report) -> str:
    lines = [f"gateway     {report.base_url}"]
    for result in report.results:
        mark = "ok  " if result.usable else "FAIL"
        line = f"  {mark} {result.model} [{result.outcome}] {result.elapsed:.1f}s, {result.turns} turn(s)"
        if result.detail:
            line += f"\n       {result.detail}"
        lines.append(line)

    if report.verdict == "ok":
        lines.append(f"verdict     usable — {report.usable[0].model} calls tools twice over")
    elif report.verdict == "unverified":
        lines.append("verdict     unverified — the gateway did not answer, so this says nothing about the models")
        lines.append("fix:        start the gateway (docker compose up -d omniroute) and re-run")
    else:
        lines.append("verdict     the configured chain cannot build — a build would exit 0 having written nothing")
        lines.append("fix:        check the provider's quota, not the model name — see docs/build-model.md")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the configured build model(s) call a tool, and can be asked twice.",
    )
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent.parent),
        help="repository root holding .env (default: this script's checkout)",
    )
    parser.add_argument("--env-file", default="", help="read configuration from this file instead of <repo>/.env")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="model to check instead of the configured chain (repeatable)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"per-request seconds (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    env = repo_env(repo, dict(os.environ))
    if args.env_file:
        env.update(parse_env_file(Path(args.env_file).read_text(encoding="utf-8")))

    models = model_chain(env, args.model or None)
    base_url = (env.get("OMNIROUTE_BASE_URL") or "").strip()
    api_key = (env.get("OMNIROUTE_API_KEY") or "").strip()

    # Configuration problems are reported as unverified (2), not as a broken
    # model (1): the difference is what the operator has to go and look at.
    missing: list[str] = []
    if not base_url:
        missing.append("OMNIROUTE_BASE_URL")
    if not api_key:
        missing.append("OMNIROUTE_API_KEY")
    if not models:
        missing.append("OMNIROUTE_MODEL / OMNIROUTE_MODEL_FALLBACK")
    if missing:
        message = f"cannot check: {', '.join(missing)} not set in {repo / '.env'} or the environment"
        if args.json:
            print(json.dumps({"verdict": "unconfigured", "error": message}, indent=2))
        else:
            print(f"gateway     (none)\nverdict     {message}", file=sys.stderr)
        return EXIT_UNVERIFIED

    report = run_chain(base_url, api_key, models, args.timeout)

    if args.json:
        print(
            json.dumps(
                {
                    "verdict": report.verdict,
                    "gateway": report.base_url,
                    "results": [r.as_dict() for r in report.results],
                },
                indent=2,
            )
        )
    else:
        print(render(report))
        if report.verdict != "ok":
            print("see         docs/build-model.md", file=sys.stderr)

    if report.verdict == "ok":
        return EXIT_OK
    if report.verdict == "unverified":
        return EXIT_UNVERIFIED
    return EXIT_NO_MODEL


if __name__ == "__main__":
    sys.exit(main())

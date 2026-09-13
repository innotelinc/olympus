# The build model

Why this page exists: the most expensive failure in this stack has no error
message. A build can run for ten minutes, exit **0**, and write nothing at all —
and the log does not say why, because nothing failed.

This is the page that failure sends you to.

## The symptom

You press **Build it** in Studio, or run `make app`, and:

* the build reports `succeeded` with `exit_code: 0`,
* `builds/<slug>/` is empty, or has a `MANIFEST.json` but no app,
* the last log lines are the agent saying something confident.

That last part is the trap: an agent that answered in prose instead of calling a
tool has done exactly what it was asked to do, from its own point of view. Codex
exits 0 even when its final request was refused, which is why `build-app.py`
asserts on the **artifact** — files on disk — and not on the exit code. The
manifest records `artifact.files` for the same reason.

So "succeeded, 0 files" is the signal, and three unrelated causes produce it:

| Cause | What the gateway answers |
| --- | --- |
| No capacity | `429`, `402`, `401` — the account has no quota or credits |
| Answers in prose | `200` with a message and no `function_call` |
| No such model | `401`/`404` naming a provider with no credentials |

None of these is a config error on your side, and none of them is visible from a
connection count.

## What was measured here (13 Sep 2026)

Ten provider connections were restored into the gateway that serves this stack
(`scripts/omniroute-restore-providers.py`). All ten are now listed as `active`.
**Nine of them cannot serve a build.**

| Connection | Answer to an actual request |
| --- | --- |
| `agentrouter` | `402 budget pool quota exhausted` |
| `openrouter` | `401 all connection(s) credits exhausted` |
| `openai` | `429 no credits remaining` |
| `gemini` | live — then `429 quota_exhausted`, model-only lockout `1800s`, `all credentials cooling down` for 15–30 min |
| free pool (`oc/*`) | answers, and *does* call tools — including on a follow-up turn |

Two conclusions worth keeping:

1. **Connections are not capacity.** Restoring a gateway's provider list repairs
   the *shape* of the deployment and can still leave it unable to build anything.
   Check the account, not the count.
2. **An earlier note in this repo was wrong**, and it is corrected here: the free
   `oc/big-pickle` was recorded as "never calls a tool". It calls one readily — with
   a one-line prompt, with a three-tool prompt, and on a second turn. Whatever made
   that build write nothing was the multi-turn path below, not the model. Treat
   model anecdotes in a discussion as hypotheses, and this page as the record.

## Why `auto/coding` makes the outcome luck

`auto/coding` is a gateway **combo**, not a model. It walks the whole catalogue to
find something that answers — measured at **1397 routing decisions** and 70 seconds
for a single request — and the gateway then *pins a native Codex turn* to whichever
member served the first turn. Two consequences:

* which brain finishes a build is not something you chose, and changes between
  builds of the same app;
* where the pinned member has no credentials, every turn after the first answers
  `503 No credentials for opencode` — a build that writes one file and stalls.

Naming a concrete model keeps the turn off the combo path entirely. That is why
`.env.example` pins one, with a second entry for when the first is out of quota.

## Check it

```bash
make build-model-check                 # the configured chain, in order
make build-model-check ARGS="--json"   # machine-readable
```

It probes each model in `OMNIROUTE_MODEL` → `OMNIROUTE_MODEL_FALLBACK` and asks
two questions, because the second one is where builds died here: does the model
call a tool, and can it be asked again. Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | a model in the chain calls a tool and survived a second turn |
| `1` | none did — a build would exit 0 and write nothing |
| `2` | the check could not tell (config missing, or the gateway did not answer) |

Alerts are wired to the same daily timer as the credential checks:

```bash
scripts/install-token-check-timer.sh TARGET=build-model
scripts/build-model-alert.sh --test-telegram
```

It repeats daily while the chain is broken, which is deliberate — a build model is
not something to be told about once.

**What a pass proves: "worth trying".** It is a necessary condition, not a
sufficient one: two requests against the gateway are not a nine-minute build, and
the ground truth remains `builds/<slug>` gaining files, which is what the runner's
`artifact` counts in `.factory/build-queue/*.status.json` record.

## Options, cheapest first

### A. One provider with real quota — the smallest durable fix

The gateway's credentials are deployment state, not code. They live in the data
dir the gateway was started with, **not in `.env`** — `.env` holds only the key
*Studio and the runner* use to reach the gateway. So adding capacity is a change to
the gateway, and the only thing that changes in this repo is which model `.env`
pins.

```bash
# 1. Get a key from the provider (any provider in `omniroute providers available`).
# 2. Add it to the gateway serving this stack. Read the key from stdin so it stays
#    out of your shell history and the process list:
read -rs PROVIDER_KEY
omniroute providers add <provider> \
  --base-url http://127.0.0.1:20128 \
  --api-key "$(grep '^OMNIROUTE_API_KEY=' .env | cut -d= -f2-)" \
  --name main --credential-stdin <<<"$PROVIDER_KEY"
unset PROVIDER_KEY

# 3. Confirm the gateway lists it (verified working against this deployment):
omniroute providers list --base-url http://127.0.0.1:20128 \
  --api-key "$(grep '^OMNIROUTE_API_KEY=' .env | cut -d= -f2-)"

# 4. Pin its model and check it end to end.
#    OMNIROUTE_MODEL=<provider>/<concrete-model>  in .env
make build-model-check
```

`providers add` is the supported path (the CLI's own `--help`; the REST equivalent
is `POST /api/providers` with a management token). A stock container's dashboard
password is `CHANGEME` — **change it** before exposing anything, since it also
unlocks this write path.

*Cost:* a paid account. *Fixes:* capacity, permanently. *Does not fix:* nothing on
this page — it is the only option here that makes builds reliable rather than
survivable.

### B. A free provider that is not a shared pool

Cheaper, same shape, same risk profile as the gemini accounts: quota exists, then
it cools down. Reasonable while you decide on A, and the chain plus the daily check
make it tolerable. It is not a fix; it is a schedule.

### C. A local or self-hosted model

No quota and no per-token cost. The constraint is the wire protocol, not the model:
Codex speaks the Responses API (`wire_api = "responses"`) and needs real tool
calling, so whatever you serve has to be compatible and has to emit
`function_call`s. An Ollama-style OpenAI-compatible endpoint behind the gateway is
the usual shape. *Cost:* hardware and latency. *Risk:* a model that is compatible
but poor at tools produces prose — the same silent failure, now on your own box.

### D. Keep the fallback and rely on the check

The current state, deliberately: pin the best model you have, fall back to the
combo, and let `make build-model-check` tell you the day the chain goes quiet
instead of the day an app comes out empty. Builds are unreliable but never
silently so. *Cost:* failed builds burn minutes. *Fixes:* the silence.

## Changing the model

`.env` is the only place the setting takes effect. Archon strips the repo's `.env`
keys out of a script node's environment and runs the workflow from a copy under
`artifacts/runs/<id>/workflow-source/`, so exporting `OMNIROUTE_MODEL` in a shell
changes nothing — measured: a deployment pinned to a gemini model asked the gateway
for `auto/coding` while `.env` said otherwise. `build-app.py` reads the checkout
`.env` for exactly this reason.

Check any candidate before trusting it:

```bash
make build-model-check ARGS="--model <provider>/<candidate>"
```

Known-bad on this deployment, so nobody re-measures them:

* `gemini/gemini-2.5-flash` — answers a hand-made tool request, then returns
  `400 Function calling config is set without function_declarations` under the tool
  payload Codex actually sends. A gateway-side translation gap.
* `oc/big-pickle` — *not* known-bad (see the correction above); it is simply the
  free pool's usual pick, with no quota guarantee behind it.

## Related

* `scripts/build-model-check.py` — the probe, with the reasoning in its docstring.
* `scripts/omniroute-restore-providers.py` — restoring a gateway that came back
  with no connections. Repairs the list; read the note about capacity above.
* `docs/stack.md` — how the pieces fit together.
* `.env.example` — the current pin, and the two measured ways a model "succeeds".

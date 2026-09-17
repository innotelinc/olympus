# The build model

> **17 Sep 2026 — the pool moved again, and the pin moved with it.** Every `oc/*`
> model now answers `403 OpenCode's free tier can only be used from within OpenCode`:
> a provider-policy change, so the model measured to build this stack in September
> cannot serve it at all. Re-probed the field, and the surviving chain is
> `gemini/gemini-3-flash-preview` → `auto/coding`. The gemini entry passes the
> two-turn probe and then `429`s Codex's real request while its free tier is
> burst-locked — in about seven seconds, not a hang — so the retry lands on the
> combo, which was the only thing here that wrote a file under Codex's own payload.
> The section below is left as it was measured, because how a model *stops* working
> is the part worth keeping. `.env.example` and the pin at the bottom carry the
> current state.

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

Those answers are what the providers say to a *simple* request. The next section is
what they do with the request Codex actually sends, which is a different question.

Two conclusions worth keeping:

1. **Connections are not capacity.** Restoring a gateway's provider list repairs
   the *shape* of the deployment and can still leave it unable to build anything.
   Check the account, not the count.
2. **An earlier note in this repo was wrong**, and it is corrected here: the free
   `oc/big-pickle` was recorded as "never calls a tool". It calls one readily — with
   a one-line prompt, with a three-tool prompt, and on a second turn. Whatever made
   that build write nothing was the multi-turn path below, not the model. Treat
   model anecdotes in a discussion as hypotheses, and this page as the record.

## What the real payload adds (13 Sep 2026)

`make build-model-check` asks the gateway a small question: call this one tool, then
call it again. That is necessary and not sufficient — **every model that passed the
probe and then failed did so on the first real turn**, so the probe alone will not
tell you a model can build. The check that settles it is the invocation
`build-app.py` composes, pointed at a one-file prompt:

```bash
codex exec -C "$tmp" --skip-git-repo-check --sandbox workspace-write -m "$model" \
  -c 'model_provider="omniroute"' \
  -c 'model_providers.omniroute.name="OmniRoute"' \
  -c 'model_providers.omniroute.base_url="http://127.0.0.1:20128/v1"' \
  -c 'model_providers.omniroute.wire_api="responses"' \
  -c 'model_providers.omniroute.requires_openai_auth=true' \
  "Create a file named index.html containing <h1>Hello</h1>. Use the shell tool."
```

What that ran, in one pass over every provider this deployment holds a credential
for — the failures are the interesting half, because each of them is a shape a
future reader would otherwise re-try:

| Model | Result |
| --- | --- |
| `oc/mimo-v2.5-free` | **wrote the file, ~9s** |
| `oc/big-pickle` | **wrote the file, ~9s** |
| `oc/nemotron-3-ultra-free` | **wrote the file, ~179s** |
| `oc/muse-spark-1.2` | `402` — needs a separate opencode API key |
| `oc/north-mini-code-free`, `oc/hy3-free` | `401 Model … is not supported` |
| `oc/deepseek-v4-flash-free` | `400 Model is unavailable` |
| `gemini/*` (4 models, incl. the previous pin) | `429` — model-only lockout `1800s`, per model |
| `nvidia/*` | `400` not in the active live catalog; when the combo pins one, `400 This model only supports single tool-calls at once!` |
| `alibaba/*` | `403` — account not eligible for the model |
| `ollama-cloud/*` | `401` credits exhausted, or `402` needs a subscription |
| `agentrouter/*` | `400 unknown variant 'custom'` — the gateway's tool shape, not the model |
| `openai/*` | `402` — the alias routes through an account that never purchased credits |
| `cfp/*` | `502` — needs Playwright, i.e. a browser session, not an API |
| `zc/*`, `aug/*` | `502 Stream ended before producing a non-ping SSE event` |
| `dva/*` | retries exhausted — provider busy |
| `tllm/*` | `403` — blocked by the CDN for this server's egress IP |
| `ddgw/*` | `418` — upstream anti-abuse challenge |
| `v0-vercel/*` | `404` |

Three things follow, and they are the reason this section exists:

1. **The free pool is the only thing here that builds.** Every credentialed provider
   on this host is out of credit, not eligible, or serving a tool-call shape Codex
   cannot use. This is the capacity finding above, restated as an experiment.
2. **The probe passing means "try it", not "trust it".** Four gemini models passed
   `make build-model-check` minutes before the real invocation answered `429` on all
   four. A probe you can run in ten seconds is a filter, not a verdict.
3. **A model that finishes one file is not a build.** So the candidate that survived
   was then asked for a two-file app from a spec, and finally the real thing: a
   queued build through the runner.

Measured outcome of that last step, with `.env` pinned to `oc/mimo-v2.5-free` and
`gemini/gemini-3-flash-preview` as the fallback:

```
succeeded e00bc0837b0928e4  Built builds/todo-list — 2 file(s), 4150 bytes, entry index.html
agent exit=0 attempt=1 model=oc/mimo-v2.5-free       # 145s queued -> finished
```

the fallback was never used, and the app met its spec: `index.html` + `app.js`, the
Add/Clear/Remove controls, an empty state, and every element the script looks up
present in the markup. Which is also the note to carry forward: a free model that
was previously written off here built the app on the first attempt — **the pin was
wrong, not the pool.**

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

The current state, deliberately: pin the model measured to finish a build, name a
concrete second entry for when the first goes quiet, and let
`make build-model-check` say the day the chain goes quiet instead of the day an app
comes out empty. Never the combo — a combo member is chosen by the gateway, which
is how a turn ends up on a provider that refuses Codex's tool calls.

*Current pin (17 Sep 2026, re-measured):* `OMNIROUTE_MODEL=gemini/gemini-3-flash-preview`,
`OMNIROUTE_MODEL_FALLBACK=auto/coding` — and `auto/coding` once more as
`build-app.py`'s own last attempt, so the chain a build actually walks is three deep.
The order is the same argument as before with the free pool removed: a concrete model
first, because a named model keeps the turn off the combo path; the combo after it,
because by the second attempt a coin toss beats stopping. Measured today, attempt one
is a seven-second `429` and attempt two writes the file. *Cost:* the free pool has no
quota guarantee. *Fixes:* the silence.

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

* **every `oc/*` and `opencode/*` model — `403 OpenCode's free tier can only be used
  from within OpenCode`** (measured 17 Sep 2026). This is the one entry on this page
  that invalidates an earlier one: `oc/mimo-v2.5-free` and `oc/big-pickle` were the
  models measured to build, and the provider now refuses the gateway outright. It is a
  policy refusal rather than a quota one, so it is not something waiting for a reset.
* `gemini/gemini-2.5-flash` — answers a hand-made tool request, then returns
  `400 Function calling config is set without function_declarations` under the tool
  payload Codex actually sends. A gateway-side translation gap.
* `gemini/gemini-2.5-pro` — `404 This model is no longer available to new users`.
* `gemini/gemini-3-flash-preview`, `gemini/gemini-3.1-flash-lite` — **not** known-bad,
  with a caveat that matters: both pass `make build-model-check` (two turns, a tool
  call each) and then answer `429` to the request Codex actually sends, because the
  payload is far larger than the probe's. Measured 17 Sep 2026: the probe returned in
  1.9s and 6.4s, the `codex exec` invocation `429`ed in 7s and 0s. The first is
  therefore still the pin — it is the better model and it is the entry that recovers
  when the tier's burst lock clears — but "the check passed" is not evidence a build
  will run.
* Everything in the sweep table above that is not `oc/`, with its reason. The
  refusals are provider-account facts, not model-quality judgements: re-check the
  account before concluding a model is bad.
* `oc/big-pickle` — *not* known-bad. It was recorded as "never calls a tool", then
  as "writes prose instead of files"; both were wrong. It calls tools and it built
  the two-file probe app. The lesson the sweep does support is narrower: a free
  model with no quota behind it is a schedule, not a guarantee.

## Related

* `scripts/build-model-check.py` — the probe, with the reasoning in its docstring.
* `scripts/omniroute-restore-providers.py` — restoring a gateway that came back
  with no connections. Repairs the list; read the note about capacity above.
* `docs/stack.md` — how the pieces fit together.
* `.env.example` — the current pin, and the two measured ways a model "succeeds".

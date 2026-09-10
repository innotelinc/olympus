# Olympus — Zero-Cost Coding Agent Platform

> **Local CPU, Free Cloud Brains.** 100% of heavy AI inference is outsourced to free cloud providers — your machine stays perfectly cool.

Olympus converges three open-source frameworks under one monorepo parent:

| Module | Path | Role |
|---|---|---|
| **OmniRoute** | `core-modules/omniroute` | Cloud routing gateway proxy — forwards all LLM calls to free endpoints |
| **Archon** | `core-modules/archon` | Deterministic YAML-based DAG workflow engine |
| **AI Software Factory** | `core-modules/ai-software-factory` | SDLC project scheduling & task consumer engine |

**Cost:** $0 — OpenRouter Free Tiers + DeepInfra Free evaluation endpoints.  
**Compute:** Local CPU only — bypasses local Ollama weights entirely.  
**Frontend Agent:** `Hermes 3 (70B)` via OpenRouter Free → Telegram Bot interface.  
**Backend Coding Brain:** `Qwen 2.5 Coder (32B/70B)` via OmniRoute Cloud → repository code modifications.

---

## Prerequisites

| Tool | Check | Install |
|---|---|---|
| `git` | `git --version` | https://git-scm.com/downloads |
| `bun` | `bun --version` | `curl -fsSL https://bun.sh/install \| bash` |
| `python3` (≥3.10) | `python3 --version` | https://www.python.org/downloads |
| `gh` (GitHub CLI) | `gh --version` | https://cli.github.com/ — then `gh auth login` |
| `npm` (ships with Node) | `npm --version` | https://nodejs.org/ |

> All five are validated automatically by `setup.sh`. Missing tools abort with an install hint.

You also need a **Telegram Bot Token** — create one free via [@BotFather](https://t.me/BotFather) → `/newbot`.

---

## Quickstart

### 1. Clone the repo

```bash
git clone https://github.com/<your-org>/olympus.git
cd olympus
```

### 2. Run the setup wizard

```bash
chmod +x setup.sh
./setup.sh
```

What it does:

1. Validates `git`, `bun`, `python3`, `gh`, `npm` are installed (with version floors).
2. Creates `core-modules/` and `.archon/cache/`.
3. Clones vendor tools if absent (skips if already present):
   - `core-modules/omniroute`
   - `core-modules/archon`
   - `core-modules/ai-software-factory`
4. Runs `bun install` inside each module that has a `package.json`.
5. Initializes the Software Factory setup wizard programmatically (`setup.py --non-interactive` / `scripts/setup.sh` / stub `factory/consumer.py` fallback) and installs any `requirements.txt`.
6. Securely prompts for your **Telegram Bot Token** (hidden input, `0600` perms) and writes it to `.factory-env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456:AAH...
   OPENROUTER_MODEL=nousresearch/hermes-3-llama-3-70b:free
   OMNIROUTE_BASE_URL=http://localhost:20128/v1
   QWEN_MODEL=qwen-2.5-coder-32b-instruct:free
   ARCHON_BINARY=./core-modules/archon/bin/archon
   ```

Re-running is idempotent — existing clones are `git pull --ff-only`'d and the token prompt offers to keep the current value. Non-interactive CI can inject `TELEGRAM_BOT_TOKEN=... ./setup.sh`.

### 3. Launch OmniRoute and enable Free Tier Mode

Open a dedicated terminal (OmniRoute stays running):

```bash
cd core-modules/omniroute
npm run start
```

Then open **http://localhost:20128** in your browser and toggle **Free Tier Mode → ON**.

This routes all inference through:

- **OpenRouter Free** (`nousresearch/hermes-3-llama-3-70b:free`) — Telegram chat
- **DeepInfra Free / OpenRouter Free** (`qwen-2.5-coder-32b-instruct:free`) — code edits

Leave this terminal running. Verify the gateway is reachable:

```bash
curl -s http://localhost:20128/v1/models | head
```

> Archon is configured in `.archon/config.yaml` to hit this gateway:
> ```yaml
> assistants:
>   claude:
>     claudeBinaryPath: "./core-modules/archon/bin/archon"
>     apiBaseUrl: "http://localhost:20128/v1"
>     defaultModel: "qwen-2.5-coder-32b-instruct:free"
> ```

### 4. Keep the system active — run the Factory consumer loop

Open a **second terminal** from the repo root:

```bash
source .factory-env
python3 factory/consumer.py loop
```

This is the SDLC scheduling loop: it polls the Archon DAG, pulls queued tasks, and dispatches code-modification jobs to **Qwen 2.5 Coder via OmniRoute** (`http://localhost:20128/v1`). Poll interval defaults to `5s` (`--interval 5`).

- Single poll (for debugging): `python3 factory/consumer.py once`
- Stop: `Ctrl+C`

Keep this process alive — e.g. `tmux`, `screen`, `nohup`, or a systemd unit.

### 5. Message the bot on Telegram — trigger autonomous edits at zero local CPU cost

1. Open Telegram → search for the bot username you created with `@BotFather`.
2. Send `/start` — **Hermes 3 (70B) via OpenRouter Free** replies (routed through OmniRoute, no local GPU/CPU inference).
3. Describe a codebase task in natural language, e.g.:
   - `Refactor factory/consumer.py to add retry with exponential backoff`
   - `Add unit tests for the Archon DAG parser and open a PR`
   - `Fix the failing bun install in core-modules/archon on Node 20`
4. The frontend (Hermes 3) parses intent → Archon creates a DAG run from YAML → Factory consumer picks it up → **Qwen 2.5 Coder (32B/70B) via OmniRoute Cloud** edits files locally and commits via `gh`.
5. Watch progress in the `factory/consumer.py loop` terminal and in Telegram — the bot streams status back through Hermes 3.

**Why zero local CPU cost:** Neither Hermes 3 nor Qwen ever load weights locally. All `chat/completions` calls go to `http://localhost:20128/v1` (OmniRoute), which proxies to OpenRouter/DeepInfra free endpoints. Your CPU only runs git, bun, Python, and the lightweight gateway.

---

## Project Structure

```
olympus/
├── .archon/
│   ├── config.yaml          # Telegram + Archon gateway config (see below)
│   └── cache/               # DAG run cache (gitignored)
├── .factory-env             # Telegram token + free-model env (0600, gitignored)
├── .gitignore
├── core-modules/
│   ├── omniroute/           # Cloud routing gateway proxy
│   ├── archon/              # YAML DAG workflow engine (binary at bin/archon)
│   └── ai-software-factory/ # SDLC scheduler & task consumer
├── factory/
│   └── consumer.py          # SDLC loop entrypoint (python3 factory/consumer.py loop)
├── setup.sh                 # One-shot reproducible setup wizard
└── README.md
```

### `.archon/config.yaml`

```yaml
platforms:
  telegram:
    enabled: true
    bot_token: "ENV_VAR"   # injected at runtime from $TELEGRAM_BOT_TOKEN (.factory-env)
    provider: "openrouter"
    model: "nousresearch/hermes-3-llama-3-70b:free"

assistants:
  claude:
    claudeBinaryPath: "./core-modules/archon/bin/archon"
    apiBaseUrl: "http://localhost:20128/v1"
    defaultModel: "qwen-2.5-coder-32b-instruct:free"
```

### `.gitignore`

```
node_modules/
.bun/
.uv/
.env
.factory-env
.archon/tokens.yaml
.archon/cache/
*.log
.DS_Store
```

---

## Configuration Notes

- **Free Tier Mode** must be ON at `http://localhost:20128` or upstream calls will attempt paid endpoints.
- Override upstream repos without editing the script:
  ```bash
  OMNIROUTE_REPO=https://github.com/<fork>/omniroute.git \
  ARCHON_REPO=https://github.com/<fork>/archon.git \
  FACTORY_REPO=https://github.com/<fork>/AI-Software-Factory.git \
  ./setup.sh
  ```
- `TELEGRAM_BOT_TOKEN` can be injected non-interactively: `TELEGRAM_BOT_TOKEN=123:ABC ./setup.sh`

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.sh: Missing required command: bun` | `curl -fsSL https://bun.sh/install \| bash && source ~/.bashrc` |
| `gh auth login` not authenticated | `gh auth login` then re-run `setup.sh` |
| `curl http://localhost:20128/v1/models` refused | Ensure `cd core-modules/omniroute && npm run start` is still running |
| Telegram bot not replying | `source .factory-env && echo $TELEGRAM_BOT_TOKEN` — verify token; check OmniRoute Free Tier Mode is ON; check `factory/consumer.py loop` is running |
| `Insufficient credits` / 402 from gateway | Free Tier Mode is OFF or free model string misspelled — confirm `qwen-2.5-coder-32b-instruct:free` and `nousresearch/hermes-3-llama-3-70b:free` in `.archon/config.yaml` and `.factory-env` |
| `factory/consumer.py: No such file` | Re-run `./setup.sh` — it copies/scaffolds the consumer into `factory/` |

---

## License

MIT — see `LICENSE` (if present). Vendor modules retain their upstream licenses under `core-modules/*/LICENSE`.

---

*Built for a cool CPU and a hot cloud — 100% free inference, 100% reproducible.*

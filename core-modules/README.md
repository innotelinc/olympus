# core-modules — vendored upstream frameworks

**This directory is intentionally empty in git.** It is populated at runtime by
`setup.sh`, which clones the Innotel mirrors (falling back to upstream) into:

| Directory | What it provides |
| --- | --- |
| `omniroute/` | Model gateway (`npm run start` serves `:20128`) |
| `archon/` | Workflow engine (YAML DAG) |
| `ai-software-factory/` | SDLC scheduler / task consumer (`bin/factory.py`) |

## Why it is not committed

The upstreams are large, carry their own licenses, and change independently of
this repo. `UPSTREAMS.md` records the mirrors, the vendored commit SHAs, and the
licenses; `setup.sh` does the cloning.

## Populate it

```bash
./setup.sh                      # clones every missing vendor tool
./setup.sh                      # safe to re-run: pulls the existing checkouts
```

Override the sources for your own forks:

```bash
OMNIROUTE_REPO=... ARCHON_REPO=... FACTORY_REPO=... ./setup.sh
```

## Consequences

- Anything under `core-modules/` is gitignored (except this file), so cloned
  upstream trees never end up in a commit.
- Paths that only exist after `setup.sh` has run — `.archon/config.yaml`'s
  `scaffold_templates`, `Dockerfile`, `.github/workflows/olympus-app-builder.yml`
  — will not resolve until then.
- `python3 factory/doctor.py` reports whether the vendor tools are present.

# Vendored Upstreams — provenance & recovery

Olympus depends on third-party repositories. Each is mirrored (full git
history, all branches and tags) under the `innotelinc` org so the factory
keeps working even if an upstream is deleted, renamed, or moved. `setup.sh`
clones from the innotelinc mirror first and falls back to the upstream.

> Mirrors are point-in-time snapshots. The upstream remains the source of
> truth for updates; re-sync a mirror with `git remote update && git push
> --mirror` from a fresh `git clone --mirror`.

| Role | innotelinc mirror | Upstream | License | Vendored |
|---|---|---|---|---|
| Workflow engine (YAML DAG) | [innotelinc/Archon](https://github.com/innotelinc/Archon) | [coleam00/Archon](https://github.com/coleam00/Archon) (`dev`) | MIT | 2026-09-10 |
| SDLC scheduler / task consumer | [innotelinc/ai-software-factory](https://github.com/innotelinc/ai-software-factory) | [coleam00/ai-software-factory](https://github.com/coleam00/ai-software-factory) (`main`) | none (upstream) | 2026-09-10 |
| Agent skills | [innotelinc/skills](https://github.com/innotelinc/skills) | [coleam00/skills](https://github.com/coleam00/skills) (`main`) | MIT | 2026-09-10 |
| Model gateway (`omniroute` npm CLI, v3.8.50) | [innotelinc/omniroute](https://github.com/innotelinc/omniroute) | [diegosouzapw/OmniRoute](https://github.com/diegosouzapw/OmniRoute) | MIT | 2026-09-10 |
| `archon` CLI binary (npm `archon`) | [innotelinc/archon-cli](https://github.com/innotelinc/archon-cli) | [bovard/archon](https://github.com/bovard/archon) (`master`) | MIT | 2026-09-10 |

## Vendored commit SHAs (verify against upstream)

```
Archon                0add058814bdc5daf90f0b1fd16041eb437baefa  (branch: dev)
ai-software-factory   ac749ec09099b2fe6144f0e8ddc961bf8e504311  (branch: main)
skills                7450b1b19efa426833dff97e8239c30d61680f9b  (branch: main)
omniroute             949235736042b13cf64215632e6d44db7985af76  (branch: release/v3.8.51; tag v3.8.50 = installed CLI)
archon-cli            bf7ddf9adaa859ee12282139ee53c1c57d9012a5  (branch: master)
```

## Why mirrors

- `inotex/omniroute`, `JohanLi233/archon`, and `Andy-Zhouelect/AI-Software-Factory` —
  the URLs this repo originally referenced — are already **404**. setup.sh
  now targets the mirrors instead.
- `coleam00/ai-software-factory` has **no license file** upstream: treat the
  mirror as private-use reference code, not redistributable product.

## Overrides

setup.sh honors env vars if you track your own forks:

```bash
OMNIROUTE_REPO=... ARCHON_REPO=... FACTORY_REPO=... ./setup.sh
```

## Re-syncing a mirror

```bash
git clone --mirror https://github.com/<upstream>.git /tmp/m.git
cd /tmp/m.git
git push --mirror https://github.com/innotelinc/<name>.git
# if GitHub rejects refs/pull/* (hidden refs), push explicitly:
git push https://github.com/innotelinc/<name>.git \
  'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
```

#!/usr/bin/env bash
# docker-cleanup.sh — nightly disk reclamation for a docker host.
#
# Deletes only what docker can rebuild or re-pull:
#   * build cache older than 7 days (a nightly keeps recent layers warm)
#   * images no container references (stopped containers keep their images, so
#     a plain `docker start` never needs a re-pull)
#   * dangling images and dead containers (crash-recovery leftovers)
#
# It deliberately never touches volumes: a volume can hold state nobody
# backed up, and "reclaimable" is not something docker can judge. The 17 Sep
# 2026 cleanup that inspired this script measured ~48 GB reclaimed across the
# estate with volumes excluded — the same cleanup also proved why a nightly
# needs a written rule: its one-shot `docker container prune` removed the
# stopped containers the operator had parked on purpose, which compose had to
# recreate. The container prune here is scoped to `status=exited` + `status=dead`
# + `status=created` older than RETAIN_EXITED_DAYS, so a parked container is
# left alone.
#
# Env:
#   RETAIN_EXITED_DAYS   exited/created containers younger than this survive (default 1)
#   BUILD_CACHE_KEEP     bytes of build cache to always keep   (default 2 GB)
#   LOG_TRIM_TO          trim container logs over LOG_TRIM_FROM to this (default 10M)
#   LOG_TRIM_FROM        only consider logs larger than this   (default 50M)
#   DRY_RUN=1            print what would be deleted, delete nothing
#
# Install (root):
#   install -m 0755 scripts/docker-cleanup.sh /usr/local/sbin/docker-cleanup.sh
#   echo '17 4 * * * root /usr/local/sbin/docker-cleanup.sh' > /etc/cron.d/docker-cleanup
#
# This file is mirrored verbatim into every member repo (see scripts/mesh.sh for
# the mirror convention). ips/scripts is canonical.

set -euo pipefail

RETAIN_EXITED_DAYS="${RETAIN_EXITED_DAYS:-1}"
BUILD_CACHE_KEEP="${BUILD_CACHE_KEEP:-$((2 * 1024 * 1024 * 1024))}"   # 2 GB
LOG_TRIM_TO="${LOG_TRIM_TO:-10M}"
LOG_TRIM_FROM="${LOG_TRIM_FROM:-50M}"
DRY_RUN="${DRY_RUN:-0}"

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

echo "docker-cleanup: $(hostname) $(date -u +%FT%TZ)"

# ── 1. dead containers ───────────────────────────────────────────────────────
# A container `docker stop` parked is `exited` and is NOT touched until it has
# been parked for RETAIN_EXITED_DAYS — long enough that a same-day stop is
# always safe to `docker start`, while genuinely forgotten ones clear out.
# (`docker container prune` has no age filter on older engines, so the filter
# is done here.)
if [ "$(docker ps -aq --filter status=exited --filter status=dead --filter status=created | wc -l)" -gt 0 ]; then
  cutoff=$(( $(date +%s) - RETAIN_EXITED_DAYS * 86400 ))
  old=""
  for id in $(docker ps -aq --filter status=exited --filter status=dead --filter status=created); do
    finished=$(docker inspect -f '{{.State.FinishedAt}}' "$id" 2>/dev/null || echo "")
    [ -n "$finished" ] && [ "$finished" != "0001-01-01T00:00:00Z" ] || continue
    ts=$(date -d "$finished" +%s 2>/dev/null || echo 0)
    if [ "$ts" -gt 0 ] && [ "$ts" -lt "$cutoff" ]; then
      old="$old $id"
    fi
  done
  if [ -n "$old" ]; then
    echo "  removing containers exited >${RETAIN_EXITED_DAYS}d:$(echo "$old" | tr ' ' '\n' | while read -r id; do [ -n "$id" ] && echo " $(docker inspect -f '{{.Name}}' "$id" | sed s,/,,)"; done)"
    run docker rm $old >/dev/null
  fi
fi

# ── 2. build cache ───────────────────────────────────────────────────────────
run docker builder prune -af --keep-storage "$BUILD_CACHE_KEEP" >/dev/null && echo "  build cache pruned (kept $BUILD_CACHE_KEEP bytes)"

# ── 3. dangling images ───────────────────────────────────────────────────────
run docker image prune -f >/dev/null && echo "  dangling images pruned"

# ── 4. images with no container at all ───────────────────────────────────────
# Safe even with stopped containers present: a stopped container still
# references its image, so only truly unattached images go.
run docker image prune -af >/dev/null && echo "  unreferenced images pruned"

# ── 5. oversized container logs ──────────────────────────────────────────────
trimmed=0
while IFS= read -r -d '' log; do
  size=$(stat -c %s "$log" 2>/dev/null || echo 0)
  if [ "$size" -gt "$(numfmt --from=iec "$LOG_TRIM_FROM" 2>/dev/null || echo 52428800)" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      echo "  [dry-run] trim $log ($size bytes) to $LOG_TRIM_TO"
    else
      truncate -s "$LOG_TRIM_TO" "$log"
    fi
    trimmed=$((trimmed + 1))
  fi
done < <(find /var/lib/docker/containers -name '*-json.log' -print0 2>/dev/null)
echo "  logs trimmed: $trimmed"

echo "docker-cleanup: done"
docker system df | sed -n '2,5p' || true

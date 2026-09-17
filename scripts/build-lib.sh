#!/usr/bin/env bash
# build-lib.sh — the shared build functions for Olympus app images.
#
# ONE PLACE AN IMAGE IS BUILT. `package-project.py`, `package-app.py` and
# `app-runtime.py` all end in the same act — turn a directory into a tagged
# image — and each of them used to spell it out for itself as
# `docker build --tag <tag> .`. Three copies of the command is how a host ends up
# with two of them on BuildKit and one on the deprecated legacy builder, and the
# difference shows up as a build that behaves correctly when you run it by hand
# and not when the runner does.
#
# WHY BUILDX, AND WHY IT IS NOT OPTIONAL IN PRACTICE. Docker 29 still carries the
# legacy builder, but it announces itself on every build:
#
#   DEPRECATED: The legacy builder is deprecated and will be removed in a future
#               release. Install the buildx component to build images with BuildKit
#
# The generated Dockerfiles already ask for the BuildKit frontend
# (`# syntax=docker/dockerfile:1`, written by `package-project.py`), and with the
# legacy builder that directive is a comment — so the file says one thing and the
# builder does another. The frontend is also what makes `RUN --mount=type=cache`
# and a multi-stage `COPY --from` work as written.
#
# Both paths are kept, because a checkout on a host without the plugin should
# still package, and because a build that dies with "unknown command: buildx"
# would name the wrong thing. The fallback is loud: it says what it is, and
# `scripts/buildx --check` fails so the gap is visible without reading a log.
#
#   . "$(dirname "${BASH_SOURCE[0]}")/build-lib.sh"
#   build_lib_docker_build --tag olympus-app-foo:latest .
#
# Deliberately POSIX-ish bash with no external dependencies beyond docker itself.

# ── output helpers ──────────────────────────────────────────────────────────
# The same `==>` shapes scripts/manufacture.sh uses, so a job log reads the same
# whoever wrote the line.
build_lib_say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
build_lib_warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
build_lib_die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 2; }

# ── what the host has ───────────────────────────────────────────────────────

# The docker CLI, or exit 2. Every delivery ends in an image or a container, so a
# host without docker cannot do the job at all — and saying so here means the
# message names docker rather than surfacing as a bare "file not found".
build_lib_docker() {
  local docker
  docker="$(command -v docker || true)"
  if [ -z "$docker" ]; then
    build_lib_die "docker is not on PATH; an image cannot be built without it"
  fi
  printf '%s' "$docker"
}

# The buildx plugin's version, or empty when the plugin is not installed.
#
# `docker buildx version` is the only honest test: the plugin can be absent
# (docker < 19), present but disabled by an old daemon, or present and working,
# and a `[ -x /usr/libexec/docker/cli-plugins/docker-buildx ]` test confuses the
# middle case with the last one.
build_lib_buildx_version() {
  local docker
  docker="$(command -v docker || true)"
  [ -n "$docker" ] || return 1
  "$docker" buildx version 2>/dev/null | head -1
}

build_lib_have_buildx() {
  [ -n "$(build_lib_buildx_version)" ]
}

# The exact command that installs it on this host, said as a command rather than
# as advice. Read by the installers and by `--check`, so the two cannot disagree.
build_lib_buildx_install_hint() {
  if command -v apt-get >/dev/null 2>&1; then
    # TWO NAMES, AND THE RIGHT ONE MATTERS. Docker's own apt repository ships the
    # plugin as `docker-buildx-plugin`; Ubuntu's `docker.io` package ships the same
    # binary as `docker-buildx`. A host that is told the wrong name gets "Unable to
    # locate package", which reads as "this distribution has no buildx" rather than
    # as "that is the other name for it".
    if apt-cache show docker-buildx-plugin >/dev/null 2>&1; then
      printf 'apt-get install -y docker-buildx-plugin'
    elif apt-cache show docker-buildx >/dev/null 2>&1; then
      printf 'apt-get install -y docker-buildx'
    else
      printf 'apt-get install -y docker-buildx-plugin   # or docker-buildx, on Ubuntu'
    fi
  elif command -v dnf >/dev/null 2>&1; then
    printf 'dnf install -y docker-buildx-plugin'
  elif command -v pacman >/dev/null 2>&1; then
    printf 'pacman -S --noconfirm docker-buildx'
  else
    printf 'install the docker buildx CLI plugin (https://docs.docker.com/go/buildx/)'
  fi
}

# ── the build ───────────────────────────────────────────────────────────────

# build_lib_docker_build [docker build args...]
#
# Runs the build the way this host can best run it and returns the builder's own
# exit code, untouched: the callers (`package-*.py`) decide what a non-zero code
# means, and a wrapper that swallowed it would turn a failed build into a
# successful package.
#
# `BUILDX_NAME` selects a named builder when one is wanted (a buildx builder is
# where a persistent cache volume lives). Unset — the default here — means the
# daemon's own builder, which is the `docker` driver: BuildKit, image loaded into
# the daemon's store, and no `--load` needed, because that is exactly what the
# legacy `docker build` produced. Olympus builds for the host it runs on, so a
# separate builder buys nothing and costs an image store nothing can `docker run`.
build_lib_docker_build() {
  local docker
  docker="$(build_lib_docker)"

  if build_lib_have_buildx; then
    local argv=("$docker" buildx build)
    if [ -n "${BUILDX_NAME:-}" ]; then
      if ! "$docker" buildx inspect "$BUILDX_NAME" >/dev/null 2>&1; then
        "$docker" buildx create --name "$BUILDX_NAME" >/dev/null 2>&1 \
          || build_lib_warn "could not create buildx builder $BUILDX_NAME; using the daemon's own"
      fi
      argv+=(--builder "$BUILDX_NAME")
    fi
    # A runner captures this into a job log, where the interleaved live progress
    # block is unreadable and useless to whoever scrolls it later.
    argv+=(--progress plain)
    build_lib_say "docker buildx: $(build_lib_buildx_version)${BUILDX_NAME:+ (builder $BUILDX_NAME)}"
    "${argv[@]}" "$@"
    return $?
  fi

  build_lib_warn "docker buildx is missing — falling back to the deprecated legacy builder,"
  build_lib_warn "  which ignores the '# syntax=docker/dockerfile:1' the packagers write."
  build_lib_warn "  Fix with: $(build_lib_buildx_install_hint)"
  "$docker" build "$@"
}

# ── self-test (scripts/build-lib.sh --selftest) ─────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
  echo "docker=$(command -v docker || echo '(missing)')"
  echo "buildx=$(build_lib_buildx_version || true)"
  echo "hint=$(build_lib_buildx_install_hint)"
fi

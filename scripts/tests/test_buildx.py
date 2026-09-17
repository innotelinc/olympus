#!/usr/bin/env python3
"""Tests for scripts/buildx and scripts/build-lib.sh.

WHAT THIS PINS DOWN. A build host with the buildx plugin and one without take
different paths, and only one of them is the supported answer:

  * with buildx    the image is built by BuildKit, so the `# syntax=dockerfile:1`
                   every generated Dockerfile opens with means what it says
  * without it     the build still happens, on the deprecated legacy builder, and
                   says so instead of pretending — `--check` then fails

Both branches are observed by putting a fake `docker` on PATH that answers
`buildx version` one way or the other and records the argv it was given. That is
the whole interface: nothing here mocks a Python function to test a shell script.

Also asserted: the three callers that build an image (`package-project.py`,
`package-app.py`, `app-runtime.py`) go through the wrapper rather than spelling
out `docker build` for themselves, which is the property that makes the choice
above real. Where a caller cannot be driven end to end without a real daemon, the
argv it would run is what is checked.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
BUILDX = SCRIPTS / "buildx"
BUILD_LIB = SCRIPTS / "build-lib.sh"
APP_RUNTIME = SCRIPTS / "app-runtime.py"
PACKAGE_APP = SCRIPTS / "package-app.py"
PACKAGE_PROJECT = SCRIPTS / "package-project.py"

# The fake daemon. It answers the one probe that decides the branch, appends every
# other invocation to a log, and exits with whatever the test asked for — so a
# failed build can be shown to stay failed rather than being reported as packaged.
FAKE_DOCKER = """#!/bin/sh
if [ "$1" = "buildx" ] && [ "$2" = "version" ]; then
    if [ "${FAKE_HAS_BUILDX:-0}" = "1" ]; then
        echo "github.com/docker/buildx v9.9.9 fake"
        exit 0
    fi
    exit 1
fi
printf '%s\\n' "$*" >> "${FAKE_LOG}"
exit "${FAKE_EXIT:-0}"
"""


def load(name: str, path: Path):
    """Import a hyphenated script the way its own test files do."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BuilderChoiceTest(unittest.TestCase):
    """Which builder an image goes through, and what it says when it cannot."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        self.log.write_text("")
        shim = self.bin / "docker"
        shim.write_text(FAKE_DOCKER)
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def run_buildx(self, *args: str, has_buildx: bool, exit_code: int = 0):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_LOG"] = str(self.log)
        env["FAKE_HAS_BUILDX"] = "1" if has_buildx else "0"
        env["FAKE_EXIT"] = str(exit_code)
        return subprocess.run(
            [str(BUILDX), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.strip()]

    def test_buildx_builds_with_buildkit_when_the_plugin_is_there(self):
        result = self.run_buildx("--tag", "olympus-app-todo:latest", ".", has_buildx=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.calls(),
            ["buildx build --progress plain --tag olympus-app-todo:latest ."],
        )
        # The log line says which engine built it, so a job log answers the question
        # later without anyone having to reproduce the build.
        self.assertIn("docker buildx", result.stdout)
        self.assertNotIn("legacy", result.stderr)

    def test_the_build_args_are_passed_through_untouched(self):
        self.run_buildx(
            "--tag",
            "olympus-app-todo:latest",
            "--build-arg",
            "CONVEX_DEPLOY_KEY=secret",
            ".",
            has_buildx=True,
        )

        self.assertEqual(
            self.calls(),
            [
                "buildx build --progress plain --tag olympus-app-todo:latest"
                " --build-arg CONVEX_DEPLOY_KEY=secret ."
            ],
        )

    def test_without_the_plugin_it_falls_back_and_says_what_that_costs(self):
        result = self.run_buildx("--tag", "olympus-app-todo:latest", ".", has_buildx=False)

        self.assertEqual(result.returncode, 0)
        # No `buildx` in the argv — the legacy builder would reject it.
        self.assertEqual(self.calls(), ["build --tag olympus-app-todo:latest ."])
        self.assertIn("legacy builder", result.stderr)
        self.assertRegex(result.stderr, r"docker-buildx(-plugin)?")

    def test_a_failed_build_stays_failed(self):
        result = self.run_buildx("--tag", "olympus-app-todo:latest", ".", has_buildx=True, exit_code=7)

        # The caller decides what a non-zero code means; a wrapper that swallowed it
        # would turn a failed build into a packaged one.
        self.assertEqual(result.returncode, 7)

    def test_check_fails_when_the_plugin_is_missing(self):
        result = self.run_buildx("--check", has_buildx=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("MISSING", result.stdout)
        # Either name, because which one this distribution uses is a property of the
        # host: Docker's apt repository ships `docker-buildx-plugin` and Ubuntu's own
        # `docker.io` ships the same binary as `docker-buildx`.
        self.assertRegex(result.stdout, r"apt-get install -y docker-buildx(-plugin)?")

    def test_check_passes_with_the_plugin(self):
        result = self.run_buildx("--check", has_buildx=True)

        self.assertEqual(result.returncode, 0)
        self.assertIn("v9.9.9", result.stdout)

    def test_no_docker_at_all_is_a_refusal_that_names_docker(self):
        # `build_lib_docker` is asked directly rather than through `scripts/buildx`:
        # an empty PATH is the condition under test, and the wrapper needs `dirname`
        # and `head` before it ever reaches this — which an empty PATH cannot supply,
        # so the wrapper would fail with 127 and prove something else entirely.
        bash = shutil.which("bash")
        empty = self.root / "empty"
        empty.mkdir()
        env = dict(os.environ)
        env["PATH"] = str(empty)
        result = subprocess.run(
            [bash, "-c", '. "$1"; build_lib_docker', "_", str(BUILD_LIB)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("docker is not on PATH", result.stderr)


class CallersUseTheWrapperTest(unittest.TestCase):
    """Every script that builds an image asks `scripts/buildx` to do it."""

    def capture_argv(self, module, function, *args, **kwargs):
        """Run `function` with subprocess.call replaced, and return the argv."""
        seen: list[list[str]] = []
        original = module.subprocess.call

        def record(command, *rest, **extra):
            seen.append(list(command))
            return 0

        module.subprocess.call = record
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(function(*args, **kwargs), 0)
        finally:
            module.subprocess.call = original

        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_package_project_builds_through_the_wrapper(self):
        module = load("package_project_under_test", PACKAGE_PROJECT)

        argv = self.capture_argv(
            module,
            module.build_image,
            Path("/tmp/nowhere"),
            "todo",
            {"target": "static"},
        )

        self.assertEqual(argv[0], str(BUILDX))
        self.assertEqual(argv[1:], ["--tag", "olympus-app-todo:latest", "."])

    def test_package_app_builds_through_the_wrapper(self):
        module = load("package_app_under_test", PACKAGE_APP)

        argv = self.capture_argv(module, module.build_image, Path("/tmp/nowhere"), "todo")

        self.assertEqual(argv[0], str(BUILDX))
        self.assertEqual(argv[1:], ["--tag", "olympus-app-todo:latest", "."])


class DataRootTest(unittest.TestCase):
    """A data root the build account cannot write names itself and the fix."""

    def test_an_unusable_data_root_is_reported_as_the_path_and_the_owner(self):
        module = load("app_runtime_under_test", APP_RUNTIME)

        with tempfile.TemporaryDirectory() as tmp:
            # A *file* where the tree should be: an OSError the guard has to catch,
            # and one that behaves the same whether or not this test runs as root
            # (a mode test would pass for root and prove nothing).
            blocker = Path(tmp) / "apps-root"
            blocker.write_text("not a directory\n")

            runtime = module.Runtime(blocker, {})
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                module.ensure_data_dir(runtime, "todo")

        self.assertEqual(caught.exception.code, 2)
        message = stderr.getvalue()
        self.assertIn("data", message)
        self.assertIn("install-build-runner.sh", message)


if __name__ == "__main__":
    unittest.main()

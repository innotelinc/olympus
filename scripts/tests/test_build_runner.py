#!/usr/bin/env python3
"""Tests for scripts/build-runner.py.

The runner is the process that turns a file written by a browser-facing service
into a command run as root, so the interesting tests are the refusals: what
happens when a request names a path outside `build-requests/`, or a slug that is
really a path, or a symlink. Those are asserted here rather than discovered.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

RUNNER_PATH = Path(__file__).resolve().parent.parent / "build-runner.py"

spec = importlib.util.spec_from_file_location("build_runner", RUNNER_PATH)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RepoFixture(unittest.TestCase):
    """A throwaway checkout: build-requests/ and builds/ are all the runner reads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "build-requests").mkdir()
        (self.repo / "builds").mkdir()
        (self.repo / "scripts").mkdir()
        (self.repo / "build-requests" / "todo.md").write_text("# App\n", encoding="utf-8")
        self.queue = self.repo / ".factory" / "build-queue"
        self.queue.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def request(self, **overrides: object) -> dict:
        payload = {
            "v": 1,
            "job": "0123456789abcdef",
            "spec": "build-requests/todo.md",
            "slug": "todo",
            "title": "Todo",
            "requested_by": "user:test",
            "requested_at": "2026-09-13T00:00:00+00:00",
            "replace": False,
        }
        payload.update(overrides)
        return payload


class TestEnvParsing(unittest.TestCase):
    def test_parses_values_comments_and_quotes(self) -> None:
        parsed = runner.parse_env_file(
            "\n".join(
                [
                    "# a comment",
                    "OMNIROUTE_MODEL=auto/coding",
                    "QUOTED=\"a b\"",
                    "SINGLE='c d'",
                    "TRAILING=value   # trailing note",
                    "EMPTY=",
                    "not a line",
                    "1INVALID=x",
                ]
            )
        )

        self.assertEqual(parsed["OMNIROUTE_MODEL"], "auto/coding")
        self.assertEqual(parsed["QUOTED"], "a b")
        self.assertEqual(parsed["SINGLE"], "c d")
        self.assertEqual(parsed["TRAILING"], "value")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("1INVALID", parsed)


class TestEnvAllowList(unittest.TestCase):
    """A `.env` holds the Vault token and the Authentik token. A build gets neither."""

    DOTENV = {
        "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128/v1",
        "OMNIROUTE_MODEL": "auto/coding",
        "ARCHON_BINARY": "/usr/local/bin/archon",
        "AUTHENTIK_TOKEN": "super-secret",
        "CERULEAN_ADMIN_PASSWORD": "super-secret",
        "VAULT_TOKEN": "super-secret",
        "TELEGRAM_BOT_TOKEN": "super-secret",
        "OIDC_CLIENT_SECRET": "super-secret",
        "STUDIO_SESSION_SECRET": "super-secret",
    }

    def test_only_allow_listed_keys_cross(self) -> None:
        env = runner.allowed_env(self.DOTENV, {"PATH": "/usr/bin"})

        self.assertEqual(env["OMNIROUTE_MODEL"], "auto/coding")
        self.assertEqual(env["ARCHON_BINARY"], "/usr/local/bin/archon")
        for secret in (
            "AUTHENTIK_TOKEN",
            "CERULEAN_ADMIN_PASSWORD",
            "VAULT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "OIDC_CLIENT_SECRET",
            "STUDIO_SESSION_SECRET",
        ):
            self.assertNotIn(secret, env, f"{secret} must not reach a build")

    def test_real_environment_beats_the_file(self) -> None:
        env = runner.allowed_env(self.DOTENV, {"OMNIROUTE_MODEL": "from-shell", "PATH": "/usr/bin"})
        self.assertEqual(env["OMNIROUTE_MODEL"], "from-shell")

    def test_a_secret_present_in_both_sources_is_still_excluded(self) -> None:
        # "Real env wins" used to run over every key in the file, so anything in
        # both crossed into the build. A secret is no safer for arriving from the
        # environment.
        env = runner.allowed_env(
            self.DOTENV,
            {"AUTHENTIK_TOKEN": "leaked", "VAULT_TOKEN": "leaked", "PATH": "/usr/bin"},
        )
        self.assertNotIn("AUTHENTIK_TOKEN", env)
        self.assertNotIn("VAULT_TOKEN", env)

    def test_uv_is_on_the_path(self) -> None:
        # The workflow's script nodes declare `runtime: uv`; systemd's default
        # PATH does not include it, and the failure reads as a workflow bug.
        env = runner.allowed_env({}, {"PATH": "/usr/bin"})
        self.assertIn("/root/.local/bin", env["PATH"].split(":"))

    def test_path_is_not_duplicated(self) -> None:
        env = runner.allowed_env({}, {"PATH": "/root/.local/bin:/usr/bin"})
        self.assertEqual(env["PATH"].count("/root/.local/bin"), 1)


class TestResolveSpec(RepoFixture):
    def test_accepts_a_spec_in_build_requests(self) -> None:
        target, relative = runner.resolve_spec(self.repo, "build-requests/todo.md")
        self.assertTrue(target.is_file())
        self.assertEqual(relative, "build-requests/todo.md")

    def test_refuses_absolute_paths(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.resolve_spec(self.repo, "/etc/passwd.md")

    def test_refuses_traversal(self) -> None:
        for attempt in (
            "build-requests/../../etc/passwd.md",
            "../build-requests/todo.md",
            "build-requests/sub/../../../todo.md",
        ):
            with self.assertRaises(runner.RequestError, msg=attempt):
                runner.resolve_spec(self.repo, attempt)

    def test_refuses_non_markdown(self) -> None:
        (self.repo / "build-requests" / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(runner.RequestError):
            runner.resolve_spec(self.repo, "build-requests/notes.txt")

    def test_refuses_nested_specs(self) -> None:
        (self.repo / "build-requests" / "sub").mkdir()
        (self.repo / "build-requests" / "sub" / "deep.md").write_text("x", encoding="utf-8")
        with self.assertRaises(runner.RequestError):
            runner.resolve_spec(self.repo, "build-requests/sub/deep.md")

    def test_refuses_a_symlink_that_leaves_the_directory(self) -> None:
        outside = self.repo / "outside.md"
        outside.write_text("# elsewhere\n", encoding="utf-8")
        link = self.repo / "build-requests" / "link.md"
        os.symlink(outside, link)

        with self.assertRaises(runner.RequestError):
            runner.resolve_spec(self.repo, "build-requests/link.md")

    def test_refuses_a_missing_spec(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.resolve_spec(self.repo, "build-requests/absent.md")

    def test_refuses_empty(self) -> None:
        for value in ("", "   ", None, 7):
            with self.assertRaises(runner.RequestError, msg=repr(value)):
                runner.resolve_spec(self.repo, value)


class TestResolveSlug(RepoFixture):
    def test_derives_from_the_spec_name(self) -> None:
        self.assertEqual(runner.resolve_slug({}, "build-requests/todo.md"), "todo")

    def test_accepts_a_matching_slug(self) -> None:
        self.assertEqual(runner.resolve_slug({"slug": "todo"}, "build-requests/todo.md"), "todo")

    def test_refuses_a_path_shaped_slug(self) -> None:
        for slug in ("../evil", "a/b", "UPPER", "with.dot", "-lead", "", "x" * 61):
            with self.assertRaises(runner.RequestError, msg=slug):
                runner.resolve_slug({"slug": slug}, "build-requests/todo.md")

    def test_refuses_a_slug_pointing_at_another_spec(self) -> None:
        # Otherwise one request could name another app's directory.
        with self.assertRaises(runner.RequestError):
            runner.resolve_slug({"slug": "other"}, "build-requests/todo.md")


class TestResolveBuildDir(RepoFixture):
    def test_stays_inside_builds(self) -> None:
        self.assertEqual(
            runner.resolve_build_dir(self.repo, "todo"),
            (self.repo / "builds" / "todo").resolve(),
        )

    def test_refuses_escape(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.resolve_build_dir(self.repo, "../outside")


class TestValidateRequest(RepoFixture):
    def test_accepts_a_well_formed_request(self) -> None:
        job = runner.validate_request(self.repo, self.request())
        self.assertEqual(job["slug"], "todo")
        self.assertEqual(job["spec"], "build-requests/todo.md")
        self.assertIs(job["replace"], False)

    def test_defaults_the_slug_from_the_spec(self) -> None:
        payload = self.request()
        payload.pop("slug")
        self.assertEqual(runner.validate_request(self.repo, payload)["slug"], "todo")

    def test_only_literal_true_enables_replace(self) -> None:
        for value in ("true", 1, "yes", None):
            payload = self.request(replace=value)
            self.assertIs(runner.validate_request(self.repo, payload)["replace"], False)

    def test_refuses_bad_version(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.validate_request(self.repo, self.request(v=99))

    def test_refuses_a_malformed_job_id(self) -> None:
        for job in ("nope", "../../etc", "ABCDEF0123456789", "0123456789abcde", 7):
            with self.assertRaises(runner.RequestError, msg=repr(job)):
                runner.validate_request(self.repo, self.request(job=job))

    def test_refuses_non_objects(self) -> None:
        for payload in ([], "x", 3, None):
            with self.assertRaises(runner.RequestError, msg=repr(payload)):
                runner.validate_request(self.repo, payload)

    def test_title_is_bounded_and_never_a_path(self) -> None:
        job = runner.validate_request(self.repo, self.request(title="y" * 500))
        self.assertEqual(len(job["title"]), 120)


class TestRunnerQueue(RepoFixture):
    def make_runner(self, **kwargs: object) -> "runner.Runner":
        return runner.Runner(self.repo, self.queue, **kwargs)

    def write_request(self, payload: dict) -> Path:
        path = self.queue / f"{payload['job']}{runner.REQUEST_SUFFIX}"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_claim_is_atomic_and_only_once(self) -> None:
        path = self.write_request(self.request())
        instance = self.make_runner()

        claimed = instance.claim(path)
        self.assertIsNotNone(claimed)
        self.assertFalse(path.exists())
        self.assertTrue(claimed and claimed.name.endswith(runner.RUNNING_SUFFIX))

        # A second runner scanning the same directory finds nothing to claim.
        self.assertIsNone(self.make_runner().claim(path))

    def test_requests_ignores_hidden_and_non_requests(self) -> None:
        self.write_request(self.request())
        (self.queue / ".hidden.request.json").write_text("{}", encoding="utf-8")
        (self.queue / "runner.heartbeat.json").write_text("{}", encoding="utf-8")
        (self.queue / "abc.status.json").write_text("{}", encoding="utf-8")

        names = [path.name for path in self.make_runner().requests()]
        self.assertEqual(names, ["0123456789abcdef.request.json"])

    def test_hidden_files_are_not_scanned(self) -> None:
        # A `..name.tmp` is a partial write; it must never be picked up as a job.
        (self.queue / "..0123456789abcdef.request.json.123.tmp").write_text("{}", encoding="utf-8")
        self.assertEqual(self.make_runner().requests(), [])

    def test_refusal_is_recorded_as_a_failed_status(self) -> None:
        payload = self.request(spec="../../etc/passwd.md")
        self.write_request(payload)
        instance = self.make_runner()

        instance.process(instance.requests()[0])

        status = json.loads((self.queue / "0123456789abcdef.status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("Refused", status["message"])
        # The request is consumed either way — a refused job must not be retried
        # in a loop forever.
        self.assertEqual(instance.requests(), [])
        self.assertEqual(list(self.queue.glob(f"*{runner.RUNNING_SUFFIX}")), [])

    def test_recover_fails_stale_running_jobs(self) -> None:
        stale = self.queue / f"0123456789abcdef{runner.RUNNING_SUFFIX}"
        stale.write_text(json.dumps(self.request()), encoding="utf-8")
        old = time.time() - 99999
        os.utime(stale, (old, old))

        self.make_runner(timeout_seconds=60).recover()

        status = json.loads((self.queue / "0123456789abcdef.status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertFalse(stale.exists())

    def test_recover_leaves_a_recent_running_job_alone(self) -> None:
        # A job inside its timeout may be genuine progress from a live runner.
        live = self.queue / f"0123456789abcdef{runner.RUNNING_SUFFIX}"
        live.write_text(json.dumps(self.request()), encoding="utf-8")

        self.make_runner(timeout_seconds=3600).recover()
        self.assertTrue(live.exists())
        self.assertFalse((self.queue / "0123456789abcdef.status.json").exists())


class TestStatusWriting(RepoFixture):
    def test_status_is_written_atomically_and_readable(self) -> None:
        instance = runner.Runner(self.repo, self.queue)
        instance.write_status("0123456789abcdef", state="running", message="ok")

        path = instance.status_path("0123456789abcdef")
        self.assertEqual(json.loads(path.read_text())["state"], "running")
        # Leave no partial write behind.
        self.assertEqual(list(self.queue.glob(".*tmp")), [])

    def test_log_tail_reads_the_end_of_a_long_log(self) -> None:
        instance = runner.Runner(self.repo, self.queue)
        path = instance.log_path("0123456789abcdef")
        path.write_text("x" * 10000 + "TAIL", encoding="utf-8")

        tail = runner.log_tail(path, limit=100)
        self.assertTrue(tail.endswith("TAIL"))
        self.assertLessEqual(len(tail), 100)

    def test_log_tail_of_a_missing_log_is_empty(self) -> None:
        self.assertEqual(runner.log_tail(self.queue / "absent.log"), "")

    def test_read_manifest_summarises_the_artifact(self) -> None:
        build = self.repo / "builds" / "todo"
        build.mkdir()
        (build / "MANIFEST.json").write_text(
            json.dumps({"artifact": {"dir": str(build), "files": 2, "bytes": 30, "entry": "index.html"}}),
            encoding="utf-8",
        )

        summary = runner.read_manifest(build)
        self.assertEqual(summary["files"], 2)
        self.assertEqual(summary["entry"], "index.html")

    def test_read_manifest_of_an_absent_build_is_none(self) -> None:
        self.assertIsNone(runner.read_manifest(self.repo / "builds" / "nothing"))


class TestSubmit(RepoFixture):
    def test_submit_writes_a_request_using_the_runner_validation(self) -> None:
        code, payload = runner.submit(self.queue, self.repo, "build-requests/todo.md", replace=True)
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["slug"], "todo")

        written = json.loads(
            (self.queue / f"{payload['job']}{runner.REQUEST_SUFFIX}").read_text()
        )
        self.assertEqual(written["spec"], "build-requests/todo.md")
        self.assertIs(written["replace"], True)

    def test_submit_refuses_what_the_runner_refuses(self) -> None:
        code, payload = runner.submit(self.queue, self.repo, "../../etc/passwd.md", replace=False)
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertEqual(list(self.queue.glob(f"*{runner.REQUEST_SUFFIX}")), [])


class TestProcess(RepoFixture):
    """`process()` end to end, with a fake manufacture script instead of a model run.

    This is the part that turns a queue file into a command, records status, and
    reads the artifact back — the honest way to test it is to run it.
    """

    def fake_manufacture(self, body: str) -> None:
        script = self.repo / "scripts" / "manufacture.sh"
        script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        script.chmod(0o755)

    def make_runner(self) -> "runner.Runner":
        return runner.Runner(self.repo, self.queue, poll_seconds=1)

    def enqueue(self, **overrides: object) -> "runner.Runner":
        payload = self.request(**overrides)
        (self.queue / f"{payload['job']}{runner.REQUEST_SUFFIX}").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        instance = self.make_runner()
        instance.process(instance.requests()[0])
        return instance

    def status(self) -> dict:
        return json.loads((self.queue / "0123456789abcdef.status.json").read_text())

    def test_a_successful_build_records_the_artifact(self) -> None:
        self.fake_manufacture(
            "set -euo pipefail\n"
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"dir":"builds/todo","files":1,"bytes":42,"entry":"index.html"}}\' '
            "> builds/todo/MANIFEST.json\n"
            'echo "manufacture: done"\n'
        )

        self.enqueue()
        status = self.status()

        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["slug"], "todo")
        self.assertEqual(status["artifact"]["files"], 1)
        self.assertEqual(status["artifact"]["entry"], "index.html")
        self.assertIsNotNone(status["started_at"])
        self.assertIsNotNone(status["finished_at"])
        self.assertIn("manufacture: done", status["log_tail"])

    def test_a_failing_build_is_recorded_as_failed(self) -> None:
        self.fake_manufacture('echo "boom" >&2\nexit 3\n')

        self.enqueue()
        status = self.status()

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 3)
        self.assertIn("boom", status["log_tail"])

    def test_exit_zero_without_an_artifact_is_a_failure(self) -> None:
        # The whole reason `verify` exists: an agent that exits 0 having written
        # nothing must never be reported as a build.
        self.fake_manufacture('echo "I did nothing"\nexit 0\n')

        self.enqueue()
        status = self.status()

        self.assertEqual(status["state"], "failed")
        self.assertIn("wrote no manifest", status["message"])

    def test_a_rebuild_without_replace_keeps_the_previous_app(self) -> None:
        existing = self.repo / "builds" / "todo"
        existing.mkdir()
        (existing / "keep.txt").write_text("previous", encoding="utf-8")
        self.fake_manufacture("echo 'refusing to overwrite' >&2\nexit 1\n")

        self.enqueue(replace=False)

        self.assertTrue((existing / "keep.txt").is_file())
        self.assertEqual(self.status()["state"], "failed")

    def test_replace_removes_the_previous_app_first(self) -> None:
        existing = self.repo / "builds" / "todo"
        existing.mkdir()
        (existing / "stale.txt").write_text("previous", encoding="utf-8")
        self.fake_manufacture(
            "set -euo pipefail\n"
            'test ! -f builds/todo/stale.txt || { echo "stale file survived"; exit 9; }\n'
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"files":1,"bytes":1,"entry":"index.html"}}\' > builds/todo/MANIFEST.json\n'
        )

        self.enqueue(replace=True)

        self.assertFalse((existing / "stale.txt").exists())
        self.assertEqual(self.status()["state"], "succeeded")

    def test_the_build_does_not_see_repo_secrets(self) -> None:
        (self.repo / ".env").write_text(
            "OMNIROUTE_MODEL=auto/coding\n"
            "AUTHENTIK_TOKEN=leak-me\n"
            "CERULEAN_ADMIN_PASSWORD=leak-me\n"
            "VAULT_TOKEN=leak-me\n",
            encoding="utf-8",
        )
        self.fake_manufacture(
            "set -euo pipefail\n"
            "env | sort > builds/env.txt\n"
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"files":1,"bytes":1,"entry":"index.html"}}\' > builds/todo/MANIFEST.json\n'
        )

        self.enqueue()

        seen = (self.repo / "builds" / "env.txt").read_text(encoding="utf-8")
        self.assertIn("OMNIROUTE_MODEL=auto/coding", seen)
        for secret in ("leak-me", "AUTHENTIK_TOKEN", "CERULEAN_ADMIN_PASSWORD", "VAULT_TOKEN"):
            self.assertNotIn(secret, seen, f"{secret} reached the build environment")

    def test_the_spec_is_passed_as_one_argument(self) -> None:
        # No shell, so a spec name can never become a second word — but assert it,
        # because this is the line where that would regress.
        self.fake_manufacture(
            "set -euo pipefail\n"
            'printf %s "$1" > builds/arg.txt\n'
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"files":1,"bytes":1,"entry":"index.html"}}\' > builds/todo/MANIFEST.json\n'
        )

        self.enqueue()

        self.assertEqual((self.repo / "builds" / "arg.txt").read_text(), "build-requests/todo.md")


if __name__ == "__main__":
    unittest.main()

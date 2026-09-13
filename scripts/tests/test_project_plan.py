#!/usr/bin/env python3
"""Tests for scripts/project_plan.py — the plan contract.

The rules here are shared by three readers that disagreeing would break in a way
nobody can see: the runner validates the plan that came back from a browser, the
packager and the runtime read the plan off disk, and the greenfield workflow plans a
spec before building it. So the cases are the refusals — what makes a plan not a
plan — and the two places the readers are allowed to differ.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_plan  # noqa: E402 - the path insert above is what makes this importable


def plan(**overrides) -> dict:
    base = {
        "name": "Weight Tracker",
        "kind": "app",
        "summary": "Daily weigh-ins over time.",
        "runtime": {"language": "python", "frameworks": ["flask"], "database": "sqlite"},
        "run": {
            "install": "pip install -r requirements.txt",
            "build": "",
            "start": "python app.py",
            "port": 8000,
            "healthcheck": "/healthz",
        },
        "files": [{"path": "app.py", "purpose": "the server"}],
    }
    base.update(overrides)
    return base


class Slugs(unittest.TestCase):
    def test_a_name_becomes_a_hostname_label(self) -> None:
        self.assertEqual(project_plan.slugify("Café & Bar / v2"), "cafe-bar-v2")
        self.assertEqual(project_plan.slugify("  Weight   Tracker  "), "weight-tracker")

    def test_a_name_that_normalises_to_nothing_falls_back(self) -> None:
        # An empty label is not a hostname, and this one becomes a DNS name.
        self.assertEqual(project_plan.slugify("***"), "app")
        self.assertEqual(project_plan.slugify("", fallback="website"), "website")

    def test_it_is_bounded_and_does_not_end_on_a_hyphen(self) -> None:
        slug = project_plan.slugify("x" * 39 + " y z")
        self.assertLessEqual(len(slug), 40)
        self.assertFalse(slug.endswith("-"))


class ExtractingTheJson(unittest.TestCase):
    def test_prose_around_the_object_is_tolerated(self) -> None:
        reply = 'Sure! Here is the plan:\n{"name": "Tracker", "run": {"start": "go run ."}}\nHope that helps.'
        self.assertEqual(
            json.loads(project_plan.extract_json_object(reply) or "{}")["name"],
            "Tracker",
        )

    def test_a_brace_inside_a_string_does_not_end_the_object(self) -> None:
        # A file purpose or a note containing "}" is a normal thing for a model to
        # write, and a regex would cut the plan in half there.
        reply = '{"notes": "if { this } then that", "run": {"start": "npm start"}}'
        extracted = project_plan.extract_json_object(reply)
        self.assertEqual(json.loads(extracted or "{}")["notes"], "if { this } then that")

    def test_no_object_at_all(self) -> None:
        self.assertIsNone(project_plan.extract_json_object("I could not plan this."))


class Normalising(unittest.TestCase):
    def test_reads_the_plan(self) -> None:
        parsed = project_plan.normalize_plan(plan())
        self.assertEqual(parsed["runtime"]["language"], "python")
        self.assertEqual(parsed["run"]["start"], "python app.py")
        self.assertEqual(parsed["run"]["port"], 8000)
        self.assertEqual(parsed["slug"], "weight-tracker")
        self.assertEqual(parsed["files"], [{"path": "app.py", "purpose": "the server"}])

    def test_refuses_what_is_not_an_object(self) -> None:
        for value in ("a plan", 42, [1, 2], None):
            with self.assertRaises(project_plan.PlanError):
                project_plan.normalize_plan(value)

    def test_refuses_a_plan_with_no_run_section(self) -> None:
        with self.assertRaises(project_plan.PlanError) as caught:
            project_plan.normalize_plan({"name": "x"})
        self.assertIn("run", str(caught.exception))

    def test_refuses_a_plan_that_would_start_nothing(self) -> None:
        with self.assertRaises(project_plan.PlanError) as caught:
            project_plan.normalize_plan(plan(run={"install": "npm ci", "start": "   "}))
        self.assertIn("start command", str(caught.exception))

    def test_refuses_a_command_that_is_not_a_string(self) -> None:
        with self.assertRaises(project_plan.PlanError):
            project_plan.normalize_plan(plan(run={"start": "npm start", "install": ["rm", "-rf", "/"]}))

    def test_refuses_a_command_over_the_limit_rather_than_cutting_it(self) -> None:
        # Truncating can leave a command that still runs and does something else — a
        # build that reports success over an output directory nothing wrote.
        with self.assertRaises(project_plan.PlanError) as caught:
            project_plan.normalize_plan(plan(run={"start": "npm start", "install": "x" * 501}))
        self.assertIn("longer than", str(caught.exception))

    def test_flattens_a_command_onto_one_line(self) -> None:
        # A newline in a Dockerfile RUN is a continuation: it joins the next
        # instruction onto this command.
        parsed = project_plan.normalize_plan(plan(run={"start": "npm   run   start\n  "}))
        self.assertEqual(parsed["run"]["start"], "npm run start")

    def test_a_port_that_is_not_a_number_is_absent_for_the_wire(self) -> None:
        # This plan has already been typed by the browser, so a string here means the
        # contract was broken, and absent is safer than treating "8000" as 8000.
        parsed = project_plan.normalize_plan(plan(run={"start": "npm start", "port": "8000"}))
        self.assertIsNone(parsed["run"]["port"])

    def test_a_port_the_planner_quoted_is_read_when_coercing(self) -> None:
        # A model writes "8000" often enough that reading it is right.
        parsed = project_plan.normalize_plan(
            plan(run={"start": "npm start", "port": "8000"}),
            default_port=project_plan.DEFAULT_PORT,
            coerce=True,
        )
        self.assertEqual(parsed["run"]["port"], 8000)

    def test_a_port_the_planner_omitted_gets_the_default(self) -> None:
        parsed = project_plan.normalize_plan(
            plan(run={"start": "npm start"}), default_port=project_plan.DEFAULT_PORT, coerce=True
        )
        self.assertEqual(parsed["run"]["port"], 3000)

    def test_ports_outside_the_range_are_refused(self) -> None:
        # Below 1024 needs root in the container, which the runtime does not grant;
        # above 49151 is the client side's ephemeral range.
        for value in (80, 22, 99999):
            parsed = project_plan.normalize_plan(plan(run={"start": "npm start", "port": value}))
            self.assertIsNone(parsed["run"]["port"], value)

    def test_a_healthcheck_that_is_not_a_path_falls_back(self) -> None:
        parsed = project_plan.normalize_plan(plan(run={"start": "npm start", "healthcheck": "http://x"}))
        self.assertEqual(parsed["run"]["healthcheck"], "/")

    def test_a_healthcheck_query_string_is_dropped(self) -> None:
        parsed = project_plan.normalize_plan(plan(run={"start": "npm start", "healthcheck": "/up?x=1"}))
        self.assertEqual(parsed["run"]["healthcheck"], "/up")

    def test_kind_defaults_to_app_and_is_read_from_the_payload(self) -> None:
        self.assertEqual(project_plan.normalize_plan(plan())["kind"], "app")
        self.assertEqual(project_plan.normalize_plan(plan(kind="website"))["kind"], "website")
        # The caller's choice wins: a model reply must not change what "app" means.
        self.assertEqual(project_plan.normalize_plan(plan(), "website")["kind"], "website")

    def test_a_planned_file_that_escapes_the_project_is_dropped(self) -> None:
        parsed = project_plan.normalize_plan(
            plan(files=[{"path": "./app.py", "purpose": "ok"}, {"path": "../etc/passwd", "purpose": "no"}])
        )
        self.assertEqual([entry["path"] for entry in parsed["files"]], ["app.py"])

    def test_a_repeated_file_is_listed_once(self) -> None:
        parsed = project_plan.normalize_plan(
            plan(files=[{"path": "a.py", "purpose": "one"}, {"path": "a.py", "purpose": "two"}])
        )
        self.assertEqual(len(parsed["files"]), 1)

    def test_the_file_list_is_capped(self) -> None:
        many = [{"path": f"f{index}.py", "purpose": "x"} for index in range(80)]
        self.assertEqual(len(project_plan.normalize_plan(plan(files=many))["files"]), 60)


class ReadingThePlanOnDisk(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def write(self, payload: object) -> None:
        (self.directory / "plan.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_absent_is_none(self) -> None:
        self.assertIsNone(project_plan.read_plan_file(self.directory))

    def test_a_valid_plan_is_read(self) -> None:
        self.write(plan())
        read = project_plan.read_plan_file(self.directory)
        assert read is not None
        self.assertEqual(read["run"]["start"], "python app.py")

    def test_a_corrupt_file_reads_as_absent(self) -> None:
        (self.directory / "plan.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(project_plan.read_plan_file(self.directory))

    def test_a_plan_that_would_not_run_reads_as_absent(self) -> None:
        # Read by one process, executed by another: half a plan is not a plan, and the
        # caller falls back to the project's own packager rather than running it.
        self.write({"name": "x", "run": {"install": "npm ci"}})
        self.assertIsNone(project_plan.read_plan_file(self.directory))

    def test_writing_it_puts_it_where_the_packager_reads_it(self) -> None:
        written = project_plan.write_plan_file(self.directory / "app", plan())
        self.assertEqual(written, self.directory / "app" / "plan.json")
        self.assertIsNotNone(project_plan.read_plan_file(self.directory / "app"))


class ControlFiles(unittest.TestCase):
    def test_the_workflows_own_records_are_named(self) -> None:
        self.assertTrue(project_plan.is_control_file("plan.json"))
        self.assertTrue(project_plan.is_control_file("./plan.json"))
        self.assertFalse(project_plan.is_control_file("app.py"))
        self.assertFalse(project_plan.is_control_file("src/plan.json"))


class ThePlannersPrompt(unittest.TestCase):
    def test_it_asks_for_the_six_languages_the_packager_can_build(self) -> None:
        prompt = project_plan.plan_system_prompt()
        for language in project_plan.LANGUAGES:
            self.assertIn(f'"{language}"', prompt)

    def test_it_says_what_the_plan_is_built_for(self) -> None:
        self.assertIn("FULL-STACK APPLICATION", project_plan.plan_system_prompt("app"))
        self.assertIn("WEBSITE", project_plan.plan_system_prompt("website"))

    def test_the_turn_carries_the_spec_and_the_title(self) -> None:
        messages = project_plan.plan_messages("# Spec\n\nA tracker.", "Weight Tracker")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Weight Tracker", messages[1]["content"])
        self.assertIn("A tracker.", messages[1]["content"])


class TheBuildersContract(unittest.TestCase):
    def test_it_states_the_commands_and_the_port(self) -> None:
        block = project_plan.plan_prompt_block(project_plan.normalize_plan(plan()))
        self.assertIn("python app.py", block)
        self.assertIn("pip install -r requirements.txt", block)
        self.assertIn("/healthz", block)
        self.assertIn("8000", block)

    def test_it_insists_the_server_binds_every_interface(self) -> None:
        # A server on 127.0.0.1 is unreachable from outside its container, and the
        # failure is invisible from inside it — so it is stated, not implied.
        block = project_plan.plan_prompt_block(project_plan.normalize_plan(plan()))
        self.assertIn("0.0.0.0", block)

    def test_it_lists_the_planned_files_with_their_purposes(self) -> None:
        block = project_plan.plan_prompt_block(project_plan.normalize_plan(plan()))
        self.assertIn("app.py", block)
        self.assertIn("the server", block)

    def test_it_puts_the_notes_in_front_of_the_builder(self) -> None:
        block = project_plan.plan_prompt_block(project_plan.normalize_plan(plan(notes="Uses SQLite only.")))
        self.assertIn("Uses SQLite only.", block)

    def test_a_website_with_no_install_says_so_rather_than_leaving_a_gap(self) -> None:
        block = project_plan.plan_prompt_block(
            project_plan.normalize_plan(
                plan(kind="website", runtime={"language": "static"}, run={"start": "nginx -g 'daemon off;'", "install": "", "build": ""}),
                "website",
            )
        )
        self.assertIn("(nothing to install)", block)
        self.assertIn("(nothing to compile)", block)


class AskingTheGateway(unittest.TestCase):
    def urlopen_returning(self, payload: object):
        body = json.dumps(payload).encode()

        class Response:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return lambda request, timeout=None: Response()

    def test_a_reply_becomes_a_plan(self) -> None:
        reply = {"choices": [{"message": {"content": json.dumps(plan())}}]}
        with mock.patch.object(project_plan.urllib.request, "urlopen", self.urlopen_returning(reply)):
            parsed = project_plan.request_plan("http://gw/v1", "k", "auto/coding", [])
        self.assertEqual(parsed["run"]["port"], 8000)

    def test_a_plan_wrapped_in_prose_is_still_read(self) -> None:
        reply = {"choices": [{"message": {"content": "Here you go:\n" + json.dumps(plan())}}]}
        with mock.patch.object(project_plan.urllib.request, "urlopen", self.urlopen_returning(reply)):
            parsed = project_plan.request_plan("http://gw/v1", "k", "auto/coding", [])
        self.assertEqual(parsed["name"], "Weight Tracker")

    def test_a_gateway_refusal_says_what_the_gateway_said(self) -> None:
        def boom(request, timeout=None):
            raise urllib.error.HTTPError(
                "http://gw/v1/chat/completions", 502, "Bad Gateway", {}, io.BytesIO(b"upstream sent too big header")
            )

        with mock.patch.object(project_plan.urllib.request, "urlopen", boom):
            with self.assertRaises(project_plan.PlanError) as caught:
                project_plan.request_plan("http://gw/v1", "k", "auto/coding", [])
        self.assertIn("502", str(caught.exception))
        self.assertIn("upstream sent too big header", str(caught.exception))

    def test_an_unreachable_gateway_says_where_it_looked(self) -> None:
        def unreachable(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(project_plan.urllib.request, "urlopen", unreachable):
            with self.assertRaises(project_plan.PlanError) as caught:
                project_plan.request_plan("http://127.0.0.1:20128/v1", "k", "auto/coding", [])
        self.assertIn("127.0.0.1:20128", str(caught.exception))

    def test_a_reply_with_no_message_is_a_refusal_not_a_crash(self) -> None:
        with mock.patch.object(project_plan.urllib.request, "urlopen", self.urlopen_returning({"choices": []})):
            with self.assertRaises(project_plan.PlanError):
                project_plan.request_plan("http://gw/v1", "k", "auto/coding", [])

    def test_a_reply_that_is_not_a_plan_is_a_refusal(self) -> None:
        reply = {"choices": [{"message": {"content": "I would build this in Rust."}}]}
        with mock.patch.object(project_plan.urllib.request, "urlopen", self.urlopen_returning(reply)):
            with self.assertRaises(project_plan.PlanError):
                project_plan.request_plan("http://gw/v1", "k", "auto/coding", [])


class PlanningASpec(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "build-requests").mkdir()
        (self.root / "build-requests" / "tracker.md").write_text(
            "# Application Specification: Weight Tracker\n\nA tracker.\n", encoding="utf-8"
        )

    def settings(self, **values) -> dict:
        return {"OMNIROUTE_BASE_URL": "http://gw/v1", "OMNIROUTE_API_KEY": "k", **values}

    def test_it_plans_the_spec_it_was_given(self) -> None:
        calls: list[str] = []

        def fake_request(base_url, api_key, model, messages, **kwargs):
            calls.append(model)
            self.assertIn("A tracker.", messages[1]["content"])
            return project_plan.normalize_plan(plan(), default_port=project_plan.DEFAULT_PORT, coerce=True)

        with mock.patch.object(project_plan, "gateway_settings", lambda root: self.settings(OMNIROUTE_MODEL="m1")):
            with mock.patch.object(project_plan, "request_plan", fake_request):
                parsed = project_plan.plan_for_spec(self.root / "build-requests" / "tracker.md")
        self.assertEqual(calls, ["m1"])
        self.assertEqual(parsed["run"]["start"], "python app.py")

    def test_it_tries_the_fallback_when_the_first_model_cannot_answer(self) -> None:
        tried: list[str] = []

        def fake_request(base_url, api_key, model, messages, **kwargs):
            tried.append(model)
            if model == "primary":
                raise project_plan.PlanError("rate limited")
            return project_plan.normalize_plan(plan(), default_port=project_plan.DEFAULT_PORT, coerce=True)

        settings = self.settings(OMNIROUTE_MODEL="primary", OMNIROUTE_MODEL_FALLBACK="fallback")
        with mock.patch.object(project_plan, "gateway_settings", lambda root: settings):
            with mock.patch.object(project_plan, "request_plan", fake_request):
                project_plan.plan_for_spec(self.root / "build-requests" / "tracker.md")
        self.assertEqual(tried, ["primary", "fallback"])

    def test_when_no_model_can_plan_it_raises_the_last_reason(self) -> None:
        def fake_request(*args, **kwargs):
            raise project_plan.PlanError("the gateway refused the planning turn (HTTP 503)")

        settings = self.settings(OMNIROUTE_MODEL="primary", OMNIROUTE_MODEL_FALLBACK="fallback")
        with mock.patch.object(project_plan, "gateway_settings", lambda root: settings):
            with mock.patch.object(project_plan, "request_plan", fake_request):
                with self.assertRaises(project_plan.PlanError) as caught:
                    project_plan.plan_for_spec(self.root / "build-requests" / "tracker.md")
        self.assertIn("503", str(caught.exception))

    def test_a_spec_that_is_not_there_is_refused_before_any_turn(self) -> None:
        with self.assertRaises(project_plan.PlanError) as caught:
            project_plan.plan_for_spec(self.root / "build-requests" / "missing.md")
        self.assertIn("not readable", str(caught.exception))

    def test_the_model_chain_is_the_builds_chain_deduplicated(self) -> None:
        self.assertEqual(
            project_plan.model_chain({"OMNIROUTE_MODEL": "a", "OMNIROUTE_MODEL_FALLBACK": "a"}),
            ["a", "auto/coding"],
        )
        self.assertEqual(
            project_plan.model_chain({"OMNIROUTE_MODEL": "a", "OMNIROUTE_MODEL_FALLBACK": "b"}),
            ["a", "b", "auto/coding"],
        )
        # Nothing configured still has to ask something.
        self.assertEqual(project_plan.model_chain({}), ["auto/coding"])

    def test_the_composed_route_is_behind_the_configured_models(self) -> None:
        # Planning's chain has to match the build's, or a spec plans with one model and
        # generates with another. Neither repeats the combo: the pinned models here run
        # on free tiers, and a plan that gives up when both are cooling down fails
        # before the far more expensive agent run has even started.
        for settings, expected in (
            ({"OMNIROUTE_MODEL": "a", "OMNIROUTE_MODEL_FALLBACK": "b"}, ["a", "b", "auto/coding"]),
            ({"OMNIROUTE_MODEL": "a"}, ["a", "auto/coding"]),
            ({}, ["auto/coding"]),
            # A deployment that pinned the combo *is* asking for it first; the point is
            # that it is never duplicated into a second identical attempt.
            ({"OMNIROUTE_MODEL": "auto/coding", "OMNIROUTE_MODEL_FALLBACK": "a"}, ["auto/coding", "a"]),
        ):
            with self.subTest(settings=settings):
                chain = project_plan.model_chain(settings)
                self.assertEqual(chain, expected)
                self.assertEqual(chain.count("auto/coding"), 1)


class FindingTheCheckout(unittest.TestCase):
    def test_it_walks_up_from_a_path_inside_it(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "scripts").mkdir()
        (root / "scripts" / "project_plan.py").write_text("# marker\n", encoding="utf-8")
        inside = root / "builds" / "some-app"
        inside.mkdir(parents=True)

        self.assertEqual(project_plan.find_repo_root(str(inside)), root)
        # A checkout is above wherever the paths are, so anywhere inside it resolves —
        # including a directory that does not exist yet, which is the normal case for
        # the app directory a plan is about to create.
        self.assertEqual(project_plan.find_repo_root(str(root / "builds" / "not-yet")), root)

    def test_a_path_outside_any_checkout_resolves_to_nothing(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        self.assertIsNone(project_plan.find_repo_root(str(Path(elsewhere.name) / "app")))
        self.assertIsNone(project_plan.find_repo_root(""))


class ReadingTheDeploymentsGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".env").write_text(
            "OMNIROUTE_BASE_URL=http://from-file:20128/v1\n"
            "OMNIROUTE_MODEL=file/model\n"
            "AUTHENTIK_TOKEN=not-a-model\n",
            encoding="utf-8",
        )

        # This is a deployed host: the shell that runs these tests has the stack's
        # own OMNIROUTE_* exported, and the environment outranks the file by design.
        # So every test here states the environment it means.
        ambient = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("OMNIROUTE_")}
        self.addCleanup(os.environ.update, ambient)

    def test_the_file_is_read_for_the_gateway_keys_only(self) -> None:
        values = project_plan.gateway_settings(self.root)
        self.assertEqual(values["OMNIROUTE_MODEL"], "file/model")
        self.assertNotIn("AUTHENTIK_TOKEN", values)

    def test_the_environment_wins_over_the_file(self) -> None:
        # Archon strips the repo's own .env keys out of a node's environment, so the
        # file is often the only copy — but the caller's value is never overridden.
        with mock.patch.dict("os.environ", {"OMNIROUTE_MODEL": "operator/choice"}):
            values = project_plan.gateway_settings(self.root)
        self.assertEqual(values["OMNIROUTE_MODEL"], "operator/choice")

    def test_no_checkout_is_not_a_crash(self) -> None:
        self.assertEqual(project_plan.gateway_settings(None), {})


if __name__ == "__main__":
    unittest.main()

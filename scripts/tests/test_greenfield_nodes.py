#!/usr/bin/env python3
"""Tests for the greenfield workflow's nodes and the plan they share.

The planning node writes `plan.json` into the app directory before the agent runs,
which puts it in front of two nodes that answer questions about that directory:

  * build-app decides whether to run the agent again by asking "is anything here?",
    and a plan that counted as something would end the retry before the first
    attempt had a chance to write anything;
  * verify-app decides what the app *is* by counting its files, and a plan counted
    as a file would report a directory of nothing as a directory of one file —
    which is exactly the failure that node exists to catch.

Both would pass a test written against a directory with nothing in it, so the cases
here are the ones with a plan present.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

NODES = (
    Path(__file__).resolve().parents[2]
    / ".archon"
    / "workflows"
    / "app"
    / "greenfield"
    / "scripts"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import project_plan  # noqa: E402 - the path insert above is what makes this importable

PLAN = {
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


def load_node(name: str):
    spec = importlib.util.spec_from_file_location(f"{name.replace('-', '_')}_under_test", NODES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NodeFixture(unittest.TestCase):
    """A temp app directory, and the INPUTS_* a node is handed."""

    watched = ("INPUTS_APP_DIR", "INPUTS_SPEC_PATH", "INPUTS_TITLE", "INPUTS_KIND")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app_dir = Path(self.tmp.name) / "builds" / "weight-tracker"
        self.app_dir.mkdir(parents=True)

        saved = {key: os.environ.pop(key, None) for key in self.watched}
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in self.watched])
        self.addCleanup(lambda: os.environ.update({k: v for k, v in saved.items() if v is not None}))
        os.environ["INPUTS_APP_DIR"] = str(self.app_dir)

    def write_plan(self, payload: object | None = None) -> None:
        (self.app_dir / "plan.json").write_text(
            json.dumps(PLAN if payload is None else payload, indent=2), encoding="utf-8"
        )

    def write(self, relative: str, body: str = "x") -> Path:
        path = self.app_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path


class TheLoaderAndStalePlan(unittest.TestCase):
    def test_only_workflow_plan_is_classified_as_reusable_state(self) -> None:
        node = load_node("load-spec")
        self.assertEqual(node.WORKFLOW_CONTROL_FILES, {"plan.json"})
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory)
            (app_dir / "plan.json").write_text("{}", encoding="utf-8")
            entries = list(app_dir.iterdir())
            project_entries = [entry for entry in entries if entry.name not in node.WORKFLOW_CONTROL_FILES]
            self.assertEqual(project_entries, [])

    def test_a_real_file_remains_protected(self) -> None:
        node = load_node("load-spec")
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory)
            (app_dir / "plan.json").write_text("{}", encoding="utf-8")
            (app_dir / "index.html").write_text("<html />", encoding="utf-8")
            entries = list(app_dir.iterdir())
            project_entries = [entry for entry in entries if entry.name not in node.WORKFLOW_CONTROL_FILES]
            self.assertEqual([entry.name for entry in project_entries], ["index.html"])


class TheBuilderAndThePlan(NodeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.node = load_node("build-app")

    def test_the_prompt_tells_the_agent_the_plan(self) -> None:
        self.write_plan()
        section = self.node.plan_section(self.app_dir)
        self.assertIn("python app.py", section)
        self.assertIn("8000", section)
        self.assertIn("app.py", section)

    def test_with_no_plan_it_says_so_rather_than_leaving_a_placeholder(self) -> None:
        section = self.node.plan_section(self.app_dir)
        self.assertIn("not decided", section)
        self.assertNotIn("{{PLAN}}", section)

    def test_a_plan_the_contract_refuses_is_treated_as_no_plan(self) -> None:
        # Half a plan is not a plan: it would put a start command in front of the
        # agent with no port and no language, which reads as a decision nobody made.
        self.write_plan({"name": "x", "run": {"install": "npm ci"}})
        self.assertIn("not decided", self.node.plan_section(self.app_dir))

    def test_the_template_has_the_placeholder_that_gets_filled_in(self) -> None:
        # `plan_section` is only reached through this, so a template that lost the
        # placeholder would silently ship a prompt with no plan in it.
        self.assertIn("{{PLAN}}", self.node.prompt_template())

    def test_a_plan_is_not_an_artifact_the_agent_wrote(self) -> None:
        # Otherwise the retry ends after attempt 1 — the directory is not empty, so
        # the node stops — and an app that was never written goes to verify.
        self.write_plan()
        self.assertFalse(self.node.has_artifact(self.app_dir))

    def test_a_real_file_is_an_artifact_and_a_plan_does_not_hide_it(self) -> None:
        self.write_plan()
        self.write("src/App.tsx", "export default function App() {}")
        self.assertTrue(self.node.has_artifact(self.app_dir))

    def test_an_empty_directory_is_not_an_artifact(self) -> None:
        self.assertFalse(self.node.has_artifact(self.app_dir))


class RetryingAgainstARateLimit(NodeFixture):
    """The retry has to outlast the thing it is retrying.

    Measured on this deployment: three attempts 20s apart all failed with `429 Too Many
    Requests`, and the same invocation four minutes later worked. The delay grows for
    that reason, and the failure says 429 when it is one.
    """

    def setUp(self) -> None:
        super().setUp()
        self.node = load_node("build-app")

    def test_the_first_attempt_does_not_wait(self) -> None:
        self.assertEqual(self.node.retry_delay(1), 0)

    def test_the_wait_grows_between_attempts(self) -> None:
        delays = [self.node.retry_delay(attempt) for attempt in (2, 3)]
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[1])

    def test_the_last_wait_is_minutes_not_seconds(self) -> None:
        # A rate-limit window is measured in minutes; a flat 20s retry spends a whole
        # agent run learning nothing.
        self.assertGreaterEqual(self.node.retry_delay(self.node.MAX_ATTEMPTS), 60)

    def test_a_429_is_recognised_in_what_the_agent_said(self) -> None:
        said = 'exceeded retry limit, last status: 429 Too Many Requests, request id: abc'
        self.assertTrue(self.node.looked_rate_limited(said))
        self.assertFalse(self.node.looked_rate_limited("compiled 3 files"))


class TheVerifierAndThePlan(NodeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.node = load_node("verify-app")

    def test_a_planned_but_unbuilt_directory_reports_no_files(self) -> None:
        self.write_plan()
        self.assertEqual(self.node.inventory(self.app_dir), [])

    def test_it_counts_the_apps_own_files(self) -> None:
        self.write_plan()
        self.write("index.html", "<html></html>")
        self.write("styles.css", "body{}")
        found = {name for name, _ in self.node.inventory(self.app_dir)}
        self.assertEqual(found, {"index.html", "styles.css"})

    def test_it_still_skips_installed_dependencies(self) -> None:
        self.write("node_modules/left-pad/index.js", "module.exports = 1")
        self.write("app.py", "print('hi')")
        found = {name for name, _ in self.node.inventory(self.app_dir)}
        self.assertEqual(found, {"app.py"})

    def test_the_plan_is_a_workflow_file_by_name(self) -> None:
        self.assertTrue(self.node.is_workflow_file(self.app_dir, self.app_dir / "plan.json"))
        self.write("app.py")
        self.assertFalse(self.node.is_workflow_file(self.app_dir, self.app_dir / "app.py"))


class ThePlanTheNodeWrites(NodeFixture):
    """`plan-app`'s two answers: a plan on disk, or a stop with a reason."""

    def setUp(self) -> None:
        super().setUp()
        self.node = load_node("plan-app")
        self.spec = Path(self.tmp.name) / "build-requests" / "weight-tracker.md"
        self.spec.parent.mkdir(parents=True, exist_ok=True)
        self.spec.write_text("# Application Specification: Weight Tracker\n\nA tracker.\n", encoding="utf-8")
        os.environ["INPUTS_SPEC_PATH"] = str(self.spec)
        os.environ["INPUTS_TITLE"] = "Weight Tracker"
        os.environ["INPUTS_KIND"] = "app"

    def test_it_refuses_to_run_without_a_spec(self) -> None:
        os.environ["INPUTS_SPEC_PATH"] = ""
        with self.assertRaises(SystemExit) as caught:
            self.node.main()
        self.assertEqual(caught.exception.code, 1)

    def test_it_refuses_an_unknown_kind(self) -> None:
        os.environ["INPUTS_KIND"] = "service"
        with self.assertRaises(SystemExit) as caught:
            self.node.main()
        self.assertEqual(caught.exception.code, 1)

    def test_a_planning_failure_stops_the_workflow_rather_than_building_blind(self) -> None:
        # The whole reason this node is a separate step: an agent run against a stack
        # nobody chose costs minutes and ends in an app that cannot be packaged.
        with mock.patch.object(project_plan, "plan_for_spec") as planning:
            planning.side_effect = project_plan.PlanError("the gateway refused the planning turn (HTTP 503)")
            with self.assertRaises(SystemExit) as caught:
                self.node.main()
        self.assertEqual(caught.exception.code, 1)

    def test_it_writes_the_plan_and_reports_what_the_workflow_asked_for(self) -> None:
        with mock.patch.object(project_plan, "plan_for_spec") as planning:
            planning.return_value = project_plan.normalize_plan(
                PLAN, default_port=project_plan.DEFAULT_PORT, coerce=True
            )
            with mock.patch("sys.stdout") as out:
                self.node.main()

        written = json.loads((self.app_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(written["run"]["port"], 8000)

        emitted = json.loads(out.write.call_args_list[0][0][0])
        for key in ("language", "port", "start", "files", "summary", "detail"):
            self.assertIn(key, emitted)
        self.assertEqual(emitted["language"], "python")
        self.assertEqual(emitted["port"], 8000)
        self.assertEqual(emitted["files"], 1)


if __name__ == "__main__":
    unittest.main()

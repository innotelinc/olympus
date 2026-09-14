#!/usr/bin/env python3
"""Tests for scripts/olympus-tui.py.

The curses window is not assertable, which is why the script keeps the decisions —
what the outline says, what a stage means, what the instruction becomes — in plain
functions and lets the screen only draw them. Those are what is tested here, plus
the one non-interactive path (`--once`) that a script or a person in a hurry runs.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TUI_PATH = Path(__file__).resolve().parent.parent / "olympus-tui.py"

spec = importlib.util.spec_from_file_location("olympus_tui", TUI_PATH)
assert spec and spec.loader
tui = importlib.util.module_from_spec(spec)
sys.modules["olympus_tui"] = tui
spec.loader.exec_module(tui)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "build-requests").mkdir()
        self.queue = self.repo / ".factory" / "build-queue"
        self.queue.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_status(self, job: str, **fields: object) -> None:
        payload = {"v": 1, "job": job, "slug": "todo", "action": "build", "state": "running"}
        payload.update(fields)
        (self.queue / f"{job}.status.json").write_text(json.dumps(payload), encoding="utf-8")

    def write_log(self, job: str, text: str) -> None:
        (self.queue / f"{job}.log").write_text(text, encoding="utf-8")


class TheInstructionBecomesASpec(Fixture):
    def test_the_title_is_the_first_line_of_the_instruction(self) -> None:
        self.assertEqual(
            tui.title_for("A weight tracker: log daily.\nWith a chart."),
            "A weight tracker: log daily",
        )

    def test_a_title_that_is_all_punctuation_still_has_a_name(self) -> None:
        self.assertEqual(tui.title_for("   ...   "), "Untitled build")
        self.assertEqual(tui.title_for(""), "Untitled build")

    def test_the_slug_is_project_plans_rule_not_a_second_copy(self) -> None:
        # The label reaches the network, so there is one authority on what one may
        # be; this asserts the UI defers to it rather than that it reimplements it.
        self.assertEqual(tui.slug_for("Café & Bar / v2"), tui.project_plan.slugify("Café & Bar / v2"))
        self.assertEqual(tui.slug_for("!!!"), "untitled-build")

    def test_the_spec_carries_the_instruction_verbatim(self) -> None:
        instruction = "A recipe box with search by ingredient"
        text = tui.spec_text(instruction, tui.title_for(instruction))

        self.assertIn(instruction, text)
        self.assertTrue(text.startswith("# A recipe box with search by ingredient"))

    def test_the_spec_adds_no_requirements_of_its_own(self) -> None:
        # Nothing here may name a technology: the request is the whole request, and
        # a template that suggests one is a template that changes what gets built.
        text = tui.spec_text("A tiny tool", "A tiny tool").lower()

        for word in ("react", "sqlite", "fastapi", "vite", "express"):
            self.assertNotIn(word, text)


class WritingTheSpec(Fixture):
    def test_it_is_written_into_build_requests(self) -> None:
        path = tui.write_spec(self.repo, "todo", "# Todo\n", replace=False)
        self.assertEqual(path, self.repo / "build-requests" / "todo.md")
        self.assertEqual(path.read_text(encoding="utf-8"), "# Todo\n")

    def test_an_existing_spec_is_refused_rather_than_overwritten(self) -> None:
        # The spec is what the build is. Replacing one silently would replace a
        # document someone may have written by hand with a one-line instruction.
        tui.write_spec(self.repo, "todo", "mine\n", replace=False)
        with self.assertRaises(FileExistsError):
            tui.write_spec(self.repo, "todo", "theirs\n", replace=False)
        self.assertEqual((self.repo / "build-requests" / "todo.md").read_text(), "mine\n")

    def test_replacing_is_how_it_is_asked_for(self) -> None:
        tui.write_spec(self.repo, "todo", "mine\n", replace=False)
        tui.write_spec(self.repo, "todo", "theirs\n", replace=True)
        self.assertEqual((self.repo / "build-requests" / "todo.md").read_text(), "theirs\n")


class TheOutline(Fixture):
    def states(self, action: str, status: dict | None, log: str) -> dict[str, str]:
        return {label: state for label, state in tui.stages(action, status, log)}

    def test_nothing_is_done_before_the_runner_has_the_job(self) -> None:
        outlined = tui.stages("build", None, "")
        self.assertEqual([state for _label, state in outlined], ["active", "todo", "todo", "todo", "todo"])

    def test_every_stage_is_read_off_the_run_not_the_clock(self) -> None:
        # A status plus a log that shows the plan and the manifest: four stages are
        # then facts, and the fifth — packaging — is still in flight.
        log = "wrote builds/todo/plan.json\nwrote builds/todo/project.manifest.json\n"
        outlined = self.states(
            "build", {"state": "running", "action": "build"}, log
        )

        self.assertEqual(outlined["Queued for the runner"], "done")
        self.assertEqual(outlined["Runner picked it up"], "done")
        self.assertEqual(outlined["Stack planned"], "done")
        self.assertEqual(outlined["Files written and built"], "done")
        self.assertEqual(outlined["Packaged"], "active")

    def test_a_finished_run_has_no_step_in_flight(self) -> None:
        # A live step on a run that has ended is the same lie as a progress bar that
        # keeps moving after the work stopped.
        outlined = tui.stages("build", {"state": "failed", "action": "build", "message": "nope"}, "")
        self.assertNotIn("active", outlined and [state for _label, state in outlined])

    def test_a_preview_ends_on_the_preview_name_not_on_a_publish(self) -> None:
        outlined = self.states(
            "preview",
            {"state": "succeeded", "action": "preview", "previewUrl": "https://x-preview.example"},
            "",
        )
        self.assertEqual(outlined["Running, preview name registered"], "done")

    def test_a_publish_ends_on_the_published_url(self) -> None:
        outlined = self.states(
            "publish",
            {"state": "succeeded", "action": "publish", "publishedUrl": "https://x.example"},
            "",
        )
        self.assertEqual(outlined["Running and published"], "done")

    def test_the_action_comes_from_the_status_when_it_is_there(self) -> None:
        session = tui.Session(self.repo, self.queue)
        session.action = "build"
        self.assertEqual(session.action_for({"action": "publish"}), "publish")
        self.assertEqual(session.action_for({"action": "nonsense"}), "build")
        self.assertEqual(session.action_for(None), "build")


class TheRenderedScreen(Fixture):
    def test_the_intro_offers_what_to_type(self) -> None:
        session = tui.Session(self.repo, self.queue)
        text = "\n".join(line for _style, line in tui.render(session, None, "", 88))
        self.assertIn("Type what you want built", text)
        self.assertIn("/list", text)

    def test_a_job_shows_its_outline_files_and_log(self) -> None:
        session = tui.Session(self.repo, self.queue)
        session.slug = "todo"
        session.job = "a" * 16
        session.instruction = "a todo list"
        log = "wrote builds/todo/plan.json\nwrote builds/todo/src/App.tsx\nlistening on 3000\n"
        status = {"state": "running", "action": "build", "message": "Building todo"}

        text = "\n".join(line for _style, line in tui.render(session, status, log, 88))

        self.assertIn("todo", text)
        self.assertIn("Stack planned", text)
        self.assertIn("Building todo", text)
        self.assertIn("src/App.tsx", text)
        self.assertIn("listening on 3000", text)

    def test_the_outcome_is_the_runners_own_message(self) -> None:
        # The runner wrote that sentence with the knowledge of what failed; a
        # paraphrase here would be a second, worse diagnosis of the same run.
        status = {"state": "failed", "action": "build", "message": "Publishing stopped at package-app.py (exit 2)."}
        line = tui.outcome_line(status, "publish", "todo", "studio.example.test")
        self.assertEqual(line, "Publishing stopped at package-app.py (exit 2).")

    def test_a_preview_says_it_is_a_preview_name(self) -> None:
        line = tui.outcome_line({"state": "running"}, "preview", "todo", "studio.example.test")
        self.assertIn("todo-preview.studio.example.test", line)

    def test_nothing_running_says_so_instead_of_inventing_progress(self) -> None:
        line = tui.outcome_line(None, "build", "todo", "")
        self.assertIn("Waiting for the runner", line)


class FilesInTheLog(Fixture):
    def test_paths_are_read_from_the_log_while_the_build_is_still_going(self) -> None:
        log = "wrote builds/todo/plan.json\nwrote builds/todo/src/App.tsx\n"
        self.assertEqual(tui.files_written(log), ["builds/todo/plan.json", "builds/todo/src/App.tsx"])

    def test_a_path_that_scrolls_past_twice_is_one_file(self) -> None:
        self.assertEqual(tui.files_written("wrote a.ts\nwrote a.ts\n"), ["a.ts"])


class TheQueueListing(Fixture):
    def test_an_untouched_queue_is_reported_as_one(self) -> None:
        self.assertIn("queue empty", tui.listing(self.repo, self.queue))

    def test_it_counts_by_state_and_says_whether_the_runner_is_alive(self) -> None:
        (self.queue / "abc.request.json").write_text("{}", encoding="utf-8")
        self.write_status("d" * 16, state="succeeded")
        (self.queue / tui.HEARTBEAT_NAME).write_text('{"beat_at": "now"}', encoding="utf-8")

        reported = tui.listing(self.repo, self.queue)

        self.assertIn("runner live", reported)
        self.assertIn("1 queued", reported)
        self.assertIn("1 succeeded", reported)


class DeliveringWhatWasBuilt(Fixture):
    """Preview and publish read the app's source out of `builds/<slug>`.

    The runner cannot read this host's `builds/` — a delivery request carries the
    files — so what goes in the payload is what gets packaged, and a payload that
    happens to include a stale `dist/` is how a publish serves code that no longer
    exists.
    """

    def seed_build(self) -> Path:
        build = self.repo / "builds" / "todo"
        (build / "src").mkdir(parents=True)
        (build / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
        (build / "package.json").write_text("{}\n", encoding="utf-8")
        (build / tui.project_plan.PLAN_NAME).write_text("{}\n", encoding="utf-8")
        (build / "MANIFEST.json").write_text("{}\n", encoding="utf-8")
        (build / "project.zip").write_text("zip\n", encoding="utf-8")
        (build / "dist").mkdir()
        (build / "dist" / "index.html").write_text("<html>\n", encoding="utf-8")
        (build / "node_modules").mkdir()
        (build / "node_modules" / "left-pad.js").write_text("x\n", encoding="utf-8")
        return build

    def test_the_source_is_sent_and_the_build_output_is_not(self) -> None:
        paths = {entry["path"] for entry in tui.delivery_files(self.seed_build())}

        self.assertIn("src/App.tsx", paths)
        self.assertIn("package.json", paths)
        for excluded in ("dist/index.html", "node_modules/left-pad.js", "MANIFEST.json", "project.zip"):
            self.assertNotIn(excluded, paths)

    def test_a_project_that_was_never_built_reads_as_nothing(self) -> None:
        self.assertEqual(tui.delivery_files(self.repo / "builds" / "absent"), [])

    def test_the_request_is_one_the_runner_accepts(self) -> None:
        # The strongest thing testable here: hand the queue entry to the runner's
        # own validator. A second definition of the request shape would fail here.
        files = tui.delivery_files(self.seed_build())
        job = tui.submit_delivery(self.queue, "todo", "publish", files)
        payload = json.loads((self.queue / f"{job}.request.json").read_text(encoding="utf-8"))

        validated = tui.build_runner.validate_request(self.repo, payload)

        self.assertEqual(validated["action"], "publish")
        self.assertEqual(validated["slug"], "todo")
        self.assertTrue(validated["publish"])
        self.assertEqual(validated["spec"], "")
        self.assertEqual(len(validated["files"]), len(files))

    def test_a_preview_does_not_publish(self) -> None:
        files = tui.delivery_files(self.seed_build())
        job = tui.submit_delivery(self.queue, "todo", "preview", files)
        payload = json.loads((self.queue / f"{job}.request.json").read_text(encoding="utf-8"))
        self.assertFalse(tui.build_runner.validate_request(self.repo, payload)["publish"])

    def test_the_command_says_so_rather_than_queueing_nothing(self) -> None:
        session = tui.Session(self.repo, self.queue)
        note = tui.Screen(session, "")._command("publish absent", 0)
        self.assertIn("nothing built", note)

    def test_the_command_points_the_window_at_the_job_it_queued(self) -> None:
        self.seed_build()
        session = tui.Session(self.repo, self.queue)
        note = tui.Screen(session, "")._command("preview todo", 0)

        self.assertIn("queued preview", note)
        self.assertEqual(session.slug, "todo")
        self.assertEqual(session.action, "preview")
        self.assertRegex(session.job, r"^[0-9a-f]{16}$")


class TheNonInteractivePath(Fixture):
    """`--once` is the same code as the window, minus the window.

    It is also the only part of this script a test can drive end to end, which is why
    the layout lives in `render` rather than in the draw loop.
    """

    def stub_submit(self, job: str = "b" * 16, **status: object) -> None:
        """Queue the job the way the runner would see it, then finish it."""

        def fake_submit(queue: Path, repo: Path, spec: str, *, replace: bool):
            # The slug the real submitter derives: the spec's own name. Deriving it
            # here too is what lets a later run recognise the app it already built.
            slug = Path(spec).stem
            payload = {"v": 1, "job": job, "spec": spec, "slug": slug}
            (queue / f"{job}.request.json").write_text(json.dumps(payload), encoding="utf-8")
            reported = {"v": 1, "job": job, "slug": slug, "action": "build", "state": "succeeded"}
            reported.update(status)
            (queue / f"{job}.status.json").write_text(json.dumps(reported), encoding="utf-8")
            (queue / f"{job}.log").write_text(f"wrote builds/{slug}/plan.json\n", encoding="utf-8")
            return 0, payload

        original = tui.build_runner.submit
        tui.build_runner.submit = fake_submit
        self.addCleanup(setattr, tui.build_runner, "submit", original)

    def test_a_successful_build_prints_the_outline_and_exits_zero(self) -> None:
        self.stub_submit(message="Built todo — 6 file(s).")
        code = tui.main(
            [
                "--once",
                "a todo list",
                "--repo",
                str(self.repo),
                "--queue",
                str(self.queue),
                "--timeout",
                "5",
            ]
        )
        self.assertEqual(code, 0)

    def test_a_failed_build_exits_non_zero(self) -> None:
        self.stub_submit(state="failed", message="The build failed (exit 1).")
        code = tui.main(
            ["--once", "a todo list", "--repo", str(self.repo), "--queue", str(self.queue), "--timeout", "5"]
        )
        self.assertEqual(code, 1)

    def test_the_spec_it_left_behind_is_the_instruction(self) -> None:
        self.stub_submit()
        tui.main(["--once", "a todo list", "--repo", str(self.repo), "--queue", str(self.queue), "--timeout", "5"])
        written = (self.repo / "build-requests" / "a-todo-list.md").read_text(encoding="utf-8")
        self.assertIn("a todo list", written)

    def test_a_name_already_in_use_is_refused_rather_than_built_over(self) -> None:
        self.stub_submit()
        tui.main(["--once", "a todo list", "--repo", str(self.repo), "--queue", str(self.queue), "--timeout", "5"])

        # Second time with the same instruction and the spec is still there: the
        # script follows the existing job instead of overwriting the document.
        result = subprocess.run(
            [
                sys.executable,
                str(TUI_PATH),
                "--once",
                "a todo list",
                "--repo",
                str(self.repo),
                "--queue",
                str(self.queue),
                "--timeout",
                "3",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already exists", result.stderr)


if __name__ == "__main__":
    unittest.main()

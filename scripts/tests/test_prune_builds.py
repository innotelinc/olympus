#!/usr/bin/env python3
"""Tests for scripts/prune-builds.py.

The script deletes built apps, so what matters is what it refuses to select: a
build a running job is using, a request still waiting to be claimed, the runner's
own lock and heartbeat, and — unless asked — a spec git is tracking.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

PRUNE_PATH = Path(__file__).resolve().parent.parent / "prune-builds.py"

spec = importlib.util.spec_from_file_location("prune_builds", PRUNE_PATH)
assert spec and spec.loader
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

DAY = 86400.0


class PruneFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.builds = self.repo / "builds"
        self.builds.mkdir()
        self.queue = self.repo / ".factory" / "build-queue"
        self.queue.mkdir(parents=True)
        (self.repo / "build-requests").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def age(self, path: Path, days: float) -> Path:
        """Create the entry if needed, then backdate it — these tests are about
        mtime, and an empty file is a valid queue artefact."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        stamp = time.time() - days * DAY
        os.utime(path, (stamp, stamp))
        return path


class SelectBuilds(PruneFixture):
    def test_only_old_directories_are_selected(self) -> None:
        # Backdate AFTER populating: creating a file inside a directory bumps the
        # directory's own mtime, so aging first would make every build look fresh.
        (self.builds / "old-app").mkdir()
        (self.builds / "old-app" / "index.html").write_text("x", encoding="utf-8")
        self.age(self.builds / "old-app", 10)
        (self.builds / "new-app").mkdir()
        (self.builds / "new-app" / "index.html").write_text("x", encoding="utf-8")
        self.age(self.builds / "new-app", 1)

        selected = prune.select_builds(self.builds, 7)

        self.assertEqual([entry.name for entry in selected], ["old-app"])

    def test_zero_age_selects_everything(self) -> None:
        (self.builds / "a").mkdir()
        (self.builds / "b").mkdir()

        self.assertEqual(len(prune.select_builds(self.builds, 0)), 2)

    def test_loose_files_are_not_builds(self) -> None:
        (self.builds / "MANIFEST.json").write_text("{}", encoding="utf-8")

        self.assertEqual(prune.select_builds(self.builds, 0), [])

    def test_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(prune.select_builds(self.repo / "absent", 7), [])


class SelectQueueArtefacts(PruneFixture):
    def test_finished_job_files_are_selected_once_old(self) -> None:
        status = self.age(self.queue / "abc.status.json", 3)
        log = self.age(self.queue / "abc.log", 3)

        selected = {entry.name for entry in prune.select_queue_artefacts(self.queue, 1)}

        self.assertEqual(selected, {status.name, log.name})

    def test_pending_request_is_never_selected(self) -> None:
        # A request with no status/receipt is queued work, not exhaust. Removing
        # it would silently drop a build the operator asked for.
        self.age(self.queue / "pending.request.json", 30)
        self.age(self.queue / "pending.running.json", 30)

        selected = {entry.name for entry in prune.select_queue_artefacts(self.queue, 0)}

        self.assertEqual(selected, set())

    def test_an_old_cancel_marker_is_swept(self) -> None:
        # The runner consumes its own marker, but one written for a job an older
        # runner never cancelled would otherwise sit in the queue forever.
        marker = self.age(self.queue / "abc.cancel.json", 10)

        selected = {entry.name for entry in prune.select_queue_artefacts(self.queue, 7)}

        self.assertEqual(selected, {marker.name})

    def test_a_recent_cancel_marker_is_left_alone(self) -> None:
        # It may be the marker for a build that is still winding down.
        self.age(self.queue / "abc.cancel.json", 0.01)

        self.assertEqual(prune.select_queue_artefacts(self.queue, 7), [])

    def test_lock_and_heartbeat_are_never_selected(self) -> None:
        self.age(self.queue / "runner.lock", 30)
        self.age(self.queue / "runner.heartbeat.json", 30)

        self.assertEqual(prune.select_queue_artefacts(self.queue, 0), [])

    def test_fresh_artefacts_survive(self) -> None:
        self.age(self.queue / "abc.status.json", 0.1)

        self.assertEqual(prune.select_queue_artefacts(self.queue, 7), [])


class BusySlugs(PruneFixture):
    def test_a_running_build_is_protected(self) -> None:
        (self.queue / "runner.heartbeat.json").write_text(
            json.dumps({"busy_with": "current-app", "beat_at": "now"}), encoding="utf-8"
        )

        self.assertEqual(prune.busy_slugs(self.queue), {"current-app"})

    def test_a_stale_heartbeat_protects_nothing(self) -> None:
        # A crashed runner leaves busy_with set forever; honouring it would make
        # the build permanently unprunable.
        heartbeat = self.queue / "runner.heartbeat.json"
        heartbeat.write_text(json.dumps({"busy_with": "ghost"}), encoding="utf-8")
        self.age(heartbeat, 1)

        self.assertEqual(prune.busy_slugs(self.queue), set())

    def test_idle_and_malformed_heartbeats_are_safe(self) -> None:
        heartbeat = self.queue / "runner.heartbeat.json"
        heartbeat.write_text(json.dumps({"busy_with": None}), encoding="utf-8")
        self.assertEqual(prune.busy_slugs(self.queue), set())

        heartbeat.write_text("{not json", encoding="utf-8")
        self.assertEqual(prune.busy_slugs(self.queue), set())

    def test_no_heartbeat_at_all(self) -> None:
        self.assertEqual(prune.busy_slugs(self.queue), set())


class SelectSpecs(PruneFixture):
    def test_untracked_specs_are_selected_when_old(self) -> None:
        spec = self.repo / "build-requests" / "scratch.md"
        spec.write_text("# App\n", encoding="utf-8")
        self.age(spec, 30)

        self.assertEqual(prune.select_specs(self.repo, 1), [spec])

    def test_specs_git_tracks_need_an_explicit_flag(self) -> None:
        spec = self.repo / "build-requests" / "todo.md"
        spec.write_text("# App\n", encoding="utf-8")
        self.age(spec, 30)
        # This fixture is a throwaway directory, not a checkout, so `git ls-files`
        # reports nothing. Stub the lookup rather than shelling out to git.
        original = prune.tracked_specs
        prune.tracked_specs = lambda repo: {"build-requests/todo.md"}
        try:
            self.assertEqual(prune.select_specs(self.repo, 1), [])
            self.assertEqual(prune.select_specs(self.repo, 1, include_tracked=True), [spec])
        finally:
            prune.tracked_specs = original

    def test_readme_is_never_selected(self) -> None:
        readme = self.repo / "build-requests" / "README.md"
        readme.write_text("# Specs\n", encoding="utf-8")
        self.age(readme, 30)

        self.assertEqual(prune.select_specs(self.repo, 0, include_tracked=True), [])


class QueueOnly(PruneFixture):
    """`--queue-only` must not reach build directories, at any age.

    The two halves age differently: a finished queue entry is litter within the
    hour, while `builds/<slug>` is the source a rebuild and a re-package read — and
    the container keeps serving from its image after the tree is gone, so losing it
    is not visible until the next build. The default seven-day filter hides that;
    `--older-than 0` does not. So the flag that makes a full sweep of the queue safe
    has to be the flag that makes it *only* the queue, rather than relying on a
    heartbeat that a hand-started build never writes.
    """

    def seed(self) -> None:
        (self.builds / "live-app").mkdir()
        (self.builds / "live-app" / "index.html").write_text("x", encoding="utf-8")
        self.age(self.builds / "live-app", 30)
        self.age(self.queue / "abc123.status.json", 30)
        self.age(self.queue / "abc123.log", 30)

    def run_prune(self, *extra: str) -> int:
        self.addCleanup(os.environ.pop, "BUILD_QUEUE_DIR", None)
        os.environ["BUILD_QUEUE_DIR"] = str(self.queue)
        with contextlib.redirect_stdout(io.StringIO()):
            return prune.main(
                ["--repo", str(self.repo), "--older-than", "0", "--yes", *extra]
            )

    def test_a_build_directory_survives_an_all_ages_sweep(self) -> None:
        self.seed()
        self.assertEqual(self.run_prune("--queue-only"), 0)
        self.assertTrue(
            (self.builds / "live-app").is_dir(),
            "the source tree of an app that may be running is not queue litter",
        )

    def test_the_queue_it_is_pointed_at_is_cleared(self) -> None:
        self.seed()
        self.run_prune("--queue-only")
        self.assertEqual([path.name for path in self.queue.iterdir()], [])

    def test_a_pending_request_still_survives(self) -> None:
        # The guard is not the flag: work waiting to be claimed is work, whatever
        # the age filter says.
        self.seed()
        self.age(self.queue / "waiting.request.json", 30)
        self.run_prune("--queue-only")
        self.assertEqual([path.name for path in self.queue.iterdir()], ["waiting.request.json"])


if __name__ == "__main__":
    unittest.main()

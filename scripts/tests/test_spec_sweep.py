#!/usr/bin/env python3
"""The spec sweep: specs that arrive by push, manufactured like a Studio request.

Why this is a sweep in the runner and not a CI job: the build reads its model from
the platform gateway, whose API is LAN-only by design, so no GitHub-hosted runner can
reach it. The job that used to try now reports that and manufactures nothing, and this
process — the one already sitting where the toolchain, the checkout and the gateway are
— is what turns a pushed spec into an app.

The decisions worth pinning are the ones that spend real money and touch a deployment's
working tree: what counts as "already built", when a rebuild is implied rather than
asked for, and the two cases where the fast-forward has to refuse instead of resolving
something. Each of those is a case here.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER_PATH = Path(__file__).resolve().parent.parent / "build-runner.py"

spec = importlib.util.spec_from_file_location("build_runner_sweep", RUNNER_PATH)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

SPEC_REL = runner.SPECS_STATE_REL


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


class SweepFixture(unittest.TestCase):
    """A throwaway checkout with a checkout-shaped git repo in it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "olympus"
        (self.repo / "build-requests").mkdir(parents=True)
        (self.repo / "builds").mkdir()
        (self.repo / "scripts").mkdir()
        (self.repo / "build-requests" / "README.md").write_text(
            "# Build requests\n", encoding="utf-8"
        )
        self.queue = self.repo / ".factory" / "build-queue"
        self.queue.mkdir(parents=True)

    def write_spec(self, name: str, body: str) -> Path:
        path = self.repo / "build-requests" / name
        path.write_text(body, encoding="utf-8")
        return path

    def make_runner(self, **kwargs: object) -> "runner.Runner":
        # The pull is off by default here: every test below is about what the sweep
        # decides, and a real fetch in each of them would test git, not the runner.
        options = {"specs_pull": False, "spec_poll_seconds": 1}
        options.update(kwargs)
        return runner.Runner(self.repo, self.queue, **options)

    def queued(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.queue.glob(f"*{runner.REQUEST_SUFFIX}"))
        ]

    def state(self) -> dict:
        path = self.repo / SPEC_REL
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("specs", {})

    def init_repo_with_origin(self) -> Path:
        """Commit the fixture, then publish it to a bare remote this checkout tracks.

        `git init --bare` leaves HEAD on `master` whatever branch is pushed to it, so
        a clone of that remote checks out *nothing* and every file the fixture wrote
        is missing. Naming HEAD's branch is what makes the clones below real clones.
        """
        origin = Path(self._tmp.name) / "origin.git"
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        subprocess.run(
            ("git", "init", "-q", "--bare", str(origin)), capture_output=True, check=False
        )
        git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        return origin

    def clone_elsewhere(self, name: str) -> Path:
        """A second clone of origin — how a commit reaches it without touching ours."""
        other = Path(self._tmp.name) / name
        subprocess.run(
            ("git", "clone", "-q", str(self.origin), str(other)), capture_output=True, check=False
        )
        git(other, "config", "user.email", "t@example.invalid")
        git(other, "config", "user.name", "t")
        return other


class TheSweepQueues(SweepFixture):
    def test_a_new_spec_is_queued(self) -> None:
        self.write_spec("todo.md", "# App\n")
        self.make_runner().sweep_specs()

        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["spec"], "build-requests/todo.md")
        self.assertEqual(queued[0]["slug"], "todo")
        self.assertEqual(queued[0]["requested_by"], "spec-sweep")

    def test_the_directories_readme_is_not_a_request(self) -> None:
        self.make_runner().sweep_specs()
        self.assertEqual(self.queued(), [])

    def test_unchanged_content_is_not_queued_again(self) -> None:
        self.write_spec("todo.md", "# App\n")
        instance = self.make_runner()
        instance.sweep_specs()
        instance.sweep_specs()

        self.assertEqual(len(self.queued()), 1, "the same content was queued twice")

    def test_changed_content_is_queued_again(self) -> None:
        self.write_spec("todo.md", "# App\n")
        instance = self.make_runner()
        instance.sweep_specs()
        self.write_spec("todo.md", "# App\n\nAdd a search box.\n")
        instance.sweep_specs()

        self.assertEqual(len(self.queued()), 2)

    def test_a_second_spec_is_seen_alongside_the_first(self) -> None:
        self.write_spec("todo.md", "# App\n")
        self.write_spec("notes.md", "# App\n")
        self.make_runner().sweep_specs()
        self.assertEqual(sorted(job["slug"] for job in self.queued()), ["notes", "todo"])

    def test_the_sweep_can_be_turned_off(self) -> None:
        self.write_spec("todo.md", "# App\n")
        self.make_runner(specs=False).sweep_specs()
        self.assertEqual(self.queued(), [])


class TheSweepDerivesReplace(SweepFixture):
    """A rebuild is implied when the app already exists — and never otherwise.

    The load node refuses to manufacture over an existing app, so a spec whose content
    is new *and* whose app directory is already there is exactly the case that needs the
    consent. Deriving it is what makes a pushed change to a shipped app work at all.
    """

    def test_an_existing_app_means_replace(self) -> None:
        self.write_spec("todo.md", "# App\n")
        (self.repo / "builds" / "todo").mkdir()
        self.make_runner().sweep_specs()
        self.assertIs(self.queued()[0]["replace"], True)

    def test_a_first_build_does_not_replace(self) -> None:
        self.write_spec("todo.md", "# App\n")
        self.make_runner().sweep_specs()
        self.assertIs(self.queued()[0]["replace"], False)

    def test_an_empty_app_directory_still_counts_as_existing(self) -> None:
        # The load node treats a directory holding only its own control files as
        # resumable, so an empty one is not an overwrite — but replace is a consent the
        # node may ignore, and asking for it when a directory is there is never wrong.
        self.write_spec("todo.md", "# App\n")
        (self.repo / "builds" / "todo").mkdir()
        self.make_runner().sweep_specs()
        self.assertIs(self.queued()[0]["replace"], True)


class TheSweepRecordsWhatItCannotBuild(SweepFixture):
    """A refusal is reported once, as the thing it is — not on every interval.

    `build-requests/` is where an operator puts a file by hand, so a file that can never
    be manufactured is a realistic mistake, and a sweep that re-reported it every minute
    would be noise that hides the next real problem.
    """

    def test_an_unmanufacturable_spec_is_recorded_as_refused(self) -> None:
        # A name the runner's slug rule refuses: uppercase is not a safe app directory.
        self.write_spec("Not A Slug.md", "# App\n")
        instance = self.make_runner()
        instance.sweep_specs()

        self.assertEqual(self.queued(), [])
        entry = self.state()["build-requests/Not A Slug.md"]
        self.assertIn("refused", entry)

    def test_a_refusal_does_not_come_back_on_the_next_pass(self) -> None:
        self.write_spec("Not A Slug.md", "# App\n")
        instance = self.make_runner()
        instance.sweep_specs()
        before = self.state()
        instance.sweep_specs()
        self.assertEqual(self.state(), before)

    def test_a_refused_spec_that_is_fixed_is_then_queued(self) -> None:
        path = self.write_spec("Not A Slug.md", "# App\n")
        instance = self.make_runner()
        instance.sweep_specs()
        path.rename(path.with_name("renamed.md"))
        instance.sweep_specs()
        self.assertEqual([job["slug"] for job in self.queued()], ["renamed"])


class TheSweepRemembersJobs(SweepFixture):
    def test_running_a_request_records_the_spec_it_was_for(self) -> None:
        self.write_spec("todo.md", "# App\n")
        digest = runner.sha256_file(self.repo / "build-requests" / "todo.md")

        instance = self.make_runner()
        payload = runner.build_request(
            self.repo, "build-requests/todo.md", replace=False, requested_by="test"
        )
        runner.enqueue(self.queue, payload)

        # Refused rather than built — the manufacture script is absent — and that is the
        # point: the record is written for a job that *failed* too, which is what keeps
        # the sweep from re-queueing it forever.
        instance.process(self.queue / f"{payload['job']}{runner.REQUEST_SUFFIX}")

        entry = self.state()["build-requests/todo.md"]
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual(entry["job"], payload["job"])

    def test_a_recorded_job_stops_the_sweep_queueing_the_same_content(self) -> None:
        self.write_spec("todo.md", "# App\n")
        instance = self.make_runner()
        payload = runner.build_request(
            self.repo, "build-requests/todo.md", replace=False, requested_by="test"
        )
        runner.enqueue(self.queue, payload)
        instance.process(self.queue / f"{payload['job']}{runner.REQUEST_SUFFIX}")
        for path in self.queue.glob(f"*{runner.STATUS_SUFFIX}"):
            path.unlink()

        instance.sweep_specs()
        self.assertEqual(self.queued(), [])


class TheStateFileIsACache(SweepFixture):
    def test_a_missing_state_file_reads_as_nothing_built(self) -> None:
        self.assertEqual(runner.read_specs_state(self.repo / SPEC_REL), {})

    def test_a_corrupt_state_file_reads_as_nothing_built(self) -> None:
        path = self.repo / SPEC_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(runner.read_specs_state(path), {})

    def test_a_state_file_with_the_wrong_shape_reads_as_nothing_built(self) -> None:
        path = self.repo / SPEC_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"specs": ["not", "a", "mapping"]}), encoding="utf-8")
        self.assertEqual(runner.read_specs_state(path), {})

    def test_the_digest_is_over_content_not_mtime(self) -> None:
        path = self.write_spec("todo.md", "# App\n")
        first = runner.sha256_file(path)
        os.utime(path, (0, 0))
        self.assertEqual(runner.sha256_file(path), first)


class TheFastForward(SweepFixture):
    """The one thing the sweep does to a deployment's working tree — so it must refuse.

    Pulling is how a pushed spec reaches the host at all. Resolving a conflict or
    discarding an edit in the checkout a deployment builds from would be a way to lose
    someone's work to a scheduled sweep, and a rewritten upstream history (which this
    repository has had) is a state to report rather than to force through.
    """

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.init_repo_with_origin()

    def commit_elsewhere(self, body: str) -> None:
        """Put a commit on origin without touching this checkout."""
        other = self.clone_elsewhere("elsewhere")
        (other / "build-requests" / "pushed.md").write_text(body, encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "a pushed spec")
        git(other, "push", "-q", "origin", "main")

    def test_it_fast_forwards_a_clean_checkout(self) -> None:
        self.commit_elsewhere("# Pushed\n")
        instance = self.make_runner(specs_pull=True)
        instance.fast_forward()

        self.assertTrue((self.repo / "build-requests" / "pushed.md").is_file())

    def test_it_leaves_a_dirty_checkout_alone(self) -> None:
        self.commit_elsewhere("# Pushed\n")
        (self.repo / "build-requests" / "mine.md").write_text("# Mine\n", encoding="utf-8")

        instance = self.make_runner(specs_pull=True)
        instance.fast_forward()

        self.assertTrue((self.repo / "build-requests" / "mine.md").is_file())
        self.assertFalse((self.repo / "build-requests" / "pushed.md").exists())

    def test_it_refuses_a_rewritten_history_rather_than_forcing_it(self) -> None:
        # A rewrite on origin: the remote's main becomes a history this checkout is not
        # an ancestor of, so this can never be a fast-forward. An orphan branch is how
        # this repository's own force-updated main actually happened. The checkout must
        # stay exactly where it was.
        before = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        other = self.clone_elsewhere("rewriter")
        git(other, "checkout", "-q", "--orphan", "rewritten")
        (other / "rewritten.md").write_text("# Rewritten\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "rewrite")
        git(other, "push", "-q", "--force", "origin", "rewritten:main")

        instance = self.make_runner(specs_pull=True)
        instance.fast_forward()

        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertFalse((self.repo / "rewritten.md").exists())

    def test_a_checkout_that_is_not_a_git_repo_is_not_an_error(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        (plain / "build-requests").mkdir(parents=True)
        instance = runner.Runner(plain, self.queue, specs_pull=True, spec_poll_seconds=1)
        instance.fast_forward()  # must simply report, not raise


class TheSweepPullsBeforeItLooks(SweepFixture):
    def test_a_pushed_spec_is_built_after_the_pull(self) -> None:
        # The whole point of the pull: a spec that exists only on origin becomes a build
        # without anyone logging into the host.
        self.origin = self.init_repo_with_origin()
        other = self.clone_elsewhere("pusher")
        (other / "build-requests" / "pushed.md").write_text("# Pushed\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "a pushed spec")
        git(other, "push", "-q", "origin", "main")

        self.make_runner(specs_pull=True).sweep_specs()

        self.assertEqual([job["slug"] for job in self.queued()], ["pushed"])


class TheOnceModeSweeps(SweepFixture):
    def test_once_looks_for_pushed_specs_before_it_drains(self) -> None:
        # `--once` is how an operator or a timer says "do all of it now"; a pass that
        # skipped the sweep would quietly mean "do half of it".
        source = RUNNER_PATH.read_text(encoding="utf-8")
        marker = source.index("if args.once:")
        block = source[marker : marker + 400]
        self.assertIn("sweep_specs()", block)


if __name__ == "__main__":
    unittest.main()

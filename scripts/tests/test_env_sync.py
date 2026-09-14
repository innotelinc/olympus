#!/usr/bin/env python3
"""Tests for scripts/env-sync.py.

The script edits `.env` — the one file in a deployment that holds credentials and
often carries decisions somebody made by hand — so what matters is what it refuses
to touch. It appends keys the example documents and the file has never mentioned;
it does not reorder, rewrite or remove a line that is already there, and a key that
is present but commented out is a deliberate "not this one", not a gap.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ENV_SYNC_PATH = Path(__file__).resolve().parent.parent / "env-sync.py"

spec = importlib.util.spec_from_file_location("env_sync", ENV_SYNC_PATH)
assert spec and spec.loader
env_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env_sync)

EXAMPLE = """\
# Where the queue lives. Studio cannot run the build itself.
STUDIO_BUILD_QUEUE_DIR=/app/build-queue

# Generations per minute. Set 0 to disable.
STUDIO_RATE_LIMIT_PER_MIN=

# --- Edge --------------------------------------------------------------------

# The port the proxy listens on.
GATEWAY_SSO_PORT=20129
"""


class SyncFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.example = self.root / ".env.example"
        self.env = self.root / ".env"
        self.example.write_text(EXAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_env(self, text: str) -> None:
        self.env.write_text(text, encoding="utf-8")

    def run_sync(self, *argv: str) -> tuple[int, str]:
        """Run main() with stdout captured, the way `make env-sync` would."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = env_sync.main(
                ["--example", str(self.example), "--env", str(self.env), *argv]
            )
        return code, out.getvalue()


class Reporting(SyncFixture):
    def test_reports_drift_and_writes_nothing(self):
        self.write_env("STUDIO_BUILD_QUEUE_DIR=/app/build-queue\n")
        before = self.env.read_text(encoding="utf-8")

        code, out = self.run_sync()

        self.assertEqual(code, 2, "drift is a non-zero exit so CI can gate on it")
        self.assertIn("STUDIO_RATE_LIMIT_PER_MIN", out)
        self.assertIn("GATEWAY_SSO_PORT", out)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before, "no --write, no edit")

    def test_clean_file_exits_zero(self):
        self.write_env(EXAMPLE)

        code, out = self.run_sync()

        self.assertEqual(code, 0)
        self.assertIn("has every key", out)

    def test_a_commented_key_is_present_not_missing(self):
        # `# STUDIO_RATE_LIMIT_PER_MIN=…` is an operator saying "not this one".
        # Appending an active line under it would overrule that decision.
        self.write_env(
            "STUDIO_BUILD_QUEUE_DIR=/app/build-queue\n"
            "# STUDIO_RATE_LIMIT_PER_MIN=\n"
            "GATEWAY_SSO_PORT=20129\n"
        )

        code, out = self.run_sync()

        self.assertEqual(code, 0, "nothing to add — every documented key is mentioned")
        self.assertIn("present but commented out: STUDIO_RATE_LIMIT_PER_MIN", out)


class Writing(SyncFixture):
    def test_appends_missing_keys_with_their_comments(self):
        self.write_env("STUDIO_BUILD_QUEUE_DIR=/app/build-queue\n")

        code, out = self.run_sync("--write")

        self.assertEqual(code, 0)
        body = self.env.read_text(encoding="utf-8")
        self.assertIn("added: STUDIO_RATE_LIMIT_PER_MIN", out)
        # The example's own words travel with the key — a bare `KEY=` is a riddle.
        self.assertIn("# Generations per minute. Set 0 to disable.", body)
        self.assertIn("STUDIO_RATE_LIMIT_PER_MIN=", body)
        self.assertIn("GATEWAY_SSO_PORT=20129", body)
        self.assertIn("env-sync.py", body, "the addition says where it came from")

    def test_existing_bytes_are_a_prefix_of_the_result(self):
        # The whole safety story: nothing above the addition is rewritten, so a
        # hand-edited value, a stray blank line and an unusual ordering all survive.
        original = "# my own note\nZED=1\n\nSTUDIO_BUILD_QUEUE_DIR=/custom  # inline\n"
        self.write_env(original)

        self.run_sync("--write")

        after = self.env.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(original), after)
        self.assertIn("/custom  # inline", after, "a value set by hand is untouched")

    def test_a_key_only_in_env_is_left_alone(self):
        # The example is the documented surface, not the whole file: a deployment
        # is allowed keys the example never had, and they must not be seen as drift.
        self.write_env("STUDIO_BUILD_QUEUE_DIR=/app/build-queue\nONE_OFF=keep-me\n")

        code, _ = self.run_sync("--write")

        self.assertEqual(code, 0)
        body = self.env.read_text(encoding="utf-8")
        self.assertEqual(body.count("ONE_OFF=keep-me"), 1)
        self.assertEqual(body.count("STUDIO_BUILD_QUEUE_DIR="), 1)

    def test_second_run_adds_nothing(self):
        self.write_env("STUDIO_BUILD_QUEUE_DIR=/app/build-queue\n")
        self.run_sync("--write")
        once = self.env.read_text(encoding="utf-8")

        code, out = self.run_sync("--write")

        self.assertEqual(code, 0)
        self.assertEqual(self.env.read_text(encoding="utf-8"), once, "idempotent")
        self.assertIn("has every key", out)

    def test_appended_file_keeps_one_trailing_newline(self):
        self.write_env("STUDIO_BUILD_QUEUE_DIR=/app/build-queue")

        self.run_sync("--write")

        body = self.env.read_text(encoding="utf-8")
        self.assertTrue(body.endswith("\n"))
        self.assertFalse(body.endswith("\n\n"), "the addition is not stranded by a blank line")


class Blocks(SyncFixture):
    def test_a_section_header_does_not_attach_to_the_next_key(self):
        # A blank line ends the comment run, so `# --- Edge ---` describes the
        # section and not GATEWAY_SSO_PORT — otherwise appending the port would
        # re-print a header that belongs to a group the file does not have here.
        blocks = {b.key: b.lines for b in env_sync.example_blocks(EXAMPLE)}

        self.assertEqual(blocks["GATEWAY_SSO_PORT"][-1], "GATEWAY_SSO_PORT=20129")
        self.assertNotIn("# --- Edge ---", blocks["GATEWAY_SSO_PORT"])
        self.assertIn("# The port the proxy listens on.", blocks["GATEWAY_SSO_PORT"])

    def test_leading_comments_attach_to_their_key(self):
        blocks = {b.key: b.lines for b in env_sync.example_blocks(EXAMPLE)}

        self.assertEqual(
            blocks["STUDIO_RATE_LIMIT_PER_MIN"][0], "# Generations per minute. Set 0 to disable."
        )


class GuardRails(SyncFixture):
    def test_a_missing_env_is_never_created(self):
        # Creating one here would invent a `.env` full of blank secrets. That is
        # `make setup`'s job; guessing at it is how a deployment loses a secret.
        code, _ = self.run_sync("--write")

        self.assertEqual(code, 1)
        self.assertFalse(self.env.exists())

    def test_a_missing_example_is_reported_not_guessed(self):
        self.example.unlink()

        code, _ = self.run_sync()

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

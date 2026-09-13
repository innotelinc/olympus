"""The app builder's model resolution, which is where a build silently goes wrong.

`build-app.py` runs as an Archon script node. Archon strips the target repo's `.env`
keys out of the node's environment ("stripped 42 keys"), *and* runs the workflow from
a copy under `artifacts/runs/<id>/workflow-source/…`. So a node that reads only its
environment, or only its own directory, ends up on the code defaults while the
deployment's `.env` says otherwise — measured: a node configured with
`gemini/gemini-3-flash-preview` asked the gateway for `auto/coding`, the manifest
recorded the model nobody configured, and the combo walked 1109 fallbacks before
answering. The failure is silent: a wrong model still produces a build log, just not a
build. These pin the layering that prevents it.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".archon"
    / "workflows"
    / "app"
    / "greenfield"
    / "scripts"
    / "build-app.py"
)

WATCHED = (
    "OMNIROUTE_MODEL",
    "OMNIROUTE_MODEL_FALLBACK",
    "OMNIROUTE_BASE_URL",
    "INPUTS_APP_DIR",
    "INPUTS_SPEC_PATH",
)


def load_module():
    spec = importlib.util.spec_from_file_location("build_app_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvFixture(unittest.TestCase):
    """A checkout, and — separately — wherever the node happens to be running from."""

    def setUp(self) -> None:
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()

        # Where the script runs from by default: inside its own checkout.
        self.script = self.checkout / ".archon" / "workflows" / "app" / "greenfield" / "scripts" / "build-app.py"
        self.script.parent.mkdir(parents=True)
        self._saved_file = self.module.__file__
        self.module.__file__ = str(self.script)
        self.addCleanup(self._restore)

        # The environment is a shared, mutable input to the thing under test: the node's
        # layering reads it, and several tests here set a variable on purpose. The whole
        # environment is snapshotted and handed back rather than only the keys named in
        # WATCHED — a test that exported a value used to leave it set for every file that
        # ran afterwards, and what that produced was a failure somewhere else that read as
        # the runner ignoring a checkout's `.env` when it was obeying an override nobody
        # had exported.
        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)

        for key in WATCHED:
            os.environ.pop(key, None)

    def _restore(self) -> None:
        self.module.__file__ = self._saved_file
        self.module.repo_omniroute.cache_clear()

    def _restore_environ(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def write_env(self, body: str) -> None:
        (self.checkout / ".env").write_text(body, encoding="utf-8")
        self.module.repo_omniroute.cache_clear()

    def run_from_a_copy(self) -> Path:
        """Move the node onto a copy outside the checkout, the way Archon does."""
        copy = self.root / "archon" / "artifacts" / "runs" / "abc" / "workflow-source" / "project" / "build-app.py"
        copy.parent.mkdir(parents=True)
        self.module.__file__ = str(copy)
        return copy


class ReadsTheCheckoutDotenv(EnvFixture):
    def test_the_configured_model_is_the_one_asked_for(self) -> None:
        self.write_env(
            "OMNIROUTE_MODEL=gemini/gemini-3-flash-preview\n"
            "OMNIROUTE_MODEL_FALLBACK=gemini/gemini-2.5-flash\n"
        )
        self.assertEqual(
            self.module.model_chain(),
            ["gemini/gemini-3-flash-preview", "gemini/gemini-2.5-flash", "auto/coding"],
        )

    def test_the_gateway_comes_from_the_file_too(self) -> None:
        self.write_env("OMNIROUTE_BASE_URL=http://gateway.internal:20128/v1\n")
        self.assertEqual(self.module.setting("OMNIROUTE_BASE_URL"), "http://gateway.internal:20128/v1")

    def test_comments_quotes_and_other_keys_are_handled(self) -> None:
        self.write_env(
            "# a comment\n"
            'OMNIROUTE_MODEL="gemini/gemini-3-flash-preview"\n'
            "AUTHENTIK_TOKEN=vault://cerulean/olympus/authentik\n"
            "\n"
            "OMNIROUTE_MODEL_FALLBACK=gemini/gemini-2.5-flash\n"
        )
        self.assertEqual(
            self.module.model_chain(),
            ["gemini/gemini-3-flash-preview", "gemini/gemini-2.5-flash", "auto/coding"],
        )
        # Only OMNIROUTE_* is read: a script node has no business holding the tokens.
        self.assertEqual(sorted(self.module.repo_omniroute()), ["OMNIROUTE_MODEL", "OMNIROUTE_MODEL_FALLBACK"])


class FindsTheRealCheckout(EnvFixture):
    """The copy Archon runs from has no `.env` of its own to walk up to."""

    def test_the_app_directory_locates_the_checkout(self) -> None:
        self.run_from_a_copy()
        self.write_env("OMNIROUTE_MODEL=gemini/gemini-3-flash-preview\n")
        os.environ["INPUTS_APP_DIR"] = str(self.checkout / "builds" / "my-app")

        self.assertEqual(self.module.model_chain()[0], "gemini/gemini-3-flash-preview")

    def test_the_spec_path_works_when_the_app_dir_is_not_set(self) -> None:
        self.run_from_a_copy()
        self.write_env("OMNIROUTE_MODEL=gemini/gemini-3-flash-preview\n")
        os.environ["INPUTS_SPEC_PATH"] = str(self.checkout / "build-requests" / "my-app.md")

        self.assertEqual(self.module.model_chain()[0], "gemini/gemini-3-flash-preview")

    def test_a_copy_with_nothing_to_go_on_falls_back_rather_than_guessing(self) -> None:
        self.run_from_a_copy()

        self.assertEqual(self.module.model_chain()[0], self.module.DEFAULT_MODEL)


class Precedence(EnvFixture):
    def test_the_nodes_environment_beats_the_file(self) -> None:
        self.write_env("OMNIROUTE_MODEL=gemini/gemini-3-flash-preview\n")
        os.environ["OMNIROUTE_MODEL"] = "operator/override"
        self.assertEqual(self.module.model_chain()[0], "operator/override")

    def test_an_empty_value_in_the_file_is_not_a_model(self) -> None:
        self.write_env("OMNIROUTE_MODEL=\n")
        self.assertEqual(self.module.model_chain()[0], self.module.DEFAULT_MODEL)

    def test_a_repeated_model_is_tried_once(self) -> None:
        self.write_env("OMNIROUTE_MODEL=same/model\nOMNIROUTE_MODEL_FALLBACK=same/model\n")
        # The repeat is dropped; the composed route is still there behind it, so a
        # deployment that pinned one model twice is not left with no second attempt.
        self.assertEqual(self.module.model_chain(), ["same/model", "auto/coding"])

    def test_the_composed_route_is_the_last_attempt_never_the_first(self) -> None:
        # Measured, not theoretical: two configured models both unavailable — one
        # timing out upstream, the other in `model_cooldown` with a 23-minute reset —
        # failed three attempts while a third of the catalogue was answering. The
        # combo walks the catalogue, so it belongs last: ahead of it the configured
        # model is the deliberate choice, behind it luck is better than nothing.
        self.write_env("OMNIROUTE_MODEL=gemini/gemini-3-flash-preview\n")
        chain = self.module.model_chain()
        self.assertEqual(chain[0], "gemini/gemini-3-flash-preview")
        self.assertEqual(chain[-1], "auto/coding")
        self.assertEqual(chain.count("auto/coding"), 1)

    def test_no_dotenv_at_all_falls_back_to_the_code_defaults(self) -> None:
        # DEFAULT_MODEL *is* the composed route, so a bare install asks it first and
        # the concrete default second — the order only differs from a pinned one.
        self.assertEqual(
            self.module.model_chain(),
            [self.module.DEFAULT_MODEL, self.module.DEFAULT_FALLBACK_MODEL],
        )
        self.assertEqual(self.module.setting("OMNIROUTE_BASE_URL"), "")


if __name__ == "__main__":
    unittest.main()

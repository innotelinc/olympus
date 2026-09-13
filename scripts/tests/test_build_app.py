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

import contextlib
import importlib.util
import io
import json
import os
import shutil
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


class ThePlanBuildAndItsRepair(EnvFixture):
    """The plan's own build runs here, and its failure goes back to the agent.

    Measured, not hypothetical: a spec-driven build wrote its files and exited 0, and
    only failed later when packaging ran the plan's commands in the image — by which
    point the agent that wrote the tree was gone and the failure was a compiler message
    nobody could act on. Nothing in the workflow ran the *plan's* build: `verify-app`
    runs a check the spec declares, and the packager runs it in the image. These pin the
    loop that closes that gap, and the two ways it must stay quiet.
    """

    def setUp(self) -> None:
        super().setUp()
        self.module.plan_contract.cache_clear()
        self.addCleanup(self.module.plan_contract.cache_clear)

        real = Path(__file__).resolve().parents[2]
        # The node finds the checkout's plan contract and its prompt by path, so both
        # have to exist under the fake checkout for `main()` to run at all.
        (self.checkout / "scripts").mkdir(parents=True)
        shutil.copy(
            real / "scripts" / "project_plan.py", self.checkout / "scripts" / "project_plan.py"
        )
        real_commands = real / ".archon" / "workflows" / "app" / "greenfield" / "commands"
        commands = self.checkout / ".archon" / "workflows" / "app" / "greenfield" / "commands"
        commands.mkdir(parents=True)
        shutil.copy(real_commands / "build.md", commands / "build.md")

        self.app_dir = self.checkout / "builds" / "probe-app"
        self.app_dir.mkdir(parents=True)
        self.spec = self.checkout / "build-requests" / "probe-app.md"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text(
            "# Probe App\n\n## Verification Criteria\n\n- open it and look\n", encoding="utf-8"
        )

        os.environ["INPUTS_APP_DIR"] = str(self.app_dir)
        os.environ["INPUTS_SPEC_PATH"] = str(self.spec)
        os.environ["INPUTS_TITLE"] = "Probe App"

        self.calls = self.root / "codex-calls"

    def write_codex(self, behaviour: str) -> None:
        """A stand-in for the agent, so the node's loop can be driven without a gateway."""
        path = self.root / "codex"
        path.write_text(
            "#!/bin/sh\n" f"echo call >> {self.calls}\n" + behaviour,
            encoding="utf-8",
        )
        path.chmod(0o755)
        os.environ["CODEX_BIN"] = str(path)

    def write_plan(self, build: str, language: str = "node") -> None:
        (self.app_dir / "plan.json").write_text(
            json.dumps(
                {
                    "name": "Probe App",
                    "kind": "app",
                    "summary": "a probe",
                    "runtime": {"language": language, "frameworks": [], "database": None},
                    "run": {
                        "install": "",
                        "build": build,
                        "start": "node server/index.js",
                        "port": 3000,
                        "healthcheck": "/",
                    },
                    "files": [],
                }
            ),
            encoding="utf-8",
        )

    def run_main(self) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = self.module.main()
        self.assertEqual(code, 0, "the node reports the outcome in its JSON, not its exit code")
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def call_count(self) -> int:
        if not self.calls.is_file():
            return 0
        return len(self.calls.read_text(encoding="utf-8").splitlines())

    def test_a_failing_build_is_handed_back_and_the_repaired_tree_passes(self) -> None:
        # The build command fails while the agent's `broken` marker exists, which is the
        # shape of a real one: the tree is present, and the plan's own command rejects it.
        self.write_plan(build="test ! -f broken")
        self.write_codex(
            f'app="{self.app_dir}"\n'
            'case "$*" in\n'
            '  *"does not build"*) rm -f "$app/broken" ;;\n'
            '  *) touch "$app/broken" ;;\n'
            "esac\n"
            'printf "<html>probe</html>" > "$app/index.html"\n'
            "exit 0\n"
        )

        result = self.run_main()

        self.assertEqual(result["repairs"], 1)
        self.assertEqual(result["build_exit"], 0, "the repair left a tree that builds")
        self.assertEqual(self.call_count(), 2, "one attempt, then one repair")
        self.assertFalse((self.app_dir / "broken").exists())

    def test_a_tree_that_builds_first_time_is_never_repaired(self) -> None:
        self.write_plan(build="test -f index.html")
        self.write_codex(
            f'app="{self.app_dir}"\nprintf "<html>probe</html>" > "$app/index.html"\n'
        )

        result = self.run_main()

        self.assertEqual(result["build_exit"], 0)
        self.assertEqual(result["repairs"], 0)
        self.assertEqual(self.call_count(), 1, "no second agent run over a tree that builds")

    def test_a_build_this_node_cannot_run_is_skipped_not_blamed_on_the_agent(self) -> None:
        # The packager installs its own toolchain in the image. A program missing *here*
        # is this node's limitation, and handing that to the agent as a project failure
        # would have it rewrite a working tree to satisfy a tool nobody has.
        self.write_plan(build="definitely-not-a-program --build")
        self.write_codex(
            f'app="{self.app_dir}"\nprintf "<html>probe</html>" > "$app/index.html"\n'
        )

        result = self.run_main()

        self.assertEqual(result["build_exit"], 0)
        self.assertEqual(result["repairs"], 0)
        self.assertEqual(self.call_count(), 1)

    def test_no_plan_means_there_is_no_build_to_run(self) -> None:
        self.assertEqual(self.module.plan_build_commands(self.app_dir), [])

    def test_a_language_whose_install_leaves_the_project_is_not_built_here(self) -> None:
        # The node runs on the host, and `pip install` would install into whatever
        # interpreter is on PATH — a change outside the project. That plan is built in
        # its image instead, where the install cannot reach the host.
        self.write_plan(build="pip install -r requirements.txt", language="python")
        self.assertEqual(self.module.plan_build_commands(self.app_dir), [])

    def test_the_repair_keeps_the_original_instruction_and_the_guardrails(self) -> None:
        prompt = self.module.repair_prompt(
            "BASE INSTRUCTION",
            ["npm install", "npm run build"],
            "src/App.tsx(3,1): error TS2304: Cannot find name 'x'.",
        )

        self.assertTrue(prompt.startswith("BASE INSTRUCTION"))
        self.assertIn("npm install && npm run build", prompt)
        self.assertIn("Cannot find name 'x'", prompt)
        # The three ways a model makes a build error go away without fixing it.
        lowered = prompt.lower()
        self.assertIn("do not run a packager", lowered)
        self.assertIn("do not remove a feature", lowered)
        self.assertIn("change the stack to make the error go away", lowered)

    def test_the_first_command_names_the_program_that_has_to_be_here(self) -> None:
        self.assertEqual(self.module.build_program(["npm install", "npm run build"]), "npm")
        self.assertEqual(self.module.build_program([]), "")


class ThePlanBuildRunsInTheImageOrder(EnvFixture):
    def test_a_failing_command_reports_its_own_output(self) -> None:
        # Through a shell, because that is what a Dockerfile `RUN` is: the plan's
        # commands are run in the order and with the operators the plan asked for.
        exit_code, output = self.module.run_plan_build(
            self.checkout, ["printf 'boom from the compiler\\n'; exit 3"], dict(os.environ)
        )

        self.assertEqual(exit_code, 3)
        self.assertIn("boom from the compiler", output)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for scripts/build-model-check.py.

The check decides whether a build can produce anything at all, and its whole
value is the classification: a 200 is not a pass, a greeting is not a tool call,
and a model that calls a tool once and then cannot be asked again is the measured
way a build stalls. Those are the cases asserted here, plus the configuration
precedence, because the script reads `.env` under the process environment and the
two disagreeing is how a check reports on a model nobody configured.

No network: every classification test drives pure functions, and the exit-code
tests point the gateway at a closed port.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

CHECK_PATH = Path(__file__).resolve().parent.parent / "build-model-check.py"

spec = importlib.util.spec_from_file_location("build_model_check", CHECK_PATH)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
# Registered before exec_module because the script's dataclasses resolve their
# own module through sys.modules; a spec loaded without this is invisible to
# them and `@dataclass` fails on a NoneType. Nothing about the script needs it.
sys.modules["build_model_check"] = check
spec.loader.exec_module(check)


def responses_payload(*items: object) -> dict:
    return {"status": "completed", "output": list(items)}


FUNCTION_CALL = {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"command":"echo ok"}'}
MESSAGE = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I have written the file."}]}


class Classify(unittest.TestCase):
    def test_a_function_call_is_the_only_pass(self) -> None:
        outcome, detail = check.classify(200, responses_payload(FUNCTION_CALL))
        self.assertEqual(outcome, check.OUTCOME_TOOL)
        self.assertIn("echo ok", detail)

    def test_a_200_that_answers_in_prose_is_not_a_pass(self) -> None:
        # The failure this whole script exists for: it looks like work.
        outcome, detail = check.classify(200, responses_payload(MESSAGE))
        self.assertEqual(outcome, check.OUTCOME_PROSE)
        self.assertIn("written the file", detail)

    def test_a_reasoning_only_answer_is_not_a_pass(self) -> None:
        outcome, _ = check.classify(200, responses_payload({"type": "reasoning", "summary": []}))
        self.assertEqual(outcome, check.OUTCOME_EMPTY)

    def test_no_output_items_is_not_a_pass(self) -> None:
        outcome, _ = check.classify(200, {"status": "completed", "output": []})
        self.assertEqual(outcome, check.OUTCOME_EMPTY)

    def test_an_http_refusal_reports_the_gateways_reason(self) -> None:
        outcome, detail = check.classify(429, {"error": {"message": "All credentials are cooling down"}})
        self.assertEqual(outcome, check.OUTCOME_ERROR)
        self.assertIn("429", detail)
        self.assertIn("cooling down", detail)

    def test_an_error_field_on_a_200_is_not_a_pass(self) -> None:
        outcome, detail = check.classify(200, {"error": {"message": "quota_exhausted"}})
        self.assertEqual(outcome, check.OUTCOME_ERROR)
        self.assertIn("quota_exhausted", detail)

    def test_a_non_json_body_is_an_error_not_a_crash(self) -> None:
        outcome, _ = check.classify(200, "not json at all")
        self.assertEqual(outcome, check.OUTCOME_ERROR)

    def test_a_tool_call_without_a_name_still_describes_itself(self) -> None:
        # Measured: the gateway this stack uses omits `name` on some providers.
        outcome, detail = check.classify(200, responses_payload({"type": "function_call", "arguments": '{"command":"ls"}'}))
        self.assertEqual(outcome, check.OUTCOME_TOOL)
        self.assertIn("ls", detail)
        self.assertNotIn("None", detail)

    def test_a_tool_call_without_arguments_is_still_a_tool_call(self) -> None:
        outcome, detail = check.classify(200, responses_payload({"type": "function_call", "name": "shell"}))
        self.assertEqual(outcome, check.OUTCOME_TOOL)
        self.assertEqual(detail, "shell")

    def test_a_non_dict_payload_is_an_error(self) -> None:
        outcome, _ = check.classify(200, None)
        self.assertEqual(outcome, check.OUTCOME_ERROR)


class FollowUp(unittest.TestCase):
    def test_the_call_is_echoed_with_a_result_and_a_stable_id(self) -> None:
        items = check.follow_up_input({"type": "function_call", "call_id": "abc", "name": "shell", "arguments": "{}"})
        self.assertEqual([item["type"] for item in items], ["function_call", "function_call_output"])
        self.assertEqual(items[0]["call_id"], "abc")
        self.assertEqual(items[1]["call_id"], "abc")
        self.assertEqual(items[1]["output"], "ok")

    def test_a_missing_call_id_gets_a_fallback_not_an_empty_string(self) -> None:
        # An empty call_id is how the follow-up turn silently becomes a new one.
        items = check.follow_up_input({"type": "function_call", "arguments": "{}"})
        self.assertTrue(items[0]["call_id"])
        self.assertEqual(items[0]["call_id"], items[1]["call_id"])

    def test_a_missing_name_falls_back_to_the_probed_tool(self) -> None:
        items = check.follow_up_input({"type": "function_call", "arguments": "{}"})
        self.assertEqual(items[0]["name"], check.PROBE_TOOL["name"])


class EnvFile(unittest.TestCase):
    def test_comments_quotes_and_an_export_prefix_are_handled(self) -> None:
        parsed = check.parse_env_file(
            "\n".join(
                [
                    "# a comment",
                    "PLAIN=value",
                    'QUOTED="quoted value"',
                    "SINGLE='single value'",
                    "export EXPORTED=yes",
                    "INLINE=clean  # trailing note",
                    "BAD LINE",
                    "",
                ]
            )
        )
        self.assertEqual(parsed["PLAIN"], "value")
        self.assertEqual(parsed["QUOTED"], "quoted value")
        self.assertEqual(parsed["SINGLE"], "single value")
        self.assertEqual(parsed["EXPORTED"], "yes")
        self.assertEqual(parsed["INLINE"], "clean")
        self.assertNotIn("BAD LINE", parsed)

    def test_a_hash_in_a_value_survives(self) -> None:
        # A vault:// reference carries its key in a fragment; truncating it would
        # make the token look configured and fail at the gateway.
        parsed = check.parse_env_file("AUTHENTIK_TOKEN=vault://cerulean/studio#token\n")
        self.assertEqual(parsed["AUTHENTIK_TOKEN"], "vault://cerulean/studio#token")


class Configuration(unittest.TestCase):
    def test_the_environment_wins_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".env").write_text("OMNIROUTE_MODEL=from-file\nOMNIROUTE_BASE_URL=http://file/v1\n", encoding="utf-8")
            env = check.repo_env(repo, {"OMNIROUTE_MODEL": "from-env"})
            self.assertEqual(env["OMNIROUTE_MODEL"], "from-env")
            self.assertEqual(env["OMNIROUTE_BASE_URL"], "http://file/v1")

    def test_an_empty_export_does_not_erase_a_real_file_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".env").write_text("OMNIROUTE_API_KEY=real-key\n", encoding="utf-8")
            env = check.repo_env(repo, {"OMNIROUTE_API_KEY": ""})
            self.assertEqual(env["OMNIROUTE_API_KEY"], "real-key")

    def test_the_chain_keeps_order_and_drops_duplicates(self) -> None:
        env = {"OMNIROUTE_MODEL": "primary/model", "OMNIROUTE_MODEL_FALLBACK": "auto/coding"}
        self.assertEqual(check.model_chain(env), ["primary/model", "auto/coding"])
        self.assertEqual(check.model_chain({"OMNIROUTE_MODEL": "same", "OMNIROUTE_MODEL_FALLBACK": "same"}), ["same"])

    def test_blank_entries_are_not_models(self) -> None:
        self.assertEqual(check.model_chain({"OMNIROUTE_MODEL": "  ", "OMNIROUTE_MODEL_FALLBACK": ""}), [])

    def test_an_explicit_model_replaces_the_chain(self) -> None:
        env = {"OMNIROUTE_MODEL": "primary/model"}
        self.assertEqual(check.model_chain(env, ["explicit/one", "explicit/one", "explicit/two"]), ["explicit/one", "explicit/two"])


class Verdict(unittest.TestCase):
    def report(self, *outcomes: str) -> "check.Report":
        report = check.Report(base_url="http://gw/v1")
        for index, outcome in enumerate(outcomes):
            report.results.append(check.Result(model=f"m{index}", outcome=outcome))
        return report

    def test_one_usable_model_is_enough(self) -> None:
        # The fallback existing is the whole point of the chain.
        report = self.report(check.OUTCOME_ERROR, check.OUTCOME_TOOL)
        self.assertEqual(report.verdict, "ok")
        self.assertEqual([r.model for r in report.usable], ["m1"])
        self.assertIn("usable", check.render(report))

    def test_a_chain_that_only_breaks_is_not_ok(self) -> None:
        report = self.report(check.OUTCOME_FIRST_TURN_ONLY, check.OUTCOME_PROSE)
        self.assertEqual(report.verdict, "no-usable-model")
        self.assertIn("cannot build", check.render(report))
        self.assertIn("docs/build-model.md", check.render(report))

    def test_an_unanswered_gateway_is_unverified_not_broken(self) -> None:
        # Nothing was learned about the models, and claiming otherwise would send
        # the operator to look at the wrong thing.
        report = self.report(check.OUTCOME_UNREACHABLE)
        self.assertEqual(report.verdict, "unverified")
        self.assertIn("unverified", check.render(report))

    def test_a_broken_chain_alongside_an_unreachable_model_is_still_broken(self) -> None:
        report = self.report(check.OUTCOME_UNREACHABLE, check.OUTCOME_PROSE)
        self.assertEqual(report.verdict, "no-usable-model")

    def test_every_outcome_has_a_meaning_to_report(self) -> None:
        for outcome in (
            check.OUTCOME_TOOL,
            check.OUTCOME_FIRST_TURN_ONLY,
            check.OUTCOME_PROSE,
            check.OUTCOME_EMPTY,
            check.OUTCOME_ERROR,
            check.OUTCOME_UNREACHABLE,
        ):
            self.assertIn(outcome, check.OUTCOME_MEANING)


class ExitCodes(unittest.TestCase):
    """The contract the timer and CI read. Driven without touching a real gateway."""

    def run_main(self, argv: list[str], environ: dict[str, str]) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_missing_configuration_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_main(["--repo", tmp], {})
        self.assertEqual(code, check.EXIT_UNVERIFIED)
        self.assertIn("cannot check", output)

    def test_an_unanswered_gateway_is_unverified(self) -> None:
        # 127.0.0.1:1 is closed, so this is a refused connection and no model is
        # ever reached — the point of the case.
        code, output = self.run_main(
            ["--model", "some/model", "--timeout", "2"],
            {
                "OMNIROUTE_BASE_URL": "http://127.0.0.1:1/v1",
                "OMNIROUTE_API_KEY": "test-key",
            },
        )
        self.assertEqual(code, check.EXIT_UNVERIFIED)
        self.assertIn("unverified", output)

    def test_json_output_carries_the_verdict_and_the_meanings(self) -> None:
        code, output = self.run_main(
            ["--model", "some/model", "--json", "--timeout", "2"],
            {"OMNIROUTE_BASE_URL": "http://127.0.0.1:1/v1", "OMNIROUTE_API_KEY": "test-key"},
        )
        payload = json.loads(output)
        self.assertEqual(code, check.EXIT_UNVERIFIED)
        self.assertEqual(payload["verdict"], "unverified")
        self.assertEqual(payload["results"][0]["outcome"], check.OUTCOME_UNREACHABLE)
        self.assertTrue(payload["results"][0]["meaning"])


if __name__ == "__main__":
    unittest.main()

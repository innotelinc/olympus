#!/usr/bin/env python3
"""Tests for scripts/delivery-evidence.py — did the publish actually publish?

A publish job that returns success proves the runner finished. This script is the
other half: it fetches the name, asks the local state, and classifies the answer.
`classify()` says of itself "pure, so every branch can be asserted", and these are
those assertions — the interesting part is which combinations are *not* the same
problem:

  * the app is running and the name is **missing** from the edge -> one command
    (`make edge-publish SLUG=…`), nothing rebuilt;
  * the app is running and **Cerulean is unreachable** -> the same retry, and the
    verdict says explicitly that nothing needs to be rebuilt (the roadmap asks for
    that, because "publish failed" and "the name was never registered" are not the
    same failure and used to look identical);
  * the app is running and the name **is registered** -> nothing a retry can fix:
    the fault is between them;
  * **no slug at all** (a name outside the publishing suffix, like the gateway) ->
    never "the app is stopped", because there is no app to stop.

v2 adds the chain: when the name does not serve, `gateway-edge-check.py` is asked
*where* it stopped — DNS, TLS or the edge — and that names the broken link in the
detail instead of leaving the reader to re-run the diagnosis by hand.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parents[1] / "delivery-evidence.py"


def _load():
    spec = importlib.util.spec_from_file_location("delivery_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence = _load()


class ClassifyTests(unittest.TestCase):
    """Every branch of the verdict table, including the ones that are not failures."""

    def test_a_name_that_serves_is_done(self):
        outcome = evidence.classify(True, "unknown", "registered", "todo-list", False)
        self.assertEqual((outcome.code, outcome.verdict), (0, "served"))
        self.assertIsNone(outcome.retry)

    def test_served_ignores_the_halves_it_did_not_need(self):
        # The signature allows a served name to arrive with any app/edge reading; the
        # verdict must not depend on what was not asked.
        for app, edge in (("unknown", "registered"), ("running", "missing"), ("stopped", "unreachable")):
            with self.subTest(app=app, edge=edge):
                self.assertEqual(evidence.classify(True, app, edge, "s", False).code, 0)

    def test_app_running_with_the_name_missing_is_one_command(self):
        outcome = evidence.classify(False, "running", "missing", "todo-list", False)
        self.assertEqual((outcome.code, outcome.verdict), (3, "name-missing"))
        self.assertEqual(outcome.retry, "make edge-publish SLUG=todo-list")

    def test_a_preview_registers_at_the_preview_name(self):
        outcome = evidence.classify(False, "running", "missing", "todo-list", True)
        self.assertEqual(outcome.retry, "make edge-preview SLUG=todo-list")
        self.assertIn("not registered at the edge", outcome.detail)

    def test_app_running_with_cerulean_unreachable_is_retryable_and_says_nothing_was_rebuilt(self):
        outcome = evidence.classify(False, "running", "unreachable", "todo-list", False)
        self.assertEqual((outcome.code, outcome.verdict), (3, "edge-unreachable"))
        self.assertEqual(outcome.retry, "make edge-publish SLUG=todo-list")
        self.assertIn("nothing was rebuilt", outcome.detail)
        self.assertIn("nothing needs to be", outcome.detail)

    def test_app_running_and_registered_is_not_a_retry(self):
        # The one case a retry cannot fix, and the only one where the fault is
        # between the two halves rather than in one of them.
        outcome = evidence.classify(False, "running", "registered", "todo-list", False)
        self.assertEqual((outcome.code, outcome.verdict), (1, "registered-but-not-serving"))
        self.assertIsNone(outcome.retry)

    def test_a_stopped_app_needs_the_app_back_not_the_name(self):
        outcome = evidence.classify(False, "stopped", "unknown", "todo-list", False)
        self.assertEqual(outcome.verdict, "app-stopped")
        self.assertEqual(outcome.retry, "make app-publish SLUG=todo-list")
        self.assertIn("not running", outcome.detail)

    def test_a_name_outside_the_suffix_is_never_reported_as_a_stopped_app(self):
        outcome = evidence.classify(False, "unknown", "registered", "", False)
        self.assertEqual((outcome.code, outcome.verdict), (1, "not-a-published-name"))
        self.assertIn("not under this deployment's publishing suffix", outcome.detail)
        self.assertIsNone(outcome.retry)

    def test_an_unreadable_edge_is_reported_as_unknown_not_as_missing(self):
        outcome = evidence.classify(False, "running", "unknown", "todo-list", False)
        self.assertEqual(outcome.verdict, "not-serving")
        self.assertIn("could not be read", outcome.detail)

    def test_no_runtime_record_is_its_own_verdict(self):
        outcome = evidence.classify(False, "absent", "unknown", "todo-list", False)
        self.assertEqual(outcome.verdict, "app-absent")
        self.assertIn("no runtime record exists", outcome.detail)

    def test_a_docker_that_cannot_be_asked_is_not_a_stopped_app(self):
        outcome = evidence.classify(False, "unknown", "unknown", "todo-list", False)
        self.assertEqual(outcome.verdict, "app-unknown")


class ChainTests(unittest.TestCase):
    """The chain walk (v2): which link broke, and never a broken chain on silence."""

    def chain(self, broken: list[str], links: dict | None = None):
        return {
            "ok": not broken,
            "verdict": "something broke",
            "broken": broken,
            "links": links or {name: {"ok": name not in broken, "error": "boom"} for name in
                               ("dns", "tls", "edge")},
        }

    def test_a_broken_chain_names_the_link_in_the_detail(self):
        outcome = evidence.classify(False, "running", "missing", "todo-list", False,
                                    self.chain(["dns"]))
        self.assertIn("the name's chain stops at dns (boom)", outcome.detail)
        # ...and the retry is still the registration, which is not the chain's problem.
        self.assertEqual(outcome.retry, "make edge-publish SLUG=todo-list")

    def test_the_first_broken_link_is_the_one_reported(self):
        outcome = evidence.classify(False, "stopped", "unknown", "s", False,
                                    self.chain(["tls", "edge"]))
        self.assertIn("stops at tls", outcome.detail)
        self.assertNotIn("stops at edge", outcome.detail)

    def test_a_healthy_chain_adds_nothing_to_the_detail(self):
        with_chain = evidence.classify(False, "running", "registered", "s", False,
                                       self.chain([]))
        without = evidence.classify(False, "running", "registered", "s", False, None)
        self.assertEqual(with_chain.detail, without.detail)

    def test_a_chain_that_could_not_be_walked_is_absent_not_clean(self):
        # `chain=None` means the walker could not run. It must not read as a passing
        # chain, and it must not invent a broken link either.
        outcome = evidence.classify(False, "running", "registered", "s", False, None)
        self.assertNotIn("chain", outcome.detail)

    def test_a_broken_link_without_its_own_error_still_names_the_link(self):
        chain = {"ok": False, "verdict": "", "broken": ["edge"], "links": {"edge": {"ok": False}}}
        outcome = evidence.classify(False, "running", "missing", "s", False, chain)
        self.assertIn("the name's chain stops at edge", outcome.detail)
        self.assertNotIn("()", outcome.detail)

    def test_chain_state_returns_none_when_the_walker_cannot_run(self):
        # A missing walker is reported as absent evidence, never as a clean chain.
        original = evidence.load_script
        evidence.load_script = lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(2))
        self.addCleanup(lambda: setattr(evidence, "load_script", original))
        self.assertIsNone(evidence.chain_state("example.invalid", {}))

    def test_chain_state_records_flags_and_the_first_error(self):
        class FakeWalker:
            @staticmethod
            def walk(host, env, port, expect_sso=True):
                self.assertFalse(expect_sso)          # a site serves directly
                return (
                    {"dns": {"ok": True}, "tls": {"ok": False, "error": "certificate expired"},
                     "edge": {"ok": True}},
                    "tls is unusable",
                    1,
                )

        original = evidence.load_script
        evidence.load_script = lambda *_a, **_k: FakeWalker()
        self.addCleanup(lambda: setattr(evidence, "load_script", original))

        chain = evidence.chain_state("site.example.invalid", {})
        self.assertEqual(chain["broken"], ["tls"])
        self.assertFalse(chain["ok"])
        self.assertEqual(chain["links"]["tls"]["error"], "certificate expired")
        self.assertTrue(chain["links"]["dns"]["ok"])


class EvidenceFileTests(unittest.TestCase):
    """What is written, and what is kept across writes."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = {"v": evidence.EVIDENCE_VERSION, "host": "a.example.invalid",
                        "verdict": "served", "served": True}

    def test_a_record_is_written_per_name_and_read_back(self):
        path = evidence.write_evidence(self.root, "a.example.invalid", self.payload)
        self.assertIsNotNone(path)
        self.assertEqual(evidence.read_evidence(self.root, "a.example.invalid")["verdict"],
                         "served")

    def test_the_check_count_accumulates_and_is_kept_on_rewrite(self):
        evidence.write_evidence(self.root, "a.example.invalid", self.payload)
        evidence.write_evidence(self.root, "a.example.invalid", self.payload)
        record = evidence.read_evidence(self.root, "a.example.invalid")
        self.assertEqual(record["checks"], 2)
        # The directory holds one file per name, not one per check: a history nobody
        # reads is a directory that grows on every build.
        self.assertEqual(len(list((self.root / "evidence").iterdir())), 1)

    def test_the_version_is_the_shape_of_the_record(self):
        # v2 is the chain field; a v1 record read back must not be mistaken for one
        # that was walked and found clean.
        self.assertEqual(evidence.EVIDENCE_VERSION, 2)

    def test_no_root_means_nothing_is_written(self):
        self.assertIsNone(evidence.write_evidence(None, "a.example.invalid", self.payload))

    def test_a_corrupt_record_is_absent_not_an_exception(self):
        (self.root / "evidence").mkdir()
        (self.root / "evidence" / "a.example.invalid.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(evidence.read_evidence(self.root, "a.example.invalid"))


class TargetTests(unittest.TestCase):
    """A slug becomes this deployment's own name; a name is taken as given."""

    SUFFIX = "studio.olympus.innotel.us"

    def test_a_slug_becomes_a_published_name(self):
        self.assertEqual(evidence.resolve_target("todo-list", self.SUFFIX, False),
                         ("todo-list.studio.olympus.innotel.us", "todo-list", False))

    def test_a_slug_becomes_a_preview_name(self):
        self.assertEqual(evidence.resolve_target("todo-list", self.SUFFIX, True),
                         ("todo-list-preview.studio.olympus.innotel.us", "todo-list", True))

    def test_a_name_under_the_suffix_reads_its_slug_back(self):
        host, slug, preview = evidence.resolve_target("todo-list-preview.studio.olympus.innotel.us",
                                                      self.SUFFIX, False)
        self.assertEqual((host, slug, preview),
                         ("todo-list-preview.studio.olympus.innotel.us", "todo-list", True))

    def test_a_name_outside_the_suffix_has_no_slug_to_look_up(self):
        host, slug, _preview = evidence.resolve_target("gateway.olympus.innotel.us",
                                                       self.SUFFIX, False)
        self.assertEqual(host, "gateway.olympus.innotel.us")
        self.assertEqual(slug, "")

    def test_a_trailing_dot_and_capitals_do_not_change_the_answer(self):
        self.assertEqual(evidence.resolve_target("TODO-LIST.", self.SUFFIX, False),
                         ("todo-list.studio.olympus.innotel.us", "todo-list", False))


if __name__ == "__main__":
    unittest.main()

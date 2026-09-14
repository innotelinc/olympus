#!/usr/bin/env python3
"""Tests for scripts/site-check.py.

The check's whole job is the verdict, and its interesting cases are the names behind
the identity provider: they answer 307/302 on purpose and were reported FAILED by the
version this replaced. So what is pinned here is that a gate is *named* rather than
counted as a fault, and that everything which is not a gate still fails — a redirect
to somewhere else, a 200 that is not a page, and a name that answers nothing at all.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SITE_CHECK_PATH = Path(__file__).resolve().parent.parent / "site-check.py"

spec = importlib.util.spec_from_file_location("site_check", SITE_CHECK_PATH)
assert spec and spec.loader
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

ENV = (
    "OIDC_ISSUER_URL=https://auth.cerulean.innotel.us/application/o/studio/\n"
    "GATEWAY_OIDC_ISSUER_URL=https://auth.cerulean.innotel.us/application/o/omniroute/\n"
)
ISSUERS = {"auth.cerulean.innotel.us"}


def result(**overrides) -> dict:
    base = {"code": 200, "final": "https://site.studio.olympus.innotel.us/", "body": "<html><script>x</script></html>"}
    base.update(overrides)
    return base


class IssuerHosts(unittest.TestCase):
    def test_both_issuer_keys_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(ENV, encoding="utf-8")
            self.assertEqual(site.issuer_hosts(site.load_env(path)), ISSUERS)

    def test_the_path_of_an_issuer_is_not_part_of_the_comparison(self) -> None:
        # Authentik's issuer path is the application slug; the host is what a redirect
        # is matched on, and two applications share a host.
        self.assertEqual(
            site.issuer_hosts({"OIDC_ISSUER_URL": "https://auth.example.test/application/o/x/"}),
            {"auth.example.test"},
        )

    def test_no_issuer_configured_is_an_empty_set_not_a_crash(self) -> None:
        self.assertEqual(site.issuer_hosts({}), set())
        self.assertEqual(site.issuer_hosts({"OIDC_ISSUER_URL": ""}), set())


class Verdicts(unittest.TestCase):
    HOST = "site.studio.olympus.innotel.us"

    def check(self, payload: dict):
        return site.verdict(payload, self.HOST, ISSUERS)

    def test_a_built_page_is_a_pass(self) -> None:
        ok, note, failure = self.check(result())
        self.assertTrue(ok)
        self.assertIsNone(failure)
        self.assertIn("built page", note)

    def test_the_marker_check_is_case_insensitive(self) -> None:
        self.assertTrue(self.check(result(body="<HTML><BODY>hi</BODY></HTML>"))[0])

    def test_a_200_that_is_not_a_page_still_fails(self) -> None:
        # The edge's own error page answers 200 on some configurations, which is the
        # difference the original check existed to catch.
        ok, _, failure = self.check(result(body="ok"))
        self.assertFalse(ok)
        self.assertIn("not with a page", failure)

    def test_a_redirect_to_the_identity_provider_is_a_pass_and_names_it(self) -> None:
        ok, note, failure = self.check(
            result(code=200, final="https://auth.cerulean.innotel.us/application/o/authorize/?client_id=studio")
        )
        self.assertTrue(ok, failure)
        self.assertIn("auth-gated", note)
        self.assertIn("auth.cerulean.innotel.us", note)

    def test_a_redirect_somewhere_else_fails_and_names_where(self) -> None:
        # "it redirects somewhere" is not an answer to "does it serve".
        ok, _, failure = self.check(result(code=200, final="https://elsewhere.test/landing"))
        self.assertFalse(ok)
        self.assertIn("elsewhere.test", failure)

    def test_a_redirect_that_never_leaves_the_name_fails(self) -> None:
        # Studio's first hop is its own /api/auth/login. Without the IdP landing at
        # the end of the chain, that is a loop, not a gate.
        ok, _, failure = self.check(result(code=307, final="https://site.studio.olympus.innotel.us/api/auth/login"))
        self.assertFalse(ok)
        self.assertIn("307", failure)

    def test_a_non_200_from_the_name_is_a_failure_with_the_code(self) -> None:
        for code in (404, 500, 502, 503):
            ok, _, failure = self.check(result(code=code))
            self.assertFalse(ok, code)
            self.assertIn(str(code), failure)

    def test_no_answer_is_a_failure_naming_the_reason(self) -> None:
        ok, _, failure = self.check({"error": "TLSV1_UNRECOGNIZED_NAME"})
        self.assertFalse(ok)
        self.assertIn("TLSV1_UNRECOGNIZED_NAME", failure)

    def test_an_issuer_that_is_not_configured_means_the_gate_is_not_assumed(self) -> None:
        # Without an issuer there is nothing to recognise a gate by, so a redirect off
        # the name is reported as a failure — the old behaviour, kept for the case
        # where the answer is genuinely unknown rather than merely unexpected.
        ok, _, failure = site.verdict(
            result(code=200, final="https://auth.cerulean.innotel.us/application/o/authorize/"),
            self.HOST,
            set(),
        )
        self.assertFalse(ok)
        self.assertIn("not the configured identity provider", failure)


class HostMatching(unittest.TestCase):
    def test_the_host_comes_out_lowercase_and_bare(self) -> None:
        self.assertEqual(site.host_of("https://Auth.Cerulean.Innotel.us/application/o/x/"), "auth.cerulean.innotel.us")

    def test_a_trailing_dot_on_the_asked_name_still_matches(self) -> None:
        # `https://host./` is what a client with a search domain sends, and it is the
        # same name — treating it as "a different host" would report a gate on a site.
        ok, _, _ = site.verdict(
            result(final="https://site.studio.olympus.innotel.us/"),
            "site.studio.olympus.innotel.us.",
            ISSUERS,
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

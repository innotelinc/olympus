"""Tests for scripts/studio-sites.py.

Only the pure half is asserted — slug validation, name derivation, and which
certificate a name may borrow. The network half (Cerulean, NPM) is exercised by
`make sites-wildcard` and `make site-publish` against the live deployment, where a
failure is visible as a name that does not answer.

The two things worth pinning, because both fail silently otherwise:

  * a slug becomes a DNS label. Accepting one that is not a valid label gets a
    certificate covering a name nobody can resolve, which reads as "DNS is broken".
  * a wildcard covers exactly one label. Being permissive here attaches a
    certificate to a name it does not carry, which the edge serves as a browser
    warning rather than an error anyone reads.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sites = load("studio_sites", "studio-sites.py")
api = load("cerulean_api", "cerulean_api.py")

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class Config:
    """A stand-in for what `Config` reads out of `.env`."""

    suffix = "studio.olympus.innotel.us"
    port = 20130


class SlugTests(unittest.TestCase):
    def test_accepts_labels(self) -> None:
        for value in ("todo-list", "a", "weight-tracker-2", "x9"):
            self.assertEqual(sites.normalise_slug(value), value, value)

    def test_folds_case_rather_than_rejecting_it(self) -> None:
        # DNS is case-insensitive, so this is not a rewrite into a different name.
        self.assertEqual(sites.normalise_slug("Todo-List"), "todo-list")

    def test_refuses_what_cannot_be_a_label(self) -> None:
        for value in ("", "  ", "-leading", "trailing-", "under_score", "a.b", "a/b", "..", "ünicode"):
            with self.assertRaises(SystemExit, msg=repr(value)):
                sites.normalise_slug(value)

    def test_refuses_a_label_that_is_too_long(self) -> None:
        self.assertEqual(len(sites.normalise_slug("a" * 63)), 63)
        with self.assertRaises(SystemExit):
            sites.normalise_slug("a" * 64)


class HostnameTests(unittest.TestCase):
    def test_derives_the_name_from_the_slug(self) -> None:
        self.assertEqual(
            sites.hostname_for(Config(), "todo-list"),
            "todo-list.studio.olympus.innotel.us",
        )

    def test_a_bad_slug_never_becomes_a_name(self) -> None:
        with self.assertRaises(SystemExit):
            sites.hostname_for(Config(), "../../etc/passwd")


class HostSuffixTests(unittest.TestCase):
    def test_matches_only_names_under_the_suffix(self) -> None:
        self.assertTrue(sites.host_suffix({"domain_names": ["a.studio.olympus.innotel.us"]}, Config()))
        self.assertFalse(sites.host_suffix({"domain_names": ["gateway.olympus.innotel.us"]}, Config()))
        # A prefix is not a suffix: this is the string `--list` must not claim.
        self.assertFalse(
            sites.host_suffix({"domain_names": ["studio.olympus.innotel.us.evil.test"]}, Config())
        )


class CertificateCoverageTests(unittest.TestCase):
    def test_a_wildcard_covers_one_label(self) -> None:
        self.assertTrue(
            api.certificate_covers(["*.studio.olympus.innotel.us"], "a.studio.olympus.innotel.us")
        )

    def test_a_wildcard_does_not_cover_the_bare_base(self) -> None:
        self.assertFalse(
            api.certificate_covers(["*.studio.olympus.innotel.us"], "studio.olympus.innotel.us")
        )

    def test_a_wildcard_does_not_cover_two_labels(self) -> None:
        self.assertFalse(
            api.certificate_covers(["*.studio.olympus.innotel.us"], "a.b.studio.olympus.innotel.us")
        )

    def test_an_exact_name_always_covers(self) -> None:
        self.assertTrue(api.certificate_covers(["a.example.test"], "a.example.test"))

    def test_the_wildcard_flag_is_read_even_when_the_name_list_omits_it(self) -> None:
        # Cerulean records a wildcard as `domain: <base>` + `wildcard: true`, and
        # its `domains` list does not always spell the `*.` out. Trusting the list
        # alone sends every publish back down the slow per-name issuance path.
        cert = {
            "id": 17,
            "domain": "studio.olympus.innotel.us",
            "wildcard": True,
            "domains": ["studio.olympus.innotel.us"],
            "status": "issued",
            "hasMaterial": True,
            "expiresAt": "Dec 12 14:06:46 2026 GMT",
        }
        chosen = api.select_certificate([cert], "todo.studio.olympus.innotel.us", NOW)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], 17)

    def test_a_near_expiry_is_not_reused(self) -> None:
        cert = {
            "id": 17,
            "domain": "studio.olympus.innotel.us",
            "wildcard": True,
            "domains": ["*.studio.olympus.innotel.us"],
            "status": "issued",
            "hasMaterial": True,
            "expiresAt": (NOW + timedelta(days=3)).strftime("%b %d %H:%M:%S %Y GMT"),
        }
        self.assertIsNone(api.select_certificate([cert], "x.studio.olympus.innotel.us", NOW))

    def test_an_issued_row_without_material_is_not_reused(self) -> None:
        cert = {
            "id": 17,
            "domain": "studio.olympus.innotel.us",
            "wildcard": True,
            "domains": ["*.studio.olympus.innotel.us"],
            "status": "issued",
            "hasMaterial": False,
            "expiresAt": "Dec 12 14:06:46 2026 GMT",
        }
        self.assertIsNone(api.select_certificate([cert], "x.studio.olympus.innotel.us", NOW))


class CertificateRequestTests(unittest.TestCase):
    def test_a_wildcard_is_requested_as_a_base_plus_a_flag(self) -> None:
        # Cerulean's validator for `domain` is /^[a-z0-9.-]+$/, so a literal `*`
        # there is rejected with "Invalid domain name" — measured, after the
        # obvious request failed. The flag is what makes the certificate wild.
        body = api._certificate_body("*.studio.olympus.innotel.us")
        self.assertEqual(body["domain"], "studio.olympus.innotel.us")
        self.assertIs(body["wildcard"], True)
        self.assertEqual(body["name"], "*.studio.olympus.innotel.us")

    def test_a_plain_name_is_requested_as_itself(self) -> None:
        body = api._certificate_body("gateway.olympus.innotel.us")
        self.assertEqual(body["domain"], "gateway.olympus.innotel.us")
        self.assertNotIn("wildcard", body)


if __name__ == "__main__":
    unittest.main()

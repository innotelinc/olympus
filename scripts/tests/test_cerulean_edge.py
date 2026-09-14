#!/usr/bin/env python3
"""Tests for scripts/cerulean-edge.py.

This script writes to a live DNS zone and a live TLS edge for names that other
services answer on. The decisions worth pinning are therefore the refusals:

* a record whose value does not match is never silently repointed, because that
  is how one service takes another's traffic;
* a certificate is only reused when it actually has material and is comfortably
  inside its validity, because an `issued` row with no PEM is what a failed
  export leaves behind;
* a placeholder is never accepted as a credential, because the platform's own
  process environment carries the .env.example one and it shadows the real value.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

EDGE_PATH = Path(__file__).resolve().parent.parent / "cerulean-edge.py"

spec = importlib.util.spec_from_file_location("cerulean_edge", EDGE_PATH)
assert spec and spec.loader
edge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def certificate(**overrides) -> dict:
    base = {
        "id": 16,
        "domain": "gateway.olympus.innotel.us",
        "domains": ["gateway.olympus.innotel.us"],
        "status": "issued",
        "hasMaterial": True,
        "expiresAt": "Dec 12 12:24:25 2026 GMT",
    }
    base.update(overrides)
    return base


class Placeholders(unittest.TestCase):
    def test_the_template_password_is_not_a_credential(self) -> None:
        # Measured: this is the exact value the platform's process environment
        # exports, and it shadowed a working .env into an HTTP 401.
        self.assertFalse(edge.is_usable("change-me-cerulean-admin"))

    def test_other_template_values_are_not_credentials(self) -> None:
        for value in ("", "change-me", "changeme", "change-password", "change-me-admin", " ADMIN ", "Password"):
            self.assertFalse(edge.is_usable(value), f"{value!r} was treated as a credential")

    def test_a_real_value_is_a_credential(self) -> None:
        for value in ("Tr0ub4dor&3", "innotel!1", "a" * 24):
            self.assertTrue(edge.is_usable(value), f"{value!r} was rejected")


class RelativeName(unittest.TestCase):
    def test_the_zone_is_stripped_without_touching_the_rest(self) -> None:
        self.assertEqual(edge._relative("gateway.olympus.innotel.us", "innotel.us"), "gateway.olympus")

    def test_a_name_outside_the_zone_is_left_alone(self) -> None:
        # Better to hand Technitium a name it will reject than to mangle one it
        # might accept under the wrong zone.
        self.assertEqual(edge._relative("gateway.other.test", "innotel.us"), "gateway.other.test")

    def test_trailing_dots_and_case_do_not_matter(self) -> None:
        self.assertEqual(edge._relative("Gateway.Olympus.Innotel.us.", "innotel.us."), "gateway.olympus")


class SelectRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {"name": "studio.olympus.innotel.us", "type": "CNAME", "rData": "innotel.us"},
            {"name": "gateway.olympus.innotel.us", "type": "CNAME", "rData": "innotel.us"},
            {"name": "gateway.olympus.innotel.us", "type": "TXT", "rData": "something-else"},
        ]

    def test_finds_the_record_by_fqdn_and_type(self) -> None:
        found = edge.select_record(self.records, "gateway.olympus.innotel.us", "CNAME")
        self.assertIsNotNone(found)
        self.assertEqual(edge.record_value(found), "innotel.us")

    def test_the_type_has_to_match(self) -> None:
        # A TXT row must not satisfy a request for the CNAME — otherwise the
        # CNAME never gets created and the name silently never resolves.
        self.assertIsNone(edge.select_record(self.records, "gateway.olympus.innotel.us", "A"))

    def test_a_different_name_is_not_a_match(self) -> None:
        self.assertIsNone(edge.select_record(self.records, "other.olympus.innotel.us", "CNAME"))

    def test_the_value_reader_accepts_both_spellings(self) -> None:
        self.assertEqual(edge.record_value({"rData": "innotel.us."}), "innotel.us")
        self.assertEqual(edge.record_value({"value": "Innotel.US"}), "innotel.us")
        self.assertEqual(edge.record_value({}), "")


class ParseExpiry(unittest.TestCase):
    def test_the_c_asctime_shape_cerulean_reports(self) -> None:
        parsed = edge.parse_expiry("Dec 12 12:24:25 2026 GMT")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_anything_else_is_unknown_rather_than_guessed(self) -> None:
        # The caller's fallback (request a new certificate) is cheaper than
        # trusting a date that was never understood.
        for value in ("", "2026-12-12T12:24:25Z", "garbage", "Dec 12 12:24:25"):
            self.assertIsNone(edge.parse_expiry(value), f"{value!r} parsed unexpectedly")

    def test_a_named_zone_other_than_gmt_is_still_understood(self) -> None:
        # `%Z` accepts the zones the platform knows, so a provider that renders
        # `UTC` rather than `GMT` must not be read as "no expiry" — that would
        # reissue a certificate that is perfectly valid.
        for value in ("Dec 12 12:24:25 2026 GMT", "Dec 12 12:24:25 2026 UTC"):
            parsed = edge.parse_expiry(value)
            self.assertIsNotNone(parsed, f"{value!r} did not parse")
            self.assertEqual(parsed.hour, 12)


class SelectCertificate(unittest.TestCase):
    def test_a_healthy_certificate_is_reused(self) -> None:
        chosen = edge.select_certificate([certificate()], "gateway.olympus.innotel.us", NOW)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], 16)

    def test_status_alone_is_not_enough_without_material(self) -> None:
        # This is the row a failed export to NPM leaves behind.
        self.assertIsNone(
            edge.select_certificate([certificate(hasMaterial=False)], "gateway.olympus.innotel.us", NOW)
        )

    def test_a_non_issued_row_is_not_reused(self) -> None:
        self.assertIsNone(edge.select_certificate([certificate(status="issuing")], "gateway.olympus.innotel.us", NOW))

    def test_a_certificate_about_to_expire_is_reissued(self) -> None:
        soon = (NOW + timedelta(days=5)).strftime("%b %d %H:%M:%S %Y GMT")
        self.assertIsNone(
            edge.select_certificate([certificate(expiresAt=soon)], "gateway.olympus.innotel.us", NOW)
        )

    def test_the_cover_can_come_from_the_domain_field_alone(self) -> None:
        self.assertIsNotNone(
            edge.select_certificate(
                [certificate(domains=[], domain="gateway.olympus.innotel.us")],
                "gateway.olympus.innotel.us",
                NOW,
            )
        )

    def test_an_unrelated_certificate_is_ignored(self) -> None:
        self.assertIsNone(
            edge.select_certificate(
                [certificate(domain="other.test", domains=["other.test"])],
                "gateway.olympus.innotel.us",
                NOW,
            )
        )

    def test_when_several_cover_the_name_the_longest_lived_one_wins(self) -> None:
        later = (NOW + timedelta(days=120)).strftime("%b %d %H:%M:%S %Y GMT")
        earlier = (NOW + timedelta(days=40)).strftime("%b %d %H:%M:%S %Y GMT")
        chosen = edge.select_certificate(
            [certificate(id=1, expiresAt=earlier), certificate(id=2, expiresAt=later)],
            "gateway.olympus.innotel.us",
            NOW,
        )
        self.assertEqual(chosen["id"], 2)

    def test_a_wildcard_does_not_cover_two_labels(self) -> None:
        # The limit of a wildcard, and the reason this cannot be a substring test:
        # `*.olympus.innotel.us` covers one label, not `a.b.olympus.innotel.us`.
        # Getting this wrong in the permissive direction attaches a certificate to
        # a name it does not carry, which the edge serves as a browser warning.
        wildcard = certificate(domain="*.olympus.innotel.us", domains=["*.olympus.innotel.us"])
        self.assertIsNone(
            edge.select_certificate([wildcard], "a.b.olympus.innotel.us", NOW)
        )

    def test_a_wildcard_does_not_cover_the_bare_name(self) -> None:
        wildcard = certificate(domain="*.olympus.innotel.us", domains=["*.olympus.innotel.us"])
        self.assertIsNone(edge.select_certificate([wildcard], "olympus.innotel.us", NOW))

    def test_a_wildcard_is_reused_when_it_is_the_only_cover(self) -> None:
        # The change that makes publishing a site instant: without wildcard reuse,
        # every published name requests its own certificate and "instant" is a
        # minute of waiting per site.
        wildcard = certificate(
            id=7, domain="*.studio.olympus.innotel.us", domains=["*.studio.olympus.innotel.us"]
        )
        chosen = edge.select_certificate([wildcard], "todo-list.studio.olympus.innotel.us", NOW)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], 7)

    def test_a_dedicated_certificate_beats_a_wildcard(self) -> None:
        # A wildcard is a legitimate fallback and a poor default: it couples one
        # name's certificate to every other name it covers.
        wildcard = certificate(
            id=1, domain="*.studio.olympus.innotel.us", domains=["*.studio.olympus.innotel.us"]
        )
        dedicated = certificate(
            id=2, domain="todo.studio.olympus.innotel.us", domains=["todo.studio.olympus.innotel.us"]
        )
        chosen = edge.select_certificate(
            [wildcard, dedicated], "todo.studio.olympus.innotel.us", NOW
        )
        self.assertEqual(chosen["id"], 2)

    def test_the_longer_validity_wins_within_a_tier(self) -> None:
        near = certificate(id=1, domain="*.studio.olympus.innotel.us", domains=["*.studio.olympus.innotel.us"])
        far = certificate(id=2, domain="*.studio.olympus.innotel.us", domains=["*.studio.olympus.innotel.us"])
        far["expiresAt"] = "Mar 01 00:00:00 2027 GMT"
        chosen = edge.select_certificate([near, far], "x.studio.olympus.innotel.us", NOW)
        self.assertEqual(chosen["id"], 2)


class UnusableDnsSession(unittest.TestCase):
    """The 500 that repeats no matter how many times you obey its instruction.

    Measured on this platform: `make gateway-edge` and `make site-publish` both died on
    `Technitium session expired for /api/zones/records/get — retry`, and retrying was
    the one thing that could not work — a static token was configured, and Cerulean
    uses a configured token as-is with no re-login on `invalid-token`.
    """

    def test_the_retry_instruction_is_replaced_with_what_actually_fixes_it(self) -> None:
        hint = edge.unusable_dns_session("Technitium session expired for /api/zones/records/get — retry")
        self.assertIsNotNone(hint)
        self.assertIn("will not fix it", hint)
        # Both credentials, because the platform's app now falls back from the token to
        # the login: this 500 is what arrives when neither of them worked, and naming
        # only the token would send the operator to the half that was already tried.
        self.assertIn("TECHNITIUM_USER/TECHNITIUM_PASSWORD", hint)
        self.assertIn("recreate its app container", hint)

    def test_it_also_catches_the_error_in_a_structured_payload(self) -> None:
        self.assertIsNotNone(edge.unusable_dns_session({"error": {"message": "Technitium session expired"}}))

    def test_any_other_failure_is_left_to_the_script_that_raised_it(self) -> None:
        # A fabricated diagnosis is worse than none: this must not swallow a 404 from a
        # zone that is not registered, or a Cerulean that is simply down.
        for payload in ("Zone innotel.us is not registered in Cerulean", {"error": "fetch failed"}, ""):
            self.assertIsNone(edge.unusable_dns_session(payload), payload)


if __name__ == "__main__":
    unittest.main()

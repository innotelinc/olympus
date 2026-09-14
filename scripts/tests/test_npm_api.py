#!/usr/bin/env python3
"""Tests for scripts/npm_api.py — the one writer of an edge `advanced_config`.

This module exists because Cerulean's NPM passthrough reports success and drops the
field, so the tests that matter are about honesty and blast radius:

* the rule is read back after the write, because "the request was accepted" and "the
  edge is enforcing it" came apart here once already;
* a PUT carries the host as it was read, because NPM replaces the object and a
  partial body resets whatever it does not name — TLS enforcement, websockets, the
  certificate, all of which this host depends on;
* nothing is written when the config already says what was asked for.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

NPM_API_PATH = Path(__file__).resolve().parent.parent / "npm_api.py"

spec = importlib.util.spec_from_file_location("npm_api", NPM_API_PATH)
assert spec and spec.loader
npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(npm)

HOST = {
    "id": 198,
    "domain_names": ["gateway.olympus.innotel.us"],
    "forward_scheme": "http",
    "forward_host": "192.168.1.10",
    "forward_port": 20129,
    "certificate_id": 174,
    "ssl_forced": True,
    "http2_support": True,
    "allow_websocket_upgrade": True,
    "block_exploits": True,
    "hsts_enabled": True,
    "advanced_config": "",
    "locations": [],
    "access_list_id": 0,
    "meta": {},
    "created_on": "2026-09-01T00:00:00.000Z",
    "modified_on": "2026-09-01T00:00:00.000Z",
    "owner_user_id": 3,
}


class FakeTransport(npm.NpmApi):
    """NpmApi with the socket replaced by a scripted host table.

    `stores` decides whether a PUT is persisted, which is how the read-back check is
    driven: a write that is accepted and forgotten is exactly the behaviour Cerulean's
    passthrough showed on this field.
    """

    def __init__(self, hosts: list[dict], stores: bool = True) -> None:
        super().__init__("http://npm.test", "admin@test", "secret")
        self.hosts = [dict(host) for host in hosts]
        self.stores = stores
        self.calls: list[tuple[str, str, dict | None]] = []

    def call(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
        self.calls.append((path, method, body))
        if path == "/api/tokens":
            return 200, {"token": "test-token"}
        if method == "GET":
            return 200, self.hosts
        if method == "PUT" and self.stores:
            for host in self.hosts:
                if f"/{host['id']}" in path:
                    host.update(body or {})
        return 200, body or {}

    def writes(self) -> list[dict]:
        return [call[2] for call in self.calls if call[1] == "PUT"]


class ClosedPathsConfig(unittest.TestCase):
    def test_a_path_is_refused_with_and_without_its_trailing_slash(self) -> None:
        # `^~ /v1/` does not match `/v1`, and the bare path is where a client lands
        # when it drops the suffix — the one case worth being exact about.
        snippet = npm.closed_paths_config(["/v1"])
        self.assertIn("location ^~ /v1/ { return 403; }", snippet)
        self.assertIn("location = /v1 { return 403; }", snippet)

    def test_slashes_and_a_bare_name_arrive_at_the_same_rule(self) -> None:
        self.assertEqual(npm.closed_paths_config(["/v1"]), npm.closed_paths_config(["v1/"]))

    def test_the_whole_site_is_not_a_path_this_will_close(self) -> None:
        # Closing "/" would be a name that answers nothing — that is `sites-down`,
        # not something to write into a live host by accident.
        self.assertEqual(npm.closed_paths_config(["/"]), "")
        self.assertEqual(npm.closed_paths_config([]), "")

    def test_the_report_reads_the_paths_back_out_of_the_snippet(self) -> None:
        # A log line that says "refuses /v1" while the config says something else is
        # the failure mode this module was written for.
        self.assertEqual(
            npm.refused_paths(npm.closed_paths_config(["/v1", "/api/internal"])),
            "refuses /v1, /api/internal at the edge",
        )


class PayloadFields(unittest.TestCase):
    def test_the_rest_of_the_host_is_carried_through(self) -> None:
        # The reset this guards against is invisible: the host keeps working while
        # websockets, TLS enforcement or the certificate quietly fall off it.
        body = npm.payload_fields(HOST, "location ^~ /v1/ { return 403; }")
        for field in ("allow_websocket_upgrade", "ssl_forced", "hsts_enabled", "block_exploits", "http2_support"):
            self.assertEqual(body[field], True, field)
        self.assertEqual(body["certificate_id"], 174)
        self.assertEqual(body["forward_host"], "192.168.1.10")

    def test_read_only_fields_are_not_sent_back(self) -> None:
        body = npm.payload_fields(HOST, "location ^~ /v1/ { return 403; }")
        for field in ("id", "created_on", "modified_on", "owner_user_id"):
            self.assertNotIn(field, body)

    def test_a_detached_certificate_is_not_sent_as_null(self) -> None:
        # `certificate_id: None` is not "leave it"; it is "detach it", and this host
        # exists to be the TLS front of the dashboard.
        host = dict(HOST, certificate_id=None, access_list_id=None)
        body = npm.payload_fields(host, "location ^~ /v1/ { return 403; }")
        self.assertNotIn("certificate_id", body)
        self.assertNotIn("access_list_id", body)


class SetAdvancedConfig(unittest.TestCase):
    def snippet(self) -> str:
        return npm.closed_paths_config(["/v1"])

    def test_a_config_that_already_says_it_is_left_alone(self) -> None:
        api = FakeTransport([dict(HOST, advanced_config=self.snippet())])
        report = api.set_advanced_config(api.hosts[0], self.snippet())
        self.assertIn("already refuses /v1", report)
        self.assertEqual(api.writes(), [])

    def test_a_door_that_drifted_open_is_closed_again(self) -> None:
        api = FakeTransport([dict(HOST)])
        report = api.set_advanced_config(api.hosts[0], self.snippet())
        self.assertEqual(len(api.writes()), 1, report)
        self.assertIn("refuses /v1 at the edge", report)

    def test_a_write_that_is_accepted_and_forgotten_is_a_failure(self) -> None:
        # Measured behaviour of the passthrough this file replaced: HTTP 200, and the
        # edge still open. Reporting success there is the defect, so it exits instead.
        # `sys.exit(str)` prints to the C-level stderr, which a Python-level redirect
        # does not capture — so the message is asserted off the exception, which is
        # the same string the operator sees.
        api = FakeTransport([dict(HOST)], stores=False)
        with self.assertRaises(SystemExit) as caught:
            api.set_advanced_config(api.hosts[0], self.snippet())
        self.assertIn("is not on the edge", str(caught.exception.code))

    def test_a_dry_run_writes_nothing(self) -> None:
        api = FakeTransport([dict(HOST)])
        report = api.set_advanced_config(api.hosts[0], self.snippet(), dry_run=True)
        self.assertIn("would set advanced_config", report)
        self.assertEqual(api.writes(), [])

    def test_an_empty_advanced_config_is_not_a_no_op_reason_to_skip_the_read_back(self) -> None:
        # The comparison is on the snippet, not on "is there anything there": a host
        # that has some *other* config is rewritten to the one that was asked for.
        api = FakeTransport([dict(HOST, advanced_config="location /x { return 418; }")])
        api.set_advanced_config(api.hosts[0], self.snippet())
        self.assertEqual(api.writes()[0]["advanced_config"], self.snippet())


class ProxyHostLookup(unittest.TestCase):
    def test_the_name_is_matched_exactly(self) -> None:
        # A substring match would let `gateway.olympus.innotel.us.example` decide that
        # the gateway's name is published.
        api = FakeTransport([dict(HOST, domain_names=["evil-gateway.olympus.innotel.us.test"])])
        self.assertIsNone(api.proxy_host("gateway.olympus.innotel.us"))

    def test_a_trailing_dot_and_case_do_not_matter(self) -> None:
        api = FakeTransport([dict(HOST, domain_names=["Gateway.Olympus.Innotel.us."])])
        self.assertEqual(api.proxy_host("gateway.olympus.innotel.us")["id"], 198)


class Login(unittest.TestCase):
    def test_a_refused_login_says_which_credential(self) -> None:
        class Refused(npm.NpmApi):
            def call(self, path, method="GET", body=None, timeout=30):
                return 401, "Unauthorized"

        api = Refused("http://npm.test", "admin@test", "wrong")
        with self.assertRaises(SystemExit) as caught:
            api.login()
        self.assertIn("NPM_EMAIL / NPM_PASSWORD", str(caught.exception.code))


if __name__ == "__main__":
    unittest.main()

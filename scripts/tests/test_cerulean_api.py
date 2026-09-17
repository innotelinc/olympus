#!/usr/bin/env python3
"""Tests for scripts/cerulean_api.py — how an unattended caller authenticates.

The client used to have exactly one way in: `POST /api/auth/login` with the admin
password. Cerulean then moved its people to Authentik and made that password
break-glass, so the publishing path kept working right up until it didn't — the
failure was `403 Password sign-in is disabled`, which names a flag and not the
problem, and it happened *after* a build had already been packaged.

What is pinned here:

* every path the client calls has a service-bridge equivalent, asserted by
  reading the module's own source rather than a hand-kept list, so a new call
  site that the bridge cannot serve fails here instead of at a publish;
* the credential choice is the service key when there is one, and the
  break-glass password only when there is not;
* a refusable configuration is refused before any request is made.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

API_PATH = Path(__file__).resolve().parent.parent / "cerulean_api.py"

spec = importlib.util.spec_from_file_location("cerulean_api", API_PATH)
assert spec and spec.loader
api_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_module)


class Paths(unittest.TestCase):
    """The bridge translation, and the guard that it stays complete."""

    def test_the_session_paths_the_client_calls_all_translate(self) -> None:
        for path in (
            "/api/certificates",
            "/api/certificates/20",
            "/api/domains",
            "/api/npm/export-cert",
            "/api/npm/hosts",
            "/api/npm/hosts/79",
        ):
            translated = api_module.service_path(path)
            self.assertTrue(translated.startswith("/api/service/"), path)
            self.assertEqual(translated, "/api/service" + path[len("/api") :])

    def test_a_path_with_no_bridge_route_is_refused_rather_than_passed_through(
        self,
    ) -> None:
        # Passing it through would send it to a session-only route, which answers
        # `401 Unauthorized` and says nothing about why.
        with self.assertRaises(SystemExit) as caught:
            api_module.service_path("/api/vault/secrets")
        self.assertIn("no service-bridge equivalent", str(caught.exception))

    def test_every_path_written_in_this_module_is_reachable(self) -> None:
        """The list above is not the contract — the module is.

        A call site added to `cerulean_api.py` that the bridge does not serve is
        a publish that dies at the last step, so it is checked by reading the
        source for `/api/...` literals.
        """
        source = API_PATH.read_text(encoding="utf-8")
        literals = set(re.findall(r'"(/api/[A-Za-z0-9/_{}-]*)"', source))
        self.assertTrue(literals, "no API paths found — did the pattern go stale?")

        # Two kinds of literal are not call sites to translate: the break-glass
        # sign-in, which has no bridge equivalent and must stay that way (a key
        # that could mint a session would be a key that could do everything,
        # forever), and the bridge paths themselves, which are this table's
        # targets rather than inputs to it.
        session_only = {path for path in literals if path.startswith("/api/service/")}
        session_only |= {"/api/auth/login"}
        for path in sorted(literals - session_only):
            # `{zone_id}` stands in for an id and `{zone_id}`-style templates are
            # the records route, which `Api.path_for` rewrites before this.
            concrete = path.replace("{zone_id}", "1")
            if concrete.endswith("/records"):
                continue
            with self.subTest(path=path):
                self.assertTrue(
                    api_module.service_path(concrete).startswith("/api/service/"),
                    f"{path} has no bridge route",
                )

    def test_a_key_asking_for_zone_records_is_rewritten_to_the_zone_form(self) -> None:
        client = api_module.Api("http://cerulean.test:3003")
        client.use_service_key("ceru_0123456789abcdef_" + "a" * 24)
        client.zone = "innotel.us"
        self.assertEqual(
            client.path_for("/api/domains/3/records"),
            "/api/service/dns/records?zone=innotel.us",
        )

    def test_a_session_client_uses_the_session_paths(self) -> None:
        client = api_module.Api("http://cerulean.test:3003")
        self.assertEqual(client.path_for("/api/domains/3/records"), "/api/domains/3/records")
        self.assertEqual(client.path_for("/api/npm/hosts"), "/api/npm/hosts")


class Credentials(unittest.TestCase):
    """Which way in, and what happens when neither is usable."""

    def setUp(self) -> None:
        self.settings = {
            "CERULEAN_DNS_API_URL": "http://cerulean.test:3003",
            "CERULEAN_ZONE": "innotel.us",
            "CERULEAN_API_TOKEN": "ceru_0123456789abcdef_" + "b" * 24,
            "CERULEAN_ADMIN_PASSWORD": "",
        }

    def test_a_service_key_is_the_way_in(self) -> None:
        client = api_module.connect_client(
            self.settings["CERULEAN_DNS_API_URL"],
            token=self.settings["CERULEAN_API_TOKEN"],
        )
        self.assertTrue(client.service)
        self.assertEqual(client.token, self.settings["CERULEAN_API_TOKEN"])

    def test_something_that_is_not_a_service_key_is_named_rather_than_sent(self) -> None:
        # A command line that `vault://…`-resolves to the wrong thing, or a
        # password pasted into the token field, otherwise arrives as a 401.
        with self.assertRaises(SystemExit) as caught:
            api_module.connect_client("http://cerulean.test:3003", token="hunter2")
        self.assertIn("ceru_", str(caught.exception))

    def test_a_lone_password_still_signs_in(self) -> None:
        # Patched rather than called: the assertion is which way in was chosen,
        # and a test that needs a Cerulean host to answer is a test that fails on
        # a laptop.
        with mock.patch.object(api_module.Api, "login") as login:
            client = api_module.connect_client(
                "http://cerulean.test:3003", password="break-glass"
            )
        login.assert_called_once_with("break-glass")
        self.assertFalse(client.service)

    def test_a_service_key_is_enough_on_its_own(self) -> None:
        api_module.require_cerulean(Path("/tmp/.env"), self.settings)

    def test_the_url_and_zone_are_still_required(self) -> None:
        for name in ("CERULEAN_DNS_API_URL", "CERULEAN_ZONE"):
            settings = dict(self.settings)
            settings[name] = ""
            with self.assertRaises(SystemExit) as caught:
                api_module.require_cerulean(Path("/tmp/.env"), settings)
            self.assertIn(name, str(caught.exception))

    def test_no_credential_at_all_is_refused_with_the_thing_to_do_about_it(
        self,
    ) -> None:
        settings = dict(self.settings, CERULEAN_API_TOKEN="")
        with self.assertRaises(SystemExit) as caught:
            api_module.require_cerulean(Path("/tmp/.env"), settings)
        message = str(caught.exception)
        self.assertIn("CERULEAN_API_TOKEN", message)
        self.assertIn("BREAKGLASS_LOGIN", message)

    def test_the_zone_override_wins_over_the_file(self) -> None:
        values = api_module.read_cerulean_settings(
            Path("/tmp/.env"), set(), zone_override="example.test"
        )
        self.assertEqual(values["CERULEAN_ZONE"], "example.test")


class RecordBody(unittest.TestCase):
    """The bridge is zone-addressed; the session path is id-addressed."""

    class Stub:
        def __init__(self, service: bool) -> None:
            self.service = service
            self.zone = "innotel.us"
            self.calls: list[tuple[str, str, dict | None]] = []

        def call(self, path, method="GET", body=None, timeout=60):
            self.calls.append((path, method, body))
            if method == "GET":
                return 200, []
            return 201, {"ok": True}

    def test_the_bridge_body_carries_the_zone(self) -> None:
        client = self.Stub(service=True)
        report = api_module.ensure_record(
            client, 3, "site.studio.innotel.us", "site.studio", "A", "192.0.2.10", False
        )
        sent = [body for _, method, body in client.calls if method == "POST"][0]
        self.assertEqual(sent["zone"], "innotel.us")
        self.assertIn("created", report)

    def test_the_session_body_does_not(self) -> None:
        client = self.Stub(service=False)
        api_module.ensure_record(
            client, 3, "site.studio.innotel.us", "site.studio", "A", "192.0.2.10", False
        )
        sent = [body for _, method, body in client.calls if method == "POST"][0]
        self.assertNotIn("zone", sent)


if __name__ == "__main__":
    unittest.main()

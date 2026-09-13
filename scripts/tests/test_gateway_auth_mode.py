"""Tests for scripts/gateway-auth-mode.py.

The script does one security-relevant thing — it turns the gateway's own
authentication off — so the cases that matter here are the ones where it must
*refuse*, and the ordering of the two settings it touches.

Two things are deliberately tested against a real HTTP server rather than a mock:

1. **`currentPassword` travels with the PATCH.** OmniRoute answers
   `400 PASSWORD_REQUIRED` without it, and it is the guard that stops a hijacked
   session from opening the dashboard. A mock that accepted the call would pass
   whether or not the script sent it.
2. **The result is re-read rather than trusted.** A PATCH that returns 200 while
   the flag stays true is exactly the silent no-op this script must not have.

`loopback_binding` is the check the whole change rests on, so every branch of it
is asserted — including the "cannot tell" case, which must not be mistaken for
"safe".
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "gateway_auth_mode", HERE.parent / "gateway-auth-mode.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gateway_auth_mode"] = module
    spec.loader.exec_module(module)
    return module


auth_mode = load_module()

PASSWORD = "a-management-password"


class FakeGateway:
    """Just enough of the gateway: a settings object, a login, and the PATCH guard."""

    def __init__(self, require_login: bool = True, apply_patch: bool = True, ungates: bool = True) -> None:
        self.settings = {"requireLogin": require_login, "hasPassword": True}
        self.password = PASSWORD
        self.apply_patch = apply_patch      # False simulates a PATCH that changes nothing
        self.ungates = ungates              # False simulates a gateway that still gates itself
        self.patches: list[dict] = []
        self.logins: list[dict] = []
        self.sessions: set[str] = set()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _handler(inner):  # noqa: N805 - a factory, named for readability at the call site
        outer = inner

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: ANN002 - silence the test output
                pass

            def _json(self, status: int, payload: dict, cookies: list[str] | None = None) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                for cookie in cookies or []:
                    self.send_header("set-cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def _signed_in(self) -> bool:
                cookie = self.headers.get("cookie", "")
                return any(token in cookie for token in outer.sessions)

            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                if self.path == "/api/settings":
                    if outer.settings.get("requireLogin") is True and not self._signed_in():
                        return self._json(401, {"error": {"code": "AUTH_001", "message": "Authentication required"}})
                    return self._json(200, {"settings": outer.settings})

                if self.path == "/api/providers":
                    gated = outer.settings.get("requireLogin") is not False or not outer.ungates
                    if gated and not self._signed_in():
                        return self._json(401, {"error": {"code": "AUTH_001", "message": "Authentication required"}})
                    return self._json(200, {"connections": [], "total": 0})

                return self._json(404, {"error": {"message": "not found"}})

            def do_PATCH(self) -> None:  # noqa: N802 - http.server's naming
                # The settings route is PATCH, and BaseHTTPRequestHandler implements
                # no method it was not told about — it answers 501, which would look
                # like the script failing rather than the fake being incomplete.
                self.do_POST()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")

                if self.path == "/api/auth/login":
                    outer.logins.append(payload)
                    if payload.get("password") != outer.password:
                        return self._json(401, {"error": "Invalid password"})
                    token = f"auth_token=token{len(outer.sessions)}"
                    outer.sessions.add(token)
                    return self._json(200, {"success": True}, cookies=[f"{token}; Path=/; HttpOnly"])

                if self.path == "/api/settings":
                    if not self._signed_in():
                        return self._json(401, {"error": {"code": "AUTH_001", "message": "Authentication required"}})
                    outer.patches.append(payload)
                    # The guard this script has to satisfy.
                    if "currentPassword" not in payload and "requireLogin" in payload:
                        return self._json(400, {"error": {"code": "PASSWORD_REQUIRED",
                                                          "message": "currentPassword required",
                                                          "keys": ["requireLogin"]}})
                    if outer.apply_patch:
                        outer.settings.update({k: v for k, v in payload.items() if k != "currentPassword"})
                    return self._json(200, {"settings": outer.settings})

                return self._json(404, {"error": {"message": "not found"}})

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = auth_mode.main(argv)
    return code, out.getvalue(), err.getvalue()


class UnmanagedTests(unittest.TestCase):
    """What the script intends to change, as a pure function."""

    def test_require_login_true_is_the_thing_it_turns_off(self) -> None:
        self.assertEqual(auth_mode.unmanaged({"requireLogin": True}), {"requireLogin": False})

    def test_already_off_is_nothing_to_do(self) -> None:
        self.assertEqual(auth_mode.unmanaged({"requireLogin": False}), {})

    def test_an_absent_key_is_a_change_not_a_pass(self) -> None:
        # A gateway with no `requireLogin` at all behaves as if it were required, so
        # treating the missing key as "already correct" would report success on a
        # gateway that still asks for a password.
        self.assertEqual(auth_mode.unmanaged({}), {"requireLogin": False})

    def test_it_touches_nothing_else(self) -> None:
        # A PATCH here merges, so a key this script has no opinion about must never
        # appear in its payload.
        settings = {"requireLogin": True, "comboStrategy": "fallback", "stickyRoundRobinLimit": 3}
        self.assertEqual(set(auth_mode.unmanaged(settings)), {"requireLogin"})


class BindingTests(unittest.TestCase):
    """The check the change rests on: is the gateway reachable beyond this host?"""

    def binding(self, ports, docker=True, returncode=0):
        with mock.patch.object(auth_mode.shutil, "which", return_value="/usr/bin/docker" if docker else None):
            with mock.patch.object(
                auth_mode.subprocess, "run",
                return_value=mock.Mock(returncode=returncode, stdout=json.dumps(ports)),
            ):
                return auth_mode.loopback_binding()

    def test_loopback_only_is_safe(self) -> None:
        bound, note = self.binding({"20128/tcp": [{"HostIp": "127.0.0.1", "HostPort": "20128"}]})
        self.assertTrue(bound)
        self.assertIn("127.0.0.1", note)

    def test_ipv6_loopback_is_also_safe(self) -> None:
        self.assertTrue(self.binding({"20128/tcp": [{"HostIp": "::1", "HostPort": "20128"}]})[0])

    def test_all_interfaces_is_not_safe(self) -> None:
        # An empty HostIp is Docker's spelling of 0.0.0.0 — the case this exists for.
        bound, note = self.binding({"20128/tcp": [{"HostIp": "", "HostPort": "20128"}]})
        self.assertFalse(bound)
        self.assertIn("all interfaces", note)

    def test_an_explicit_wildcard_is_not_safe(self) -> None:
        self.assertFalse(self.binding({"20128/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20128"}]})[0])

    def test_one_wide_binding_out_of_two_is_not_safe(self) -> None:
        # Half-open is open.
        bound, _ = self.binding({
            "20128/tcp": [{"HostIp": "127.0.0.1", "HostPort": "20128"}],
            "20129/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20129"}],
        })
        self.assertFalse(bound)

    def test_no_published_port_at_all_is_not_safe(self) -> None:
        bound, note = self.binding({})
        self.assertFalse(bound)
        self.assertIn("publishes no TCP port", note)

    def test_no_docker_is_unknown_rather_than_fine(self) -> None:
        bound, note = self.binding({}, docker=False)
        self.assertIsNone(bound)
        self.assertIn("not available", note)

    def test_a_container_that_is_not_running_is_unknown(self) -> None:
        bound, note = self.binding({}, returncode=1)
        self.assertIsNone(bound)
        self.assertIn(auth_mode.GATEWAY_CONTAINER, note)


class VaultReferenceTests(unittest.TestCase):
    def env(self, **overrides):
        base = {
            "OMNIROUTE_INITIAL_PASSWORD": "vault://cerulean/olympus#INITIAL_PASSWORD",
            "VAULT_ADDR": "http://vault.example:8200",
            "VAULT_TOKEN": "s.token",
        }
        base.update(overrides)
        return base

    def secret(self, env, payload):
        body = json.dumps(payload).encode()
        with mock.patch.object(auth_mode.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = body
            with mock.patch.object(auth_mode.json, "load", return_value=payload):
                return auth_mode.vault_secret(env)

    def test_reads_the_named_field_from_kv_v2(self) -> None:
        value = self.secret(self.env(), {"data": {"data": {"INITIAL_PASSWORD": "from-vault", "STACK": "x"}}})
        self.assertEqual(value, "from-vault")

    def test_a_reference_without_a_field_still_works(self) -> None:
        value = self.secret(
            self.env(OMNIROUTE_INITIAL_PASSWORD="vault://cerulean/olympus"),
            {"data": {"data": {"INITIAL_PASSWORD": "from-vault"}}},
        )
        self.assertEqual(value, "from-vault")

    def test_a_plain_value_is_not_a_vault_reference(self) -> None:
        self.assertEqual(auth_mode.vault_secret(self.env(OMNIROUTE_INITIAL_PASSWORD="hunter2")), "")

    def test_no_address_or_token_is_empty_not_an_error(self) -> None:
        self.assertEqual(auth_mode.vault_secret(self.env(VAULT_ADDR="")), "")
        self.assertEqual(auth_mode.vault_secret(self.env(VAULT_TOKEN="", VAULT_TOKEN_FILE="/nonexistent")), "")

    def test_a_missing_field_is_empty(self) -> None:
        self.assertEqual(self.secret(self.env(), {"data": {"data": {"STACK": "x"}}}), "")


class RefusalTests(unittest.TestCase):
    """Where it must not proceed, and must not have touched anything."""

    def test_a_non_loopback_url_is_refused(self) -> None:
        code, _, err = run(["--url", "http://192.168.1.10:20128", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("not loopback", err)

    def test_a_gateway_published_on_all_interfaces_is_refused(self) -> None:
        # The scenario the guard exists for, and the assertion that matters is the
        # second one: it refused *before* changing anything.
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding",
                               return_value=(False, "published on 0.0.0.0 (all interfaces)")):
            code, _, err = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 2)
        self.assertIn("publish its management API", err)
        self.assertEqual(server.patches, [], "the gateway was modified despite the refusal")
        self.assertEqual(server.settings["requireLogin"], True)

    def test_an_unreachable_gateway_is_not_a_silent_success(self) -> None:
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, _, err = run(["--url", "http://127.0.0.1:1", "--password", PASSWORD])
        self.assertEqual(code, 2)
        self.assertIn("is it up", err)

    def test_no_password_anywhere_cannot_authenticate_and_says_so(self) -> None:
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            with mock.patch.object(auth_mode, "vault_secret", return_value=""):
                code, _, err = run(["--url", server.url, "--env-file", "/nonexistent/.env"])

        self.assertEqual(code, 2)
        self.assertIn("no management password", err)
        self.assertEqual(server.patches, [])

    def test_a_rejected_password_stops_before_any_change(self) -> None:
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, _, err = run(["--url", server.url, "--password", "wrong-password"])

        self.assertEqual(code, 2)
        self.assertIn("rejected", err)
        self.assertEqual(server.patches, [])

    def test_an_unverifiable_binding_warns_but_proceeds(self) -> None:
        # Unlike a *known* wide binding, not being able to look is not evidence of
        # one — so it proceeds and says what it could not check.
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(None, "no docker here")):
            code, out, _ = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertIn("warning", out)
        self.assertEqual(server.settings["requireLogin"], False)


class ApplyTests(unittest.TestCase):
    def test_the_password_travels_with_the_change(self) -> None:
        # OmniRoute refuses the change without it, so this is the difference between
        # working and a 400 — and it is the guard against a hijacked session.
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, out, _ = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertEqual(len(server.patches), 1)
        self.assertEqual(server.patches[0]["requireLogin"], False)
        self.assertEqual(server.patches[0]["currentPassword"], PASSWORD)
        self.assertEqual(server.settings["requireLogin"], False)
        self.assertIn("Authentik is now the only gate", out)

    def test_a_flipped_flag_is_not_reported_as_success(self) -> None:
        # A 200 that changes nothing is the failure mode this script exists to
        # catch, so the result is re-read instead of trusted.
        server = FakeGateway(apply_patch=False)
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, _, err = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 1)
        self.assertIn("still", err)

    def test_a_gateway_that_keeps_gating_itself_is_a_failure(self) -> None:
        # The flag is off but the management API still demands a session, so the
        # proxy is not actually the only gate and reporting success would be wrong.
        server = FakeGateway(ungates=False)
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, _, err = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 1)
        self.assertIn("still gating itself", err)

    def test_it_is_idempotent(self) -> None:
        server = FakeGateway(require_login=False)
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, out, _ = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertIn("already in Authentik-only mode", out)
        self.assertEqual(server.patches, [])

    def test_dry_run_reports_and_writes_nothing(self) -> None:
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, out, _ = run(["--url", server.url, "--dry-run", "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertIn("--dry-run", out)
        self.assertEqual(server.patches, [])
        self.assertEqual(server.settings["requireLogin"], True)

    def test_verify_fails_while_the_gateway_still_gates_itself(self) -> None:
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, _, err = run(["--url", server.url, "--verify", "--password", PASSWORD])

        self.assertEqual(code, 1)
        self.assertIn("still requires its own login", err)
        self.assertEqual(server.patches, [], "--verify must not change anything")

    def test_verify_passes_once_it_is_the_only_gate(self) -> None:
        server = FakeGateway(require_login=False)
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, out, _ = run(["--url", server.url, "--verify", "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertIn("already in Authentik-only mode", out)

    def test_the_password_is_resolved_from_vault_when_not_passed(self) -> None:
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            with mock.patch.object(auth_mode, "vault_secret", return_value=PASSWORD) as secret:
                code, _, _ = run(["--url", server.url, "--env-file", "/nonexistent/.env"])

        self.assertEqual(code, 0)
        secret.assert_called_once()
        self.assertEqual(server.logins[0]["password"], PASSWORD)

    def test_the_password_is_never_printed(self) -> None:
        # It arrives from Vault and is sent in a request body; it must not appear in
        # the report an operator pastes into an issue.
        server = FakeGateway()
        self.addCleanup(server.close)
        with mock.patch.object(auth_mode, "loopback_binding", return_value=(True, "loopback")):
            code, out, err = run(["--url", server.url, "--password", PASSWORD])

        self.assertEqual(code, 0)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(PASSWORD, err)


if __name__ == "__main__":
    unittest.main()

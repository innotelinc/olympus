"""Tests for scripts/gateway-edge-check.py.

The point of the check is the *diagnosis*, so that is what is asserted: which link
is named broken, and the sentence that comes with it. A check that cannot fail is
not a check, so the failure cases get at least as much attention here as the
passing ones — and the DNS parser is fed synthetic packets rather than trusted,
because a response is untrusted input and the script's whole job is to report
rather than to crash.

What is deliberately not tested: anything that needs the network outside this
process. `check_tls`, `check_edge` and `check_proxy` are thin wrappers over
sockets; the decisions they make are split into pure functions (`edge_verdict`,
`parse_response`, `exit_code`) and those are asserted here.

The session store is the exception, and it gets a real socket: a fake redis on a
loopback port, because the failure it exists to catch was a WIRE-LEVEL mistake in
this very file — `AUTH <password> PING` sent as one three-element array is not two
commands, it is one malformed AUTH, and redis answers `-WRONGPASS`. That reads
exactly like a wrong password and is not one. A test that mocked the socket would
have missed it, so the fake server decodes the wire format and asserts on the
commands it actually received.
"""

from __future__ import annotations

import importlib.util
import io
import socket
import struct
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "gateway_edge_check", HERE.parent / "gateway-edge-check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gateway_edge_check"] = module
    spec.loader.exec_module(module)
    return module


check = load_module()


# --- building DNS packets -----------------------------------------------------


def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"


def packet(rcode: int = 0, answers: list[tuple[int, bytes]] | None = None, question: str = "host.example") -> bytes:
    answers = answers or []
    header = struct.pack(">HHHHHH", 0x1234, 0x8180 | rcode, 1, len(answers), 0, 0)
    body = encode_name(question) + struct.pack(">HH", 1, 1)
    for rtype, rdata in answers:
        # Compression pointer back to the question name, which is what a real
        # resolver sends and what the parser has to follow.
        body += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
    return header + body


def a_record(address: str) -> tuple[int, bytes]:
    import socket

    return 1, socket.inet_aton(address)


def cname_record(target: str) -> tuple[int, bytes]:
    return 5, encode_name(target)


class ParserTests(unittest.TestCase):
    def test_reads_a_cname_and_the_address_it_points_at(self) -> None:
        data = packet(
            answers=[
                cname_record("innotel.us"),
                a_record("73.68.203.71"),
            ]
        )
        result = check.parse_response(data)

        self.assertEqual(result["rcode"], 0)
        self.assertEqual(result["name"], "NOERROR")
        self.assertEqual(result["cname"], "innotel.us")
        self.assertEqual(result["addresses"], ["73.68.203.71"])

    def test_reads_nxdomain(self) -> None:
        result = check.parse_response(packet(rcode=3))
        self.assertEqual(result["rcode"], 3)
        self.assertEqual(result["name"], "NXDOMAIN")
        self.assertEqual(result["addresses"], [])

    def test_reads_aaaa(self) -> None:
        import socket

        data = packet(answers=[(28, socket.inet_pton(socket.AF_INET6, "2001:db8::1"))])
        self.assertEqual(check.parse_response(data)["addresses"], ["2001:db8::1"])

    def test_a_truncated_packet_is_an_error_not_a_crash(self) -> None:
        # Every one of these is a thing a broken or hostile resolver can send, and
        # the script must report them rather than traceback.
        for data in (b"", b"\x00" * 8, packet()[:20], packet()[:-3]):
            with self.assertRaises(check.DnsError, msg=repr(data[:16])) as raised:
                check.parse_response(data)
            self.assertTrue(str(raised.exception))

    def test_a_header_with_no_records_is_an_empty_answer_not_an_error(self) -> None:
        # A 12-byte header is a well-formed response with nothing in it. It is not a
        # parse failure — it is an answer that resolved nothing, which is what
        # `check_dns` marks unresolved, and conflating the two would report a
        # malformed packet for a server that simply had nothing to say.
        result = check.parse_response(b"\x00" * 12)
        self.assertEqual(result["rcode"], 0)
        self.assertEqual(result["addresses"], [])

    def test_an_oversized_label_is_refused(self) -> None:
        # A label length above 63 is not a length, it is a compression pointer that
        # did not set its top bits — reading it as a label would run off the packet.
        data = packet() + b"\x40" + b"x" * 10
        with self.assertRaises(check.DnsError):
            check.read_name(data, len(packet()))

    def test_a_pointer_loop_terminates(self) -> None:
        # A pointer that points at itself. Without a hop limit this is an infinite
        # loop inside the check that is supposed to detect outages.
        data = bytearray(packet())
        offset = len(data)
        data += b"\xc0" + bytes([offset])  # points at itself
        with self.assertRaises(check.DnsError, msg="pointer loop") as raised:
            check.read_name(bytes(data), offset)
        self.assertIn("loop", str(raised.exception))

    def test_a_response_with_no_answers_is_reported_as_empty_not_missing(self) -> None:
        result = check.parse_response(packet())
        self.assertEqual(result["addresses"], [])
        self.assertEqual(result["rcode"], 0)


class EdgeVerdictTests(unittest.TestCase):
    """The one decision with a security consequence."""

    def test_a_redirect_to_the_idp_is_the_healthy_answer(self) -> None:
        ok, note, failure = check.edge_verdict(
            302, "https://auth.cerulean.innotel.us/application/o/authorize/?client_id=omniroute", True
        )
        self.assertTrue(ok)
        self.assertIsNone(failure)
        self.assertIn("identity provider", note)

    def test_being_served_the_dashboard_is_a_failure(self) -> None:
        # The reason this deployment exists: a 200 here means the identity-aware
        # proxy is not in the path and the dashboard answers unauthenticated. It has
        # to fail, and it has to say that rather than "unexpected status".
        ok, _, failure = check.edge_verdict(200, "", True)
        self.assertFalse(ok)
        self.assertIn("unauthenticated", failure)

    def test_a_non_redirect_is_a_failure(self) -> None:
        for status in (404, 502, 503):
            ok, _, failure = check.edge_verdict(status, "", True)
            self.assertFalse(ok, status)
            self.assertIn(str(status), failure)

    def test_a_redirect_somewhere_else_is_not_an_idp_redirect(self) -> None:
        ok, _, failure = check.edge_verdict(302, "https://example.com/", True)
        self.assertFalse(ok)
        self.assertIn("302", failure)

    def test_without_sso_a_2xx_is_the_expected_answer(self) -> None:
        self.assertTrue(check.edge_verdict(200, "", False)[0])
        self.assertTrue(check.edge_verdict(204, "", False)[0])
        # A redirect is not a weaker pass when the name is meant to serve.
        self.assertFalse(check.edge_verdict(302, "https://elsewhere/", False)[0])
        self.assertFalse(check.edge_verdict(404, "", False)[0])


class ExitCodeTests(unittest.TestCase):
    def links(self, **states: bool) -> dict:
        return {name: {"ok": ok} for name, ok in states.items()}

    def test_every_link_ok_is_zero(self) -> None:
        self.assertEqual(check.exit_code(self.links(dns=True, tls=True, edge=True, proxy=True)), 0)

    def test_any_broken_link_is_non_zero(self) -> None:
        for broken in ("dns", "tls", "edge", "proxy"):
            states = {"dns": True, "tls": True, "edge": True, "proxy": True}
            states[broken] = False
            self.assertEqual(check.exit_code(self.links(**states)), 1, broken)

    def test_a_warning_does_not_fail_the_check(self) -> None:
        # A resolver that did not answer while another did is reported, not failed:
        # the question this answers is whether the name is reachable.
        dns = {"ok": True, "warning": "192.168.1.99 did not answer"}
        links = {"dns": dns, "tls": {"ok": True}, "edge": {"ok": True}, "proxy": {"ok": True}}
        self.assertEqual(check.exit_code(links), 0)


class DiagnosisTests(unittest.TestCase):
    def links(self, dns, tls, edge, proxy) -> dict:
        return {"dns": dns, "tls": tls, "edge": edge, "proxy": proxy}

    def ok(self) -> dict:
        return {"ok": True}

    def test_nxdomain_says_the_record_is_missing(self) -> None:
        dns = {"ok": False, "answers": [{"resolver": "system", "server": "1.1.1.1", "rcode": 3, "name": "NXDOMAIN"}]}
        verdict = check.diagnose(self.links(dns, self.ok(), self.ok(), self.ok()), "gateway.example")
        self.assertIn("does not exist in the zone", verdict)
        self.assertIn("make gateway-edge", verdict)

    def test_a_silent_resolver_is_reported_as_the_resolver_not_the_name(self) -> None:
        # The finding that matters most in practice: this zone's own servers live on
        # the same host as the edge, so an outage there reads as a broken name.
        dns = {
            "ok": False,
            "answers": [
                {"resolver": "system", "server": "127.0.0.53", "error": "no answer within 5s"},
                {"resolver": "cerulean", "server": "192.168.1.46", "error": "no answer within 5s"},
            ],
        }
        verdict = check.diagnose(self.links(dns, self.ok(), self.ok(), self.ok()), "gateway.example")
        self.assertIn("no resolver answered", verdict)
        self.assertIn("192.168.1.46", verdict)
        self.assertIn("another network", verdict)

    def test_a_broken_certificate_is_named_and_dns_is_absolved(self) -> None:
        tls = {"ok": False, "error": "certificate rejected: certificate has expired"}
        verdict = check.diagnose(self.links(self.ok(), tls, self.ok(), self.ok()), "gateway.example")
        self.assertIn("certificate", verdict)
        self.assertNotIn("does not exist", verdict)

    def test_an_unreachable_edge_says_dns_is_not_the_problem(self) -> None:
        edge = {"ok": False, "error": "no answer from https://gateway.example/: timed out"}
        verdict = check.diagnose(self.links(self.ok(), self.ok(), edge, self.ok()), "gateway.example")
        self.assertIn("DNS is not the problem", verdict)

    def test_a_dead_proxy_says_the_name_is_fine(self) -> None:
        proxy = {"ok": False, "error": "http://127.0.0.1:20129/ping did not answer: refused"}
        verdict = check.diagnose(self.links(self.ok(), self.ok(), self.ok(), proxy), "gateway.example")
        self.assertIn("The name is fine; the container is not", verdict)

    def test_everything_healthy_says_so(self) -> None:
        verdict = check.diagnose(self.links(self.ok(), self.ok(), self.ok(), self.ok()), "gateway.example")
        self.assertIn("reachable and gated", verdict)

    def test_the_first_broken_link_is_the_one_named(self) -> None:
        # Repairing a chain from the far end is guesswork, so only the first one is
        # reported even when several are down.
        dns = {"ok": False, "answers": [{"resolver": "system", "server": "1.1.1.1", "rcode": 3}]}
        tls = {"ok": False, "error": "could not connect"}
        verdict = check.diagnose(self.links(dns, tls, self.ok(), self.ok()), "gateway.example")
        self.assertIn("does not exist in the zone", verdict)
        self.assertNotIn("could not connect", verdict)


class ConfigTests(unittest.TestCase):
    def test_the_platform_resolver_comes_from_the_cerulean_url(self) -> None:
        self.assertEqual(
            check.configured_resolver({"CERULEAN_DNS_API_URL": "http://192.168.1.46:3003"}),
            "192.168.1.46",
        )
        self.assertEqual(
            check.configured_resolver({"CERULEAN_DNS_API_URL": "https://dns.example"}),
            "dns.example",
        )
        self.assertEqual(check.configured_resolver({}), "")

    def test_env_parsing_ignores_comments_and_keeps_quoted_values(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# a comment\n"
                "GATEWAY_PUBLIC_HOST=gateway.olympus.innotel.us\n"
                'GATEWAY_SSO_PORT="20129"\n'
                "\n",
                encoding="utf-8",
            )
            env = check.load_env(path)

        self.assertEqual(env["GATEWAY_PUBLIC_HOST"], "gateway.olympus.innotel.us")
        self.assertEqual(env["GATEWAY_SSO_PORT"], "20129")
        self.assertNotIn("# a comment", env)

    def test_a_missing_env_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(check.load_env(Path("/nonexistent/.env")), {})


# --- a fake redis, so the wire format is asserted and not assumed ---------------


def parse_commands(buffer: bytes) -> tuple[list[list[str]], bytes]:
    """Split a RESP byte stream into complete commands. Returns (commands, rest).

    Deliberately strict, and deliberately in the test: if this cannot parse what
    `redis_encode` produced, the encoder is wrong.
    """
    commands: list[list[str]] = []
    while buffer.startswith(b"*"):
        head, _, rest = buffer.partition(b"\r\n")
        count = int(head[1:])
        arguments: list[str] = []
        buffer = rest
        for _ in range(count):
            size_line, _, rest = buffer.partition(b"\r\n")
            if not size_line.startswith(b"$"):
                raise AssertionError(f"expected a bulk string, got {size_line!r}")
            size = int(size_line[1:])
            if len(rest) < size + 2:
                raise AssertionError("truncated bulk string")
            arguments.append(rest[:size].decode())
            buffer = rest[size + 2 :]
        commands.append(arguments)
    return commands, buffer


class FakeRedis:
    """A redis that answers AUTH and PING and records what it was sent."""

    def __init__(self, password: str = "s3cret", accept_auth: bool = True) -> None:
        self.password = password
        self.accept_auth = accept_auth
        self.commands: list[list[str]] = []
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._server.accept()
        with connection:
            buffer = b""
            while True:
                chunk = connection.recv(1024)
                if not chunk:
                    return
                received, buffer = parse_commands(buffer + chunk)
                for arguments in received:
                    self.commands.append(arguments)
                    connection.sendall(self._reply(arguments))

    def _reply(self, arguments: list[str]) -> bytes:
        if arguments[0] == "AUTH":
            if not self.accept_auth or arguments[-1] != self.password:
                return b"-WRONGPASS invalid username-password pair or user is disabled.\r\n"
            return b"+OK\r\n"
        if arguments[0] == "PING":
            return b"+PONG\r\n"
        return b"-ERR unknown command\r\n"

    def close(self) -> None:
        self._thread.join(timeout=5)
        self._server.close()


class RedisWireTests(unittest.TestCase):
    """The mistake that cost an afternoon, held down by a test."""

    def test_one_command_is_one_array(self) -> None:
        self.assertEqual(check.redis_encode("PING"), b"*1\r\n$4\r\nPING\r\n")
        self.assertEqual(check.redis_encode("AUTH", "hunter2"), b"*2\r\n$4\r\nAUTH\r\n$7\r\nhunter2\r\n")

    def test_concatenated_commands_parse_as_separate_commands(self) -> None:
        # The distinction that matters: `*3` carrying AUTH, a password and PING is
        # ONE command, and redis answers it with -WRONGPASS.
        both = check.redis_encode("AUTH", "hunter2") + check.redis_encode("PING")
        commands, rest = parse_commands(both)
        self.assertEqual(commands, [["AUTH", "hunter2"], ["PING"]])
        self.assertEqual(rest, b"")

        one_array = check.redis_encode("AUTH", "hunter2", "PING")
        commands, _ = parse_commands(one_array)
        self.assertEqual(len(commands), 1, "this is the shape that fails")
        self.assertEqual(len(commands[0]), 3)

    def test_auth_and_the_command_are_sent_as_two_commands(self) -> None:
        server = FakeRedis(password="hunter2")
        try:
            reply = check.redis_command(server.port, "hunter2", "PING")
        finally:
            server.close()

        self.assertEqual(server.commands, [["AUTH", "hunter2"], ["PING"]])
        self.assertEqual(reply, "+PONG")

    def test_a_wrong_password_is_returned_rather_than_the_ping_reply(self) -> None:
        # If the AUTH reply were skipped, a caller could read the PING reply of an
        # unauthenticated connection and call the store healthy.
        server = FakeRedis(password="hunter2")
        try:
            reply = check.redis_command(server.port, "wrong", "PING")
        finally:
            server.close()

        self.assertTrue(reply.startswith("-WRONGPASS"), reply)
        self.assertEqual(server.commands, [["AUTH", "wrong"]])

    def test_no_password_means_no_auth_command(self) -> None:
        server = FakeRedis(password="hunter2")
        try:
            check.redis_command(server.port, "", "PING")
        finally:
            server.close()
        self.assertEqual(server.commands, [["PING"]])


class SessionStoreTests(unittest.TestCase):
    """The link that cannot be reached with curl, and the regression it covers."""

    def session(self, store: str | None, note: str = "", password: str = "hunter2", accept: bool = True):
        server = FakeRedis(password=password, accept_auth=accept)
        self.addCleanup(server.close)
        env = {"GATEWAY_SSO_REDIS_PORT": str(server.port), "GATEWAY_SSO_REDIS_PASSWORD": password}
        with mock.patch.object(check, "proxy_session_store", return_value=(store, note)):
            return check.check_session(env), server

    def test_a_healthy_store_passes_and_says_where_sessions_live(self) -> None:
        payload, server = self.session("redis")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["store"], "redis")
        self.assertEqual(payload["port"], server.port)
        self.assertNotIn("error", payload)

    def test_cookie_sessions_are_reported_as_the_502_they_cause(self) -> None:
        # The whole reason this link exists. A cookie session overflows on an
        # identity with many groups, the edge's buffer is smaller than the resulting
        # Set-Cookie headers, and the login callback gets a 502 that names nothing.
        payload, _ = self.session("cookie")
        self.assertFalse(payload["ok"])
        self.assertIn("session-store-type=cookie", payload["error"])
        self.assertIn("502", payload["error"])

    def test_a_dead_store_names_the_consequence_not_just_the_port(self) -> None:
        # Nothing listening: bind a port, then close it, so it is definitely free.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        env = {"GATEWAY_SSO_REDIS_PORT": str(port), "GATEWAY_SSO_REDIS_PASSWORD": "x"}
        with mock.patch.object(check, "proxy_session_store", return_value=("redis", "")):
            payload = check.check_session(env)

        self.assertFalse(payload["ok"])
        self.assertIn(str(port), payload["error"])
        self.assertIn("logins cannot complete", payload["error"])

    def test_a_password_mismatch_is_not_reported_as_a_dead_store(self) -> None:
        # Redis answers a wrong password with an error reply rather than dropping the
        # connection, so the two cases are distinguishable and must read differently:
        # the fixes are "check the store" and "check the secret".
        payload, server = self.session("redis", password="hunter2", accept=False)
        self.assertFalse(payload["ok"])
        self.assertIn("GATEWAY_SSO_REDIS_PASSWORD", payload["error"])
        self.assertIn(str(server.port), payload["error"])

    def test_an_unreadable_store_config_is_a_warning_not_a_bare_pass(self) -> None:
        # Without docker the proxy's own configuration cannot be read, so only half
        # the question was answered. Passing is right; passing silently is not.
        payload, _ = self.session(None, note="not verified — no docker on this host")
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["store"])
        self.assertIn("no docker", payload["warning"])

    def test_the_store_is_read_from_the_running_proxy_not_from_the_file(self) -> None:
        # The container is the thing serving traffic; `.env` is what somebody meant.
        def run(argv, **_kwargs):
            self.assertEqual(argv[:3], ["docker", "inspect", check.SSO_CONTAINER])
            return mock.Mock(returncode=0, stdout='["oauth2-proxy","--session-store-type=redis","--provider=oidc"]')

        with mock.patch.object(check.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(check.subprocess, "run", side_effect=run):
                self.assertEqual(check.proxy_session_store(), ("redis", ""))

    def test_cookie_sessions_are_recognised_from_the_container_command(self) -> None:
        with mock.patch.object(check.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(
                check.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout='["oauth2-proxy","--session-store-type=cookie"]'),
            ):
                self.assertEqual(check.proxy_session_store(), ("cookie", ""))

    def test_a_command_with_no_session_flag_is_unreadable_not_assumed_cookie(self) -> None:
        # The default IS cookie, but inferring it from a flag that is not there
        # would report a regression on the strength of a missing line.
        with mock.patch.object(check.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(
                check.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='["oauth2-proxy"]')
            ):
                store, note = check.proxy_session_store()
        self.assertIsNone(store)
        self.assertIn("could not be read", note)

    def test_a_missing_container_is_reported_as_such(self) -> None:
        with mock.patch.object(check.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(check.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
                store, note = check.proxy_session_store()
        self.assertIsNone(store)
        self.assertIn(check.SSO_CONTAINER, note)

    def test_no_docker_means_not_verified_rather_than_broken(self) -> None:
        with mock.patch.object(check.shutil, "which", return_value=None):
            store, note = check.proxy_session_store()
        self.assertIsNone(store)
        self.assertIn("not verified", note)

    def test_a_broken_session_link_fails_the_check_and_says_why(self) -> None:
        links = {
            "dns": {"ok": True},
            "tls": {"ok": True},
            "edge": {"ok": True},
            "proxy": {"ok": True},
            "session": {"ok": False, "error": "the store is down"},
        }
        verdict = check.diagnose(links, "gateway.example")
        self.assertIn("a login would not complete", verdict)
        self.assertIn("the store is down", verdict)
        self.assertEqual(check.exit_code(links), 1)

    def test_a_healthy_session_link_changes_nothing_about_the_verdict(self) -> None:
        links = {
            "dns": {"ok": True},
            "tls": {"ok": True},
            "edge": {"ok": True},
            "proxy": {"ok": True},
            "session": {"ok": True, "store": "redis"},
        }
        self.assertIn("reachable and gated", check.diagnose(links, "gateway.example"))
        self.assertEqual(check.exit_code(links), 0)

    def test_a_diagnosis_without_a_session_link_still_works(self) -> None:
        # `--no-sso` checks a published site, which has no proxy and no store.
        links = {"dns": {"ok": True}, "tls": {"ok": True}, "edge": {"ok": True}, "proxy": {"ok": True}}
        self.assertIn("reachable and gated", check.diagnose(links, "site.example"))
        self.assertEqual(check.exit_code(links), 0)

    def test_the_session_link_is_skipped_for_a_name_without_sso(self) -> None:
        # Without this the store would be reported as broken for a published site,
        # which has no login to fail.
        def run(argv):
            with redirect_stdout(io.StringIO()) as output:
                code = check.main(argv)
            return code, output.getvalue()

        healthy = {"ok": True}
        dns = {"ok": True, "answers": []}
        with mock.patch.object(check, "check_dns", return_value=dns):
            with mock.patch.object(check, "check_tls", return_value=healthy):
                with mock.patch.object(check, "check_edge", return_value=healthy):
                    with mock.patch.object(check, "check_proxy", return_value=healthy):
                        broken = {"ok": False, "error": "the store is down"}
                        with mock.patch.object(check, "check_session", return_value=broken) as session:
                            _, with_sso = run(["--host", "a.example", "--env-file", "/nonexistent/.env"])
                            self.assertEqual(session.call_count, 1)
                            _, without = run(
                                ["--host", "a.example", "--no-sso", "--env-file", "/nonexistent/.env"]
                            )
                            self.assertEqual(session.call_count, 1, "not called again with --no-sso")

        self.assertIn("session", with_sso)
        self.assertNotIn("session", without)


class CliTests(unittest.TestCase):
    def test_a_name_that_is_not_a_hostname_is_refused(self) -> None:
        # Exit 2 is "could not run", distinct from "the name is broken" — a mistyped
        # argument must not be reported as an outage. None of these reach the
        # network: the shape check comes first.
        for host in ("localhost", "x", "notahost"):
            self.assertEqual(
                check.main(["--host", host, "--json", "--env-file", "/nonexistent/.env"]),
                2,
                host,
            )

    def test_no_host_anywhere_falls_back_to_the_documented_name(self) -> None:
        # With nothing configured the check still has a name to check — the one the
        # deployment docs name — rather than exiting 2 on an empty string.
        self.assertEqual(check.DEFAULT_HOST, "gateway.olympus.innotel.us")


if __name__ == "__main__":
    unittest.main()

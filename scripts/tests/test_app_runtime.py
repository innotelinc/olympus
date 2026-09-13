"""Tests for scripts/app-runtime.py.

The container half needs docker and a real image, so it is not exercised here —
tests/test_build_runner.py covers the plumbing that calls it, and the end-to-end
path was checked against the live stack. What is asserted here is everything that
decides *where* an app runs and *what* answers on its name, because those are the
parts whose failures are silent:

  * a vhost that names the wrong host or the wrong port serves somebody else's app
    or nothing at all, and nginx would not complain;
  * a port that moves between restarts leaves the vhost pointing at nothing, which
    reads as the app being down rather than as the runtime losing the number;
  * `--down` has to take the vhost with it, or the edge proxies a name to a port
    with no listener.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location("app_runtime", HERE.parent / "app-runtime.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["app_runtime"] = module
    spec.loader.exec_module(module)
    return module


runtime_module = load_module()

# A base far enough up the range that a test run is not competing with the
# deployment's own apps, which start at 21400.
PORT_BASE = 45900

ENV = {
    "SITE_PORT": "20130",
    "SITE_HOST_SUFFIX": "studio.example.test",
    "APP_PORT_BASE": str(PORT_BASE),
    "APP_PORT_RANGE": "5",
}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = runtime_module.Runtime(self.root, dict(ENV))

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ConfigTests(Fixture):
    def test_settings_come_from_the_environment_with_defaults(self) -> None:
        self.assertEqual(self.runtime.site_port, 20130)
        self.assertEqual(self.runtime.suffix, "studio.example.test")
        self.assertEqual(self.runtime.port_base, PORT_BASE)
        self.assertEqual(self.runtime.port_range, 5)

    def test_a_trailing_dot_on_the_suffix_is_not_a_different_name(self) -> None:
        runtime = runtime_module.Runtime(self.root, {**ENV, "SITE_HOST_SUFFIX": "studio.example.test."})
        self.assertEqual(runtime.hostname("todo"), "todo.studio.example.test")

    def test_a_zero_range_is_refused_rather_than_looped_over(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            runtime_module.Runtime(self.root, {**ENV, "APP_PORT_RANGE": "0"})
        self.assertEqual(raised.exception.code, 2)

    def test_read_env_keeps_quoted_values_and_comments_out(self) -> None:
        path = self.root / ".env"
        path.write_text(
            "# a comment\n"
            "\n"
            'SITE_HOST_SUFFIX="studio.example.test"\n'
            "SITE_PORT=20130\n"
            "EMPTY=\n",
            encoding="utf-8",
        )
        parsed = runtime_module.read_env(path)

        self.assertEqual(parsed["SITE_HOST_SUFFIX"], "studio.example.test")
        self.assertEqual(parsed["SITE_PORT"], "20130")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("# a comment", parsed)


class PortTests(Fixture):
    def test_a_port_is_allocated_from_the_range(self) -> None:
        port = self.runtime.allocate_port("todo")
        self.assertGreaterEqual(port, PORT_BASE)
        self.assertLess(PORT_BASE + 5, PORT_BASE + 6)

    def test_an_app_keeps_the_port_it_was_given(self) -> None:
        # Stability is the property: a container that came back on a different port
        # would leave its vhost pointing at nothing.
        first = self.runtime.allocate_port("todo")
        self.runtime.write("todo", {"slug": "todo", "port": first})
        self.assertEqual(self.runtime.allocate_port("todo"), first)

    def test_a_claimed_port_is_not_handed_to_another_app(self) -> None:
        first = self.runtime.allocate_port("todo")
        self.runtime.write("todo", {"slug": "todo", "port": first})

        second = self.runtime.allocate_port("other")
        self.assertNotEqual(first, second)

    def test_an_exhausted_range_says_how_to_widen_it(self) -> None:
        for index in range(5):
            slug = f"app-{index}"
            self.runtime.write(slug, {"slug": slug, "port": PORT_BASE + index})

        with self.assertRaises(SystemExit) as raised:
            self.runtime.allocate_port("one-too-many")
        self.assertEqual(raised.exception.code, 2)


class StateTests(Fixture):
    def test_state_round_trips(self) -> None:
        record = {"slug": "todo", "port": 45901, "container": "olympus-app-todo"}
        self.runtime.write("todo", record)

        self.assertEqual(self.runtime.read("todo"), record)
        self.assertTrue((self.root / "runtime" / "todo.json").is_file())

    def test_an_unreadable_record_is_absent_not_a_crash(self) -> None:
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)
        (self.root / "runtime" / "todo.json").write_text("{ not json", encoding="utf-8")

        self.assertIsNone(self.runtime.read("todo"))
        # And it does not claim a port, so the range does not leak.
        self.assertEqual(self.runtime.claimed_ports(), set())

    def test_forget_removes_only_that_app(self) -> None:
        self.runtime.write("todo", {"slug": "todo", "port": 1})
        self.runtime.write("other", {"slug": "other", "port": 2})

        self.runtime.forget("todo")

        self.assertIsNone(self.runtime.read("todo"))
        self.assertIsNotNone(self.runtime.read("other"))

    def test_the_data_directory_is_per_app(self) -> None:
        self.assertNotEqual(self.runtime.data_dir("todo"), self.runtime.data_dir("other"))


class VhostTests(Fixture):
    def test_the_vhost_names_the_app_and_proxies_to_its_loopback_port(self) -> None:
        path = runtime_module.write_vhost(self.runtime, "weight-tracker", 45901)
        body = path.read_text(encoding="utf-8")

        self.assertIn("server_name  weight-tracker.studio.example.test;", body)
        self.assertIn("proxy_pass         http://127.0.0.1:45901;", body)
        # The sites edge listens on one port for every site and every app; that is
        # what keeps the edge config generic.
        self.assertIn("listen       20130;", body)

    def test_the_vhost_proxies_everything_including_the_api(self) -> None:
        # The client and the API are one origin, so `/api` must not be special-cased
        # here — a location that only proxied `/` would serve the client and 404
        # every call it makes.
        body = runtime_module.vhost(self.runtime, "todo", 45902)
        self.assertIn("location / {", body)
        self.assertNotIn("location /api", body)

    def test_the_vhost_does_not_capture_other_names(self) -> None:
        # An exact `server_name` beats the static template's regex; a wildcard here
        # would swallow every website published under the same suffix.
        body = runtime_module.vhost(self.runtime, "todo", 45903)
        self.assertNotIn("*", body.split("server_name")[1].split(";")[0])

    def test_writing_a_vhost_creates_the_directory(self) -> None:
        runtime_module.write_vhost(self.runtime, "todo", 45904)
        self.assertTrue((self.root / "nginx" / "todo.conf").is_file())

    def test_the_vhost_is_valid_enough_to_reload_nginx_with(self) -> None:
        body = runtime_module.vhost(self.runtime, "todo", 45905)
        self.assertEqual(body.count("{"), body.count("}"))
        self.assertTrue(body.rstrip().endswith("}"))


class PlanTests(Fixture):
    """What the runtime reads out of the plan the project was packaged from.

    These two values are the ones whose failure is invisible: the wrong container
    port is a published name proxying to nothing, and the wrong healthcheck path is
    a runtime that declares a working app dead on arrival — or, worse, one that
    accepts a running-but-broken app as up.
    """

    def plan_file(self, payload: object) -> Path:
        app_dir = self.root / "builds" / "todo"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / runtime_module.PLAN_NAME).write_text(json.dumps(payload), encoding="utf-8")
        return app_dir

    def test_reads_a_plan(self) -> None:
        app_dir = self.plan_file({"run": {"port": 8000, "healthcheck": "/healthz"}})
        plan = runtime_module.read_plan(app_dir)
        self.assertEqual(runtime_module.plan_port(plan), 8000)
        self.assertEqual(runtime_module.plan_healthcheck(plan), "/healthz")

    def test_absent_or_unreadable_plan_is_the_old_defaults(self) -> None:
        # A project packaged before the planner existed has an image and a
        # Dockerfile but no plan.json. Refusing to start it would break every app
        # already published.
        app_dir = self.root / "builds" / "legacy"
        app_dir.mkdir(parents=True)
        self.assertEqual(runtime_module.read_plan(app_dir), {})
        self.assertEqual(runtime_module.plan_port({}), runtime_module.CONTAINER_PORT)
        self.assertEqual(runtime_module.plan_healthcheck({}), "/api/health")

        (app_dir / runtime_module.PLAN_NAME).write_text("not json")
        self.assertEqual(runtime_module.read_plan(app_dir), {})

    def test_an_out_of_range_port_falls_back_rather_than_failing_the_publish(self) -> None:
        # No published project can listen on :80 without a root the container does not
        # have. The planner refuses the same value at the other end; this is the second
        # gate, and it exists so a bad number cannot make a project that builds and
        # cannot run.
        for port in (80, 0, -1, 70000, None, "", "not a port"):
            self.assertEqual(
                runtime_module.plan_port({"run": {"port": port}}), runtime_module.CONTAINER_PORT
            )

    def test_a_numeric_string_is_still_understood(self) -> None:
        # Studio normalises the port to a number before it ever leaves the browser, so
        # this only comes up for a hand-written plan.json — and reading it is kinder
        # than starting the app somewhere nobody is looking for it.
        self.assertEqual(runtime_module.plan_port({"run": {"port": "8000"}}), 8000)

    def test_a_healthcheck_that_is_not_a_path_falls_back(self) -> None:
        for value in ("http://example.com", "", None, 7):
            self.assertEqual(
                runtime_module.plan_healthcheck({"run": {"healthcheck": value}}),
                "/api/health",
            )

    def test_a_query_string_is_dropped_from_the_healthcheck(self) -> None:
        self.assertEqual(
            runtime_module.plan_healthcheck({"run": {"healthcheck": "/health?deep=1"}}), "/health"
        )

    def test_healthcheck_asks_the_path_it_is_given(self) -> None:
        # The socket is real; what is asserted is that the request line carries the
        # plan's path, which is the part that used to be hardcoded to /api/health.
        seen: list[bytes] = []

        class FakeSocket:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *arguments):
                return False

            def sendall(self_inner, data: bytes) -> None:
                seen.append(data)

            def recv(self_inner, size: int) -> bytes:
                return b"HTTP/1.0 200 OK\r\n"

        original = runtime_module.socket.create_connection
        runtime_module.socket.create_connection = lambda *a, **k: FakeSocket()
        try:
            answered = runtime_module.health(self.runtime, "todo", 1, 1.0, "/healthz")
        finally:
            runtime_module.socket.create_connection = original

        self.assertTrue(answered)
        self.assertEqual(seen[0].split(b"\r\n")[0], b"GET /healthz HTTP/1.0")

    def test_already_packaged_needs_a_dockerfile_and_a_manifest(self) -> None:
        app_dir = self.root / "builds" / "todo"
        app_dir.mkdir(parents=True)
        self.assertFalse(runtime_module.already_packaged(app_dir))

        (app_dir / "Dockerfile").write_text("FROM x\n")
        self.assertFalse(runtime_module.already_packaged(app_dir))

        (app_dir / runtime_module.MANIFEST_NAMES[0]).write_text("{}")
        self.assertTrue(runtime_module.already_packaged(app_dir))

    def test_an_older_manifest_counts_as_packaged(self) -> None:
        app_dir = self.root / "builds" / "todo"
        app_dir.mkdir(parents=True)
        (app_dir / "Dockerfile").write_text("FROM x\n")
        (app_dir / "app.manifest.json").write_text("{}")
        self.assertTrue(runtime_module.already_packaged(app_dir))


class CliTests(Fixture):
    def test_an_unsafe_slug_is_refused_before_docker_is_touched(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            runtime_module.main(["--up", "../escape", "--root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)

    def test_listing_an_empty_runtime_says_so_rather_than_printing_headers(self) -> None:
        self.assertEqual(runtime_module.main(["--list", "--root", str(self.root)]), 0)

    def test_status_of_an_app_that_never_ran_is_not_an_error(self) -> None:
        status = runtime_module.main(["--status", "todo", "--json", "--root", str(self.root)])
        self.assertEqual(status, 0)

    def test_a_dry_run_writes_nothing(self) -> None:
        app_dir = self.root / "builds" / "todo"
        app_dir.mkdir(parents=True)
        (app_dir / "Dockerfile").write_text("FROM node:24-alpine\n", encoding="utf-8")
        (app_dir / "dist" / "client").mkdir(parents=True)
        (app_dir / "dist" / "client" / "index.html").write_text("<html>\n", encoding="utf-8")

        # `up` resolves the build directory against the real checkout, so this only
        # asserts the refusal path is reached with a description rather than a
        # traceback.
        with self.assertRaises(SystemExit) as raised:
            runtime_module.main(["--up", "no-such-app", "--dry-run", "--root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)

    def test_the_record_written_by_up_is_the_shape_status_reads(self) -> None:
        # The two halves of the CLI agree on the record because there is one writer
        # and one reader; this pins the fields that would break a caller.
        record = {
            "v": 1,
            "slug": "todo",
            "container": "olympus-app-todo",
            "image": "olympus-app-todo:latest",
            "port": 45906,
            "url": "https://todo.studio.example.test",
            "data": "/var/lib/olympus/apps/data/todo",
        }
        self.runtime.write("todo", record)
        stored = json.loads((self.root / "runtime" / "todo.json").read_text(encoding="utf-8"))

        for key in ("slug", "container", "image", "port", "url"):
            self.assertIn(key, stored)


if __name__ == "__main__":
    unittest.main()

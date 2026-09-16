"""Tests for scripts/mirror-published-site.py.

The tool's whole claim is that a recovered name serves the *same bytes* as the
origin it was copied from, so what is asserted here is the verdict and its exit
code. That matters more than usual for a verifier, because a verifier that cannot
fail is worse than no verifier: it gets believed. The failure branches therefore
get at least as much attention as the passing one.

The fetch is stubbed — a fake origin that returns whatever the test says — which
is the only way to reach the mismatch branches without a server. That means the
crawl itself is deliberately not covered: `crawl` walks a live site's own links,
and its decisions that are worth pinning are covered by `dest_path` and the
manifest comparison here.

Exit codes are part of the interface (0 ok, 1 a fetch or comparison failed, 2
misconfiguration), so they are asserted rather than the printed prose.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "mirror_published_site", HERE.parent / "mirror-published-site.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mirror_published_site"] = module
    spec.loader.exec_module(module)
    return module


def serving(body: bytes, content_type: str = "text/html"):
    """A `fetch` stand-in: every path answers with the same body."""

    def fetch(url: str, timeout: int):
        return 200, body, content_type

    return fetch


class VerifyManifest(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def manifest_for(self, body: bytes) -> dict:
        return {
            "host": "site.example",
            "files": {
                "/": {
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                    "content_type": "text/html",
                }
            },
            "unfetched": [],
        }

    def test_identical_bytes_pass(self):
        body = b"<!doctype html><h1>hi</h1>"
        with mock.patch.object(self.mod, "fetch", serving(body)):
            with redirect_stdout(io.StringIO()) as out:
                code = self.mod.verify_manifest(self.manifest_for(body), "https://site.example", 5)
        self.assertEqual(code, 0)
        self.assertIn("all 1 file(s) identical", out.getvalue())

    def test_a_different_body_fails(self):
        manifest = self.manifest_for(b"<h1>original</h1>")
        with mock.patch.object(self.mod, "fetch", serving(b"<h1>tampered</h1>")):
            with redirect_stdout(io.StringIO()) as out:
                code = self.mod.verify_manifest(manifest, "https://site.example", 5)
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH /", out.getvalue())

    def test_a_missing_path_fails(self):
        manifest = self.manifest_for(b"<h1>x</h1>")
        with mock.patch.object(
            self.mod, "fetch", lambda url, timeout: (404, b"Not Found", "text/html")
        ):
            with redirect_stdout(io.StringIO()) as out:
                code = self.mod.verify_manifest(manifest, "https://site.example", 5)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", out.getvalue())

    def test_a_size_mismatch_fails_even_when_the_hash_matches(self):
        # Not reachable against a real server. The branch exists so that a manifest
        # written before `bytes` was recorded still gets its size checked.
        manifest = self.manifest_for(b"<h1>x</h1>")
        manifest["files"]["/"]["bytes"] = 999
        with mock.patch.object(self.mod, "fetch", serving(b"<h1>x</h1>")):
            with redirect_stdout(io.StringIO()):
                code = self.mod.verify_manifest(manifest, "https://site.example", 5)
        self.assertEqual(code, 1)

    def test_an_empty_manifest_is_misconfiguration_not_a_pass(self):
        with redirect_stdout(io.StringIO()):
            code = self.mod.verify_manifest(
                {"host": "site.example", "files": {}}, "https://site.example", 5
            )
        self.assertEqual(code, 2)


class VerifyOnlyArguments(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = str(Path(self.tmp.name) / "site")

    def run_main(self, argv: list[str]) -> int:
        with mock.patch.object(sys, "argv", ["mirror-published-site.py", *argv]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return self.mod.main()

    def write_manifest(self, body: bytes) -> None:
        Path(self.dest + ".manifest.json").write_text(
            json.dumps(
                {
                    "host": "site.example",
                    "files": {
                        "/": {
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "bytes": len(body),
                            "content_type": "text/html",
                        }
                    },
                    "unfetched": [],
                }
            )
        )

    def test_verify_only_checks_the_name_against_the_stored_manifest(self):
        body = b"<h1>hello</h1>"
        self.write_manifest(body)
        with mock.patch.object(self.mod, "fetch", serving(body)):
            code = self.run_main(
                ["--verify-only", "--dest", self.dest, "--verify-base", "https://site.example"]
            )
        self.assertEqual(code, 0)

    def test_verify_only_fails_when_the_name_serves_something_else(self):
        self.write_manifest(b"<h1>hello</h1>")
        with mock.patch.object(self.mod, "fetch", serving(b"<h1>something else</h1>")):
            code = self.run_main(
                ["--verify-only", "--dest", self.dest, "--verify-base", "https://site.example"]
            )
        self.assertEqual(code, 1)

    def test_verify_only_without_a_manifest_is_exit_2(self):
        code = self.run_main(
            ["--verify-only", "--dest", self.dest, "--verify-base", "https://site.example"]
        )
        self.assertEqual(code, 2)

    def test_verify_only_needs_a_base_to_compare_against(self):
        self.write_manifest(b"x")
        self.assertEqual(self.run_main(["--verify-only", "--dest", self.dest]), 2)

    def test_mirroring_needs_a_host(self):
        self.assertEqual(self.run_main(["--dest", self.dest]), 2)


class DestinationPaths(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_a_root_path_becomes_index_html(self):
        self.assertTrue(self.mod.dest_path("/d", "/").endswith("index.html"))

    def test_a_traversing_path_is_refused(self):
        with self.assertRaises(ValueError):
            self.mod.dest_path("/d", "/../../etc/passwd")


if __name__ == "__main__":
    unittest.main()

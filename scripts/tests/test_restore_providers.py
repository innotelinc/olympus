#!/usr/bin/env python3
"""Tests for scripts/omniroute-restore-providers.py.

The script writes credentials into a live gateway, so the decision that matters
is which exported entries it is willing to replay: only API keys can be copied,
and the two it must not pretend to copy are OAuth connections (which need their
own flow on the target) and free providers (which need no credential).

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

RESTORE_PATH = Path(__file__).resolve().parent.parent / "omniroute-restore-providers.py"

spec = importlib.util.spec_from_file_location("restore_providers", RESTORE_PATH)
assert spec and spec.loader
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class Classify(unittest.TestCase):
    def test_an_api_key_is_copied(self) -> None:
        kind, credential = restore.classify(
            {"provider": "openai", "name": "main", "authType": "apikey", "apiKey": "sk-live"}
        )
        self.assertEqual(kind, "apikey")
        self.assertEqual(credential, "sk-live")

    def test_an_oauth_connection_is_never_imported_as_an_api_key(self) -> None:
        # Sending an OAuth access token as `apiKey` would look configured and fail
        # at the first request — the failure this reporting exists to prevent.
        kind, credential = restore.classify(
            {"provider": "github", "name": "github", "authType": "oauth", "accessToken": "gho_x"}
        )
        self.assertEqual(kind, "oauth")
        self.assertIsNone(credential)

    def test_a_free_provider_needs_nothing(self) -> None:
        kind, credential = restore.classify({"provider": "opencode", "authType": "apikey"})
        self.assertEqual(kind, "none")
        self.assertIsNone(credential)

    def test_an_empty_key_is_not_a_credential(self) -> None:
        for empty in ("", None, 0, False):
            kind, _ = restore.classify({"provider": "opencode", "apiKey": empty})
            self.assertEqual(kind, "none", f"{empty!r} was treated as a credential")

    def test_a_decrypted_export_is_used_verbatim(self) -> None:
        # `auth export` names the field apiKey; a hand-written file may say
        # `credential`. Both are the secret and neither is re-encoded.
        kind, credential = restore.classify({"provider": "gemini", "credential": "AIza-x"})
        self.assertEqual(kind, "apikey")
        self.assertEqual(credential, "AIza-x")


class BaseUrl(unittest.TestCase):
    def test_the_management_api_is_addressed_at_the_root(self) -> None:
        # OMNIROUTE_BASE_URL points at the OpenAI-compatible surface (/v1); the
        # management endpoints are not under it.
        self.assertEqual(
            restore.normalise_base_url("http://127.0.0.1:20128/v1"), "http://127.0.0.1:20128"
        )

    def test_plain_and_trailing_slash_forms(self) -> None:
        self.assertEqual(restore.normalise_base_url("http://host:20128/"), "http://host:20128")
        self.assertEqual(restore.normalise_base_url("http://host:20128"), "http://host:20128")
        self.assertEqual(restore.normalise_base_url("https://gw.example.test/v1/"), "https://gw.example.test")

    def test_a_versions_less_path_is_left_alone(self) -> None:
        # A gateway behind a path prefix must not have it stripped.
        self.assertEqual(
            restore.normalise_base_url("https://gw.example.test/omniroute"), "https://gw.example.test/omniroute"
        )


class FindCli(unittest.TestCase):
    """Where the CLI is found from — because a timer's PATH is not a login shell's.

    The bug this pins: the operator who runs a backup by hand has nvm's bin dir on
    PATH, and the systemd unit that runs it daily does not, so the export half of
    the backup only ever worked when a human was logged in.
    """

    def setUp(self) -> None:
        self._saved_env = dict(os.environ)
        self._saved_home = os.environ.get("HOME")
        self._real_path = restore.shutil.which
        self._tmp = tempfile.TemporaryDirectory()
        os.environ.pop("OMNIROUTE_BIN", None)

    def tearDown(self) -> None:
        restore.shutil.which = self._real_path
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)
        if self._saved_home is None:
            os.environ.pop("HOME", None)

    def fake_cli(self, relative: str) -> Path:
        path = Path(self._tmp.name) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_path_wins_over_the_fallback_globs(self) -> None:
        restore.shutil.which = lambda name: "/usr/bin/omniroute" if name == "omniroute" else None
        self.assertEqual(restore.find_cli(), "/usr/bin/omniroute")

    def test_an_explicit_binary_is_used(self) -> None:
        binary = self.fake_cli("custom/omniroute")
        os.environ["OMNIROUTE_BIN"] = str(binary)
        restore.shutil.which = lambda name: None
        self.assertEqual(restore.find_cli(), str(binary))

    def test_an_explicit_binary_that_is_not_executable_is_refused(self) -> None:
        path = Path(self._tmp.name) / "not-a-binary"
        path.write_text("x")
        os.environ["OMNIROUTE_BIN"] = str(path)
        restore.shutil.which = lambda name: None
        with self.assertRaises(restore.GatewayError):
            restore.find_cli()

    def test_an_nvm_install_is_found_with_a_minimal_path(self) -> None:
        # What the timer actually looks like: no nvm on PATH, only the file on disk.
        binary = self.fake_cli("home/.nvm/versions/node/v24.20.0/bin/omniroute")
        os.environ["HOME"] = str(Path(self._tmp.name) / "home")
        restore.shutil.which = lambda name: None
        self.assertEqual(restore.find_cli(), str(binary))

    def test_a_missing_cli_names_the_ways_to_point_at_it(self) -> None:
        os.environ["HOME"] = str(Path(self._tmp.name) / "empty-home")
        restore.shutil.which = lambda name: None
        with self.assertRaises(restore.GatewayError) as caught:
            restore.find_cli()
        message = str(caught.exception)
        self.assertIn("OMNIROUTE_BIN", message)
        self.assertIn("--creds", message)


if __name__ == "__main__":
    unittest.main()

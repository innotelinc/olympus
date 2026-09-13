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


if __name__ == "__main__":
    unittest.main()

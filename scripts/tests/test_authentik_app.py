#!/usr/bin/env python3
"""Tests for scripts/authentik-studio-app.py.

The registration tool is idempotent, and the two behaviours worth pinning are the
ones that are easy to get silently wrong: a re-run must repair a provider that is
already there rather than skip it, and `--rotate-secret` must be the *only* thing
that replaces a secret — an ordinary re-run that quietly invalidated every
existing client would be a much worse failure than the missing secret it was
meant to fix.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent.parent / "authentik-studio-app.py"

spec = importlib.util.spec_from_file_location("authentik_app", APP_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

REDIRECT = "https://gateway.example.test/oauth2/callback"
ISSUER = "https://auth.example.test/application/o/omniroute/"


class FakeApi:
    """Stands in for Authentik: enough shape to reach the provider branch."""

    def __init__(self, provider, applications=None, redirect_uris=None, grant_types=None):
        # main() prints this, so the stand-in has to carry it.
        self.base = "https://auth.example.test"
        self.patched: list[tuple[str, dict]] = []
        self.created: list[dict] = []
        self._provider = provider
        self._applications = applications or []
        self._redirect_uris = redirect_uris or []
        # `or` would swallow the empty list this fixture exists to reproduce.
        self._grant_types = (
            ["authorization_code", "refresh_token"] if grant_types is None else grant_types
        )

    def results(self, path, optional=False, **params):
        if path == "/flows/instances/":
            slug = params.get("slug", "flow")
            return [{"pk": f"flow-{slug}", "slug": slug}]
        if path == "/propertymappings/provider/scope/":
            return [
                {"pk": "s1", "scope_name": "openid"},
                {"pk": "s2", "scope_name": "email"},
                {"pk": "s3", "scope_name": "profile"},
            ]
        if path in ("/crypto/certificatekeypairs/", "/core/certificatekeypairs/"):
            return [{"pk": "key1", "name": "authentik Self-signed Certificate"}]
        if path == "/core/applications/":
            return self._applications
        if path == "/providers/oauth2/":
            if not self._provider:
                return []
            return [
                {
                    "pk": 30,
                    "name": "OmniRoute Gateway",
                    "client_id": "omniroute",
                    # Reported the way a real Authentik provider reports it, and the
                    # fixture gets to override it: a provider that is "fully configured"
                    # is confidential, so that is the honest default. Sending nothing here
                    # would make every provider look public and the script's repair would
                    # fire on all of them — which is how this test used to pass while the
                    # branch it guards was never exercised at all.
                    "client_type": (self._provider or {}).get("client_type", "confidential"),
                    "grant_types": list(self._grant_types),
                    "redirect_uris": [
                        {"matching_mode": "strict", "url": url} for url in self._redirect_uris
                    ],
                }
            ]
        raise AssertionError(f"unexpected GET {path}")

    def request(self, method, path, body=None, optional=False):
        if method == "PATCH":
            self.patched.append((path, body or {}))
            return {**(self._provider or {}), **(body or {})}
        self.created.append({"path": path, "body": body})
        return {"pk": 99, **(body or {})}


def run_main(api, argv):
    """Invoke main() with the network replaced, returning (exit code, stdout)."""
    original = app.Api
    app.Api = lambda base, token, insecure: api
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = app.main()
    finally:
        app.Api = original
    return code, buffer.getvalue()


def argv_for(*extra):
    return [
        "authentik-studio-app.py",
        "--api-base", "https://auth.example.test",
        "--token", "test-token",
        "--env-file", "/nonexistent/.env",
        "--slug", "omniroute",
        "--client-id", "omniroute",
        "--name", "OmniRoute Gateway",
        "--redirect-uri", REDIRECT,
        *extra,
    ]


class RotateSecret(unittest.TestCase):
    def test_rotate_secret_patches_the_provider_and_prints_the_new_value(self) -> None:
        api = FakeApi(provider={"pk": 30}, redirect_uris=[REDIRECT])
        original_argv = app.sys.argv
        app.sys.argv = argv_for("--rotate-secret")
        try:
            code, out = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        self.assertEqual(len(api.patched), 1)
        path, body = api.patched[0]
        self.assertEqual(path, "/providers/oauth2/30/")
        secret = body.get("client_secret")
        self.assertTrue(secret, "a rotated secret must be sent")
        # Printed exactly once, because Authentik will never show it again.
        self.assertEqual(out.count(secret), 1)
        self.assertIn("PREVIOUS secret stopped working", out)

    def test_an_ordinary_rerun_does_not_touch_the_secret(self) -> None:
        # The whole point of the flag: repairing redirect URIs must not invalidate
        # every client that already holds the secret.
        api = FakeApi(provider={"pk": 30}, redirect_uris=[])
        original_argv = app.sys.argv
        app.sys.argv = argv_for()
        try:
            code, out = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        self.assertEqual(len(api.patched), 1)
        _, body = api.patched[0]
        self.assertNotIn("client_secret", body)
        self.assertIn(REDIRECT, [entry["url"] for entry in body["redirect_uris"]])
        self.assertNotIn("client_secret:", out)

    def test_a_fully_configured_provider_is_left_untouched(self) -> None:
        # "Fully configured" includes HOW the client authenticates: the fake reports
        # client_type `confidential` unless a fixture says otherwise, because a
        # provider that works is confidential. See the `public` case below.
        api = FakeApi(provider={"pk": 30}, redirect_uris=[REDIRECT])
        original_argv = app.sys.argv
        app.sys.argv = argv_for()
        try:
            code, out = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        self.assertEqual(api.patched, [], "nothing was wrong, so nothing should be written")
        self.assertIn("fully configured", out)

    def test_grant_types_are_repaired_alongside_a_rotation(self) -> None:
        # An empty grant_types is the failure that answers "Invalid grant_type for
        # provider" at /authorize — a rotation should not sail past it.
        api = FakeApi(provider={"pk": 30}, redirect_uris=[REDIRECT], grant_types=[])
        original_argv = app.sys.argv
        app.sys.argv = argv_for("--rotate-secret")
        try:
            code, _ = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        _, body = api.patched[0]
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])
        self.assertIn("client_secret", body)

    def test_a_public_client_is_repaired_to_confidential(self) -> None:
        # A provider left as `public` still sends its secret, and Authentik answers
        # `400 invalid_client` at the token endpoint. This was the Olympus sign-in
        # failure, and no other repair in the script looks at HOW the client
        # authenticates — so it needs its own test rather than riding on one.
        api = FakeApi(provider={"pk": 30, "client_type": "public"}, redirect_uris=[REDIRECT])
        original_argv = app.sys.argv
        app.sys.argv = argv_for()
        try:
            code, out = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        _, body = api.patched[0]
        self.assertEqual(body["client_type"], "confidential")
        self.assertNotIn("client_secret", body)
        self.assertIn("invalid_client", out)

    def test_dry_run_reports_the_rotation_without_sending_it(self) -> None:
        api = FakeApi(provider={"pk": 30}, redirect_uris=[REDIRECT])
        original_argv = app.sys.argv
        app.sys.argv = argv_for("--rotate-secret", "--dry-run")
        try:
            code, out = run_main(api, app.sys.argv)
        finally:
            app.sys.argv = original_argv

        self.assertEqual(code, 0)
        self.assertEqual(api.patched, [])
        self.assertIn("client_secret", out)
        self.assertIn("dry run", out)
        # The preview must not leak a value that was never committed.
        self.assertNotIn("client_secret: ", out.replace("client_secret: <new", "<new"))


class ResultsFollowPagination(unittest.TestCase):
    """`results()` must not stop at the first page.

    On the live instance `/providers/oauth2/` passes 20 rows, which sorts the
    Studio provider onto page 2. Seeing page 1 only made the lookup miss it, so
    the idempotent repair POSTed a duplicate and Authentik answered "provider
    with this name already exists" — the failure this pins down.
    """

    def _api(self, pages: list[dict]):
        api = app.Api("https://auth.example.test", "t0ken", insecure=False)
        calls: list[str] = []
        remaining = list(pages)

        def fake_request(method, path, body=None, optional=False):
            calls.append(path)
            return remaining.pop(0)

        api.request = fake_request
        return api, calls

    def test_a_row_on_the_second_page_is_returned(self) -> None:
        page1 = {
            "results": [{"name": f"Application {i}"} for i in range(20)],
            "pagination": {"next": 2, "count": 21, "current": 1, "total_pages": 2},
        }
        page2 = {
            "results": [{"name": "Studio"}],
            "pagination": {"next": 0, "count": 21, "current": 2, "total_pages": 2},
        }
        api, calls = self._api([page1, page2])

        names = [row["name"] for row in api.results("/providers/oauth2/")]

        self.assertIn("Studio", names)
        self.assertEqual(len(names), 21)
        # `next` is a page number, so the follow-up is the same path + ?page=N.
        self.assertEqual(calls, ["/providers/oauth2/", "/providers/oauth2/?page=2"])

    def test_one_page_still_costs_one_request(self) -> None:
        page = {"results": [{"name": "Studio"}], "pagination": {"next": 0, "previous": 0, "count": 1}}
        api, calls = self._api([page])

        self.assertEqual([row["name"] for row in api.results("/providers/oauth2/")], ["Studio"])
        self.assertEqual(calls, ["/providers/oauth2/"])

    def test_a_repeating_next_page_does_not_loop(self) -> None:
        page = {"results": [{"name": "Studio"}], "pagination": {"next": 2, "count": 99}}
        api, calls = self._api([dict(page), dict(page), dict(page)])

        self.assertEqual([row["name"] for row in api.results("/providers/oauth2/")], ["Studio", "Studio"])
        self.assertEqual(len(calls), 2)

    def test_a_query_filter_is_preserved_across_pages(self) -> None:
        page1 = {"results": [{"slug": "other"}], "pagination": {"next": 2}}
        page2 = {"results": [{"slug": "studio"}], "pagination": {"next": 0}}
        api, calls = self._api([page1, page2])

        rows = api.results("/core/applications/", slug="studio")

        self.assertEqual([row["slug"] for row in rows], ["other", "studio"])
        self.assertEqual(
            calls,
            ["/core/applications/?slug=studio", "/core/applications/?slug=studio&page=2"],
        )


if __name__ == "__main__":
    unittest.main()

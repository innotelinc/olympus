#!/usr/bin/env python3
"""Tests for scripts/omniroute-vault-backup.py.

The script holds the gateway's decryption keys, so two things matter more than
the plumbing: it must never print a stored value, and it must refuse to
half-apply a restore. Both are pinned here, along with the config precedence that
already caused an outage on this platform (an environment placeholder shadowing
the real value in `.env`).

Nothing here talks to Vault or to a gateway: the HTTP client is replaced with an
in-memory stub, which is also what keeps the suite runnable in CI.

Run: python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

BACKUP_PATH = Path(__file__).resolve().parent.parent / "omniroute-vault-backup.py"

spec = importlib.util.spec_from_file_location("vault_backup", BACKUP_PATH)
assert spec and spec.loader
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class StubVault(backup.Vault):
    """An in-memory Vault: same interface, no HTTP, no token."""

    def __init__(self, values: dict | None = None, version: int = 1) -> None:
        self.addr = "http://vault.test"
        self.token = "t"
        self.prefix = "cerulean"
        self.path = "olympus"
        self.secret_path = f"{self.prefix}/data/{self.path}/{backup.VAULT_ENTRY}"
        self.metadata_path = f"{self.prefix}/metadata/{self.path}/{backup.VAULT_ENTRY}"
        self.values = dict(values or {})
        self.version = version
        self.writes: list[dict] = []

    def write(self, values: dict) -> int:
        self.writes.append(values)
        self.values = dict(values)
        self.version += 1
        return self.version

    def read(self) -> tuple[dict, int | None, str]:
        return dict(self.values), (self.version if self.values else None), "2026-09-13T00:00:00Z"

    def probe_write(self) -> None:
        return


class Counting(unittest.TestCase):
    def test_a_plain_list_is_counted(self) -> None:
        values = {backup.KEY_PROVIDERS: json.dumps([{"provider": "a"}, {"provider": "b"}])}
        self.assertEqual(backup.count_providers(values), 2)

    def test_a_wrapped_export_is_counted(self) -> None:
        # `omniroute auth export` has answered both shapes; neither may read as zero.
        values = {backup.KEY_PROVIDERS: json.dumps({"connections": [{"provider": "a"}]})}
        self.assertEqual(backup.count_providers(values), 1)

    def test_nothing_stored_is_zero_not_an_error(self) -> None:
        self.assertEqual(backup.count_providers({}), 0)
        self.assertEqual(backup.count_providers({backup.KEY_PROVIDERS: "not json"}), 0)
        self.assertEqual(backup.count_providers({backup.KEY_PROVIDERS: ""}), 0)

    def test_non_dict_entries_do_not_count(self) -> None:
        values = {backup.KEY_PROVIDERS: json.dumps(["nope", {"provider": "a"}, 3])}
        self.assertEqual(backup.count_providers(values), 1)


class Redaction(unittest.TestCase):
    def test_the_fingerprint_is_stable(self) -> None:
        self.assertEqual(backup.redact("secret"), backup.redact("secret"))
        self.assertEqual(backup.redact([{"a": 1}]), backup.redact([{"a": 1}]))

    def test_different_values_differ(self) -> None:
        self.assertNotEqual(backup.redact("one"), backup.redact("two"))

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        # Otherwise a re-read that reorders keys would look like a change.
        self.assertEqual(backup.redact({"a": 1, "b": 2}), backup.redact({"b": 2, "a": 1}))

    def test_the_value_never_appears_in_the_fingerprint(self) -> None:
        secret = "STORAGE_ENCRYPTION_KEY=deadbeefcafe"
        self.assertNotIn("deadbeefcafe", backup.redact(secret))


class Placeholders(unittest.TestCase):
    def test_a_template_value_is_not_a_credential(self) -> None:
        for placeholder in ("", "change-me", "changeme", "CHANGE-ME-token", "example", "placeholder"):
            self.assertFalse(backup.is_usable(placeholder), f"{placeholder!r} was treated as usable")

    def test_a_real_value_is(self) -> None:
        self.assertTrue(backup.is_usable("hvs.CAESIJabc"))
        self.assertTrue(backup.is_usable("OR-abcdefghijklmnop"))


class Setting(unittest.TestCase):
    """`.env` wins here, on purpose: the environment is the layer that lies."""

    def setUp(self) -> None:
        self._saved = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def test_the_file_beats_a_placeholder_in_the_environment(self) -> None:
        os.environ["CERULEAN_ADMIN_PASSWORD"] = "change-me-cerulean-admin"
        values = {"CERULEAN_ADMIN_PASSWORD": "the-real-one"}
        self.assertEqual(backup.setting(values, "CERULEAN_ADMIN_PASSWORD"), "the-real-one")

    def test_a_placeholder_in_the_file_falls_back_to_the_environment(self) -> None:
        os.environ["VAULT_ADDR"] = "http://real-vault:8200"
        values = {"VAULT_ADDR": "change-me"}
        self.assertEqual(backup.setting(values, "VAULT_ADDR"), "http://real-vault:8200")

    def test_a_placeholder_everywhere_reads_as_unset(self) -> None:
        os.environ["VAULT_PATH"] = "placeholder"
        self.assertEqual(backup.setting({"VAULT_PATH": "change-me"}, "VAULT_PATH"), "")

    def test_a_missing_key_is_empty_not_an_error(self) -> None:
        self.assertEqual(backup.setting({}, "NOT_SET_ANYWHERE"), "")


class ReadServerEnv(unittest.TestCase):
    def test_a_missing_file_names_what_is_at_stake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(backup.Failure) as caught:
                backup.read_server_env(Path(tmp))
        self.assertIn("STORAGE_ENCRYPTION_KEY", str(caught.exception))

    def test_the_wrong_directory_is_refused(self) -> None:
        # A plain directory that happens to hold a server.env is not the gateway's.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "server.env").write_text("SOMETHING_ELSE=1\n")
            with self.assertRaises(backup.Failure):
                backup.read_server_env(Path(tmp))

    def test_the_real_file_is_returned_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = "STORAGE_ENCRYPTION_KEY=abc\nAPI_KEY_SECRET=def\n"
            (Path(tmp) / "server.env").write_text(text)
            self.assertEqual(backup.read_server_env(Path(tmp)), text)


class DetectDataDir(unittest.TestCase):
    def test_an_explicit_path_wins(self) -> None:
        self.assertEqual(backup.detect_data_dir("/some/dir", "nothing"), Path("/some/dir"))

    def test_the_environment_is_used_before_asking_docker(self) -> None:
        saved = os.environ.get("OMNIROUTE_DATA_DIR")
        os.environ["OMNIROUTE_DATA_DIR"] = "/from/env"
        try:
            self.assertEqual(backup.detect_data_dir("", "nothing"), Path("/from/env"))
        finally:
            if saved is None:
                os.environ.pop("OMNIROUTE_DATA_DIR", None)
            else:
                os.environ["OMNIROUTE_DATA_DIR"] = saved

    def test_it_falls_back_to_the_host_data_dir(self) -> None:
        saved = os.environ.get("OMNIROUTE_DATA_DIR")
        os.environ.pop("OMNIROUTE_DATA_DIR", None)
        try:
            self.assertEqual(backup.detect_data_dir("", "a-container-that-does-not-exist"), backup.DEFAULT_HOST_DATA_DIR)
        finally:
            if saved is not None:
                os.environ["OMNIROUTE_DATA_DIR"] = saved


class Backup(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(os.environ)
        os.environ.pop("OMNIROUTE_DATA_DIR", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.server_env = "STORAGE_ENCRYPTION_KEY=deadbeef\nAPI_KEY_SECRET=cafe\n"
        (self.data_dir / "server.env").write_text(self.server_env)
        self.entries = [{"provider": "gemini", "name": "main", "apiKey": "AIza-secret"}]

        self._real_export = backup.export_connections
        backup.export_connections = lambda _dir: list(self.entries)

    def tearDown(self) -> None:
        backup.export_connections = self._real_export
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved)

    def test_it_writes_both_halves_plus_provenance(self) -> None:
        vault = StubVault()
        self.assertEqual(backup.do_backup(vault, self.data_dir, dry_run=False, as_json=False), 0)
        written = vault.writes[-1]
        self.assertEqual(written[backup.KEY_SERVER_ENV], self.server_env)
        self.assertEqual(json.loads(written[backup.KEY_PROVIDERS]), self.entries)
        self.assertEqual(written[backup.KEY_PROVIDER_COUNT], 1)
        self.assertEqual(written[backup.KEY_SOURCE], str(self.data_dir))
        self.assertIn(backup.KEY_BACKED_UP_AT, written)

    def test_a_dry_run_writes_nothing(self) -> None:
        vault = StubVault()
        self.assertEqual(backup.do_backup(vault, self.data_dir, dry_run=True, as_json=False), 0)
        self.assertEqual(vault.writes, [])

    def test_the_report_never_prints_a_stored_value(self) -> None:
        vault = StubVault()
        for as_json in (False, True):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                backup.do_backup(vault, self.data_dir, dry_run=False, as_json=as_json)
            text = buffer.getvalue()
            self.assertNotIn("deadbeef", text)
            self.assertNotIn("AIza-secret", text)
            # It must still say enough to be useful: how much was stored, and where.
            self.assertIn('"providers": 1' if as_json else "1 connection(s)", text)
            self.assertIn(vault.secret_path, text)


class Check(unittest.TestCase):
    def setUp(self) -> None:
        self._real_export = backup.export_connections

    def tearDown(self) -> None:
        backup.export_connections = self._real_export

    def stored(self, count: int, server_env: str = "STORAGE_ENCRYPTION_KEY=x\n") -> StubVault:
        return StubVault(
            {
                backup.KEY_SERVER_ENV: server_env,
                backup.KEY_PROVIDERS: json.dumps([{"provider": f"p{i}"} for i in range(count)]),
                backup.KEY_BACKED_UP_AT: "2026-09-13T00:00:00+00:00",
                backup.KEY_PROVIDER_COUNT: count,
            }
        )

    def test_a_matching_backup_passes(self) -> None:
        backup.export_connections = lambda _dir: [{"provider": "a"}, {"provider": "b"}]
        self.assertEqual(backup.do_check(self.stored(2), Path("/tmp"), as_json=True), 0)

    def test_drift_from_the_live_gateway_fails(self) -> None:
        # A backup that is behind the deployment is the failure this check exists
        # for: a restore would bring back fewer connections than are in use.
        backup.export_connections = lambda _dir: [{"provider": "a"}, {"provider": "b"}, {"provider": "c"}]
        self.assertEqual(backup.do_check(self.stored(2), Path("/tmp"), as_json=True), 1)

    def test_an_empty_vault_fails_and_says_what_to_run(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = backup.do_check(StubVault(), Path("/tmp"), as_json=False)
        self.assertEqual(code, 1)
        self.assertIn("with no arguments", buffer.getvalue())

    def test_a_half_backup_is_not_a_backup(self) -> None:
        # Connections without the keys restore a gateway that cannot read them.
        backup.export_connections = lambda _dir: [{"provider": "a"}]
        vault = self.stored(1)
        del vault.values[backup.KEY_SERVER_ENV]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backup.do_check(vault, Path("/tmp"), as_json=False), 1)

    def test_an_unreadable_data_dir_does_not_turn_into_a_pass(self) -> None:
        def explode(_dir):
            raise backup.Failure("no data dir")

        backup.export_connections = explode
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backup.do_check(self.stored(2), Path("/tmp"), as_json=False), 0)


class Restore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def keys_only(self, text: str) -> StubVault:
        return StubVault({backup.KEY_SERVER_ENV: text})

    def test_the_keys_are_written_and_locked_down(self) -> None:
        vault = self.keys_only("STORAGE_ENCRYPTION_KEY=abc\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backup.do_restore(vault, self.data_dir, False, "", "", True), 0)
        written = self.data_dir / "server.env"
        self.assertEqual(written.read_text(), "STORAGE_ENCRYPTION_KEY=abc\n")
        self.assertEqual(written.stat().st_mode & 0o777, 0o600)

    def test_a_different_existing_key_set_is_not_clobbered(self) -> None:
        # Overwriting STORAGE_ENCRYPTION_KEY silently orphans every credential the
        # gateway has already stored.
        (self.data_dir / "server.env").write_text("STORAGE_ENCRYPTION_KEY=the-live-one\n")
        vault = self.keys_only("STORAGE_ENCRYPTION_KEY=some-other-one\n")
        with redirect_stdout(io.StringIO()):
            backup.do_restore(vault, self.data_dir, False, "", "", True)
        self.assertIn("the-live-one", (self.data_dir / "server.env").read_text())

    def test_force_does_overwrite(self) -> None:
        (self.data_dir / "server.env").write_text("STORAGE_ENCRYPTION_KEY=the-live-one\n")
        vault = self.keys_only("STORAGE_ENCRYPTION_KEY=some-other-one\n")
        with redirect_stdout(io.StringIO()):
            backup.do_restore(vault, self.data_dir, True, "", "", True)
        self.assertIn("some-other-one", (self.data_dir / "server.env").read_text())

    def test_an_identical_file_is_left_alone(self) -> None:
        (self.data_dir / "server.env").write_text("STORAGE_ENCRYPTION_KEY=x\n")
        vault = self.keys_only("STORAGE_ENCRYPTION_KEY=x\n")
        with redirect_stdout(io.StringIO()):
            backup.do_restore(vault, self.data_dir, False, "", "", True)
        self.assertEqual((self.data_dir / "server.env").read_text(), "STORAGE_ENCRYPTION_KEY=x\n")

    def test_an_empty_vault_is_an_error_not_a_silent_no_op(self) -> None:
        with self.assertRaises(backup.Failure):
            backup.do_restore(StubVault(), self.data_dir, False, "", "", True)


if __name__ == "__main__":
    unittest.main()

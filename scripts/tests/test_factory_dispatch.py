#!/usr/bin/env python3
"""Unit tests for factory-dispatch.py — the L1 arming gate.

These cover the gate, not the deployment: each case builds a throwaway checkout
with (or without) a pin, a runtime, a provider env file and recorded laps, then
asserts what the gate said. The cases are the refusals the gate exists for, so a
regression in any of them is a regression in the reason the level is earned
rather than configured (see the module docstring and docs/roadmap.md's autonomy
ladder).

systemd and `pgrep` are faked at the module boundary, so the suite runs anywhere,
including CI. The module under test has a hyphen in its filename, so it is
loaded by path.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

CHECK = Path(__file__).resolve().parents[1] / "factory-dispatch.py"

REAL_CONSUMER = (
    '"""Invoke a pinned, complete Archon source installation."""\n'
    "SETTINGS = \".factory/consumer.json\"\n"
    "def read_settings(root):\n"
    "    raise ValueError(\"Integration pin required. Run factory init --source ...\")\n"
)
PLACEHOLDER_CONSUMER = (
    '"""Olympus factory consumer - placeholder.\n\n'
    "This checkout ships the deployment surface only.\n"
    '"""\nprint("no")\n'
)
REVISION = "29f6a73daee6edb6eb9d150118e4d040ee9c7bed"


def _load():
    spec = importlib.util.spec_from_file_location("factory_dispatch", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: the module uses dataclasses, and `@dataclass`
    # resolves its own module through sys.modules while the class body is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


disp = _load()


class Checkout:
    """A throwaway checkout, and the pieces of a real one."""

    def __init__(self, case: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / ".factory").mkdir(parents=True, exist_ok=True)

    def consumer(self, text: str = REAL_CONSUMER):
        (self.root / "factory").mkdir(exist_ok=True)
        (self.root / "factory" / "consumer.py").write_text(text, encoding="utf-8")
        return self

    def loop(self):
        (self.root / ".factory" / "loop.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        return self

    def pin(self, revision: str = REVISION, directory_name: str | None = None):
        source = self.root / "cache" / (directory_name or revision)
        (source / "factory").mkdir(parents=True, exist_ok=True)
        (source / "factory" / "pack.json").write_text(
            json.dumps({"repository": "https://github.com/innotelinc/Archon",
                        "integration_revision_required": revision,
                        "source_directory": ".archon/workflows/sdlc"}), encoding="utf-8")
        (source / ".archon" / "workflows" / "sdlc").mkdir(parents=True, exist_ok=True)
        for name in ("archon-lifecycle", "archon-triage"):
            (source / ".archon" / "workflows" / "sdlc" / f"{name}.yaml").write_text(
                f"name: {name}\n", encoding="utf-8")
        (self.root / ".factory" / "consumer.json").write_text(
            json.dumps({"source": str(source), "revision": revision, "repository":
                        "https://github.com/innotelinc/Archon", "bun": "bun"}), encoding="utf-8")
        return self

    def provider_env(self, mode: int = 0o600):
        path = Path(self._tmp.name) / "home" / ".factory-env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export ANTHROPIC_API_KEY=redacted\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def lap(self, result: str = "pass", workflow: str = "archon-triage", **extra):
        line = {"ts": "2026-09-18T00:00:00Z", "workflow": workflow, "run": "r1",
                "result": result, "watched_by": "dhunter", "notes": ""}
        line.update(extra)
        with (self.root / ".factory" / "laps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        return self

    def armed(self):
        (self.root / ".factory" / "trigger.json").write_text(
            json.dumps({"armed": True, "level": 1, "workflow": "archon-lifecycle"}),
            encoding="utf-8")
        return self


class GateCase(unittest.TestCase):
    """Runs the gate against a checkout with systemd and pgrep faked."""

    def setUp(self):
        self.checkout = Checkout(self)
        # `--arm` ends by installing the unit, and two things about that are host
        # state rather than the gate: whether `systemctl` is on PATH, and whether
        # the unit directory is writable. Faking only `systemctl` left the real
        # write to /etc/systemd/system in place, so every arming case here passed
        # as root on a host with systemd and failed on a runner — the CLI returned
        # 1 for a permission error that has nothing to do with the evidence gate.
        units = self.checkout.root / "units"
        units.mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.object(disp, "repo_root", lambda *a, **k: self.checkout.root),
            mock.patch.object(disp, "systemctl", lambda *a: (0, "active")),
            mock.patch.object(disp, "loop_running", lambda: False),
            mock.patch.object(disp, "UNIT_DIR", units),
            mock.patch.object(disp.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # Every gate case reads a provider env at $HOME; point HOME at the checkout
        # so a case that does not build one sees "missing" rather than the host's.
        self.home = mock.patch.dict("os.environ", {"HOME": str(self.checkout.root)})
        self.home.start()
        self.addCleanup(self.home.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = disp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def gates(self):
        return [item["gate"] for item in disp.blockers(disp.snapshot(self.checkout.root))]

    def place_env_file(self, mode: int = 0o600) -> Path:
        path = self.checkout.root / ".factory-env"
        path.write_text("export TOKEN=x\n", encoding="utf-8")
        path.chmod(mode)
        return path


class PinGate(GateCase):
    def test_no_pin_is_the_first_blocker_and_names_the_installer(self):
        code, out, _ = self.run_cli("--status")
        self.assertEqual(code, 0)
        self.assertIn("pin: absent", out)
        self.assertIn("scripts/factory-pin.sh", out)

    def test_a_pin_whose_directory_is_not_the_revision_is_not_a_pin(self):
        self.checkout.pin(directory_name="cache/latest")
        self.assertIn("pin", self.gates())

    def test_a_pin_whose_revision_is_not_a_full_sha_is_not_a_pin(self):
        self.checkout.pin(revision="29f6a73d")
        self.assertIn("pin", self.gates())

    def test_a_valid_pin_clears_its_own_blocker(self):
        self.checkout.pin().consumer().loop()
        self.assertNotIn("pin", self.gates())

    def test_workflows_come_from_the_pinned_source(self):
        self.checkout.pin().consumer().loop()
        state = disp.snapshot(self.checkout.root)
        self.assertEqual(state["workflow_names"], ["archon-lifecycle", "archon-triage"])


class RuntimeGate(GateCase):
    def test_the_placeholder_consumer_is_not_a_runtime(self):
        self.checkout.consumer(PLACEHOLDER_CONSUMER)
        self.assertIn("runtime", self.gates())

    def test_a_consumer_that_does_not_read_the_pin_is_not_a_runtime(self):
        self.checkout.consumer('"""Something else entirely."""\n')
        self.assertIn("runtime", self.gates())

    def test_the_installed_consumer_is_a_runtime(self):
        self.checkout.consumer().loop()
        self.assertNotIn("runtime", self.gates())

    def test_a_missing_loop_is_a_blocker(self):
        self.checkout.consumer()
        self.assertIn("loop", self.gates())


class ProviderLoginGate(GateCase):
    def test_a_missing_provider_env_blocks(self):
        self.checkout.pin().consumer().loop()
        self.assertIn("provider login", self.gates())

    def test_a_world_readable_provider_env_blocks(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file(mode=0o644)
        self.assertIn("provider login", self.gates())

    def test_a_mode_600_provider_env_clears_the_gate(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file(mode=0o600)
        self.assertNotIn("provider login", self.gates())


class EvidenceGate(GateCase):
    def complete(self, result: str = "pass"):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        if result:
            self.checkout.lap(result=result)
        return self

    def test_no_lap_blocks_arming(self):
        self.complete(result="")
        code, _, err = self.run_cli("--arm", "--workflow", "archon-lifecycle")
        self.assertEqual(code, 1)
        self.assertIn("refusing to arm", err)
        self.assertIn("evidence", err)

    def test_a_failed_lap_is_evidence_but_does_not_clear_the_gate(self):
        self.complete(result="fail")
        self.assertIn("evidence", self.gates())
        code, _, _ = self.run_cli("--arm", "--workflow", "archon-lifecycle")
        self.assertEqual(code, 1)

    def test_a_passing_lap_clears_the_gate_and_arms(self):
        self.complete(result="pass")
        self.assertEqual(self.gates(), [])
        code, out, _ = self.run_cli("--arm", "--workflow", "archon-lifecycle")
        self.assertEqual(code, 0)
        self.assertIn("armed", out)
        schedule = json.loads((self.checkout.root / ".factory" / "schedule.json").read_text())
        self.assertEqual(schedule["workflow"], "archon-lifecycle")
        trigger = json.loads((self.checkout.root / ".factory" / "trigger.json").read_text())
        self.assertTrue(trigger["armed"])
        self.assertEqual(trigger["level"], 1)
        # Arming is not finished until the unit is on disk: the last step is the one
        # that used to fail for a non-root caller, so it is asserted rather than
        # assumed from the exit code alone.
        self.assertTrue((disp.UNIT_DIR / disp.UNIT_NAME).is_file())

    def test_a_stop_marker_blocks_arming(self):
        self.complete(result="pass")
        (self.checkout.root / ".factory" / "STOP").write_text("halted\n", encoding="utf-8")
        self.assertIn("stop control", self.gates())
        code, _, _ = self.run_cli("--arm", "--workflow", "archon-lifecycle")
        self.assertEqual(code, 1)

    def test_recording_a_lap_requires_a_runtime_and_a_real_workflow(self):
        code, _, err = self.run_cli("--record-lap", "--workflow", "archon-triage", "--result", "pass")
        self.assertEqual(code, 2)
        self.assertIn("no installed factory runtime", err)

        self.checkout.pin().consumer().loop()
        code, _, err = self.run_cli("--record-lap", "--workflow", "archon-nope", "--result", "pass")
        self.assertEqual(code, 2)
        self.assertIn("not in the pinned source", err)

    def test_a_recorded_lap_is_appended_and_reported(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        code, out, _ = self.run_cli("--record-lap", "--workflow", "archon-triage",
                                    "--result", "pass", "--run", "run-7", "--watched-by", "dhunter")
        self.assertEqual(code, 0)
        self.assertIn("gates clear", out)
        laps, malformed = disp.read_laps(self.checkout.root)
        self.assertEqual(len(laps), 1)
        self.assertEqual(malformed, 0)
        self.assertEqual(laps[0]["run"], "run-7")


class ScheduleGate(GateCase):
    def test_arming_refuses_a_workflow_the_pin_does_not_carry(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        self.checkout.lap()
        code, _, err = self.run_cli("--arm", "--workflow", "archon-retired")
        self.assertEqual(code, 1)
        self.assertIn("not in the pinned source", err)
        self.assertFalse((self.checkout.root / ".factory" / "schedule.json").exists())

    def test_schedule_inputs_must_be_named_like_environment_variables(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        self.checkout.lap()
        code, _, err = self.run_cli("--arm", "--workflow", "archon-lifecycle",
                                    "--input", "bad-name=1")
        self.assertEqual(code, 2)
        self.assertIn("bad --input name", err)

    def test_dry_run_writes_nothing(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        self.checkout.lap()
        code, out, _ = self.run_cli("--arm", "--workflow", "archon-lifecycle", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nothing written", out)
        self.assertFalse((self.checkout.root / ".factory" / "schedule.json").exists())
        self.assertFalse((self.checkout.root / ".factory" / "trigger.json").exists())


class ChecksAndDisarm(GateCase):
    def test_check_is_quiet_on_an_unarmed_checkout(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("not armed", out)

    def test_check_fails_when_armed_without_evidence(self):
        self.checkout.armed()
        code, _, err = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("armed without evidence", err)

    def test_force_arms_but_records_the_override_and_check_notices(self):
        self.checkout.pin().consumer().loop()
        self.place_env_file()
        code, _, err = self.run_cli("--force", "--arm", "--workflow", "archon-lifecycle")
        self.assertEqual(code, 0)
        self.assertIn("gate overridden", err)
        trigger = json.loads((self.checkout.root / ".factory" / "trigger.json").read_text())
        self.assertEqual(trigger["gate_overridden"], ["evidence"])
        code, _, _ = self.run_cli("--check")
        self.assertEqual(code, 1)

    def test_disarm_removes_the_arm_record_and_the_schedule(self):
        self.checkout.armed()
        (self.checkout.root / ".factory" / "schedule.json").write_text("{}", encoding="utf-8")
        code, out, _ = self.run_cli("--disarm")
        self.assertEqual(code, 0)
        self.assertIn("disarmed", out)
        self.assertFalse((self.checkout.root / ".factory" / "trigger.json").exists())
        self.assertFalse((self.checkout.root / ".factory" / "schedule.json").exists())

    def test_json_status_carries_the_blockers(self):
        code, out, _ = self.run_cli("--status", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "NOT_ARMED")
        self.assertEqual(payload["autonomy_level"], 0)
        self.assertIn("pin", [item["gate"] for item in payload["blockers"]])

    def test_one_action_is_required(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("Pick one of", err)


class TriggerDelegation(unittest.TestCase):
    """The published `factory/trigger.py` is a reader, never an armer."""

    def test_trigger_reports_the_dispatch_status(self):
        out = subprocess.run([sys.executable, CHECK.parent.parent / "factory" / "trigger.py",
                              "--status", "--json"], capture_output=True, text=True, timeout=180,
                             cwd=CHECK.parent.parent)
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertIn(payload["state"], ("ARMED", "NOT_ARMED"))
        self.assertIn("blockers", payload)


if __name__ == "__main__":
    unittest.main()

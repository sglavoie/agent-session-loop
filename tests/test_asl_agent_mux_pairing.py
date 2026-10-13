"""``asl agent-mux pairing`` tests.

Covers the whole-state-root survey `amx config asl-sync` reads before it may
rewrite Agent Mux's configuration: how each plan state directory is classified,
what `--migrate` does and — the case that matters most — what it refuses to do
while a live run still carries state this build cannot pair with.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_pairing,
    agent_mux_projection,
    cli,
    events,
    install_verify,
    plans_files,
    state,
    state_locks,
)
from tests.support import write_plan

CURRENT = agent_mux_projection.STATE_SCHEMA_VERSION


class PairingTestBase(unittest.TestCase):
    """A throwaway state root that plans can be seeded into one at a time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(parents=True)

    def _seed(
        self,
        name: str,
        *,
        version: Any = 2,
        identity: bool = True,
        raw: str | None = None,
        locked: bool = False,
    ) -> Path:
        """Create a plan and its state directory, and return the plan directory.

        ``version`` is stamped verbatim so a record can claim a version this
        build does not understand. ``identity`` off drops the three fields the
        projection contract requires. ``raw`` writes the given bytes instead of
        a JSON object. ``locked`` takes a real lock held by this live process,
        which is the only honest way to produce an active run.
        """
        plan_dir = plans_files.canonicalize_path(self.tmp_path / "plans" / name)
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        if raw is not None:
            state_path.write_text(raw, encoding="utf-8")
        else:
            payload: dict[str, Any] = {"status": "paused"}
            if version is not None:
                payload["version"] = version
            if identity:
                payload["run_id"] = f"20260101T000000000000Z-{name}"
                payload["plan_dir"] = str(plan_dir)
                payload["run_started_at"] = "2026-01-01T00:00:00+00:00"
            state_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        if locked:
            state_locks.acquire_lock(plan_dir, self.state_dir)
            self.addCleanup(self._release, plan_dir)
        return plan_dir

    def _release(self, plan_dir: Path) -> None:
        with suppress(state.StateError):
            state_locks.release_lock(plan_dir, self.state_dir)

    def _survey(self, **kwargs: Any) -> dict[str, Any]:
        return agent_mux_pairing.survey(state_dir=self.state_dir, **kwargs)

    def _classification_of(self, report: dict[str, Any], plan_dir: Path) -> str:
        plan_state = str(state.plan_state_dir(plan_dir, self.state_dir))
        for run in report["active_incompatible_runs"]:
            if run["state_dir"] == plan_state:
                return agent_mux_pairing.CLASS_ACTIVE_INCOMPATIBLE
        for issue in report["issues"]:
            if issue["state_dir"] == plan_state:
                return issue["classification"]
        return ""

    def _state_bytes(self, plan_dir: Path) -> bytes:
        path = state.plan_state_dir(plan_dir, self.state_dir) / events.STATE_FILE_NAME
        return path.read_bytes()

    def _state_object(self, plan_dir: Path) -> dict[str, Any]:
        return json.loads(self._state_bytes(plan_dir).decode("utf-8"))


class ResolvedExecutableTests(unittest.TestCase):
    """The path pairing hands to Agent Mux to persist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _executable(self, name: str = "asl") -> Path:
        target = self.tmp_path / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def test_absolute_path_is_reported_verbatim(self) -> None:
        target = self._executable()
        self.assertEqual(
            agent_mux_pairing.resolved_executable(str(target)), str(target)
        )

    def test_relative_path_is_made_absolute(self) -> None:
        target = self._executable()
        cwd = os.getcwd()
        os.chdir(self.tmp_path)
        self.addCleanup(os.chdir, cwd)
        resolved = agent_mux_pairing.resolved_executable("./asl")
        self.assertEqual(Path(resolved).name, "asl")
        self.assertTrue(Path(resolved).is_absolute())
        self.assertTrue(os.path.samefile(resolved, target))

    def test_symlink_is_not_followed(self) -> None:
        """The stable launcher name survives, not the target an upgrade moves."""
        target = self._executable("asl-0.2.0")
        link = self.tmp_path / "asl"
        link.symlink_to(target)
        self.assertEqual(agent_mux_pairing.resolved_executable(str(link)), str(link))

    def test_bare_name_is_looked_up_on_path(self) -> None:
        target = self._executable()
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.tmp_path)
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        self.assertEqual(agent_mux_pairing.resolved_executable("asl"), str(target))

    def test_unnameable_entry_point_is_null_rather_than_a_bare_name(self) -> None:
        """`-c` and an empty argv0 must not degrade into a PATH-dependent value."""
        self.assertIsNone(agent_mux_pairing.resolved_executable(""))
        self.assertIsNone(agent_mux_pairing.resolved_executable("   "))

    def test_non_executable_file_is_refused(self) -> None:
        target = self.tmp_path / "asl"
        target.write_text("not executable\n", encoding="utf-8")
        target.chmod(0o644)
        self.assertIsNone(agent_mux_pairing.resolved_executable(str(target)))

    def test_missing_file_is_refused(self) -> None:
        missing = self.tmp_path / "nope" / "asl"
        self.assertIsNone(agent_mux_pairing.resolved_executable(str(missing)))


class PairingIdentityTests(PairingTestBase):
    """The build facts the survey reports regardless of corpus contents."""

    def test_identity_matches_the_pinned_build_constants(self) -> None:
        report = self._survey()
        self.assertEqual(
            report["contract_set_id"], agent_mux_projection.CONTRACT_SET_ID
        )
        self.assertEqual(report["state_schema_version"], CURRENT)
        self.assertEqual(
            report["bridge_protocol"], agent_mux_pairing.BRIDGE_PROTOCOL_VERSION
        )
        self.assertEqual(report["state_root"], str(self.state_dir))
        self.assertTrue(report["state_root_present"])

    def test_contract_set_is_the_pinned_literal_not_a_recomputed_digest(self) -> None:
        """The fixture corpus need not ship with an install, so the ID is pinned."""
        report = self._survey()
        self.assertTrue(report["contract_set_id"].startswith("asl-amx-1."))

    def test_empty_root_pairs_cleanly(self) -> None:
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["reason"], agent_mux_pairing.REASON_OK)
        self.assertEqual(report["plans"]["total"], 0)

    def test_absent_root_pairs_cleanly(self) -> None:
        report = agent_mux_pairing.survey(state_dir=self.tmp_path / "never-created")
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["total"], 0)


class PairingClassificationTests(PairingTestBase):
    """One class per plan state directory."""

    def test_current_schema_is_compatible(self) -> None:
        self._seed("current", version=CURRENT)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["compatible"], 1)
        self.assertEqual(report["plans"]["migratable"], 0)

    def test_inactive_legacy_schema_is_migratable(self) -> None:
        for version in agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS:
            with self.subTest(version=version):
                self._seed(f"legacy-{version}", version=version)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["plans"]["migratable"],
            len(agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS),
        )

    def test_absent_version_is_migratable(self) -> None:
        """The format has always read a version-free record as v1."""
        self._seed("versionless", version=None)
        report = self._survey()
        self.assertEqual(report["plans"]["migratable"], 1)

    def test_state_record_without_run_identity_is_unprojectable(self) -> None:
        plan_dir = self._seed("thin", identity=False)
        report = self._survey()
        self.assertEqual(report["plans"]["unprojectable"], 1)
        self.assertEqual(
            self._classification_of(report, plan_dir),
            agent_mux_pairing.CLASS_UNPROJECTABLE,
        )

    def test_non_object_state_record_is_unreadable(self) -> None:
        plan_dir = self._seed("broken", raw="[not an object]\n")
        report = self._survey()
        self.assertEqual(report["plans"]["unreadable"], 1)
        self.assertEqual(
            self._classification_of(report, plan_dir),
            agent_mux_pairing.CLASS_UNREADABLE,
        )

    def test_state_directory_without_a_record_is_empty(self) -> None:
        plan_dir = plans_files.canonicalize_path(self.tmp_path / "plans" / "bare")
        state.plan_state_dir(plan_dir, self.state_dir).mkdir(parents=True)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["empty"], 1)

    def test_uninterpretable_version_is_unprojectable_when_inactive(self) -> None:
        plan_dir = self._seed("future", version=99)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(
            self._classification_of(report, plan_dir),
            agent_mux_pairing.CLASS_UNPROJECTABLE,
        )

    def test_classification_counts_partition_the_corpus(self) -> None:
        self._seed("a", version=CURRENT)
        self._seed("b", version=2)
        self._seed("c", identity=False)
        self._seed("d", raw="oops\n")
        report = self._survey()
        counts = report["plans"]
        self.assertEqual(
            sum(counts[name] for name in agent_mux_pairing.CLASSIFICATIONS),
            counts["total"],
        )


class PairingRefusalTests(PairingTestBase):
    """A live run whose state predates the projection refuses the whole pairing."""

    def test_live_legacy_run_is_active_incompatible(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        report = self._survey()
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertEqual(len(report["active_incompatible_runs"]), 1)
        run = report["active_incompatible_runs"][0]
        self.assertEqual(run["plan_dir"], str(plan_dir))
        self.assertEqual(run["pid"], os.getpid())
        self.assertEqual(run["schema_version"], 2)

    def test_refusal_names_the_run_so_an_operator_can_act(self) -> None:
        self._seed("live", version=2, locked=True)
        run = self._survey()["active_incompatible_runs"][0]
        for field in ("plan_dir", "state_dir", "run_id", "pid", "detail"):
            self.assertIsNotNone(run[field], f"{field} must identify the run")

    def test_live_run_at_current_schema_does_not_refuse(self) -> None:
        """Liveness alone is not incompatibility."""
        self._seed("live", version=CURRENT, locked=True)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["compatible"], 1)
        self.assertEqual(report["active_incompatible_runs"], [])

    def test_live_run_at_an_uninterpretable_version_refuses(self) -> None:
        self._seed("live", version=99, locked=True)
        report = self._survey()
        self.assertFalse(report["ok"])
        self.assertEqual(report["plans"]["active-incompatible"], 1)

    def test_stale_lock_does_not_refuse(self) -> None:
        """A dead owner is not a run; the plan migrates normally."""
        plan_dir = self._seed("stale", version=2)
        lock_path = state.lock_path_for_plan(plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pid": 999_999_999,
                    "plan_dir": str(plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "hostname": os.uname().nodename,
                    "owner": "agent-session-loop",
                }
            ),
            encoding="utf-8",
        )
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["migratable"], 1)

    def test_survey_never_reclaims_the_lock_it_reads(self) -> None:
        """A read-only survey must be a function of the corpus, not of its order."""
        plan_dir = self._seed("stale", version=2)
        lock_path = state.lock_path_for_plan(plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pid": 999_999_999,
                    "plan_dir": str(plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "hostname": os.uname().nodename,
                    "owner": "agent-session-loop",
                }
            ),
            encoding="utf-8",
        )
        before = lock_path.read_bytes()
        self._survey()
        self.assertTrue(lock_path.exists(), "the survey removed a lock it only read")
        self.assertEqual(lock_path.read_bytes(), before)


class PairingMigrationTests(PairingTestBase):
    """``--migrate`` upgrades the inactive remainder, or nothing at all."""

    def test_migrate_upgrades_an_inactive_legacy_plan(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        report = self._survey(migrate=True)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertTrue(report["migrated"])
        self.assertEqual(len(report["migrations"]), 1)
        record = report["migrations"][0]
        self.assertTrue(record["ok"])
        self.assertTrue(record["applied"])
        self.assertEqual(record["schema_version_before"], 2)
        self.assertEqual(record["schema_version_after"], CURRENT)
        upgraded = self._state_object(plan_dir)
        self.assertEqual(upgraded["version"], CURRENT)
        self.assertIsInstance(upgraded.get(agent_mux_projection.PROJECTION_KEY), dict)

    def test_migrate_is_idempotent(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        self._survey(migrate=True)
        after_first = self._state_bytes(plan_dir)
        second = self._survey(migrate=True)
        self.assertTrue(second["ok"])
        self.assertEqual(second["migrations"], [], "a current plan is not migratable")
        self.assertEqual(self._state_bytes(plan_dir), after_first)

    def test_migrate_leaves_pause_evidence_intact(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        before = self._state_object(plan_dir)
        self._survey(migrate=True)
        after = self._state_object(plan_dir)
        for key, value in before.items():
            if key == "version":
                continue
            self.assertEqual(after[key], value, f"{key} was rewritten")

    def test_migrate_writes_nothing_while_a_live_legacy_run_exists(self) -> None:
        """The refusal is whole-root: the innocent bystander is not upgraded."""
        bystander = self._seed("inactive", version=2)
        self._seed("live", version=2, locked=True)
        before = self._state_bytes(bystander)
        report = self._survey(migrate=True)
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertFalse(report["migrated"])
        self.assertEqual(report["migrations"], [])
        self.assertEqual(self._state_bytes(bystander), before)

    def test_live_legacy_run_is_never_migrated_underneath(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        before = self._state_bytes(plan_dir)
        self._survey(migrate=True)
        self.assertEqual(self._state_bytes(plan_dir), before)

    def test_survey_without_migrate_writes_nothing(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        before = self._state_bytes(plan_dir)
        report = self._survey()
        self.assertFalse(report["migrated"])
        self.assertEqual(self._state_bytes(plan_dir), before)

    def test_unprojectable_and_unreadable_plans_are_not_migrated(self) -> None:
        thin = self._seed("thin", identity=False)
        broken = self._seed("broken", raw="nope\n")
        before = (self._state_bytes(thin), self._state_bytes(broken))
        report = self._survey(migrate=True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["migrations"], [])
        self.assertEqual((self._state_bytes(thin), self._state_bytes(broken)), before)


class PairingShapeTests(PairingTestBase):
    """Every outcome presents the same keys."""

    EXPECTED_KEYS = {
        "ok",
        "reason",
        "asl_version",
        "executable",
        "contract_set_id",
        "bridge_protocol",
        "state_schema_version",
        "state_root",
        "state_root_present",
        "migrate_requested",
        "migrated",
        "plans",
        "active_incompatible_runs",
        "migrations",
        "issues",
    }

    def test_clean_root_shape(self) -> None:
        self.assertEqual(set(self._survey()) - {"error"}, self.EXPECTED_KEYS)

    def test_refusing_root_shape(self) -> None:
        self._seed("live", version=2, locked=True)
        report = self._survey()
        self.assertEqual(set(report) - {"error"}, self.EXPECTED_KEYS)
        self.assertIn("error", report)

    def test_unscannable_root_shape(self) -> None:
        """A root that is not a directory still answers with every key."""
        not_a_dir = self.tmp_path / "file-root"
        not_a_dir.write_text("", encoding="utf-8")
        report = agent_mux_pairing.survey(state_dir=not_a_dir)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], agent_mux_pairing.REASON_STATE_ROOT_ERROR)
        self.assertEqual(set(report) - {"error"}, self.EXPECTED_KEYS)

    def test_report_is_json_serializable(self) -> None:
        self._seed("live", version=2, locked=True)
        self._seed("legacy", version=2)
        json.dumps(self._survey(), sort_keys=True)


class PairingCLITests(PairingTestBase):
    """The command surface Agent Mux actually invokes."""

    def _run(self, *argv: str) -> tuple[int, str, str]:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["agent-mux", "pairing", "--state-dir", str(self.state_dir), *argv]
        )
        handler = cli.resolve_handler(args)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handler(args)
        return code, out.getvalue(), err.getvalue()

    def test_clean_root_exits_zero_with_a_success_envelope(self) -> None:
        code, out, _ = self._run("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], agent_mux_pairing.COMMAND_NAME)
        self.assertEqual(payload["reason"], agent_mux_pairing.REASON_OK)

    def test_refusal_exits_nonzero_with_an_error_envelope(self) -> None:
        self._seed("live", version=2, locked=True)
        code, out, _ = self._run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertTrue(payload["error"])
        self.assertFalse(payload["paused"])
        self.assertEqual(len(payload["active_incompatible_runs"]), 1)

    def test_reason_is_present_on_both_envelopes(self) -> None:
        """A consumer branches on `reason` alone, success or failure."""
        code, out, _ = self._run("--json")
        self.assertEqual(code, 0)
        self.assertIn("reason", json.loads(out))

    def test_text_output_names_the_refusing_run_on_stderr_and_stdout(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        code, out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn(str(plan_dir), out)
        self.assertIn(str(os.getpid()), out)
        self.assertIn(agent_mux_pairing.COMMAND_NAME, err)

    def test_text_output_reports_build_identity(self) -> None:
        _, out, _ = self._run()
        self.assertIn(agent_mux_projection.CONTRACT_SET_ID, out)
        self.assertIn(str(self.state_dir), out)

    def test_migrate_flag_reaches_the_survey(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        code, out, _ = self._run("--migrate", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["migrated"])
        self.assertEqual(self._state_object(plan_dir)["version"], CURRENT)

    def test_json_output_is_a_single_line(self) -> None:
        self._seed("legacy", version=2)
        _, out, _ = self._run("--json")
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_module_main_matches_the_cli_handler(self) -> None:
        self._seed("live", version=2, locked=True)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = agent_mux_pairing.main(
                ["--state-dir", str(self.state_dir), "--json"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(out.getvalue())["reason"],
            agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS,
        )


class InstallVerifyAgentMuxTests(unittest.TestCase):
    """Paired-cockpit health is reported, and never required."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _checks(self, report: Any, *, active_asl: str = "/usr/bin/asl") -> list[dict]:
        def _fake_doctor(path_env: Any, home: Any) -> tuple[str, Any]:
            return "/usr/bin/amx", report

        original = install_verify._amx_doctor
        install_verify._amx_doctor = _fake_doctor
        self.addCleanup(setattr, install_verify, "_amx_doctor", original)
        return install_verify._check_agent_mux(
            path_env=None, home=self.tmp_path, active_asl=active_asl
        )

    def test_missing_amx_is_a_passing_advisory(self) -> None:
        """ASL must install and verify cleanly with no cockpit present."""
        checks = install_verify._check_agent_mux(
            path_env=str(self.tmp_path), home=self.tmp_path, active_asl=None
        )
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["ok"])
        self.assertTrue(checks[0]["advisory"])

    def test_healthy_pairing_passes(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "mutations_enabled": True,
                        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    }
                },
            }
        )
        self.assertTrue(all(check["ok"] for check in checks), checks)
        self.assertTrue(all(check["advisory"] for check in checks))

    def test_unreachable_bridge_is_reported_with_its_reason(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": False,
                "daemon": {
                    "asl": {
                        "mode": "auto",
                        "available": False,
                        "reason": 'exec: "asl": executable file not found in $PATH',
                    }
                },
            }
        )
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertFalse(bridge["ok"])
        self.assertIn("executable file not found", bridge["detail"])

    def test_disabled_integration_is_not_a_complaint(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {"asl": {"mode": "disabled", "available": False}},
            }
        )
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertTrue(bridge["ok"])

    def test_contract_mismatch_is_named(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "contract_set_id": "asl-amx-1.deadbeefdeadbeef",
                    }
                },
            }
        )
        mismatch = next(c for c in checks if c["name"] == "agent-mux-contract")
        self.assertFalse(mismatch["ok"])
        self.assertIn("asl-amx-1.deadbeefdeadbeef", mismatch["detail"])

    def test_daemon_resolving_a_different_asl_is_named(self) -> None:
        """Neither side can see this failure alone, which is why it is checked."""
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "executable": "/opt/other/bin/asl",
                        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    }
                },
            },
            active_asl="/usr/bin/asl",
        )
        drift = next(c for c in checks if c["name"] == "agent-mux-executable")
        self.assertFalse(drift["ok"])
        self.assertIn("/opt/other/bin/asl", drift["detail"])
        self.assertIn("asl-sync", drift["detail"])

    def test_absent_asl_block_asks_for_pairing(self) -> None:
        checks = self._checks({"schema_version": 2, "healthy": True, "daemon": {}})
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertFalse(bridge["ok"])
        self.assertIn("asl-sync", bridge["detail"])

    def test_unreadable_doctor_report_is_advisory_not_fatal(self) -> None:
        checks = self._checks(OSError("boom"))
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["ok"])
        self.assertTrue(checks[0]["advisory"])


class InstallVerifyPromotionTests(unittest.TestCase):
    """``--require-agent-mux`` is the only way an advisory fails the run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._stub_checks()

    def _stub_checks(self) -> None:
        """Reduce verification to one failing advisory and one passing check."""
        originals = {
            name: getattr(install_verify, name)
            for name in (
                "_check_asl_path",
                "_check_skill_links",
                "_check_session_run_content",
                "_check_startup_context",
                "_check_asl_skill",
                "_check_status_smoke",
                "_check_agent_mux",
            )
        }
        for name, original in originals.items():
            self.addCleanup(setattr, install_verify, name, original)
        install_verify._check_asl_path = lambda *_: (
            "/usr/bin/asl",
            install_verify._success("asl-path", "ok"),
        )
        for name in (
            "_check_skill_links",
            "_check_session_run_content",
            "_check_startup_context",
            "_check_asl_skill",
        ):
            setattr(install_verify, name, lambda *_a, **_k: [])
        install_verify._check_status_smoke = lambda **_: install_verify._success(
            "status-smoke", "ok"
        )
        install_verify._check_agent_mux = lambda **_: [
            install_verify._advisory("agent-mux-bridge", "no bridge")
        ]

    def test_advisory_failure_does_not_fail_verification_by_default(self) -> None:
        payload = install_verify.verify_install(home=self.home)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(len(payload["advisories"]), 1)
        self.assertFalse(payload["require_agent_mux"])

    def test_require_flag_promotes_the_advisory_to_a_failure(self) -> None:
        payload = install_verify.verify_install(home=self.home, require_agent_mux=True)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["failures"]), 1)
        self.assertEqual(payload["advisories"], [])
        self.assertTrue(payload["require_agent_mux"])

    def test_text_output_distinguishes_warn_from_fail(self) -> None:
        payload = install_verify.verify_install(home=self.home)
        out = io.StringIO()
        with redirect_stdout(out):
            install_verify._print_text(payload)
        text = out.getvalue()
        self.assertIn("warn agent-mux-bridge", text)
        self.assertNotIn("FAIL agent-mux-bridge", text)

    def test_require_flag_is_exposed_on_the_parser(self) -> None:
        args = install_verify.build_parser().parse_args(["--require-agent-mux"])
        self.assertTrue(args.require_agent_mux)
        self.assertFalse(install_verify.build_parser().parse_args([]).require_agent_mux)


if __name__ == "__main__":
    unittest.main()

"""``asl agent-mux pairing`` tests.

Covers the whole-state-root survey `amx config asl-sync` reads before it may
rewrite Agent Mux's configuration: how each plan state directory is classified,
what `--migrate` does and — the case that matters most — what it refuses to do
while a live run still carries state this build cannot pair with.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    agent_mux_pairing,
    agent_mux_projection,
    plans_files,
    state,
)
from tests.asl_agent_mux_pairing_support import CURRENT, PairingTestBase


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


if __name__ == "__main__":
    unittest.main()

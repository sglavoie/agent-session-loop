"""Tests for test asl guards controller."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    guards,
)
from tests import support_asl_guards as fixtures


class GuardControllerTests(unittest.TestCase):
    """Activation order, atomic materialization, and the active-set contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.run_dir = self.root / "runs" / "run-1"
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.run_dir,
            path_value=str(self.bin_dir),
        )

    def read_policy(self) -> dict[str, Any]:
        return json.loads(self.controller.policy_file.read_text(encoding="utf-8"))

    def test_add_materializes_policy_and_shim_and_reports_applied(self) -> None:
        outcome = self.controller.add(
            [("git", "push", "origin", "main")], reason="protect main"
        )
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertTrue(outcome.ok)
        self.assertEqual(1, len(outcome.guard_ids))

        policy = self.read_policy()
        self.assertEqual(guards.POLICY_VERSION, policy["version"])
        self.assertEqual("run-1", policy["run_id"])
        self.assertEqual(
            [["git", "push", "origin", "main"]],
            [guard["command"] for guard in policy["guards"]],
        )
        # The original was resolved once, at activation, outside guard-bin.
        self.assertEqual(str(self.bin_dir / "git"), policy["executables"]["git"])

        shim = self.controller.guard_bin / "git"
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_add_is_all_or_nothing_across_rules(self) -> None:
        outcome = self.controller.add(
            [("git", "push"), ("definitely-not-installed", "run")]
        )
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_FOUND, outcome.reason)
        self.assertEqual((), self.controller.active())
        self.assertFalse(self.controller.policy_file.exists())

    def test_add_rejects_an_empty_rule_set(self) -> None:
        """No command rules *and* no path rules is the one empty ``add``.

        The reason names the whole mutation rather than the command half: an
        operator who passed only ``--protect-path`` and had every path
        rejected must not be told the command list was the problem.
        """
        outcome = self.controller.add([])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EMPTY_GUARD_RULE, outcome.reason)

    def test_remove_updates_policy_before_reporting_success(self) -> None:
        applied = self.controller.add([("git", "push")])
        guard_id = applied.guard_ids[0]
        removed = self.controller.remove(guard_id)
        self.assertEqual(guards.ACTION_REMOVED, removed.action)
        self.assertEqual([], self.read_policy()["guards"])
        self.assertEqual((), self.controller.active())
        # The shim stays; it is now a plain delegator.
        self.assertTrue((self.controller.guard_bin / "git").exists())

    def test_remove_of_an_unknown_guard_is_rejected(self) -> None:
        outcome = self.controller.remove("g-nope")
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, outcome.reason)

    def test_expire_clears_the_policy_and_refuses_later_mutations(self) -> None:
        self.controller.add([("git", "push")])
        expired = self.controller.expire()
        self.assertEqual(1, len(expired))
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual((), self.controller.active())

        outcome = self.controller.add([("git", "push")])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, outcome.reason)
        self.assertEqual(guards.ACTION_REJECTED, self.controller.remove("g-1").action)

    def test_expire_is_idempotent(self) -> None:
        self.controller.add([("git", "push")])
        self.assertEqual(1, len(self.controller.expire()))
        self.assertEqual((), self.controller.expire())

    def test_prepare_turn_creates_guard_bin_and_prepends_path(self) -> None:
        env = self.controller.prepare_turn({"PATH": str(self.bin_dir), "KEEP": "1"})
        self.assertTrue(self.controller.guard_bin.is_dir())
        self.assertEqual(
            str(self.controller.guard_bin), env["PATH"].split(os.pathsep)[0]
        )
        # Nothing else about the environment is disturbed.
        self.assertEqual("1", env["KEEP"])
        self.assertIn(str(self.bin_dir), env["PATH"].split(os.pathsep))
        # No guard is active, so the empty directory is the whole overlay.
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual([], list(self.controller.guard_bin.iterdir()))

    def test_prepare_turn_rearms_a_guard_added_during_an_earlier_turn(self) -> None:
        self.controller.add([("git", "push")])
        self.controller.policy_file.unlink()
        self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertTrue(self.controller.policy_file.exists())

    def test_prepare_turn_does_not_duplicate_the_guard_bin_entry(self) -> None:
        first = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        second = self.controller.prepare_turn(first)
        self.assertEqual(
            1,
            second["PATH"].split(os.pathsep).count(str(self.controller.guard_bin)),
        )

    def gate_lane(self) -> dict[str, str]:
        return self.controller.to_payload()[0]["coverage"]["command-gate"]

    def test_prepare_turn_failure_reports_command_gate_unarmed(self) -> None:
        """A turn that never received the overlay is not reported as gated.

        ``prepare_turn`` fails open on purpose — an overlay that cannot be
        written must not cost the turn — but the environment it hands back is
        then the untouched parent one: no ``guard-bin`` on ``PATH`` and no
        policy path for a hook to read. Claiming ``path-shim-armed`` for that
        turn would tell an operator the one thing they would act on and the one
        thing that is false.
        """
        self.controller.add([("git", "push")])
        self.assertEqual(guards.COVERAGE_ACTIVE, self.gate_lane()["status"])

        base = {"PATH": str(self.bin_dir), "KEEP": "1"}
        with mock.patch.object(
            guards, "write_policy", side_effect=OSError("read-only run directory")
        ):
            env = self.controller.prepare_turn(
                base, harness="claude", pretool_armed=True
            )

        # Fails open: the turn still runs, with the environment untouched.
        self.assertEqual(base, env)
        self.assertNotIn(guards.GUARD_POLICY_ENV_VAR, env)
        self.assertNotIn(str(self.controller.guard_bin), env["PATH"].split(os.pathsep))
        # The guard is still active, and still reported — as unenforced.
        self.assertEqual(1, len(self.controller.active()))
        self.assertFalse(self.controller.overlay_armed)
        lane = self.gate_lane()
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, lane["status"])
        self.assertEqual(guards.COVERAGE_REASON_OVERLAY_UNARMED, lane["reason"])
        self.assertNotEqual(guards.COVERAGE_REASON_SHIM_ARMED, lane["reason"])
        # The pretool lane reads the policy path out of the same environment,
        # so it cannot be armed either, whatever the adapter can do in general.
        pretool = self.controller.to_payload()[0]["coverage"]["pretool"]
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, pretool["status"])
        self.assertEqual(guards.COVERAGE_REASON_OVERLAY_UNARMED, pretool["reason"])

    def test_a_later_successful_prepare_returns_the_lane_to_active(self) -> None:
        """The failure is the last turn's verdict, not a permanent state."""
        self.controller.add([("git", "push")])
        with mock.patch.object(guards, "write_policy", side_effect=OSError("boom")):
            self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertEqual(
            guards.COVERAGE_REASON_OVERLAY_UNARMED, self.gate_lane()["reason"]
        )

        env = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertTrue(self.controller.overlay_armed)
        self.assertEqual(
            str(self.controller.guard_bin), env["PATH"].split(os.pathsep)[0]
        )
        self.assertEqual(
            str(self.controller.policy_file), env[guards.GUARD_POLICY_ENV_VAR]
        )
        lane = self.gate_lane()
        self.assertEqual(guards.COVERAGE_ACTIVE, lane["status"])
        self.assertEqual(guards.COVERAGE_REASON_SHIM_ARMED, lane["reason"])

    def test_a_guard_added_after_a_failed_prepare_is_reported_unarmed_too(self) -> None:
        """The verdict belongs to the turn, not to the guard that survived it.

        A guard added mid-turn normally installs into a ``PATH`` entry the
        running worker already has. When that entry was never injected, the
        mid-turn addition has nowhere to install into either, even though its
        own ``add`` materialized cleanly.
        """
        self.controller.add([("git", "push")])
        with mock.patch.object(guards, "write_policy", side_effect=OSError("boom")):
            self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.controller.mark_turn_snapshot(True)
        self.assertEqual(
            guards.ACTION_APPLIED, self.controller.add([("git", "fetch")]).action
        )
        self.assertEqual(
            {guards.COVERAGE_REASON_OVERLAY_UNARMED},
            {
                entry["coverage"]["command-gate"]["reason"]
                for entry in self.controller.to_payload()
            },
        )

    def test_an_expired_run_still_reports_the_shim_reason_not_the_overlay_one(
        self,
    ) -> None:
        """Two different failures keep two different reasons.

        Expiry is the ordinary end of every guard and the operator needs no
        action; a failed overlay is an anomaly during a live turn. Collapsing
        them would make the anomaly invisible.
        """
        guard = guards.GuardRule(
            id="g-1",
            reason=None,
            created_at="2026-01-01T00:00:00+00:00",
            command=("git", "push"),
        )
        lanes = guards.guard_coverage(
            guard, harness="claude", gate_armed=False, overlay_armed=False
        )
        self.assertEqual(
            guards.COVERAGE_REASON_SHIM_UNAVAILABLE, lanes["command-gate"]["reason"]
        )

    def test_active_payload_describes_the_rule_for_status(self) -> None:
        self.controller.add([("git", "push", "origin", "main")], reason="why")
        payload = self.controller.to_payload()[0]
        self.assertEqual(["git", "push", "origin", "main"], payload["command"])
        self.assertEqual("git push origin main", payload["rendered_command"])
        self.assertEqual("git", payload["executable"])
        self.assertEqual("why", payload["reason"])


class ProtectedPathControllerTests(unittest.TestCase):
    """A path guard joins the same active set without needing a shim."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            path_value=str(self.bin_dir),
            workspace_aliases={"App": str(self.root / "app")},
        )

    def path(self, raw: str) -> guards.ProtectedPath:
        return guards.parse_protected_path_rule(raw)

    def read_policy(self) -> dict[str, Any]:
        return json.loads(self.controller.policy_file.read_text(encoding="utf-8"))

    def test_add_arms_a_path_guard_and_installs_no_shim(self) -> None:
        outcome = self.controller.add(paths=[self.path("App:docs/**")], reason="frozen")
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertEqual(1, len(self.controller.active_paths()))
        # A path rule shadows no executable, so no shim overlay is built at all.
        self.assertFalse(self.controller.guard_bin.exists())
        entry = self.read_policy()["guards"][0]
        self.assertEqual(guards.GUARD_KIND_PROTECTED_PATH, entry["kind"])
        self.assertEqual(
            {"qualifier": "App", "pattern": "docs/**"}, entry["protected_path"]
        )

    def test_the_policy_carries_the_repository_binding_for_the_hook(self) -> None:
        """The hook resolves an absolute tool-call path, so it needs the roots."""
        self.controller.add(paths=[self.path("App:docs/**")])
        self.assertEqual(
            {"App": str(self.root / "app")},
            self.read_policy()["workspace"]["aliases"],
        )

    def test_both_kinds_activate_together_or_not_at_all(self) -> None:
        outcome = self.controller.add(
            [("git", "push")], paths=[self.path("App:docs/**")], reason="one intent"
        )
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertEqual(2, len(outcome.guard_ids))
        self.assertEqual(1, len(self.controller.active_paths()))

    def test_an_add_with_neither_kind_is_refused(self) -> None:
        outcome = self.controller.add([], paths=[])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EMPTY_GUARD_RULE, outcome.reason)

    def test_a_turn_without_a_baseline_refuses_a_new_path_guard(self) -> None:
        """Fail closed: a guard that could never be compared is not armed.

        Refusing tells the operator now, while they can still act. Accepting
        would arm something that silently convicts or clears nothing.
        """
        self.controller.mark_turn_snapshot(False)
        outcome = self.controller.add(paths=[self.path("App:docs/**")])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_PROTECTED_PATH_EVIDENCE, outcome.reason)
        self.assertEqual((), self.controller.active_paths())
        # A command rule is unaffected: it needs no snapshot at all.
        self.assertEqual(
            guards.ACTION_APPLIED, self.controller.add([("git", "push")]).action
        )

    def test_a_turn_with_a_baseline_accepts_a_mid_turn_path_guard(self) -> None:
        self.controller.mark_turn_snapshot(True)
        self.assertEqual(
            guards.ACTION_APPLIED,
            self.controller.add(paths=[self.path("App:docs/**")]).action,
        )

    def test_between_turns_a_path_guard_is_accepted_for_the_next_baseline(self) -> None:
        self.controller.mark_turn_snapshot(None)
        self.assertEqual(
            guards.ACTION_APPLIED,
            self.controller.add(paths=[self.path("App:docs/**")]).action,
        )

    def test_removing_a_path_guard_drops_it_from_the_active_set(self) -> None:
        applied = self.controller.add(paths=[self.path("App:docs/**")])
        removed = self.controller.remove(applied.guard_ids[0])
        self.assertEqual(guards.ACTION_REMOVED, removed.action)
        self.assertEqual((), self.controller.active_paths())
        self.assertEqual([], self.read_policy()["guards"])

    def test_prepare_turn_exports_the_policy_path_for_the_hook(self) -> None:
        self.controller.add(paths=[self.path("App:docs/**")])
        env = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertEqual(
            str(self.controller.policy_file), env[guards.GUARD_POLICY_ENV_VAR]
        )

    def test_the_process_start_cohort_is_the_set_prepare_turn_saw(self) -> None:
        first = self.controller.add([("git", "push")])
        self.controller.prepare_turn(
            {"PATH": str(self.bin_dir)}, harness="copilot", pretool_armed=False
        )
        second = self.controller.add([("git", "fetch")])
        coverage = {
            entry["id"]: entry["coverage"]["pretool"]["reason"]
            for entry in self.controller.to_payload()
        }
        self.assertEqual(
            guards.COVERAGE_REASON_COPILOT_PROCESS_START,
            coverage[first.guard_ids[0]],
        )
        self.assertEqual(
            guards.COVERAGE_REASON_ADDED_AFTER_PROCESS_START,
            coverage[second.guard_ids[0]],
        )


if __name__ == "__main__":
    unittest.main()

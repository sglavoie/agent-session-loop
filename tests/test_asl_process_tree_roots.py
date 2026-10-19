"""Test asl process tree roots."""

from __future__ import annotations

import os
import signal
import unittest
from unittest import mock

from agent_session_loop import process_sampling, process_tree
from tests import support_asl_process_tree as fixtures


class VerifiedRootRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = {
            "pid": 10,
            "ppid": 1,
            "birth": fixtures._BIRTH,
            "command": "claude --print",
        }

    def prepare(self, table, *, force=False, prior=None):
        with mock.patch.object(
            process_sampling, "sample_process_table", return_value=table
        ):
            return process_tree.prepare_verified_root_recovery(
                root_pid=10,
                prior_tree=[self.root] if prior is None else prior,
                attempted_at="now",
                force=force,
                environ={},
            )

    def test_verified_root_is_signaled_with_the_selected_shutdown_stage(self) -> None:
        for force, expected in [(False, signal.SIGTERM), (True, signal.SIGKILL)]:
            with self.subTest(force=force):
                report, pids = self.prepare({10: self.root}, force=force)
                kill = mock.Mock()
                completed = process_tree.signal_verified_root_recovery(
                    report, pids, force=force, kill=kill
                )
                kill.assert_called_once_with(10, expected)
                self.assertEqual([10], completed["signaled_pids"])
                self.assertEqual(expected.name, completed["signal"])

    def test_pid_reuse_or_missing_identity_refuses_both_shutdown_stages(self) -> None:
        for force in [False, True]:
            for changes in [
                {"birth": fixtures._LATER_BIRTH},
                {"command": "stranger"},
            ]:
                report, pids = self.prepare({10: {**self.root, **changes}}, force=force)
                self.assertEqual((), pids)
                self.assertEqual(
                    "harness_root_identity", report["identity_mismatches"][0]["reason"]
                )
            for prior in [[], [{**self.root, "birth": None}]]:
                report, pids = self.prepare({10: self.root}, force=force, prior=prior)
                self.assertEqual((), pids)
                self.assertTrue(report["identity_mismatches"])

    def test_new_descendants_prevent_escalation(self) -> None:
        report, pids = self.prepare(
            {10: self.root, 20: {"pid": 20, "ppid": 10}}, force=True
        )
        self.assertEqual((), pids)
        self.assertEqual("harness_has_descendants", report["disabled"])

    def test_missing_root_and_unreadable_table_are_reported(self) -> None:
        report, pids = self.prepare({})
        self.assertEqual((), pids)
        self.assertEqual([10], report["already_gone_pids"])
        report, pids = self.prepare(None)
        self.assertEqual((), pids)
        self.assertIn("sample_error", report)

    def test_opt_out_and_protected_pids_never_sample_or_signal(self) -> None:
        with mock.patch.object(process_sampling, "sample_process_table") as sample:
            for pid, environ in [
                (10, {process_tree.SWEEP_DISABLED_ENV_VAR: "true"}),
                (1, {}),
                (os.getpid(), {}),
            ]:
                report, pids = process_tree.prepare_verified_root_recovery(
                    root_pid=pid,
                    prior_tree=[self.root],
                    attempted_at="now",
                    environ=environ,
                )
                self.assertEqual((), pids)
                self.assertIn("disabled", report)
            sample.assert_not_called()


class VerifiedLeafRecoveryTests(unittest.TestCase):
    """SIGTERM recovery selects only revalidated leaves of the harness tree."""

    @staticmethod
    def row(
        pid: int,
        ppid: int,
        command: str,
        *,
        birth: str = fixtures._BIRTH,
    ) -> dict:
        return {
            "pid": pid,
            "ppid": ppid,
            "pgid": 10,
            "birth": birth,
            "command": command,
        }

    def prepare(self, prior: list[dict], current: dict[int, dict]):
        with mock.patch.object(
            process_sampling, "sample_process_table", return_value=current
        ):
            return process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=prior,
                attempted_at="2026-08-20T12:00:00+00:00",
            )

    def test_claude_shell_eza_selects_only_eza(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c eza -la"),
            self.row(30, 20, "eza -la"),
        ]
        report, candidates = self.prepare(prior, {row["pid"]: row for row in prior})

        self.assertEqual([30], report["candidate_pids"])
        self.assertEqual((30,), candidates)
        killed: list[tuple[int, int]] = []
        completed = process_tree.signal_verified_leaf_recovery(
            report, candidates, kill=lambda pid, sig: killed.append((pid, sig))
        )
        self.assertEqual([(30, signal.SIGTERM)], killed)
        self.assertEqual([30], completed["signaled_pids"])

    def test_pipeline_selects_both_deepest_leaves(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c grep needle file | head"),
            self.row(30, 20, "grep needle file"),
            self.row(31, 20, "head"),
        ]
        report, candidates = self.prepare(prior, {row["pid"]: row for row in prior})

        self.assertEqual([30, 31], report["candidate_pids"])
        self.assertEqual((30, 31), candidates)

    def test_identity_and_ancestry_changes_are_rejected(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c workers"),
            self.row(30, 20, "eza -la"),
            self.row(31, 20, "grep needle"),
            self.row(32, 20, "head"),
        ]
        current = {row["pid"]: dict(row) for row in prior}
        current[30]["birth"] = fixtures._LATER_BIRTH
        current[31]["command"] = "grep something-else"
        current[32]["ppid"] = 1

        report, candidates = self.prepare(prior, current)

        self.assertEqual((), candidates)
        self.assertEqual(
            [
                {"pid": 30, "reason": "birth"},
                {"pid": 31, "reason": "command"},
                {"pid": 32, "reason": "ancestry"},
            ],
            report["identity_mismatches"],
        )

    def test_reused_harness_root_rejects_every_leaf(self) -> None:
        prior = [self.row(10, 1, "claude --print"), self.row(30, 10, "eza -la")]
        current = {row["pid"]: dict(row) for row in prior}
        current[10]["birth"] = fixtures._LATER_BIRTH

        report, candidates = self.prepare(prior, current)

        self.assertEqual((), candidates)
        self.assertEqual(
            [{"pid": 10, "reason": "harness_root_identity"}],
            report["identity_mismatches"],
        )

    def test_disappearing_leaf_is_already_gone_and_parent_is_not_promoted(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c eza -la"),
            self.row(30, 20, "eza -la"),
        ]

        report, candidates = self.prepare(prior, {10: prior[0], 20: prior[1]})

        self.assertEqual((), candidates)
        self.assertEqual([30], report["already_gone_pids"])

    def test_root_only_tree_and_failed_sample_never_signal(self) -> None:
        root = self.row(10, 1, "claude --print")
        report, candidates = self.prepare([root], {10: root})
        self.assertEqual([], report["candidate_pids"])
        self.assertEqual((), candidates)

        with mock.patch.object(
            process_sampling, "sample_process_table", return_value=None
        ):
            failed, failed_candidates = process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=[root, self.row(30, 10, "eza -la")],
                attempted_at="now",
            )
        self.assertEqual((), failed_candidates)
        self.assertEqual("process table sample failed", failed["sample_error"])

    def test_process_sweep_opt_out_disables_recovery(self) -> None:
        prior = [self.row(10, 1, "claude --print"), self.row(30, 10, "eza -la")]
        with mock.patch.object(process_sampling, "sample_process_table") as sample:
            report, candidates = process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=prior,
                attempted_at="now",
                environ={process_tree.SWEEP_DISABLED_ENV_VAR: "true"},
            )
        self.assertEqual((), candidates)
        self.assertEqual("process_sweep_opt_out", report["disabled"])
        sample.assert_not_called()

    def test_signal_failures_and_last_moment_exit_are_reported(self) -> None:
        report = {
            "candidate_pids": [30, 31],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }

        def kill(pid: int, sig: int) -> None:
            self.assertEqual(signal.SIGTERM, sig)
            if pid == 30:
                raise ProcessLookupError
            raise PermissionError("denied")

        completed = process_tree.signal_verified_leaf_recovery(
            report, (30, 31), kill=kill
        )

        self.assertEqual([30], completed["already_gone_pids"])
        self.assertEqual([], completed["signaled_pids"])
        self.assertEqual(31, completed["signal_failures"][0]["pid"])
        self.assertIn("denied", completed["signal_failures"][0]["error"])


if __name__ == "__main__":
    unittest.main()

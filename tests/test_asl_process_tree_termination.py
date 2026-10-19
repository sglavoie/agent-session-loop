"""Test asl process tree termination."""

from __future__ import annotations

import signal
import unittest
from unittest import mock

from agent_session_loop import process_tree
from tests import support_asl_process_tree as fixtures


class ForceStopProcessGroupTests(fixtures._UrgentBase):
    def test_one_killpg_and_nothing_else(self) -> None:
        calls: list[tuple[int, int]] = []

        report = process_tree.force_stop_process_group(
            4242, killpg=lambda pgid, sig: calls.append((pgid, sig))
        )

        self.assertEqual([(4242, signal.SIGKILL)], calls)
        self.assertTrue(report["signalled"])
        self.assertFalse(report["already_gone"])
        self.assertIsNone(report["refused"])
        self.assertIsNone(report["error"])

    def test_the_deadline_path_takes_no_process_table_probe(self) -> None:
        # The guard that keeps the five-second probe off this path: the kill
        # cannot sample even if it wanted to, because it is given no sampler.
        with (
            mock.patch.object(process_tree, "sample_process_table") as sampler,
            mock.patch.object(process_tree, "process_birth") as birth,
        ):
            process_tree.force_stop_process_group(4242, killpg=lambda *_: None)

        sampler.assert_not_called()
        birth.assert_not_called()

    def test_own_process_group_is_refused(self) -> None:
        calls: list[tuple[int, int]] = []

        report = process_tree.force_stop_process_group(
            fixtures._SELF_PGID,
            killpg=lambda pgid, sig: calls.append((pgid, sig)),
        )

        self.assertEqual(process_tree.REFUSE_OWN_PROCESS_GROUP, report["refused"])
        self.assertEqual([], calls)

    def test_pgid_of_one_or_less_is_refused(self) -> None:
        for pgid in (-1, 0, 1):
            with self.subTest(pgid=pgid):
                report = process_tree.force_stop_process_group(
                    pgid, killpg=lambda *_: None
                )
                self.assertEqual(process_tree.REFUSE_INVALID_PGID, report["refused"])

    def test_an_already_empty_group_is_reported_not_raised(self) -> None:
        def killpg(pgid: int, sig: int) -> None:
            raise ProcessLookupError

        report = process_tree.force_stop_process_group(4242, killpg=killpg)

        self.assertTrue(report["already_gone"])
        self.assertFalse(report["signalled"])

    def test_a_refused_signal_is_reported_not_raised(self) -> None:
        def killpg(pgid: int, sig: int) -> None:
            raise PermissionError("not yours")

        report = process_tree.force_stop_process_group(4242, killpg=killpg)

        self.assertFalse(report["signalled"])
        self.assertIn("not yours", report["error"])


class TerminateEscapedDescendantsTests(fixtures._UrgentBase):
    @staticmethod
    def _evidence(**overrides):
        evidence = {
            "root_pid": 4242,
            "root_pgid": 4242,
            "entries": [
                {
                    "pid": 4243,
                    "ppid": 4242,
                    "pgid": 4242,
                    "birth": fixtures._BIRTH,
                },
                {
                    "pid": 4244,
                    "ppid": 4243,
                    "pgid": 4244,
                    "birth": fixtures._BIRTH,
                },
            ],
            "escaped_pids": [4244],
            "collected": True,
            "disabled": None,
        }
        evidence.update(overrides)
        return evidence

    def _terminate(self, evidence, birth: str | None, kills: list):
        run_command = mock.Mock(return_value=self._birth_result(birth))
        return process_tree.terminate_escaped_descendants(
            evidence,
            kill=lambda pid, sig: kills.append((pid, sig)),
            run_command=run_command,
        )

    def test_only_the_escapee_is_signalled_and_only_after_it_is_identified(
        self,
    ) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), fixtures._BIRTH, kills)

        # 4243 died with the group; re-signalling it would only cost a probe.
        self.assertEqual([(4244, signal.SIGKILL)], kills)
        self.assertEqual([4244], report["candidate_pids"])
        self.assertEqual([4244], report["killed"])
        self.assertEqual(process_tree.URGENT_ESCAPEE_POLICY, report["policy"])

    def test_a_reused_pid_is_reported_and_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), fixtures._LATER_BIRTH, kills)

        self.assertEqual([], kills)
        self.assertEqual(
            [{"pid": 4244, "reason": "birth"}], report["identity_mismatches"]
        )
        self.assertEqual([], report["killed"])

    def test_an_unreadable_identity_is_reported_and_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), None, kills)

        self.assertEqual([], kills)
        self.assertEqual([4244], report["unverified"])
        self.assertEqual([], report["killed"])

    def test_an_escapee_recorded_without_a_birth_is_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(
            entries=[{"pid": 4244, "ppid": 4243, "pgid": 4244, "birth": None}]
        )

        report = self._terminate(evidence, fixtures._BIRTH, kills)

        self.assertEqual([], kills)
        self.assertEqual([4244], report["unverified"])

    def test_the_runner_and_the_root_are_never_candidates(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(escaped_pids=[1, fixtures._SELF_PID, 4242, 4244])

        report = self._terminate(evidence, fixtures._BIRTH, kills)

        self.assertEqual([4244], report["candidate_pids"])
        self.assertEqual([(4244, signal.SIGKILL)], kills)

    def test_a_last_moment_exit_is_already_gone_rather_than_a_failure(self) -> None:
        def kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        run_command = mock.Mock(return_value=self._birth_result(fixtures._BIRTH))
        report = process_tree.terminate_escaped_descendants(
            self._evidence(), kill=kill, run_command=run_command
        )

        self.assertEqual([4244], report["already_gone"])
        self.assertEqual([], report["killed"])

    def test_a_kill_failure_is_reported_not_raised(self) -> None:
        def kill(pid: int, sig: int) -> None:
            raise PermissionError("not yours")

        run_command = mock.Mock(return_value=self._birth_result(fixtures._BIRTH))
        report = process_tree.terminate_escaped_descendants(
            self._evidence(), kill=kill, run_command=run_command
        )

        self.assertEqual([{"pid": 4244, "error": "not yours"}], report["kill_failures"])

    def test_the_report_shape_is_the_same_whether_or_not_it_failed(self) -> None:
        # These reports are copied into a run's artifacts, so a reader must not
        # have to guess which keys this particular outcome happened to write.
        kills: list[tuple[int, int]] = []
        succeeded = self._terminate(self._evidence(), fixtures._BIRTH, kills)
        malformed = process_tree.terminate_escaped_descendants(
            {"entries": [], "escaped_pids": 4244},
            kill=mock.Mock(),
            run_command=mock.Mock(),
        )

        self.assertIsNotNone(malformed["error"])
        self.assertIsNone(succeeded["error"])
        self.assertEqual(set(succeeded), set(malformed))

    def test_the_process_sweep_opt_out_signals_nothing(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(disabled="process_sweep_opt_out")

        report = self._terminate(evidence, fixtures._BIRTH, kills)

        self.assertEqual("process_sweep_opt_out", report["disabled"])
        self.assertEqual([], kills)
        self.assertEqual([], report["candidate_pids"])


class ConfirmTerminationTests(fixtures._UrgentBase):
    @staticmethod
    def _evidence(**overrides):
        evidence = {
            "root_pid": 4242,
            "root_pgid": 4242,
            "entries": [
                {
                    "pid": 4243,
                    "ppid": 4242,
                    "pgid": 4242,
                    "birth": fixtures._BIRTH,
                }
            ],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
        }
        evidence.update(overrides)
        return evidence

    def _confirm(
        self,
        *,
        group: Exception | None,
        alive: Exception | None,
        birth=fixtures._BIRTH,
    ):
        def killpg(pgid: int, sig: int) -> None:
            if group is not None:
                raise group

        def kill(pid: int, sig: int) -> None:
            if alive is not None:
                raise alive

        return process_tree.confirm_termination(
            pgid=4242,
            evidence=self._evidence(),
            kill=kill,
            killpg=killpg,
            run_command=mock.Mock(return_value=self._birth_result(birth)),
        )

    def test_a_stopped_group_with_nothing_left_is_complete(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(), alive=ProcessLookupError()
        )

        self.assertEqual(process_tree.GROUP_STOPPED, confirmation["group"])
        self.assertTrue(confirmation["complete"])
        self.assertIsNone(confirmation["incomplete_reason"])
        self.assertEqual([], confirmation["remaining_pids"])

    def test_a_group_still_answering_signal_zero_is_not_stopped(self) -> None:
        confirmation = self._confirm(group=None, alive=ProcessLookupError())

        self.assertEqual(process_tree.GROUP_RUNNING, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_GROUP_NOT_STOPPED,
            confirmation["incomplete_reason"],
        )

    def test_an_unreaped_group_leader_reads_as_ownership_unknown(self) -> None:
        # A signalled but uncollected process is still in its group, and on
        # this platform that group answers signal 0 with EPERM rather than
        # ESRCH -- either way it is not gone.
        confirmation = self._confirm(
            group=PermissionError(), alive=ProcessLookupError()
        )

        self.assertEqual(process_tree.GROUP_OWNERSHIP_UNKNOWN, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
            confirmation["incomplete_reason"],
        )

    def test_a_surviving_descendant_under_its_own_identity_blocks(self) -> None:
        confirmation = self._confirm(group=ProcessLookupError(), alive=None)

        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            confirmation["incomplete_reason"],
        )

    def test_a_reused_descendant_pid_does_not_block(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(),
            alive=None,
            birth=fixtures._LATER_BIRTH,
        )

        self.assertEqual([4243], confirmation["pid_reused"])
        self.assertEqual([], confirmation["remaining_pids"])
        self.assertTrue(confirmation["complete"])

    def test_a_descendant_whose_identity_is_unreadable_blocks(self) -> None:
        confirmation = self._confirm(group=ProcessLookupError(), alive=None, birth=None)

        self.assertEqual([4243], confirmation["unverified_pids"])
        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])

    def test_a_descendant_this_run_may_not_signal_is_ownership_unknown(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(), alive=PermissionError()
        )

        self.assertTrue(confirmation["ownership_unknown"])
        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertEqual(
            process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
            confirmation["incomplete_reason"],
        )

    def test_the_process_sweep_opt_out_cannot_prove_the_work_stopped(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=4242,
            evidence={
                "entries": [],
                "collected": False,
                "disabled": "process_sweep_opt_out",
            },
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertEqual(process_tree.GROUP_STOPPED, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_SWEEP_OPT_OUT, confirmation["incomplete_reason"]
        )

    def test_the_report_shape_is_the_same_whether_or_not_it_failed(self) -> None:
        complete = self._confirm(group=ProcessLookupError(), alive=ProcessLookupError())
        malformed = process_tree.confirm_termination(
            pgid=4242,
            evidence={"entries": 4243, "collected": True},
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertIsNotNone(malformed["error"])
        self.assertFalse(malformed["complete"])
        self.assertIsNone(complete["error"])
        self.assertEqual(set(complete), set(malformed))

    def test_evidence_that_was_never_collected_cannot_prove_it_either(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=4242,
            evidence={"entries": [], "collected": False, "sample_error": "timed out"},
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_EVIDENCE_UNAVAILABLE,
            confirmation["incomplete_reason"],
        )


if __name__ == "__main__":
    unittest.main()

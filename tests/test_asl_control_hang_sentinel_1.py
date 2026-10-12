"""HangSentinelTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import unittest
from datetime import timedelta

from agent_session_loop import (
    turn_execution,
)
from tests.asl_control_hang_sentinel_support import (
    HangSentinelTestsBase,
)


class HangSentinelTests(HangSentinelTestsBase):
    def test_idle_below_trigger_never_samples_or_suspects(self) -> None:
        sentinel, calls = self.make_sentinel([self.tree(1.0)])
        sentinel.note_harness_pid(123)

        assessment = sentinel.evaluate(idle_seconds=119.9, now=self.NOW)

        self.assertIsNone(assessment.suspected)
        self.assertIsNone(assessment.transition)
        self.assertEqual([], calls)

    def test_zero_cpu_confirms_only_after_consecutive_samples(self) -> None:
        sentinel, calls = self.make_sentinel(
            [self.tree(10.0), self.tree(10.1), self.tree(10.2), self.tree(10.2)]
        )
        sentinel.note_harness_pid(123)

        first = sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        second = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        third = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)

        self.assertIsNone(first.suspected)
        self.assertIsNone(second.suspected)
        self.assertEqual("suspected", third.transition)
        block = third.suspected
        assert block is not None
        self.assertEqual(turn_execution.HANG_REASON_ZERO_CPU, block["reason"])
        self.assertEqual(123, block["harness_pid"])
        self.assertEqual(180.0, block["idle_seconds"])
        self.assertEqual(2, block["confirm_samples"])
        self.assertAlmostEqual(0.1, block["cpu_delta_seconds"], places=6)
        self.assertEqual(123, block["process_tree"][0]["pid"])
        self.assertIsNone(block["result_seen_at"])
        self.assertNotIn("process_tree_error", block)

        fourth = sentinel.evaluate(idle_seconds=210.0, now=self.NOW)
        self.assertIsNone(fourth.transition)
        assert fourth.suspected is not None
        self.assertEqual(block["detected_at"], fourth.suspected["detected_at"])
        self.assertEqual(3, fourth.suspected["confirm_samples"])
        self.assertEqual([123] * 4, calls)

    def test_cpu_positive_sample_resets_streak_and_retracts(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                self.tree(10.0),
                self.tree(10.0),
                self.tree(10.0),
                self.tree(25.0),
                self.tree(25.0),
                self.tree(25.1),
            ]
        )
        sentinel.note_harness_pid(123)

        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        suspected = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertEqual("suspected", suspected.transition)

        cleared = sentinel.evaluate(idle_seconds=210.0, now=self.NOW)
        self.assertEqual("cleared", cleared.transition)
        self.assertIsNone(cleared.suspected)
        assert cleared.retracted is not None
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU, cleared.retracted["reason"]
        )
        self.assertAlmostEqual(15.0, cleared.retracted["cpu_delta_seconds"], places=6)

        # The streak restarts from a fresh baseline: one zero-delta sample is
        # not enough, two are.
        rebaselined = sentinel.evaluate(idle_seconds=240.0, now=self.NOW)
        self.assertIsNone(rebaselined.suspected)
        resuspected = sentinel.evaluate(idle_seconds=270.0, now=self.NOW)
        self.assertEqual("suspected", resuspected.transition)

    def test_result_grace_without_pid_never_suspects(self) -> None:
        clock = {"now": 0.0}
        sentinel, calls = self.make_sentinel([], monotonic=lambda: clock["now"])
        sentinel.note_final_result()

        clock["now"] = 44.9
        early = sentinel.evaluate(idle_seconds=44.9, now=self.NOW)
        self.assertIsNone(early.suspected)

        # No pid means no process tree to sample, so the zero-CPU
        # confirmation both classes require can never be reached.
        clock["now"] = 45.0
        for idle in (45.0, 75.0, 105.0):
            assessment = sentinel.evaluate(idle_seconds=idle, now=self.NOW)
            self.assertIsNone(assessment.suspected)
            self.assertIsNone(assessment.transition)
            self.assertIsNone(assessment.cpu_progress)
        self.assertEqual([], calls)

    def test_result_grace_outranks_zero_cpu(self) -> None:
        clock = {"now": 0.0}
        sentinel, calls = self.make_sentinel(
            [self.tree(5.0), self.tree(5.0), self.tree(5.0)],
            monotonic=lambda: clock["now"],
        )
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()

        sentinel.evaluate(idle_seconds=200.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=230.0, now=self.NOW)
        clock["now"] = 60.0
        assessment = sentinel.evaluate(idle_seconds=260.0, now=self.NOW)

        self.assertEqual("suspected", assessment.transition)
        block = assessment.suspected
        assert block is not None
        self.assertEqual(turn_execution.HANG_REASON_RESULT_RECEIVED, block["reason"])
        self.assertEqual(123, block["harness_pid"])
        self.assertEqual(123, block["process_tree"][0]["pid"])
        self.assertEqual([123] * 3, calls)

    def test_ps_failure_never_confirms(self) -> None:
        sentinel, calls = self.make_sentinel([])
        sentinel.note_harness_pid(123)

        for idle in (120.0, 150.0, 180.0, 210.0):
            assessment = sentinel.evaluate(idle_seconds=idle, now=self.NOW)
            self.assertIsNone(assessment.suspected)
            self.assertIsNone(assessment.transition)
        self.assertEqual([123] * 4, calls)

    def test_ps_failure_resets_confirm_streak(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                self.tree(5.0),
                self.tree(5.0),
                None,
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
            ]
        )
        sentinel.note_harness_pid(123)

        outcomes = [
            sentinel.evaluate(idle_seconds=120.0 + index, now=self.NOW).transition
            for index in range(6)
        ]

        self.assertEqual([None, None, None, None, None, "suspected"], outcomes)

    def test_ps_failure_never_suspects_on_result_grace(self) -> None:
        clock = {"now": 100.0}
        sentinel, calls = self.make_sentinel([], monotonic=lambda: clock["now"])
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()

        # A failed sample resets the confirmation streak, so a received
        # result alone raises nothing. The helper's idle ceiling is this
        # path's backstop precisely because the runner cannot see CPU here.
        clock["now"] = 200.0
        for idle in (90.0, 120.0, 150.0):
            assessment = sentinel.evaluate(idle_seconds=idle, now=self.NOW)
            self.assertIsNone(assessment.suspected)
            self.assertIsNone(assessment.cpu_progress)
        self.assertEqual([123] * 3, calls)

    def test_ps_failure_records_error_on_refreshed_block(self) -> None:
        clock = {"now": 100.0}
        sentinel, _calls = self.make_sentinel(
            [self.tree(5.0), self.tree(5.0), self.tree(5.0), None],
            monotonic=lambda: clock["now"],
        )
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()

        clock["now"] = 200.0
        for idle in (90.0, 120.0):
            self.assertIsNone(
                sentinel.evaluate(idle_seconds=idle, now=self.NOW).suspected
            )
        suspected = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        self.assertEqual("suspected", suspected.transition)

        refreshed = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        block = refreshed.suspected
        assert block is not None
        self.assertEqual(turn_execution.HANG_REASON_RESULT_RECEIVED, block["reason"])
        self.assertIsNone(block["process_tree"])
        self.assertEqual("process tree sample failed", block["process_tree_error"])
        # A failed sample publishes no CPU evidence at all.
        self.assertIsNone(refreshed.cpu_progress)

    def test_rising_cpu_never_suspects_after_result(self) -> None:
        clock = {"now": 100.0}
        sentinel, _calls = self.make_sentinel(
            [self.tree(5.0), self.tree(14.0), self.tree(32.0), self.tree(48.0)],
            monotonic=lambda: clock["now"],
        )
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()

        clock["now"] = 200.0
        for idle in (90.0, 120.0, 150.0, 180.0):
            assessment = sentinel.evaluate(idle_seconds=idle, now=self.NOW)
            self.assertIsNone(assessment.suspected)
            self.assertIsNone(assessment.transition)
        progress = assessment.cpu_progress
        assert progress is not None
        self.assertTrue(progress["progressing"])
        self.assertEqual(123, progress["harness_pid"])
        self.assertEqual(0, progress["confirm_samples"])
        self.assertAlmostEqual(16.0, progress["cpu_delta_seconds"], places=6)

    def test_result_received_episode_retracts_then_rearms_on_cpu(self) -> None:
        clock = {"now": 100.0}
        sentinel, _calls = self.make_sentinel(
            [
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
                self.tree(20.0),
                self.tree(20.0),
                self.tree(20.0),
            ],
            monotonic=lambda: clock["now"],
        )
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()

        clock["now"] = 200.0
        sentinel.evaluate(idle_seconds=90.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        suspected = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        self.assertEqual("suspected", suspected.transition)
        assert suspected.suspected is not None
        first_episode = suspected.suspected["detected_at"]

        # A received result is sticky, but the CPU retraction is now
        # reachable: the tree burning 15s of CPU withdraws the suspicion.
        cleared = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertEqual("cleared", cleared.transition)
        assert cleared.retracted is not None
        self.assertEqual(
            turn_execution.HANG_REASON_RESULT_RECEIVED, cleared.retracted["reason"]
        )
        assert cleared.cpu_progress is not None
        self.assertTrue(cleared.cpu_progress["progressing"])

        # Going quiet again re-arms from a fresh baseline with a new episode
        # id, so a supervisor's probe count cannot span the two.
        self.assertIsNone(sentinel.evaluate(idle_seconds=210.0, now=self.NOW).suspected)
        later = self.NOW + timedelta(minutes=5)
        rearmed = sentinel.evaluate(idle_seconds=240.0, now=later)
        self.assertEqual("suspected", rearmed.transition)
        assert rearmed.suspected is not None
        self.assertNotEqual(first_episode, rearmed.suspected["detected_at"])

    def test_cpu_progress_published_without_suspicion(self) -> None:
        sentinel, _calls = self.make_sentinel([self.tree(10.0), self.tree(10.1)])
        sentinel.note_harness_pid(123)

        baseline = sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        self.assertIsNone(baseline.suspected)
        assert baseline.cpu_progress is not None
        self.assertFalse(baseline.cpu_progress["progressing"])
        # No previous total, so no delta to report yet.
        self.assertIsNone(baseline.cpu_progress["cpu_delta_seconds"])
        self.assertEqual(0, baseline.cpu_progress["confirm_samples"])

        second = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        self.assertIsNone(second.suspected)
        assert second.cpu_progress is not None
        self.assertFalse(second.cpu_progress["progressing"])
        self.assertEqual(1, second.cpu_progress["confirm_samples"])

    def test_cpu_progress_absent_when_no_sample_is_taken(self) -> None:
        sentinel, calls = self.make_sentinel([self.tree(10.0)])
        sentinel.note_harness_pid(123)

        assessment = sentinel.evaluate(idle_seconds=119.9, now=self.NOW)

        self.assertIsNone(assessment.cpu_progress)
        self.assertEqual([], calls)

    def test_churning_tree_never_advances_the_streak(self) -> None:
        """ASI-58 replay: workers exiting must not read as CPU stillness.

        The root creeps under the epsilon while multi-second workers come and
        go. Summing the live tree makes each departure a negative delta, which
        the zero-progress test reads as a hang; accumulating by identity keeps
        the new workers' CPU visible as the progress it is.
        """
        root = "Sun Jul 26 17:57:01 2026"
        sentinel, _calls = self.make_sentinel(
            [
                [
                    self.proc(123, 10.00, birth=root, ppid=1),
                    self.proc(200, 8.86, birth="w1"),
                    self.proc(201, 5.93, birth="w2"),
                ],
                [
                    self.proc(123, 10.33, birth=root, ppid=1),
                    self.proc(201, 5.93, birth="w2"),
                    self.proc(202, 3.10, birth="w3"),
                ],
                [
                    self.proc(123, 10.66, birth=root, ppid=1),
                    self.proc(202, 3.10, birth="w3"),
                    self.proc(203, 2.50, birth="w4"),
                ],
                [
                    self.proc(123, 11.00, birth=root, ppid=1),
                    self.proc(204, 4.00, birth="w5"),
                ],
            ]
        )
        sentinel.note_harness_pid(123)

        deltas: list[float | None] = []
        for idle in (120.0, 150.0, 180.0, 210.0):
            assessment = sentinel.evaluate(idle_seconds=idle, now=self.NOW)
            self.assertIsNone(assessment.suspected)
            self.assertIsNone(assessment.transition)
            progress = assessment.cpu_progress
            assert progress is not None
            deltas.append(progress["cpu_delta_seconds"])

        self.assertIsNone(deltas[0])
        for delta in deltas[1:]:
            assert delta is not None
            self.assertGreaterEqual(delta, 0.0)

    def test_static_multi_process_tree_still_confirms_a_hang(self) -> None:
        static = [
            self.proc(123, 10.0, birth="root", ppid=1),
            self.proc(200, 5.0, birth="w1"),
        ]
        sentinel, _calls = self.make_sentinel([list(static) for _ in range(3)])
        sentinel.note_harness_pid(123)

        self.assertIsNone(sentinel.evaluate(idle_seconds=120.0, now=self.NOW).suspected)
        self.assertIsNone(sentinel.evaluate(idle_seconds=150.0, now=self.NOW).suspected)
        confirmed = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)

        self.assertEqual("suspected", confirmed.transition)
        assert confirmed.suspected is not None
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU, confirmed.suspected["reason"]
        )
        self.assertEqual(0.0, confirmed.suspected["cpu_delta_seconds"])

    def test_new_descendant_cpu_clears_a_live_suspicion(self) -> None:
        static = [
            self.proc(123, 10.0, birth="root", ppid=1),
            self.proc(200, 5.0, birth="w1"),
        ]
        sentinel, _calls = self.make_sentinel(
            [
                list(static),
                list(static),
                list(static),
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(201, 9.0, birth="w2"),
                ],
            ]
        )
        sentinel.note_harness_pid(123)

        for idle in (120.0, 150.0):
            sentinel.evaluate(idle_seconds=idle, now=self.NOW)
        self.assertEqual(
            "suspected",
            sentinel.evaluate(idle_seconds=180.0, now=self.NOW).transition,
        )

        cleared = sentinel.evaluate(idle_seconds=210.0, now=self.NOW)
        self.assertEqual("cleared", cleared.transition)
        assert cleared.retracted is not None
        self.assertAlmostEqual(9.0, cleared.retracted["cpu_delta_seconds"], places=6)

    def test_departure_alone_yields_a_zero_delta_and_confirms(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(200, 4.0, birth="w1"),
                ],
                [self.proc(123, 10.0, birth="root", ppid=1)],
                [self.proc(123, 10.0, birth="root", ppid=1)],
            ]
        )
        sentinel.note_harness_pid(123)

        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        second = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        self.assertEqual(0.0, second.cpu_progress["cpu_delta_seconds"])
        confirmed = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)

        self.assertEqual("suspected", confirmed.transition)
        assert confirmed.suspected is not None
        self.assertEqual(0.0, confirmed.suspected["cpu_delta_seconds"])

    def test_pid_reuse_with_a_new_birth_counts_as_new_work(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(200, 1.0, birth="Sun Jul 26 17:57:01 2026"),
                ],
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(200, 1.0, birth="Sun Jul 26 17:57:01 2026"),
                ],
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(200, 3.0, birth="Sun Jul 26 18:03:44 2026"),
                ],
            ]
        )
        sentinel.note_harness_pid(123)

        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        reused = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)

        self.assertIsNone(reused.suspected)
        progress = reused.cpu_progress
        assert progress is not None
        self.assertTrue(progress["progressing"])
        self.assertAlmostEqual(3.0, progress["cpu_delta_seconds"], places=6)
        self.assertEqual(3, progress["tracked_processes"])

    def test_harness_pid_change_rebaselines_the_accounting(self) -> None:
        sentinel, calls = self.make_sentinel(
            [
                [self.proc(123, 10.0, birth="root", ppid=1)],
                [self.proc(123, 10.0, birth="root", ppid=1)],
                [self.proc(456, 4.0, birth="root-2", ppid=1)],
            ]
        )
        sentinel.note_harness_pid(123)

        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        second = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        assert second.cpu_progress is not None
        self.assertEqual(1, second.cpu_progress["confirm_samples"])

        sentinel.note_harness_pid(456)
        rebaselined = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)

        progress = rebaselined.cpu_progress
        assert progress is not None
        self.assertIsNone(progress["cpu_delta_seconds"])
        self.assertEqual(0, progress["confirm_samples"])
        self.assertAlmostEqual(4.0, progress["cpu_total_seconds"], places=6)
        self.assertEqual(1, progress["tracked_processes"])
        self.assertEqual([123, 123, 456], calls)


if __name__ == "__main__":
    unittest.main()

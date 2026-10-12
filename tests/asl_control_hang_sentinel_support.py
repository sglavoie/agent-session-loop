"""Shared fixtures for the HangSentinelTests modules split from test_asl_control."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_session_loop import (
    turn_execution,
)


class HangSentinelTestsBase(unittest.TestCase):
    """Unit coverage for the heartbeat-tick hang classifier."""

    NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    @staticmethod
    def tree(cpu_seconds: float) -> list[dict[str, object]]:
        return [
            {
                "pid": 123,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": cpu_seconds,
                "command": "claude --print",
            }
        ]

    @staticmethod
    def proc(
        pid: int,
        cpu_seconds: float,
        *,
        birth: str | None = None,
        ppid: int = 123,
    ) -> dict[str, object]:
        """One process row for the multi-process, churning-tree fixtures."""
        return {
            "pid": pid,
            "ppid": ppid,
            "state": "S",
            "cpu_seconds": cpu_seconds,
            "birth": birth,
            "command": "python3 -m worker",
        }

    def make_sentinel(
        self,
        samples: list[list[dict[str, object]] | None],
        *,
        monotonic=None,
    ) -> tuple[turn_execution.HangSentinel, list[int]]:
        sample_calls: list[int] = []
        queue = list(samples)

        def sample_tree(pid: int):
            sample_calls.append(pid)
            return queue.pop(0) if queue else None

        sentinel = turn_execution.HangSentinel(
            sample_tree=sample_tree,
            monotonic=(lambda: 0.0) if monotonic is None else monotonic,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=45.0,
        )
        return sentinel, sample_calls


class _AfterEvaluateSentinel(turn_execution.HangSentinel):
    """Sentinel whose ``evaluate`` runs a one-shot hook after returning.

    Deterministically reproduces the F-003 window: the hook fires after the
    verdict is computed but before ``_record_heartbeat`` folds it, exactly
    where resumed output used to be overtaken by the stale assessment.
    """

    after_evaluate = None

    def evaluate(self, **kwargs):
        assessment = super().evaluate(**kwargs)
        hook, self.after_evaluate = self.after_evaluate, None
        if hook is not None:
            hook()
        return assessment

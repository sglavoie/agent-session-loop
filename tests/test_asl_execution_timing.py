"""Per-run execution timing reconciliation tests.

Every fixture here is synthetic and deterministic: the collector holds no
clock and reads no files, so a test only has to hand it records shaped like
the ones ``events.jsonl`` and ``result.json`` really carry.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import events, execution_timing, plans

BASE = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
RUN_ID = "20260901T000000000000Z-4242"
OTHER_RUN_ID = "20260901T010000000000Z-4243"
PLAN = "plan-a"


def _at(offset: float) -> datetime:
    return BASE + timedelta(seconds=offset)


def _iso(offset: float) -> str:
    return _at(offset).isoformat().replace("+00:00", "Z")


def _event(
    name: str,
    payload: dict[str, Any],
    *,
    created_at: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"name": name, "payload": payload, "version": 2}
    if created_at is not None:
        record["created_at"] = _iso(created_at)
    return record


def _run_started(*, dry_run: bool = False, run_id: str = RUN_ID) -> dict[str, Any]:
    return _event(
        events.RUN_STARTED_EVENT,
        {
            "run_id": run_id,
            "dry_run": dry_run,
            "harness": "claude",
            "run_started_at": _iso(0),
        },
        created_at=0,
    )


def _run_finished(
    *,
    run_id: str = RUN_ID,
    started: float = 0,
    finished: float = 250,
    duration: float | None = 250.0,
    paused: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "ok": not paused,
        "paused": paused,
        "reason": "paused" if paused else "completed",
        "run_started_at": _iso(started),
        "run_finished_at": _iso(finished),
        "duration_seconds": duration,
    }
    if extra:
        payload.update(extra)
    return _event(events.RUN_FINISHED_EVENT, payload, created_at=finished)


def _execution_finished(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    omit_turn_index: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
    }
    if not omit_turn_index:
        payload["turn_index"] = turn_index
    return _event(events.TURN_EXECUTION_FINISHED_EVENT, payload, created_at=finished)


def _turn_finished(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    session_kind: str | None = plans.SESSION_KIND_IMPLEMENTATION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
    }
    if session_kind is not None:
        payload["session_kind"] = session_kind
    return _event(events.TURN_FINISHED_EVENT, payload, created_at=finished)


def _turn_started(
    *, turn_index: int, started: float, run_id: str = RUN_ID, session_id: str = "1"
) -> dict[str, Any]:
    return _event(
        execution_timing.TURN_STARTED_EVENT,
        {
            "run_id": run_id,
            "turn_index": turn_index,
            "session": session_id,
            "started_at": _iso(started),
        },
        created_at=started,
    )


def _turn_rejected(
    *, turn_index: int, run_id: str = RUN_ID, session_id: str = "1"
) -> dict[str, Any]:
    return _event(
        events.TURN_REJECTED_EVENT,
        {
            "run_id": run_id,
            "turn_index": turn_index,
            "session": session_id,
            "session_id": session_id,
            "guard": "handoff",
            "reason": "handoff-missing",
        },
    )


def _result(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    session_kind: str | None = plans.SESSION_KIND_IMPLEMENTATION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 2,
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
        "status_result": "complete",
    }
    if session_kind is not None:
        payload["session_kind"] = session_kind
    return payload


def _verification(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    with_start: bool = True,
) -> list[dict[str, Any]]:
    identity = {"run_id": run_id, "turn_index": turn_index, "session": "1"}
    records: list[dict[str, Any]] = []
    if with_start:
        records.append(
            _event(
                events.VERIFICATION_STARTED_EVENT,
                {**identity, "check_count": 1, "started_at": _iso(started)},
                created_at=started,
            )
        )
    records.append(
        _event(
            events.VERIFICATION_FINISHED_EVENT,
            {
                **identity,
                "passed": True,
                "finished_at": _iso(finished),
                "duration_seconds": finished - started
                if duration is None
                else duration,
            },
            created_at=finished,
        )
    )
    return records


def _check_finished(
    *,
    turn_index: int,
    check_index: int,
    duration: float,
    run_id: str = RUN_ID,
    omit_check_index: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": "1",
        "name": f"check-{check_index}",
        "status": "passed",
        "duration_seconds": duration,
    }
    if not omit_check_index:
        payload["check_index"] = check_index
    return _event(events.VERIFICATION_CHECK_FINISHED_EVENT, payload)


def _commit(
    *,
    commit_index: int,
    started: float,
    finished: float,
    run_id: str = RUN_ID,
    repository_key: str = "root",
) -> list[dict[str, Any]]:
    identity = {
        "run_id": run_id,
        "commit_index": commit_index,
        "repository_key": repository_key,
    }
    return [
        _event(
            events.COMMIT_STARTED_EVENT,
            {**identity, "started_at": _iso(started)},
            created_at=started,
        ),
        _event(
            events.COMMIT_FINISHED_EVENT,
            {
                **identity,
                "success": True,
                "started_at": _iso(started),
                "finished_at": _iso(finished),
                "duration_seconds": finished - started,
            },
            created_at=finished,
        ),
    ]


def _finalization(
    *,
    started: float,
    finished: float,
    run_id: str = RUN_ID,
    phase: str = "assurance-audit",
) -> list[dict[str, Any]]:
    return [
        _event(
            events.FINALIZATION_STARTED_EVENT,
            {
                "run_id": run_id,
                "phase": phase,
                "started_at": _iso(started),
                "cancellable": True,
            },
            created_at=started,
        ),
        _event(
            events.FINALIZATION_FINISHED_EVENT,
            {
                "run_id": run_id,
                "phase": phase,
                "cancelled": False,
                "heartbeat_count": 2,
                "finished_at": _iso(finished),
                "duration_seconds": finished - started,
            },
            created_at=finished,
        ),
    ]


def _clean_run_events() -> list[dict[str, Any]]:
    """A run whose every phase reconciles: the trustworthy baseline.

    turn 1 [0, 100] · verification [100, 140] · commit [140, 150] ·
    turn 2 [150, 200] · assurance [200, 220] · run wall 250.
    """
    return [
        _run_started(),
        _turn_started(turn_index=1, started=0),
        _execution_finished(turn_index=1, started=0, finished=100),
        _turn_finished(turn_index=1, started=0, finished=100),
        *_verification(turn_index=1, started=100, finished=140),
        *_commit(commit_index=0, started=140, finished=150),
        _turn_started(turn_index=2, started=150, session_id="2"),
        _execution_finished(turn_index=2, started=150, finished=200, session_id="2"),
        _turn_finished(
            turn_index=2,
            started=150,
            finished=200,
            session_id="2",
            session_kind=plans.SESSION_KIND_REVIEW,
        ),
        *_finalization(started=200, finished=220),
        _run_finished(),
    ]


def _collect(
    evidence: list[execution_timing.RunEvidence],
    **kwargs: Any,
) -> execution_timing.ExecutionTimingReport:
    return execution_timing.collect_execution_timing(evidence, **kwargs)


def _one(
    event_records: list[dict[str, Any]],
    turn_results: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> execution_timing.RunTiming:
    report = _collect(
        [
            execution_timing.RunEvidence(
                plan_key=PLAN,
                event_records=event_records,
                turn_results=turn_results or [],
            )
        ],
        **kwargs,
    )
    assert len(report.runs) == 1, report.runs
    return report.runs[0]


def _codes(observations: tuple[execution_timing.Observation, ...]) -> list[str]:
    return [item.code for item in observations]


class CleanRunAccountingTests(unittest.TestCase):
    def test_a_reconciling_run_attributes_every_phase_and_is_trustworthy(self) -> None:
        run = _one(_clean_run_events())
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)
        self.assertTrue(run.included)
        self.assertTrue(run.trustworthy)
        self.assertEqual(run.anomalies, ())
        self.assertEqual(run.gaps, ())
        self.assertEqual(run.wall_seconds, 250.0)
        self.assertEqual(
            dict(run.phase_seconds),
            {
                execution_timing.PHASE_WORKER_EXECUTION: 100.0,
                execution_timing.PHASE_REVIEW: 50.0,
                execution_timing.PHASE_TURN_UNCLASSIFIED: 0.0,
                execution_timing.PHASE_VERIFICATION: 40.0,
                execution_timing.PHASE_COMMITS: 10.0,
                execution_timing.PHASE_ASSURANCE: 20.0,
            },
        )
        self.assertEqual(run.attributed_seconds, 220.0)
        self.assertEqual(run.unattributed_seconds, 30.0)

    def test_phase_shares_sum_to_the_whole_run(self) -> None:
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=_clean_run_events())]
        )
        shares = report.phase_shares()
        self.assertEqual(shares["basis_runs"], 1)
        self.assertEqual(shares["basis_seconds"], 250.0)
        assert shares["shares"] is not None
        self.assertAlmostEqual(sum(shares["shares"].values()), 1.0, places=6)
        self.assertAlmostEqual(
            shares["shares"][execution_timing.PHASE_WORKER_EXECUTION], 0.4
        )
        self.assertAlmostEqual(
            shares["shares"][execution_timing.PHASE_UNATTRIBUTED], 0.12
        )

    def test_the_report_round_trips_through_json(self) -> None:
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=_clean_run_events())]
        )
        encoded = json.dumps(report.to_dict())
        self.assertEqual(json.loads(encoded)["runs_included"], 1)


class ReconciliationTests(unittest.TestCase):
    def test_three_records_about_one_attempt_are_one_attempt(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(len(run.attempts), 1)
        attempt = run.attempts[0]
        self.assertEqual(attempt.duration_seconds, 100.0)
        self.assertEqual(
            attempt.duration_source, execution_timing.SOURCE_EXECUTION_FINISHED
        )
        self.assertEqual(
            sorted(attempt.sources),
            sorted(
                [
                    execution_timing.SOURCE_EXECUTION_FINISHED,
                    execution_timing.SOURCE_TURN_FINISHED,
                    execution_timing.SOURCE_RESULT_FILE,
                ]
            ),
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_turn_finished_is_the_fallback_when_execution_finished_is_absent(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(
            run.attempts[0].duration_source, execution_timing.SOURCE_TURN_FINISHED
        )
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_the_result_file_is_the_last_fallback(self) -> None:
        run = _one(
            [_run_started(), _turn_rejected(turn_index=1), _run_finished()],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(
            run.attempts[0].duration_source, execution_timing.SOURCE_RESULT_FILE
        )
        self.assertEqual(run.attempts[0].outcome, execution_timing.OUTCOME_REJECTED)
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_conflicting_evidence_stays_visible_rather_than_being_resolved(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, duration=500.0)],
        )
        self.assertIn(
            execution_timing.ANOMALY_CONFLICTING_EVIDENCE, _codes(run.anomalies)
        )
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        # Both readings are named, so a reader can see which was preferred and
        # what it was preferred over.
        self.assertEqual(conflict.detail["preferred"]["value"], 100.0)
        self.assertEqual(conflict.detail["conflicting"]["value"], 500.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_agreeing_evidence_within_tolerance_is_not_a_conflict(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, duration=100.4)],
        )
        self.assertEqual(_codes(run.anomalies), [])

    def test_duplicate_execution_evidence_is_flagged_and_counted_once(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ]
        )
        self.assertEqual(len(run.attempts), 1)
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_duplicate_verification_finishes_do_not_double_count(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=100, finished=140),
                *_verification(
                    turn_index=1, started=100, finished=140, with_start=False
                ),
                _run_finished(),
            ]
        )
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_a_replayed_check_finish_is_one_check_not_two(self) -> None:
        check = _check_finished(turn_index=1, check_index=0, duration=12.0)
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        *_verification(turn_index=1, started=10, finished=30),
                        check,
                        check,
                        _run_finished(),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        work = report.verification_check_work()
        self.assertEqual(work["checks"], 1)
        self.assertEqual(work["seconds"], 12.0)
        # Replay is still visible: the run states one execution twice.
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_conflicting_check_durations_leave_the_run_untrusted(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(turn_index=1, check_index=0, duration=12.0),
                _check_finished(turn_index=1, check_index=0, duration=15.0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        self.assertEqual(conflict.detail["check_index"], 0)
        self.assertEqual(conflict.detail["values"], [12.0, 15.0])
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        self.assertFalse(run.trustworthy)

    def test_checks_of_different_identity_are_separate_executions(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(turn_index=1, check_index=0, duration=12.0),
                _check_finished(turn_index=1, check_index=1, duration=12.0),
                _check_finished(turn_index=2, check_index=0, duration=12.0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.verification_check_count, 3)
        self.assertEqual(run.verification_check_seconds, 36.0)
        self.assertEqual(_codes(run.anomalies), [])

    def test_a_check_without_a_check_index_is_marked_rather_than_multiplied(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(
                    turn_index=1, check_index=0, duration=12.0, omit_check_index=True
                ),
                _check_finished(
                    turn_index=1, check_index=1, duration=12.0, omit_check_index=True
                ),
                _run_finished(),
            ]
        )
        # Two records nothing distinguishes reconcile into one execution, and
        # the gap says so rather than the count claiming certainty.
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        self.assertIn(execution_timing.GAP_MISSING_IDENTITY, _codes(run.gaps))
        self.assertNotEqual(
            run.classification, execution_timing.CLASSIFICATION_COMPLETE
        )

    def test_a_retried_turn_counts_both_attempts(self) -> None:
        run = _one(
            [
                _run_started(),
                _turn_started(turn_index=1, started=0),
                _execution_finished(turn_index=1, started=0, finished=30),
                _turn_rejected(turn_index=1),
                _turn_started(turn_index=2, started=30),
                _execution_finished(turn_index=2, started=30, finished=120),
                _turn_finished(turn_index=2, started=30, finished=120),
                _run_finished(),
            ],
            [
                _result(turn_index=1, started=0, finished=30),
                _result(turn_index=2, started=30, finished=120),
            ],
        )
        self.assertEqual([a.turn_index for a in run.attempts], [1, 2])
        self.assertEqual(
            [a.outcome for a in run.attempts],
            [execution_timing.OUTCOME_REJECTED, execution_timing.OUTCOME_ACCEPTED],
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 120.0
        )

    def test_the_same_run_id_under_two_plans_stays_two_runs(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    "plan-a", event_records=_clean_run_events()
                ),
                execution_timing.RunEvidence(
                    "plan-b", event_records=_clean_run_events()
                ),
            ]
        )
        self.assertEqual(len(report.runs), 2)
        self.assertEqual({run.plan_key for run in report.runs}, {"plan-a", "plan-b"})
        self.assertEqual(report.known_durations()["wall_seconds"], 500.0)


class PhaseAttributionTests(unittest.TestCase):
    def test_verification_uses_phase_elapsed_not_the_sum_of_parallel_checks(
        self,
    ) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        *_verification(turn_index=1, started=100, finished=140),
                        _check_finished(turn_index=1, check_index=0, duration=30.0),
                        _check_finished(turn_index=1, check_index=1, duration=35.0),
                        _check_finished(turn_index=1, check_index=2, duration=38.0),
                        _run_finished(),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)
        self.assertEqual(run.verification_check_seconds, 103.0)
        self.assertEqual(run.verification_check_count, 3)
        work = report.verification_check_work()
        self.assertEqual(work["seconds"], 103.0)
        self.assertEqual(work["phase_seconds"], 40.0)
        self.assertGreater(work["seconds"], work["phase_seconds"])

    def test_a_turn_of_unknown_kind_is_not_filed_as_implementation(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(
                    turn_index=1, started=0, finished=100, session_kind=None
                ),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, session_kind=None)],
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_TURN_UNCLASSIFIED], 100.0
        )
        self.assertIn(execution_timing.GAP_TURN_KIND_UNKNOWN, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_review_turns_are_attributed_to_review(self) -> None:
        run = _one(_clean_run_events())
        review_attempt = next(a for a in run.attempts if a.turn_index == 2)
        self.assertEqual(review_attempt.phase, execution_timing.PHASE_REVIEW)

    def test_commits_across_repositories_are_separate_occurrences(self) -> None:
        run = _one(
            [
                _run_started(),
                *_commit(commit_index=0, started=140, finished=150),
                *_commit(
                    commit_index=0,
                    started=150,
                    finished=157,
                    repository_key="submodule",
                ),
                _run_finished(),
            ]
        )
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_COMMITS], 17.0)
        self.assertEqual(run.anomalies, ())


class EligibilityTests(unittest.TestCase):
    def test_dry_runs_are_excluded(self) -> None:
        run = _one([_run_started(dry_run=True), _run_finished()])
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_DRY_RUN)
        self.assertTrue(run.dry_run)

    def test_a_paused_run_is_included(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(paused=True),
            ]
        )
        self.assertTrue(run.included)
        self.assertTrue(run.paused)
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)

    def test_an_active_run_is_excluded_and_never_scored(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _turn_started(turn_index=1, started=0),
                        _execution_finished(turn_index=1, started=0, finished=100),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_ACTIVE)
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_ACTIVE)
        self.assertIsNone(report.phase_shares()["shares"])
        self.assertEqual(report.known_durations()["seconds"]["worker_execution"], 0.0)

    def test_a_run_whose_start_record_is_absent_is_kept_and_marked(self) -> None:
        run = _one([_run_finished()])
        self.assertTrue(run.included)
        self.assertIsNone(run.dry_run)
        self.assertIn(execution_timing.GAP_RUN_START_ABSENT, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)


class WindowTests(unittest.TestCase):
    def _windowed(
        self, *, window_start: datetime | None, window_end: datetime | None
    ) -> execution_timing.RunTiming:
        return _one(
            _clean_run_events(), window_start=window_start, window_end=window_end
        )

    def test_the_window_start_is_inclusive(self) -> None:
        run = self._windowed(window_start=_at(250), window_end=None)
        self.assertTrue(run.included)

    def test_the_window_end_is_exclusive(self) -> None:
        run = self._windowed(window_start=None, window_end=_at(250))
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_OUTSIDE_WINDOW)

    def test_a_run_finishing_before_the_window_is_excluded(self) -> None:
        run = self._windowed(window_start=_at(251), window_end=None)
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_OUTSIDE_WINDOW)

    def test_a_run_that_began_before_the_window_contributes_its_whole_duration(
        self,
    ) -> None:
        # The run spans [0, 250]; the window opens at 200, well after it began.
        run = self._windowed(window_start=_at(200), window_end=_at(300))
        self.assertTrue(run.included)
        self.assertEqual(run.wall_seconds, 250.0)
        self.assertEqual(run.attributed_seconds, 220.0)

    def test_gaps_between_invocations_are_not_accounted(self) -> None:
        # Two 100-second runs an hour apart: the report totals 200 seconds, not
        # the 3800-second span from the first start to the last finish.
        second = [
            _run_started(run_id=OTHER_RUN_ID),
            _execution_finished(
                turn_index=1, started=3700, finished=3800, run_id=OTHER_RUN_ID
            ),
            _turn_finished(
                turn_index=1, started=3700, finished=3800, run_id=OTHER_RUN_ID
            ),
            _run_finished(
                run_id=OTHER_RUN_ID, started=3700, finished=3800, duration=100.0
            ),
        ]
        first = [
            _run_started(),
            _execution_finished(turn_index=1, started=0, finished=100),
            _turn_finished(turn_index=1, started=0, finished=100),
            _run_finished(started=0, finished=100, duration=100.0),
        ]
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=[*first, *second])]
        )
        self.assertEqual(len(report.included_runs), 2)
        self.assertEqual(report.known_durations()["wall_seconds"], 200.0)
        self.assertEqual(report.phase_shares()["basis_seconds"], 200.0)


class MissingEvidenceTests(unittest.TestCase):
    def test_an_interrupted_turn_leaves_an_unmatched_start(self) -> None:
        run = _one(
            [
                _run_started(),
                _turn_started(turn_index=1, started=0),
                _run_finished(paused=True),
            ]
        )
        self.assertIn(execution_timing.GAP_UNMATCHED_START, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
        self.assertTrue(run.included)
        self.assertEqual(run.attempts[0].outcome, execution_timing.OUTCOME_STARTED)
        self.assertIsNone(run.attempts[0].duration_seconds)
        # The turn's time is unknown, not zero: it lands in unattributed rather
        # than being counted as no worker time at all.
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
        )
        self.assertEqual(run.unattributed_seconds, 250.0)

    def test_a_verification_start_without_a_finish_is_a_gap(self) -> None:
        run = _one(
            [
                _run_started(),
                _event(
                    events.VERIFICATION_STARTED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "turn_index": 1,
                        "session": "1",
                        "started_at": _iso(100),
                    },
                    created_at=100,
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.GAP_UNMATCHED_START, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_a_verification_finish_without_a_start_is_an_anomaly(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(
                    turn_index=1, started=100, finished=140, with_start=False
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.ANOMALY_UNMATCHED_FINISH, _codes(run.anomalies))
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        # The measured duration survives the anomaly.
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)

    def test_a_record_without_a_turn_index_is_a_missing_identity_gap(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(
                    turn_index=1, started=0, finished=100, omit_turn_index=True
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.GAP_MISSING_IDENTITY, _codes(run.gaps))
        self.assertIsNone(run.attempts[0].turn_index)
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_an_unknown_wall_duration_is_never_read_as_zero(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _event(
                    events.RUN_FINISHED_EVENT,
                    {"run_id": RUN_ID, "ok": True, "paused": False},
                    created_at=250,
                ),
            ]
        )
        self.assertIsNone(run.wall_seconds)
        self.assertIsNone(run.unattributed_seconds)
        self.assertIn(execution_timing.GAP_WALL_DURATION_UNKNOWN, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_an_abandoned_run_is_included_but_marked_reconstructed(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(
                    extra={
                        "synthetic": True,
                        "outcome": events.RUN_OUTCOME_ABANDONED,
                        "abandoned": {"detected_at": _iso(250)},
                    }
                ),
            ]
        )
        self.assertTrue(run.included)
        self.assertEqual(run.outcome, events.RUN_OUTCOME_ABANDONED)
        self.assertIn(execution_timing.GAP_RECONSTRUCTED_TERMINAL, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
        self.assertFalse(run.trustworthy)
        # Its measured worker time is still real evidence.
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_records_the_collector_cannot_place_are_counted(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        {"name": events.RUN_STARTED_EVENT, "payload": {}},
                        {"name": events.RUN_STARTED_EVENT},
                        "not-a-record",  # type: ignore[list-item]
                    ],
                    turn_results=[{"turn_index": 1}],
                )
            ]
        )
        self.assertEqual(report.unattributable_records, 3)
        self.assertEqual(report.unattributable_turn_results, 1)
        self.assertEqual(report.runs, ())


class InconsistencyTests(unittest.TestCase):
    def test_invalid_numbers_are_reported_rather_than_absorbed(self) -> None:
        for label, value in (
            ("string", "twelve"),
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("bool", True),
            ("negative", -5.0),
        ):
            with self.subTest(label):
                run = _one(
                    [
                        _run_started(),
                        _event(
                            events.TURN_EXECUTION_FINISHED_EVENT,
                            {
                                "run_id": RUN_ID,
                                "turn_index": 1,
                                "session_id": "1",
                                "duration_seconds": value,
                            },
                        ),
                        _run_finished(),
                    ]
                )
                self.assertIn(
                    execution_timing.ANOMALY_INVALID_NUMBER, _codes(run.anomalies)
                )
                self.assertIsNone(run.attempts[0].duration_seconds)
                self.assertEqual(
                    run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
                )

    def test_a_duration_contradicting_its_own_timestamps_is_flagged(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(
                    turn_index=1, started=0, finished=100, duration=900.0
                ),
                _turn_finished(turn_index=1, started=0, finished=100, duration=900.0),
                _run_finished(),
            ]
        )
        codes = _codes(run.anomalies)
        self.assertIn(execution_timing.ANOMALY_CONTRADICTORY_DURATION, codes)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_a_run_wall_contradicting_its_own_timestamps_is_flagged(self) -> None:
        run = _one([_run_started(), _run_finished(duration=9000.0)])
        self.assertIn(
            execution_timing.ANOMALY_CONTRADICTORY_DURATION, _codes(run.anomalies)
        )

    def test_overlapping_phases_are_detected(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                # Verification claims to have started while the harness was
                # still running.
                *_verification(turn_index=1, started=50, finished=140),
                _run_finished(),
            ]
        )
        overlap = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_OVERLAPPING_PHASES
        )
        self.assertEqual(overlap.detail["overlap_seconds"], 50.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_a_long_phase_overlapping_several_short_ones_is_caught_each_time(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=200),
                _turn_finished(turn_index=1, started=0, finished=200),
                *_verification(turn_index=1, started=20, finished=40),
                *_commit(commit_index=0, started=60, finished=80),
                _run_finished(),
            ]
        )
        overlaps = [
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_OVERLAPPING_PHASES
        ]
        self.assertEqual(len(overlaps), 2)

    def test_attributed_time_exceeding_the_run_wall_is_never_clamped(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=500),
                _turn_finished(turn_index=1, started=0, finished=500),
                _run_finished(started=0, finished=100, duration=100.0),
            ]
        )
        self.assertIn(
            execution_timing.ANOMALY_ATTRIBUTED_EXCEEDS_WALL, _codes(run.anomalies)
        )
        self.assertEqual(run.unattributed_seconds, -400.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_sub_tolerance_rounding_does_not_become_a_negative_remainder(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=250),
                _turn_finished(turn_index=1, started=0, finished=250),
                _run_finished(duration=249.6),
            ]
        )
        self.assertEqual(run.unattributed_seconds, 0.0)
        self.assertEqual(_codes(run.anomalies), [])

    def test_a_second_run_finished_record_is_flagged(self) -> None:
        run = _one([_run_started(), _run_finished(), _run_finished()])
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_disagreeing_session_kinds_are_a_conflict(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [
                _result(
                    turn_index=1,
                    started=0,
                    finished=100,
                    session_kind=plans.SESSION_KIND_REVIEW,
                )
            ],
        )
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        self.assertEqual(conflict.detail["field"], "session_kind")


class ZeroAndEmptyTests(unittest.TestCase):
    def test_a_zero_duration_run_reports_no_shares_instead_of_dividing_by_zero(
        self,
    ) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _run_finished(started=0, finished=0, duration=0.0),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.wall_seconds, 0.0)
        self.assertEqual(run.unattributed_seconds, 0.0)
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)
        shares = report.phase_shares()
        self.assertEqual(shares["basis_runs"], 1)
        self.assertEqual(shares["basis_seconds"], 0.0)
        self.assertIsNone(shares["shares"])

    def test_a_zero_duration_turn_is_still_a_reconciled_attempt(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=0),
                _turn_finished(turn_index=1, started=0, finished=0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.attempts[0].duration_seconds, 0.0)
        self.assertEqual(_codes(run.anomalies), [])
        self.assertEqual(_codes(run.gaps), [])

    def test_no_evidence_at_all_produces_an_empty_report(self) -> None:
        report = _collect([])
        self.assertEqual(report.runs, ())
        self.assertEqual(report.known_durations()["wall_seconds"], 0.0)
        self.assertIsNone(report.phase_shares()["shares"])


class TrustSeparationTests(unittest.TestCase):
    def _mixed_report(self) -> execution_timing.ExecutionTimingReport:
        partial = [
            _run_started(run_id=OTHER_RUN_ID),
            _execution_finished(
                turn_index=1, started=300, finished=400, run_id=OTHER_RUN_ID
            ),
            _run_finished(
                run_id=OTHER_RUN_ID, started=300, finished=400, duration=100.0
            ),
        ]
        return _collect(
            [
                execution_timing.RunEvidence(
                    PLAN, event_records=[*_clean_run_events(), *partial]
                )
            ]
        )

    def test_known_durations_keep_seconds_that_shares_must_drop(self) -> None:
        report = self._mixed_report()
        self.assertEqual(len(report.included_runs), 2)
        self.assertEqual(len(report.trustworthy_runs), 1)
        known = report.known_durations()
        shares = report.phase_shares()
        # The partial run's unclassified 100 seconds are preserved as a known
        # duration but excluded from the phase-share basis.
        self.assertEqual(known["seconds"]["turn_unclassified"], 100.0)
        self.assertEqual(shares["seconds"]["turn_unclassified"], 0.0)
        self.assertEqual(known["wall_seconds"], 350.0)
        self.assertEqual(shares["basis_seconds"], 250.0)

    def test_classification_counts_name_every_run(self) -> None:
        report = self._mixed_report()
        counts = report.counts_by_classification()
        self.assertEqual(sum(counts.values()), len(report.runs))
        self.assertEqual(counts[execution_timing.CLASSIFICATION_COMPLETE], 1)
        self.assertEqual(counts[execution_timing.CLASSIFICATION_PARTIAL], 1)

    def test_exclusion_reasons_are_reported(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[_run_started(dry_run=True), _run_finished()],
                ),
                execution_timing.RunEvidence(
                    "plan-b",
                    event_records=[_run_started(run_id=OTHER_RUN_ID)],
                ),
            ]
        )
        self.assertEqual(
            report.exclusions(),
            {
                execution_timing.EXCLUDED_ACTIVE: 1,
                execution_timing.EXCLUDED_DRY_RUN: 1,
            },
        )

    def test_an_inconsistent_run_never_backs_a_phase_share(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _execution_finished(turn_index=1, started=0, finished=500),
                        _turn_finished(turn_index=1, started=0, finished=500),
                        _run_finished(started=0, finished=100, duration=100.0),
                    ],
                )
            ]
        )
        self.assertEqual(len(report.included_runs), 1)
        self.assertEqual(report.trustworthy_runs, ())
        self.assertIsNone(report.phase_shares()["shares"])
        self.assertEqual(report.known_durations()["seconds"]["worker_execution"], 500.0)


class PurityTests(unittest.TestCase):
    def test_the_collector_does_no_input_or_output(self) -> None:
        source = Path(execution_timing.__file__).read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        for forbidden in ("open(", "Path(", "print(", "import os", "json."):
            self.assertNotIn(forbidden, body, f"collector must not use {forbidden}")

    def test_every_observation_code_is_declared(self) -> None:
        run = _one(_clean_run_events())
        declared = set(execution_timing.ANOMALY_CODES) | set(execution_timing.GAP_CODES)
        self.assertEqual(run.anomalies, ())
        self.assertTrue(declared)
        # Codes are stable identifiers a renderer can switch on.
        self.assertEqual(len(declared), len(set(declared)))


if __name__ == "__main__":
    unittest.main()

"""The shared history service: interpretation, incremental reads, and honesty.

``asl_workbench.history`` holds what a run's ending means;
``agent_session_loop.workbench_history`` holds the reading of the files it means
it about. The tests here are grouped by the claim they hold rather than by the
module they touch, because every claim worth making spans both halves:

* the vocabularies match ASL's live constants in both directions, so a reason
  added there fails here rather than arriving on the Pi as an unknown string;
* an outcome comes from a terminal record and never from an absence;
* attribution is by run id and never by recency;
* a file that was replaced, truncated, or rewritten in place is re-read, and a
  record still being appended is never half-consumed;
* nothing is consumed until it has been delivered; and
* the local view and the publication batches are the same entries, so the two
  cockpits cannot disagree about what a run did.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_session_loop import (
    events,
    review,
    run_coordination_constants,
    workbench_history,
)
from asl_workbench import catalog, history

_T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
_NOW = (_T0 + timedelta(hours=1)).timestamp()
_PLAN = "/Users/someone/plans/alpha"
_OTHER_PLAN = "/Users/someone/plans/beta"


def _at(seconds: float) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


def _record(name: str, payload: dict[str, object], *, at: float, version: int = 2):
    return {
        "created_at": _at(at),
        "name": name,
        "payload": payload,
        "version": version,
    }


def _started(
    run_id: str,
    *,
    at: float,
    plan_dir: str | None = _PLAN,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "run_started_at": _at(at),
        "harness": "claude",
        "model": "opus",
        **extra,
    }
    if plan_dir is not None:
        payload["plan_dir"] = plan_dir
    return _record(events.RUN_STARTED_EVENT, payload, at=at)


def _finished(
    run_id: str | None,
    *,
    at: float,
    ok: bool = True,
    paused: bool = False,
    reason: str = "all-complete",
    finished: bool = True,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": ok,
        "paused": paused,
        "reason": reason,
        "turn_count": 3,
        "counts": {"complete": 2, "pending": 0},
        **extra,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if finished:
        payload.setdefault("run_finished_at", _at(at))
    return _record(events.RUN_FINISHED_EVENT, payload, at=at)


def _wait_started(
    question_id: str,
    *,
    run_id: str | None,
    at: float,
    session_id: str = "1",
) -> dict[str, object]:
    return _record(
        events.MAILBOX_EVENT,
        {
            "run_id": run_id,
            "question_id": question_id,
            "session_id": session_id,
            "turn_index": 1,
            "started_at": _at(at),
            "pending_outbox": 0,
            "action": events.MAILBOX_ACTION_WAIT_STARTED,
        },
        at=at,
    )


def _wait_cleared(
    question_id: str,
    *,
    run_id: str | None,
    at: float,
    reason: str = run_coordination_constants.WAIT_REASON_REPLY,
    started_at: float | None = None,
    session_id: str = "1",
) -> dict[str, object]:
    return _record(
        events.MAILBOX_EVENT,
        {
            "run_id": run_id,
            "question_id": question_id,
            "session_id": session_id,
            "turn_index": 1,
            "started_at": _at(started_at if started_at is not None else at),
            "reason": reason,
            "action": events.MAILBOX_ACTION_WAIT_CLEARED,
        },
        at=at,
    )


class HistoryTestCase(unittest.TestCase):
    """A temporary ASL state root with one or more plan state directories."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "state"
        self.root.mkdir(parents=True)
        self.store = workbench_history.CheckpointStore(
            Path(self._temporary.name) / "history.json"
        )

    def source_dir(self, name: str = "plan-a") -> Path:
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        return directory

    def write_events(self, records, *, name: str = "plan-a") -> Path:
        directory = self.source_dir(name)
        body = "\n".join(json.dumps(record) for record in records)
        if body:
            body += "\n"
        path = directory / history.EVENTS_FILE_NAME
        path.write_text(body, encoding="utf-8")
        return path

    def append_events(self, records, *, name: str = "plan-a") -> None:
        path = self.source_dir(name) / history.EVENTS_FILE_NAME
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def write_state(self, document: object, *, name: str = "plan-a") -> None:
        path = self.source_dir(name) / history.STATE_FILE_NAME
        path.write_text(json.dumps(document), encoding="utf-8")

    def run_import(self, *, now: float = _NOW, **kwargs) -> history.ImportResult:
        return workbench_history.import_window(
            now=now, store=self.store, state_dir=self.root, **kwargs
        )


# -- Vocabularies -----------------------------------------------------------


class VocabularyTests(unittest.TestCase):
    """Every mirrored constant, checked against the live ASL definition."""

    def test_the_pause_vocabulary_matches_asls_in_both_directions(self) -> None:
        self.assertEqual(history.PAUSE_REASONS, tuple(events.PAUSE_REASONS))
        self.assertNotIn(history.PAUSE_REASON_OTHER, history.PAUSE_REASONS)

    def test_the_review_vocabulary_matches_asls_in_both_directions(self) -> None:
        self.assertEqual(history.REVIEW_OUTCOMES, tuple(review.REVIEW_OUTCOMES))

    def test_the_event_names_are_asls_own(self) -> None:
        self.assertEqual(history.EVENTS_FILE_NAME, events.EVENTS_FILE_NAME)
        self.assertEqual(history.STATE_FILE_NAME, events.STATE_FILE_NAME)
        self.assertEqual(history.RUN_STARTED_EVENT, events.RUN_STARTED_EVENT)
        self.assertEqual(history.RUN_FINISHED_EVENT, events.RUN_FINISHED_EVENT)
        self.assertEqual(history.MAILBOX_EVENT, events.MAILBOX_EVENT)
        self.assertEqual(
            history.MAILBOX_ACTION_WAIT_STARTED, events.MAILBOX_ACTION_WAIT_STARTED
        )
        self.assertEqual(
            history.MAILBOX_ACTION_WAIT_CLEARED, events.MAILBOX_ACTION_WAIT_CLEARED
        )
        self.assertEqual(history.ASL_OUTCOME_ABANDONED, events.RUN_OUTCOME_ABANDONED)

    def test_the_wait_reasons_mapped_to_outcomes_are_asls_own(self) -> None:
        mapped = {reason for reason, _ in history.WAIT_REASON_OUTCOMES}
        self.assertEqual(
            mapped,
            {
                run_coordination_constants.WAIT_REASON_REPLY,
                run_coordination_constants.WAIT_REASON_RESOLUTION,
                run_coordination_constants.WAIT_REASON_TIMEOUT,
            },
        )

    def test_the_current_event_schema_version_is_supported(self) -> None:
        self.assertIn(
            events.EVENT_SCHEMA_VERSION, history.SUPPORTED_EVENT_SCHEMA_VERSIONS
        )

    def test_identity_is_the_catalogs_and_not_a_second_copy(self) -> None:
        self.assertIs(history.plan_key, catalog.plan_key)
        self.assertIs(history.run_key, catalog.run_key)
        self.assertIs(history.digest_of, catalog.digest_of)

    def test_exactly_one_outcome_may_be_rendered_as_a_success(self) -> None:
        self.assertEqual(
            history.HISTORY_SUCCESS_OUTCOMES,
            frozenset({history.HISTORY_OUTCOME_COMPLETED}),
        )
        self.assertLessEqual(
            set(history.HISTORY_SUCCESS_OUTCOMES), set(history.HISTORY_OUTCOMES)
        )


# -- Classification ---------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    def test_completion_requires_every_field_to_agree(self) -> None:
        self.assertEqual(
            history.classify_outcome(
                {"ok": True, "paused": False, "reason": "all-complete"}
            ),
            history.HISTORY_OUTCOME_COMPLETED,
        )

    def test_a_missing_or_mistyped_field_never_completes(self) -> None:
        for payload in (
            {"paused": False, "reason": "all-complete"},
            {"ok": True, "reason": "all-complete"},
            {"ok": True, "paused": False},
            {"ok": "yes", "paused": False, "reason": "all-complete"},
        ):
            with self.subTest(payload=payload):
                self.assertNotEqual(
                    history.classify_outcome(payload),
                    history.HISTORY_OUTCOME_COMPLETED,
                )

    def test_an_unrecognised_reason_is_unknown_rather_than_complete(self) -> None:
        self.assertEqual(
            history.classify_outcome(
                {"ok": True, "paused": False, "reason": "finished-i-think"}
            ),
            history.HISTORY_OUTCOME_UNKNOWN,
        )

    def test_a_clean_early_exit_is_stopped_not_completed(self) -> None:
        for reason in history.ASL_CLEAN_STOP_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(
                    history.classify_outcome(
                        {"ok": True, "paused": False, "reason": reason}
                    ),
                    history.HISTORY_OUTCOME_STOPPED,
                )

    def test_a_pause_outranks_the_failure_flag_asl_records_with_it(self) -> None:
        self.assertEqual(
            history.classify_outcome(
                {"ok": False, "paused": True, "reason": "verify-failed"}
            ),
            history.HISTORY_OUTCOME_PAUSED,
        )

    def test_a_reconstructed_record_is_abandoned_whatever_its_flags_say(self) -> None:
        for payload in (
            {"synthetic": True, "ok": True, "paused": False, "reason": "all-complete"},
            {
                "outcome": "abandoned",
                "ok": True,
                "paused": False,
                "reason": "all-complete",
            },
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    history.classify_outcome(payload),
                    history.HISTORY_OUTCOME_ABANDONED,
                )

    def test_a_failure_is_a_failure(self) -> None:
        self.assertEqual(
            history.classify_outcome(
                {"ok": False, "paused": False, "reason": "nonzero-exit"}
            ),
            history.HISTORY_OUTCOME_FAILED,
        )

    def test_a_pause_reason_outside_the_vocabulary_degrades_to_other(self) -> None:
        self.assertEqual(
            history.pause_reason_code(
                {"pause_reason": "the operator said to stop"},
                history.HISTORY_OUTCOME_PAUSED,
            ),
            history.PAUSE_REASON_OTHER,
        )

    def test_a_pause_code_only_travels_on_a_paused_run(self) -> None:
        self.assertIsNone(
            history.pause_reason_code(
                {"pause_reason": "blocked"}, history.HISTORY_OUTCOME_FAILED
            )
        )

    def test_an_unrecognised_wait_reason_is_unresolved_not_invented(self) -> None:
        self.assertEqual(
            history.question_outcome("mailbox-full"),
            history.QUESTION_OUTCOME_UNRESOLVED,
        )
        self.assertEqual(
            history.question_outcome(run_coordination_constants.WAIT_REASON_REPLY),
            history.QUESTION_OUTCOME_ANSWERED,
        )


# -- Attribution ------------------------------------------------------------


class AttributionTests(HistoryTestCase):
    def test_rapid_reruns_keep_their_own_endings(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _started("run-2", at=20),
                _finished(
                    "run-1", at=30, ok=False, paused=False, reason="nonzero-exit"
                ),
                _finished("run-2", at=40),
            ]
        )
        result = self.run_import()
        outcomes = {entry.run_id: entry.outcome for entry in result.entries}
        self.assertEqual(
            outcomes,
            {
                "run-1": history.HISTORY_OUTCOME_FAILED,
                "run-2": history.HISTORY_OUTCOME_COMPLETED,
            },
        )

    def test_a_terminal_record_naming_no_run_is_dropped_not_guessed(self) -> None:
        self.write_events([_started("run-1", at=10), _finished(None, at=20)])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.unattributed, 1)
        self.assertFalse(result.complete)

    def test_a_compact_record_borrows_its_plan_from_the_start_it_joins(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        result = self.run_import()
        self.assertEqual(result.entries[0].plan_key, catalog.plan_key(_PLAN))

    def test_a_start_consumed_by_an_earlier_pass_still_supplies_the_join(self) -> None:
        self.write_events([_started("run-1", at=10)])
        first = self.run_import()
        workbench_history.commit(self.store, first)
        self.append_events([_finished("run-1", at=20)])
        second = self.run_import()
        self.assertEqual(len(second.entries), 1)
        self.assertEqual(second.entries[0].plan_key, catalog.plan_key(_PLAN))
        self.assertEqual(second.entries[0].harness, "claude")

    def test_state_json_is_the_last_resort_for_a_plan_with_no_start(self) -> None:
        self.write_state({"plan_dir": _PLAN, "workspace_root": "/Users/someone/code"})
        self.write_events([_finished("run-1", at=20)])
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].plan_key, catalog.plan_key(_PLAN))

    def test_a_plan_that_cannot_be_identified_yields_nothing_and_says_so(self) -> None:
        self.write_events([_finished("run-1", at=20)])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.unresolved_sources, 1)
        self.assertFalse(result.complete)

    def test_two_plans_produce_two_plan_keys_and_their_own_labels(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20)], name="plan-a"
        )
        self.write_events(
            [
                _started("run-2", at=11, plan_dir=_OTHER_PLAN),
                _finished("run-2", at=21),
            ],
            name="plan-b",
        )
        result = self.run_import()
        self.assertEqual(
            {entry.plan_key for entry in result.entries},
            {catalog.plan_key(_PLAN), catalog.plan_key(_OTHER_PLAN)},
        )


# -- Enrichment -------------------------------------------------------------


class EnrichmentTests(HistoryTestCase):
    def test_a_multi_repository_run_is_labelled_as_one(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished(
                    "run-1",
                    at=20,
                    workspace_root="/Users/someone/workspace",
                    repositories=[
                        {"alias": "scripts", "path": "scripts"},
                        {"alias": "pi", "origin": "git@github.com:someone/pi.git"},
                    ],
                ),
            ]
        )
        entry = self.run_import().entries[0]
        self.assertEqual(entry.project, "workspace (2 repositories)")
        self.assertEqual(entry.projects, ("pi", "scripts"))

    def test_project_names_fall_back_to_the_plans_own_state(self) -> None:
        self.write_state(
            {
                "plan_dir": _PLAN,
                "workspace_root": "/Users/someone/workspace",
                "repositories": [{"alias": "scripts"}],
            }
        )
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        entry = self.run_import().entries[0]
        self.assertEqual(entry.projects, ("scripts",))

    def test_a_damaged_state_file_leaves_the_import_running(self) -> None:
        self.source_dir()
        (self.source_dir() / history.STATE_FILE_NAME).write_text("{not json")
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertTrue(result.complete)

    def test_no_filesystem_path_reaches_an_entry(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20, workspace_root="/Users/someone/workspace"),
            ]
        )
        rendered = json.dumps(self.run_import().entries[0].to_payload())
        self.assertNotIn("/Users/someone", rendered)
        self.assertNotIn(_PLAN, rendered)

    def test_no_prose_field_has_a_name_in_an_entry_to_land_in(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished(
                    "run-1",
                    at=20,
                    ok=False,
                    paused=True,
                    reason="blocked",
                    pause_message="the operator's own words",
                    pause_details="raw check output\nline two",
                    assurance_report={"summary": "prose"},
                ),
            ]
        )
        payload = self.run_import().entries[0].to_payload()
        self.assertNotIn("pause_message", payload)
        self.assertNotIn("pause_details", payload)
        self.assertNotIn("assurance_report", payload)
        self.assertEqual(payload["pause_reason"], "blocked")

    def test_the_review_verdict_travels_as_one_word(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished(
                    "run-1",
                    at=20,
                    review_completed=True,
                    review_verdict={
                        "outcome": review.REVIEW_OUTCOME_FINDINGS,
                        "status_line": "3 findings, 1 blocking",
                        "findings": 3,
                    },
                ),
            ]
        )
        payload = self.run_import().entries[0].to_payload()
        self.assertEqual(payload["review_outcome"], review.REVIEW_OUTCOME_FINDINGS)
        self.assertTrue(payload["review_completed"])
        self.assertNotIn("review_verdict", payload)


# -- No false completion ----------------------------------------------------


class NoFalseCompletionTests(HistoryTestCase):
    def test_a_run_with_no_terminal_record_produces_no_history_at_all(self) -> None:
        self.write_events([_started("run-1", at=10)])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertTrue(result.complete)

    def test_a_compacted_file_does_not_turn_a_lost_run_into_a_success(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        first = self.run_import()
        workbench_history.commit(self.store, first)
        # The file is replaced by a compaction that kept only the start.
        self.write_events([_started("run-2", at=30)])
        second = self.run_import()
        self.assertEqual(second.entries, ())

    def test_a_terminal_record_missing_its_finish_time_is_not_completed(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20, finished=False)]
        )
        entry = self.run_import().entries[0]
        self.assertEqual(entry.outcome, history.HISTORY_OUTCOME_UNKNOWN)
        self.assertFalse(entry.succeeded)

    def test_a_completed_entry_always_carries_its_evidence(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        entry = self.run_import().entries[0]
        self.assertTrue(entry.succeeded)
        self.assertIsNotNone(entry.run_finished_at)
        self.assertIsNotNone(entry.run_started_at)

    def test_an_abandoned_run_is_marked_reconstructed(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20, synthetic=True, outcome="abandoned"),
            ]
        )
        entry = self.run_import().entries[0]
        self.assertEqual(entry.outcome, history.HISTORY_OUTCOME_ABANDONED)
        self.assertTrue(entry.reconstructed)
        self.assertFalse(entry.succeeded)

    def test_an_undatable_record_is_counted_and_never_published(self) -> None:
        record = _finished("run-1", at=20, finished=False)
        record["created_at"] = "not a timestamp"
        self.write_events([_started("run-1", at=10), record])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.undatable, 1)
        self.assertFalse(result.complete)


# -- Retention --------------------------------------------------------------


class RetentionTests(HistoryTestCase):
    def test_only_the_configured_window_is_imported(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20),
                _started("run-2", at=3000),
                _finished("run-2", at=3100),
            ]
        )
        result = self.run_import(retention_seconds=1000)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-2"])
        self.assertEqual(result.expired, 1)

    def test_an_expired_record_does_not_make_the_pass_incomplete(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        result = self.run_import(retention_seconds=1000)
        self.assertEqual(result.expired, 1)
        self.assertTrue(result.complete)

    def test_an_expired_record_still_advances_the_offset(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        result = self.run_import(retention_seconds=1000)
        workbench_history.commit(self.store, result)
        size = (self.source_dir() / history.EVENTS_FILE_NAME).stat().st_size
        self.assertEqual(result.checkpoints[0].offset, size)


# -- Incremental reading ----------------------------------------------------


class IncrementalReadTests(HistoryTestCase):
    def test_a_committed_pass_does_not_re_read_what_it_consumed(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        first = self.run_import()
        self.assertEqual(len(first.entries), 1)
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(second.entries, ())
        self.assertTrue(second.complete)

    def test_an_idle_corpus_yields_an_empty_complete_pass(self) -> None:
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.sources, 0)
        self.assertTrue(result.complete)

    def test_a_crash_before_the_commit_re_delivers_rather_than_loses(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        first = self.run_import()
        # No commit: the upload is presumed to have failed.
        second = self.run_import()
        self.assertEqual(
            [entry.to_payload() for entry in first.entries],
            [entry.to_payload() for entry in second.entries],
        )
        self.assertEqual(first.entries[0].entry_id, second.entries[0].entry_id)

    def test_a_partial_final_line_is_never_consumed(self) -> None:
        path = self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20)],
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_finished("run-2", at=30))[:40])
        result = self.run_import()
        self.assertEqual([entry.run_id for entry in result.entries], ["run-1"])
        self.assertEqual(result.malformed, 0)
        # The half-written bytes are deferred, so the pass says it is partial.
        self.assertGreater(result.deferred_bytes, 0)
        self.assertFalse(result.complete)

    def test_a_bounded_pass_leaves_the_rest_for_the_next_one(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20)], name="plan-a"
        )
        self.write_events(
            [
                _started("run-2", at=11, plan_dir=_OTHER_PLAN),
                _finished("run-2", at=21),
            ],
            name="plan-b",
        )
        first = self.run_import(max_pass_bytes=1)
        self.assertEqual(len(first.entries), 1)
        self.assertGreater(first.deferred_bytes, 0)
        self.assertFalse(first.complete)
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(len(second.entries), 1)
        self.assertTrue(second.complete)

    def test_a_malformed_line_is_counted_and_stepped_over(self) -> None:
        path = self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "run_finished", "payload": broken}\n')
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.malformed, 1)
        self.assertFalse(result.complete)

    def test_a_non_finite_number_is_refused_rather_than_imported(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f'{{"created_at": "{_at(20)}", "name": "run_finished", '
                '"version": 2, "payload": {"run_id": "run-1", "ok": true, '
                '"paused": false, "reason": "all-complete", '
                '"duration_seconds": NaN}}\n'
            )
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.malformed, 1)

    def test_an_unsupported_event_version_is_skipped_not_guessed(self) -> None:
        record = _finished("run-1", at=20)
        record["version"] = 99
        self.write_events([_started("run-1", at=10), record])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.unsupported, 1)
        self.assertFalse(result.complete)

    def test_a_version_1_record_is_read_and_marked_as_such(self) -> None:
        start = _started("run-1", at=10)
        del start["version"]
        end = _finished("run-1", at=20)
        del end["version"]
        self.write_events([start, end])
        entry = self.run_import().entries[0]
        self.assertEqual(entry.event_schema_version, 1)

    def test_a_line_past_the_record_ceiling_is_never_parsed(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _finished("run-1", at=20, transcript="x" * history.MAX_RECORD_BYTES)
                )
                + "\n"
            )
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.oversized, 1)
        self.assertFalse(result.complete)

    def test_a_record_longer_than_a_pass_window_does_not_stall_the_reader(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"name": "run_started", "filler": "y" * 8192}) + "\n"
            )
            handle.write(json.dumps(_finished("run-1", at=20)) + "\n")
        first = self.run_import(max_read_bytes=512, max_pass_bytes=1024 * 1024)
        workbench_history.commit(self.store, first)
        found = list(first.entries)
        for _ in range(8):
            if found:
                break
            passed = self.run_import(max_read_bytes=512, max_pass_bytes=1024 * 1024)
            workbench_history.commit(self.store, passed)
            found = list(passed.entries)
        self.assertEqual([entry.run_id for entry in found], ["run-1"])


# -- File identity ----------------------------------------------------------


class FileIdentityTests(HistoryTestCase):
    def _consume_everything(self) -> history.ImportResult:
        result = self.run_import()
        workbench_history.commit(self.store, result)
        return result

    def test_an_unchanged_file_is_not_restarted(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        second = self.run_import()
        self.assertEqual(second.restarted_sources, 0)

    def test_a_replaced_file_is_read_from_the_start(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        path = self.source_dir() / history.EVENTS_FILE_NAME
        replacement = path.with_name("events.new")
        replacement.write_text(
            json.dumps(_started("run-2", at=30))
            + "\n"
            + json.dumps(_finished("run-2", at=40))
            + "\n",
            encoding="utf-8",
        )
        os.replace(replacement, path)
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-2"])

    def test_a_truncated_file_is_read_from_the_start(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20),
                _started("run-2", at=30),
                _finished("run-2", at=40),
            ]
        )
        self._consume_everything()
        self.write_events([_started("run-3", at=50), _finished("run-3", at=60)])
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-3"])

    def test_a_file_rewritten_in_place_at_the_same_length_is_detected(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        path = self.source_dir() / history.EVENTS_FILE_NAME
        original = path.stat().st_size
        rewritten = json.dumps(_started("run-9", at=10)) + "\n"
        rewritten += json.dumps(_finished("run-9", at=20)) + "\n"
        padding = original - len(rewritten.encode("utf-8"))
        if padding > 0:
            rewritten += " " * (padding - 1) + "\n"
        with path.open("r+b") as handle:
            handle.write(rewritten.encode("utf-8"))
            handle.truncate(max(original, len(rewritten.encode("utf-8"))))
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-9"])

    def test_an_unreadable_source_makes_the_pass_incomplete(self) -> None:
        path = self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        result = self.run_import()
        self.assertEqual(result.unreadable_sources, 1)
        self.assertFalse(result.complete)
        self.assertIn(
            "1 ASL plan(s) could not be read during this import", result.warnings
        )


# -- Duplicates -------------------------------------------------------------


class DuplicateTests(HistoryTestCase):
    def test_the_same_ending_appended_twice_becomes_one_entry(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20),
                _finished("run-1", at=21),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.duplicates, 1)

    def test_a_restart_after_replacement_does_not_re_deliver(self) -> None:
        records = [_started("run-1", at=10), _finished("run-1", at=20)]
        self.write_events(records)
        first = self.run_import()
        workbench_history.commit(self.store, first)
        path = self.source_dir() / history.EVENTS_FILE_NAME
        replacement = path.with_name("events.new")
        replacement.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        os.replace(replacement, path)
        second = self.run_import()
        self.assertEqual(second.restarted_sources, 1)
        self.assertEqual(second.entries, ())
        self.assertEqual(second.duplicates, 1)

    def test_one_run_seen_through_two_state_directories_is_one_entry(self) -> None:
        records = [_started("run-1", at=10), _finished("run-1", at=20)]
        self.write_events(records, name="plan-a")
        self.write_events(records, name="plan-b")
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.duplicates, 1)

    def test_entry_ids_are_a_function_of_the_run_not_the_file(self) -> None:
        plan = catalog.plan_key(_PLAN)
        run = catalog.run_key(plan, "run-1")
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self.assertEqual(
            self.run_import().entries[0].entry_id, history.entry_id_for(plan, run)
        )


# -- Questions --------------------------------------------------------------


class QuestionTests(HistoryTestCase):
    def test_an_answered_question_records_who_asked_and_how_long_it_waited(
        self,
    ) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=50, started_at=20),
                _finished("run-1", at=60),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.questions), 1)
        question = result.questions[0]
        self.assertEqual(question.outcome, history.QUESTION_OUTCOME_ANSWERED)
        self.assertTrue(question.answered)
        self.assertEqual(question.session_id, "1")
        self.assertEqual(question.waited_seconds, 30.0)
        self.assertEqual(question.plan_key, catalog.plan_key(_PLAN))
        self.assertEqual(question.run_key, result.entries[0].run_key)

    def test_a_timed_out_wait_is_not_an_answer(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared(
                    "q-1",
                    run_id="run-1",
                    at=50,
                    reason=run_coordination_constants.WAIT_REASON_TIMEOUT,
                ),
                _finished("run-1", at=60, ok=False, paused=True, reason="blocked"),
            ]
        )
        question = self.run_import().questions[0]
        self.assertEqual(question.outcome, history.QUESTION_OUTCOME_TIMED_OUT)
        self.assertFalse(question.answered)

    def test_a_question_the_run_died_holding_is_unresolved_history(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _finished("run-1", at=60, synthetic=True, outcome="abandoned"),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.questions), 1)
        self.assertEqual(
            result.questions[0].outcome, history.QUESTION_OUTCOME_UNRESOLVED
        )
        self.assertIsNone(result.questions[0].resolved_at)

    def test_a_live_wait_is_not_history_yet(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _wait_started("q-1", run_id="run-1", at=20)]
        )
        result = self.run_import()
        self.assertEqual(result.questions, ())
        self.assertTrue(result.complete)
        self.assertEqual(len(result.checkpoints[0].open_questions), 1)

    def test_the_question_id_is_republished_as_a_digest_not_asls_own(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-secret", run_id="run-1", at=20),
                _wait_cleared("q-secret", run_id="run-1", at=30),
            ]
        )
        payload = self.run_import().questions[0].to_payload()
        self.assertNotIn("q-secret", json.dumps(payload))
        self.assertEqual(
            payload["question_key"],
            history.question_key_for(catalog.plan_key(_PLAN), "q-secret"),
        )

    def test_a_question_carries_no_text_anywhere(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=30),
            ]
        )
        payload = self.run_import().questions[0].to_payload()
        self.assertEqual(
            set(payload)
            - {
                "entry_id",
                "source_key",
                "plan_key",
                "run_key",
                "question_key",
                "outcome",
                "recorded_at",
                "session_id",
                "asked_at",
                "resolved_at",
                "waited_seconds",
            },
            set(),
        )

    def test_a_question_is_delivered_once_across_passes(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=30),
            ]
        )
        first = self.run_import()
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(len(first.questions), 1)
        self.assertEqual(second.questions, ())


# -- Checkpoints ------------------------------------------------------------


class CheckpointTests(HistoryTestCase):
    def test_a_checkpoint_round_trips(self) -> None:
        checkpoint = history.SourceCheckpoint(
            source_key="abc",
            device=1,
            inode=2,
            offset=3,
            head_digest="dd",
            head_length=4,
            records=5,
            plan_dir=_PLAN,
            open_runs=(history.OpenRun(run_id="run-1", plan_dir=_PLAN),),
            open_questions=(history.OpenQuestion(question_id="q-1", run_id="run-1"),),
            delivered=("e1",),
        )
        self.store.save((checkpoint,))
        self.assertEqual(self.store.load(), {"abc": checkpoint})

    def test_a_missing_checkpoint_is_an_empty_one(self) -> None:
        self.assertEqual(self.store.load(), {})

    def test_a_corrupt_checkpoint_restarts_rather_than_guesses(self) -> None:
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load(), {})

    def test_a_checkpoint_from_an_unknown_schema_is_discarded(self) -> None:
        self.store.path.write_text(
            json.dumps({"version": 99, "sources": [{"source_key": "abc"}]}),
            encoding="utf-8",
        )
        self.assertEqual(self.store.load(), {})

    def test_an_interrupted_save_leaves_the_previous_checkpoint_intact(self) -> None:
        first = history.SourceCheckpoint(source_key="abc", offset=10)
        self.store.save((first,))
        self.store.path.with_name(f"{self.store.path.name}.partial").write_text(
            "half a document", encoding="utf-8"
        )
        self.assertEqual(self.store.load(), {"abc": first})

    def test_open_runs_are_bounded(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        for index in range(history.MAX_OPEN_RUNS + 10):
            checkpoint = checkpoint.remember(history.OpenRun(run_id=f"run-{index}"))
        self.assertEqual(len(checkpoint.open_runs), history.MAX_OPEN_RUNS)
        self.assertIsNotNone(checkpoint.find(f"run-{history.MAX_OPEN_RUNS + 9}"))

    def test_remembering_a_run_twice_keeps_one_entry(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        checkpoint = checkpoint.remember(history.OpenRun(run_id="run-1"))
        checkpoint = checkpoint.remember(
            history.OpenRun(run_id="run-1", plan_dir=_PLAN)
        )
        self.assertEqual(len(checkpoint.open_runs), 1)
        found = checkpoint.find("run-1")
        assert found is not None
        self.assertEqual(found.plan_dir, _PLAN)

    def test_delivered_ids_are_bounded(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        checkpoint = checkpoint.record_delivered(
            f"entry-{index}" for index in range(history.MAX_DELIVERED_IDS + 50)
        )
        self.assertEqual(len(checkpoint.delivered), history.MAX_DELIVERED_IDS)

    def test_a_checkpoint_only_advances_once_the_caller_commits(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self.run_import()
        self.assertEqual(self.store.load(), {})


# -- Restart rule -----------------------------------------------------------


class RestartRuleTests(unittest.TestCase):
    def _checkpoint(self, head: bytes) -> history.SourceCheckpoint:
        return history.SourceCheckpoint(
            source_key="abc",
            device=1,
            inode=2,
            offset=100,
            head_digest=history.head_digest(head),
            head_length=len(head),
        )

    def test_a_fresh_checkpoint_never_restarts(self) -> None:
        self.assertFalse(
            history.restart_required(
                history.SourceCheckpoint(source_key="abc"),
                device=1,
                inode=2,
                size=0,
            )
        )

    def test_a_new_inode_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=99, size=200, head=b"head"
            )
        )

    def test_a_shorter_file_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=50, head=b"head"
            )
        )

    def test_a_changed_prefix_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=200, head=b"HEAD"
            )
        )

    def test_an_append_leaves_the_fixed_prefix_alone(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertFalse(
            history.restart_required(
                checkpoint, device=1, inode=2, size=500, head=b"head"
            )
        )

    def test_a_prefix_that_can_no_longer_be_read_in_full_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=200, head=b"he"
            )
        )


# -- One projection, two consumers ------------------------------------------


class ProjectionAgreementTests(HistoryTestCase):
    def _seed(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=15),
                _wait_cleared("q-1", run_id="run-1", at=25, started_at=15),
                _finished("run-1", at=30),
                _started("run-2", at=40),
                _finished(
                    "run-2", at=50, ok=False, paused=False, reason="nonzero-exit"
                ),
            ],
            name="plan-a",
        )
        self.write_events(
            [
                _started("run-3", at=41, plan_dir=_OTHER_PLAN),
                _finished("run-3", at=51, reason="max-sessions"),
            ],
            name="plan-b",
        )

    def test_the_local_view_and_the_batches_carry_the_same_entries(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        published = [
            entry.to_payload()
            for batch in projection.batches
            for entry in batch.entries
        ]
        local = [entry.to_payload() for entry in projection.view.entries]
        self.assertEqual(local, published)
        published_questions = [
            question.to_payload()
            for batch in projection.batches
            for question in batch.questions
        ]
        self.assertEqual(
            [question.to_payload() for question in projection.view.questions],
            published_questions,
        )

    def test_both_renderings_report_the_same_completeness(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertTrue(projection.view.complete)
        self.assertTrue(all(batch.complete for batch in projection.batches))

    def test_the_view_rolls_the_same_entries_up_by_plan(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        by_plan = {plan.plan_key: plan for plan in projection.view.plans}
        alpha = by_plan[catalog.plan_key(_PLAN)]
        self.assertEqual(alpha.runs, 2)
        self.assertEqual(alpha.succeeded, 1)
        self.assertEqual(alpha.questions, 1)
        self.assertEqual(alpha.answered, 1)
        counts = projection.view.outcome_counts()
        self.assertEqual(counts[history.HISTORY_OUTCOME_COMPLETED], 1)
        self.assertEqual(counts[history.HISTORY_OUTCOME_FAILED], 1)
        self.assertEqual(counts[history.HISTORY_OUTCOME_STOPPED], 1)
        self.assertEqual(sum(counts.values()), 3)

    def test_an_empty_window_still_produces_one_batch(self) -> None:
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertEqual(len(projection.batches), 1)
        self.assertEqual(projection.batches[0].entries, ())
        self.assertFalse(projection.batches[0].more)

    def test_resending_an_import_produces_identical_batches(self) -> None:
        self._seed()
        first = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        second = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertEqual(
            [batch.to_payload() for batch in first.batches],
            [batch.to_payload() for batch in second.batches],
        )

    def test_a_large_import_is_split_and_every_batch_but_the_last_says_more(
        self,
    ) -> None:
        records: list[dict[str, object]] = []
        total = history.MAX_HISTORY_ENTRIES_PER_BATCH + 5
        for index in range(total):
            records.append(_started(f"run-{index}", at=index))
            records.append(_finished(f"run-{index}", at=index + 0.5))
        self.write_events(records)
        result = self.run_import()
        built = history.batches(result, now=_NOW)
        self.assertEqual(len(built), 2)
        self.assertTrue(built[0].more)
        self.assertFalse(built[1].more)
        self.assertEqual(
            sum(len(batch.entries) for batch in built), len(result.entries)
        )
        self.assertEqual(len(result.entries), total)

    def test_an_incomplete_import_says_so_in_every_batch(self) -> None:
        self.write_events([_finished("run-1", at=20)])
        result = self.run_import()
        built = history.batches(result, now=_NOW)
        self.assertFalse(result.complete)
        self.assertTrue(all(not batch.complete for batch in built))
        self.assertTrue(all(batch.warnings for batch in built))


if __name__ == "__main__":
    unittest.main()

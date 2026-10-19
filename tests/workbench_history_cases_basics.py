from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_history_support import *


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

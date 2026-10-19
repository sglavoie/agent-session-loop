from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_history_support import *


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

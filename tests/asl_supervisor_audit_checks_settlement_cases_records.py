# mypy: ignore-errors
from .asl_supervisor_audit_checks_settlement_support import *


class DeterministicCheckSettlementTests(SettlementCheckBase):
    """Settlement records and turn results are graded as findings."""

    def test_a_post_terminal_commit_outside_the_settlement_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # A closed settlement exempts its own group and nothing else: an
            # end-of-run group that kept committing after the pause is the
            # unaccounted work this check exists to catch.
            epilogue = self._settlement_epilogue()
            epilogue.insert(
                3,
                (
                    events.COMMIT_FINISHED_EVENT,
                    {"run_id": RUN_ID, "group_id": "group-run-1", "scope": "run"},
                ),
            )
            self._paused_run(history, epilogue)

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "1 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_session_scoped_group_earns_no_settlement_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The exemption is keyed on the group's own `session-paused` scope.
            # An ordinary session group that opened after the terminal record
            # is a run that kept working, whatever else follows it.
            self._paused_run(
                history,
                self._settlement_epilogue(scope=commit_groups.GROUP_SCOPE_SESSION),
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "2 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_settled_pause_is_graded_rather_than_left_to_the_catch_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(history, self._settlement_epilogue())

            _, results = self._audit(history)

            self.assertEqual("pass", results["paused-work-settlement"].status)
            self.assertIn(
                "1 paused-work settlement(s) recorded an outcome ASL writes",
                results["paused-work-settlement"].detail,
            )
            # The record carries the pause's reason, which is why the catch-all
            # read every successful settlement as an unclassified failure. It
            # is exempt now because the check above owns it.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_run_that_settled_nothing_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, results = self._audit(self._history(Path(tmp)))

            self.assertEqual("pass", results["paused-work-settlement"].status)
            self.assertIn(
                "left no paused work behind",
                results["paused-work-settlement"].detail,
            )

    def test_a_settlement_outcome_asl_never_writes_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="partially-committed")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "'partially-committed', which is no settlement outcome ASL writes",
                results["paused-work-settlement"].detail,
            )

    def test_a_failed_settlement_must_record_why_and_stay_owed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="failed")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            verdict = results["paused-work-settlement"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("failed without recording why", verdict.detail)
            self.assertIn("owing no paused-work commit group", verdict.detail)

    def test_a_failed_settlement_that_accounted_for_itself_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The reason may be written as the caught exception or as the
            # underlying commit's own outcome; `run_reporting` reads the pair.
            record = self._settlement_record(
                result="failed", commit={"status_result": "dirty-after-commit"}
            )
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
                unresolved=[
                    {
                        "group_id": "group-paused-1",
                        "scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                        "session_id": 1,
                        "status": commit_groups.GROUP_STATUS_FAILED,
                    }
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["paused-work-settlement"].status)
            # The loss is still reported, by the check that owns it.
            self.assertEqual("fail", results["commit-group-resolution"].status)

    def test_a_clean_settlement_that_left_residue_contradicts_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(
                result="committed", residue={"repositories": [{"alias": "app"}]}
            )
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "reported a clean commit while naming 1 repository(ies) it left dirty",
                results["paused-work-settlement"].detail,
            )

    def test_a_residue_settlement_must_name_where_the_residue_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="committed-with-residue")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "reported residue while naming no repository it was left in",
                results["paused-work-settlement"].detail,
            )

    def test_a_settlement_state_never_mirrored_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The append and the write happen under one lock, so a record with
            # no mirror is a run that died between them -- and every reader but
            # this check reads the mirror.
            self._paused_run(history, self._settlement_epilogue(), mirror=None)

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "was never mirrored into the run's state",
                results["paused-work-settlement"].detail,
            )

    def test_a_mirror_that_disagrees_with_the_record_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(
                history,
                self._settlement_epilogue(),
                mirror=self._settlement_record(result="no-changes"),
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "recorded 'committed' while state mirrors 'no-changes'",
                results["paused-work-settlement"].detail,
            )

    def test_a_turn_without_a_result_is_never_a_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[3] / "turn-0001-01" / state.RESULT_FILE_NAME).unlink()

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(state.RESULT_FILE_NAME, results["terminal-artifacts"].detail)
            self.assertEqual("unavailable", results["mutation-commit-guards"].status)
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertNotIn(
                "pass",
                {
                    results["terminal-artifacts"].status,
                    results["mutation-commit-guards"].status,
                    results["scope-escape"].status,
                },
            )

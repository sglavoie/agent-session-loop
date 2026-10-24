"""Issue publication, deduplication and unavailable-audit tests."""

from __future__ import annotations

import subprocess
import sys
import threading

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_primitives, assurance_models_summary
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication, ProjectionError
from tests.support_asl_assurance_projection import (
    _PUBLISH_WORKER_SOURCE,
    INPUT_DIGEST,
    LAST_TIMESTAMP,
    LATER_TIMESTAMP,
    OTHER_INPUT_DIGEST,
    OTHER_RUN_ID,
    REPO_ROOT,
    RUN_ID,
    THIRD_INPUT_DIGEST,
    THIRD_RUN_ID,
    TIMESTAMP,
    ProjectionTestCase,
    _artifacts,
    _finding,
    _publication,
    _worker_digest,
    _worker_run_id,
)


class PublicationTests(ProjectionTestCase):
    def test_publishing_writes_the_summary_and_one_observation_per_finding(
        self,
    ) -> None:
        result = self.publish(
            _publication(
                findings=(_finding(), _finding(invariant_key="scope.no-escape"))
            )
        )
        self.assertTrue(result.created)
        self.assertEqual(5, len(result.records))
        self.assertEqual("audit_completed", result.records[0].kind)
        self.assertEqual(
            [
                "issue_observed",
                "issue_observed",
                "issue_identity_bound",
                "issue_identity_bound",
            ],
            [record.kind for record in result.records[1:]],
        )
        self.assertEqual(("ASI-1", "ASI-2"), result.issue_ids)
        self.assertEqual([1, 2, 3, 4, 5], [record.seq for record in result.records])

    def test_the_summary_accounts_for_the_issue_ids_it_opened(self) -> None:
        summary = self.publish(_publication()).summary
        self.assertEqual(("ASI-1",), summary.issue_ids)
        self.assertEqual(1, summary.new_issue_count)
        self.assertEqual(0, summary.recurring_issue_count)
        self.assertEqual("findings", summary.state)

    def test_the_observation_points_at_the_audit_report(self) -> None:
        result = self.publish(_publication())
        observation = result.observations[0].payload
        self.assertEqual("supervisor-audit.json", observation.audit_artifact)
        self.assertEqual(TIMESTAMP, observation.observed_at)

    def test_republishing_the_same_audit_writes_nothing(self) -> None:
        first = self.publish(_publication())
        second = self.publish(_publication())
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual((), second.records)
        self.assertEqual(first.summary, second.summary)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(1, issues[0].occurrence_count)

    def test_republishing_a_different_audit_input_records_a_second_occurrence(
        self,
    ) -> None:
        self.publish(_publication())
        second = self.publish(
            _publication(artifacts=_artifacts(OTHER_INPUT_DIGEST)),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertTrue(second.created)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(2, issues[0].occurrence_count)
        self.assertEqual(0, second.summary.new_issue_count)
        self.assertEqual(1, second.summary.recurring_issue_count)

    def test_a_pending_record_does_not_swallow_a_later_skip(self) -> None:
        pending = self.publish(
            AuditPublication(run_id=RUN_ID, state="pending", eligible=True)
        )
        skipped = self.publish(
            AuditPublication(
                run_id=RUN_ID, state="skipped", eligible=False, reason="opted-out"
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertTrue(pending.created)
        self.assertTrue(skipped.created)
        run = self.read().run(RUN_ID)
        self.assertEqual("skipped", run.state)
        self.assertEqual(("pending", "skipped"), tuple(s.state for s in run.summaries))

    def test_skipping_creates_no_issue(self) -> None:
        self.publish(
            AuditPublication(
                run_id=RUN_ID, state="skipped", eligible=False, reason="opted-out"
            )
        )
        projected = self.read()
        self.assertEqual((), projected.issues)
        self.assertEqual((), projected.run(RUN_ID).issue_ids)

    def test_an_all_clear_audit_carries_no_findings(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError) as caught:
            AuditPublication(
                run_id=RUN_ID,
                state="all-clear",
                eligible=True,
                findings=(_finding(),),
            )
        self.assertIn("opens no issue", str(caught.exception))

    def test_a_findings_audit_must_carry_a_finding(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            AuditPublication(run_id=RUN_ID, state="findings", eligible=True)

    def test_one_audit_cannot_report_the_same_identity_twice(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError) as caught:
            AuditPublication(
                run_id=RUN_ID,
                state="findings",
                eligible=True,
                findings=(_finding(), _finding(severity="low")),
            )
        self.assertIn("repeats the finding identity", str(caught.exception))

    def test_a_linked_issue_the_inbox_never_opened_is_refused(self) -> None:
        self.publish(_publication())

        with self.assertRaises(projection.ProjectionError) as caught:
            self.publish(
                _publication(
                    run_id=OTHER_RUN_ID,
                    artifacts=_artifacts(OTHER_INPUT_DIGEST),
                    linked_issue_ids=("ASI-999",),
                    completed_at=LATER_TIMESTAMP,
                ),
                recorded_at=LATER_TIMESTAMP,
            )

        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        # The refusal is the whole transaction: no summary, no observation, no
        # binding, and no run that names a phantom issue.
        self.assertEqual(3, len(ledger.read_ledger(state_dir=self.state_dir).records))
        projected = self.read()
        self.assertEqual((RUN_ID,), tuple(run.run_id for run in projected.runs))
        self.assertEqual(
            ("ASI-1",), tuple(issue.issue_id for issue in projected.issues)
        )

    def test_a_linked_issue_the_ledger_opened_is_named_once(self) -> None:
        self.publish(_publication())

        result = self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                linked_issue_ids=("ASI-1",),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

        self.assertEqual(("ASI-2", "ASI-1"), result.issue_ids)
        self.assertEqual(1, result.summary.new_issue_count)
        self.assertEqual(1, result.summary.recurring_issue_count)
        self.assertEqual(1, len(result.observations))
        self.assertEqual(1, self.read().issue("ASI-1").occurrence_count)

    def test_a_linked_issue_merged_away_is_accepted_as_its_owner(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        result = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                state="unavailable",
                findings=(),
                reason="timeout",
                attempts=(
                    assurance_models_summary.AuditAttempt(
                        number=1,
                        outcome="timeout",
                        artifact_dir="supervisor-audit-attempt-1",
                    ),
                ),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                linked_issue_ids=("ASI-2",),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )

        self.assertIn("ASI-1", result.issue_ids)
        self.assertNotIn("ASI-2", result.issue_ids)

    def test_publication_key_separates_kinds_runs_and_inputs(self) -> None:
        self.assertNotEqual(
            projection.publication_key("audit_pending", RUN_ID, None),
            projection.publication_key("audit_skipped", RUN_ID, None),
        )
        self.assertNotEqual(
            projection.publication_key("audit_completed", RUN_ID, INPUT_DIGEST),
            projection.publication_key("audit_completed", RUN_ID, OTHER_INPUT_DIGEST),
        )
        self.assertNotEqual(
            projection.publication_key("audit_completed", RUN_ID, INPUT_DIGEST),
            projection.publication_key("audit_completed", OTHER_RUN_ID, INPUT_DIGEST),
        )


class ConcurrentPublicationTests(ProjectionTestCase):
    def test_concurrent_threads_publish_one_occurrence(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 12
        barrier = threading.Barrier(workers)
        lock = threading.Lock()
        created: list[bool] = []
        failures: list[BaseException] = []

        def publish() -> None:
            try:
                barrier.wait()
                result = self.publish(_publication())
                with lock:
                    created.append(result.created)
            except BaseException as error:
                with lock:
                    failures.append(error)

        threads = [threading.Thread(target=publish) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual(1, created.count(True))
        self.assertEqual(workers - 1, created.count(False))
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(3, len(view.records))
        self.assertEqual((), view.rejected)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(1, issues[0].occurrence_count)
        self.assertEqual(("issue.asi-1",), issues[0].identity_keys)

    def test_concurrent_processes_publish_one_occurrence(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 5
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _PUBLISH_WORKER_SOURCE,
                    str(self.state_dir),
                    RUN_ID,
                    INPUT_DIGEST,
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(workers)
        ]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
            outcomes.append(stdout.strip())
        self.assertEqual(1, outcomes.count("created"))
        self.assertEqual(workers - 1, outcomes.count("existing"))
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(3, len(view.records))
        self.assertEqual((), view.rejected)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(1, issues[0].occurrence_count)
        self.assertEqual(("issue.asi-1",), issues[0].identity_keys)

    def assert_one_issue_many_occurrences(self, workers: int) -> None:
        """Assert competing publishers of one defect minted a single issue.

        Every publisher carries the same fingerprint under a different run, so each
        one has an audit of its own to record and all of them are describing the same
        defect. One issue, one identity key, and one observation per run is the only
        outcome that is neither a duplicate nor a loss.
        """
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual((), view.rejected)
        kinds = [record.kind for record in view.records]
        self.assertEqual(workers, kinds.count(ledger.KIND_AUDIT_COMPLETED))
        self.assertEqual(workers, kinds.count(ledger.KIND_ISSUE_OBSERVED))
        self.assertEqual(1, kinds.count(ledger.KIND_ISSUE_IDENTITY_BOUND))
        projected = self.read()
        self.assertEqual((), projected.anomalies)
        self.assertEqual(1, len(projected.issues))
        issue = projected.issues[0]
        self.assertEqual("ASI-1", issue.issue_id)
        self.assertEqual(("issue.asi-1",), issue.identity_keys)
        self.assertEqual(workers, issue.occurrence_count)
        self.assertEqual(workers, len(projected.runs))

    def test_competing_threads_mint_one_issue_for_one_defect(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 12
        barrier = threading.Barrier(workers)
        lock = threading.Lock()
        failures: list[BaseException] = []

        def publish(index: int) -> None:
            try:
                barrier.wait()
                self.publish(
                    _publication(
                        run_id=_worker_run_id(index),
                        artifacts=_artifacts(_worker_digest(index)),
                    )
                )
            except BaseException as error:
                with lock:
                    failures.append(error)

        threads = [
            threading.Thread(target=publish, args=(index,)) for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assert_one_issue_many_occurrences(workers)

    def test_competing_processes_mint_one_issue_for_one_defect(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 5
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _PUBLISH_WORKER_SOURCE,
                    str(self.state_dir),
                    _worker_run_id(index),
                    _worker_digest(index),
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(workers)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
        self.assert_one_issue_many_occurrences(workers)


class DeduplicationTests(ProjectionTestCase):
    def test_a_matching_fingerprint_routes_to_the_existing_issue(self) -> None:
        first = self.publish(_publication())
        second = self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(summary="Seen again, worded differently."),),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertEqual(("ASI-1",), first.issue_ids)
        self.assertEqual(("ASI-1",), second.issue_ids)
        self.assertEqual(0, second.summary.new_issue_count)
        self.assertEqual(1, second.summary.recurring_issue_count)

    def test_repeated_open_findings_increment_occurrences(self) -> None:
        self.publish(_publication())
        self.publish(
            _publication(run_id=OTHER_RUN_ID, completed_at=LATER_TIMESTAMP),
            recorded_at=LATER_TIMESTAMP,
        )
        self.publish(
            _publication(run_id=THIRD_RUN_ID, completed_at=LAST_TIMESTAMP),
            recorded_at=LAST_TIMESTAMP,
        )
        issue = self.read().issue("ASI-1")
        self.assertEqual(3, issue.occurrence_count)
        self.assertEqual("open", issue.state)
        self.assertEqual((RUN_ID, OTHER_RUN_ID, THIRD_RUN_ID), issue.run_ids)
        self.assertEqual(TIMESTAMP, issue.first_seen_at)
        self.assertEqual(LAST_TIMESTAMP, issue.last_seen_at)

    def test_a_new_identity_opens_a_new_issue(self) -> None:
        self.open_two_issues()
        projected = self.read()
        self.assertEqual(["ASI-1", "ASI-2"], [i.issue_id for i in projected.issues])
        self.assertEqual(1, projected.issue("ASI-2").occurrence_count)

    def test_the_latest_wording_of_a_recurring_finding_wins(self) -> None:
        self.publish(_publication())
        self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(severity="critical", summary="Now it is worse."),),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        issue = self.read().issue("ASI-1")
        self.assertEqual("critical", issue.finding.severity)
        self.assertEqual("Now it is worse.", issue.finding.summary)
        self.assertEqual(
            ("high", "critical"), tuple(o.severity for o in issue.occurrences)
        )


class UnavailableAuditTests(ProjectionTestCase):
    def _unavailable(self, run_id: str, reason: str, detail: str) -> AuditPublication:
        return AuditPublication(
            run_id=run_id,
            state="unavailable",
            eligible=True,
            reason=reason,
            attempts=(
                assurance_models_summary.AuditAttempt(
                    number=1,
                    outcome="transport-error",
                    artifact_dir="supervisor-audit-attempt-1",
                    detail=detail,
                ),
            ),
        )

    def test_an_unavailable_audit_opens_one_system_issue(self) -> None:
        result = self.publish(
            self._unavailable(RUN_ID, "harness-transport", "socket hangup at 0x7ffe")
        )
        self.assertEqual(("ASI-1",), result.issue_ids)
        issue = self.read().issue("ASI-1")
        self.assertEqual(projection.SYSTEM_ISSUE_CATEGORY, issue.finding.category)
        self.assertEqual(
            "audit.availability.harness-transport", issue.finding.invariant_key
        )
        self.assertEqual(("supervisor-audit-attempt-1",), issue.finding.evidence)

    def test_a_reason_a_rerun_cannot_clear_is_not_told_to_rerun(self) -> None:
        # The standing advice is to re-run the audit, which is right for a
        # failure a re-run could clear. An exhausted quota is not one, and
        # telling an operator to re-run it is what made an outage read as a
        # defect they were expected to chase (ASI-127).
        default = self.publish(
            self._unavailable(RUN_ID, "harness-transport", "socket hangup")
        )
        quota = self.publish(
            self._unavailable(OTHER_RUN_ID, "quota-exhausted", "weekly limit"),
            recorded_at=LATER_TIMESTAMP,
        )
        view = self.read()
        self.assertEqual(
            projection.DEFAULT_UNAVAILABLE_RECOMMENDATION,
            view.issue(default.issue_ids[0]).finding.recommendation,
        )
        advice = view.issue(quota.issue_ids[0]).finding.recommendation
        self.assertNotEqual(projection.DEFAULT_UNAVAILABLE_RECOMMENDATION, advice)
        self.assertIn("once the limit resets", advice)

    def test_every_rerun_recommendation_names_the_run(self) -> None:
        # A bare process-pending pass reports a run that already has an
        # unavailable record as already audited; only --run-id re-audits it.
        for advice in (
            projection.DEFAULT_UNAVAILABLE_RECOMMENDATION,
            *projection.UNAVAILABLE_RECOMMENDATIONS.values(),
        ):
            with self.subTest(advice=advice[:40]):
                self.assertIn("process-pending --run-id RUN_ID", advice)

    def test_one_reason_class_deduplicates_across_distinct_exception_text(self) -> None:
        self.publish(
            self._unavailable(RUN_ID, "harness-transport", "socket hangup at 0x7ffe")
        )
        second = self.publish(
            self._unavailable(
                OTHER_RUN_ID, "harness-transport", "connection reset by peer (errno 54)"
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertEqual(("ASI-1",), second.issue_ids)
        self.assertEqual(1, second.summary.recurring_issue_count)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(2, issues[0].occurrence_count)

    def test_a_different_reason_class_opens_a_different_issue(self) -> None:
        self.publish(self._unavailable(RUN_ID, "harness-transport", "socket hangup"))
        second = self.publish(
            self._unavailable(OTHER_RUN_ID, "timeout", "socket hangup"),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertEqual(("ASI-2",), second.issue_ids)
        self.assertEqual(2, len(self.read().issues))

    def test_an_unavailable_audit_naming_no_evidence_is_refused(self) -> None:
        publication = AuditPublication(
            run_id=RUN_ID, state="unavailable", eligible=True, reason="timeout"
        )
        with self.assertRaises(ProjectionError) as caught:
            self.publish(publication)
        self.assertEqual(
            projection.UNAVAILABLE_EVIDENCE_REQUIRED, caught.exception.reason
        )
        self.assertFalse(self.ledger_path.exists())

    def test_an_unavailable_audit_may_also_carry_partial_findings(self) -> None:
        publication = AuditPublication(
            run_id=RUN_ID,
            state="unavailable",
            eligible=True,
            reason="timeout",
            findings=(_finding(),),
            artifacts=_artifacts(),
        )
        result = self.publish(publication)
        self.assertEqual(("ASI-1", "ASI-2"), result.issue_ids)
        self.assertEqual(2, result.summary.new_issue_count)

    def test_an_unavailable_audit_must_name_its_reason_class(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            AuditPublication(run_id=RUN_ID, state="unavailable", eligible=True)

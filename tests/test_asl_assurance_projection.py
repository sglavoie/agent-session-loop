"""Issue projection, deduplication, merge, and lifecycle tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication, ProjectionError


REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "20260810T101112123456Z-4242"
OTHER_RUN_ID = "20260810T101112999999Z-4243"
THIRD_RUN_ID = "20260810T101113111111Z-4244"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"
LAST_TIMESTAMP = "2026-08-10T12:11:12+00:00"
INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64


_PUBLISH_WORKER_SOURCE = """
import sys
from pathlib import Path

from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection

state_dir = Path(sys.argv[1])
finding = models.Finding(
    category="lifecycle",
    component="agent_session_loop/runner.py",
    invariant_key="retry.budget-respected",
    lane="lifecycle-retry-resume",
    severity="high",
    summary="A retry ran after the budget was exhausted.",
    recommendation="Clamp the retry counter before relaunching.",
    evidence=("events.jsonl",),
)
publication = projection.AuditPublication(
    run_id="%s",
    state="findings",
    eligible=True,
    findings=(finding,),
    artifacts=(
        models.EvidenceRef(
            kind="artifact",
            locator="supervisor-audit-input.json",
            digest="%s",
        ),
    ),
    completed_at="%s",
)
result = projection.publish_audit(
    publication, state_dir=state_dir, recorded_at="%s"
)
sys.stdout.write("created" if result.created else "existing")
""" % (RUN_ID, INPUT_DIGEST, TIMESTAMP, TIMESTAMP)


def _finding(**overrides) -> models.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
    }
    payload.update(overrides)
    return models.Finding(**payload)


def _artifacts(digest: str | None = INPUT_DIGEST) -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
        ),
        models.EvidenceRef(kind="artifact", locator="supervisor-audit.json"),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_ID,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": TIMESTAMP,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


class ProjectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", TIMESTAMP)
        return projection.publish_audit(
            publication, state_dir=self.state_dir, **kwargs
        )

    def read(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    def force_record(self, kind: str, payload) -> ledger.LedgerRecord:
        """Append a record the projection would refuse, to test replay tolerance."""
        return ledger.append_record(kind, payload, state_dir=self.state_dir)

    def open_two_issues(self) -> None:
        self.publish(_publication())
        self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

    def build_every_kind(self) -> None:
        """Write one record of all nine ledger kinds across three runs."""
        self.publish(AuditPublication(run_id=RUN_ID, state="pending", eligible=True))
        self.publish(_publication(), recorded_at=LATER_TIMESTAMP)
        self.publish(
            AuditPublication(
                run_id=OTHER_RUN_ID, state="skipped", eligible=False, reason="opted-out"
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.publish(
            AuditPublication(
                run_id=THIRD_RUN_ID,
                state="unavailable",
                eligible=True,
                reason="timeout",
                attempts=(
                    models.AuditAttempt(
                        number=1,
                        outcome="timeout",
                        artifact_dir="supervisor-audit-attempt-1",
                    ),
                ),
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.force_record(
            ledger.KIND_HUMAN_CLOSEOUT,
            models.Closeout(
                run_id=RUN_ID,
                outcome="reworked",
                recorded_at=LAST_TIMESTAMP,
                issue_ids=("ASI-1",),
                rework_minutes=25,
            ),
        )
        self.force_record(
            ledger.KIND_IMPROVEMENT_PLAN_DRAFTED,
            models.ImprovementPlanDraft(
                issue_id="ASI-1",
                harness="codex",
                output_path="/tmp/improvement.md",
                output_sha256="c" * 64,
                drafted_at=LAST_TIMESTAMP,
            ),
        )


class PublicationTests(ProjectionTestCase):
    def test_publishing_writes_the_summary_and_one_observation_per_finding(self) -> None:
        result = self.publish(
            _publication(
                findings=(_finding(), _finding(invariant_key="scope.no-escape"))
            )
        )
        self.assertTrue(result.created)
        self.assertEqual(3, len(result.records))
        self.assertEqual("audit_completed", result.records[0].kind)
        self.assertEqual(
            ["issue_observed", "issue_observed"],
            [record.kind for record in result.records[1:]],
        )
        self.assertEqual(("ASI-1", "ASI-2"), result.issue_ids)
        self.assertEqual([1, 2, 3], [record.seq for record in result.records])

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
        with self.assertRaises(models.AssuranceError) as caught:
            AuditPublication(
                run_id=RUN_ID,
                state="all-clear",
                eligible=True,
                findings=(_finding(),),
            )
        self.assertIn("opens no issue", str(caught.exception))

    def test_a_findings_audit_must_carry_a_finding(self) -> None:
        with self.assertRaises(models.AssuranceError):
            AuditPublication(run_id=RUN_ID, state="findings", eligible=True)

    def test_one_audit_cannot_report_the_same_identity_twice(self) -> None:
        with self.assertRaises(models.AssuranceError) as caught:
            AuditPublication(
                run_id=RUN_ID,
                state="findings",
                eligible=True,
                findings=(_finding(), _finding(severity="low")),
            )
        self.assertIn("repeats the finding identity", str(caught.exception))

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
            except BaseException as error:  # noqa: BLE001 - reported below
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
        self.assertEqual(2, len(view.records))
        self.assertEqual((), view.rejected)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(1, issues[0].occurrence_count)

    def test_concurrent_processes_publish_one_occurrence(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 5
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", _PUBLISH_WORKER_SOURCE, str(self.state_dir)],
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
        self.assertEqual(2, len(view.records))
        self.assertEqual((), view.rejected)
        issues = self.read().issues
        self.assertEqual(1, len(issues))
        self.assertEqual(1, issues[0].occurrence_count)


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
        self.assertEqual(("high", "critical"), tuple(o.severity for o in issue.occurrences))


class LifecycleTests(ProjectionTestCase):
    def _observe_again(self, timestamp: str = LATER_TIMESTAMP, run_id: str = OTHER_RUN_ID) -> None:
        self.publish(
            _publication(run_id=run_id, completed_at=timestamp),
            recorded_at=timestamp,
        )

    def test_an_observed_issue_starts_open(self) -> None:
        self.publish(_publication())
        self.assertEqual("open", self.read().issue("ASI-1").state)

    def test_each_disposition_moves_the_issue_through_an_append(self) -> None:
        self.publish(_publication())
        for kind, expected in models.DISPOSITION_RESULT_STATE.items():
            projection.record_disposition(
                "ASI-1", kind, state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
            )
            self.assertEqual(expected, self.read().issue("ASI-1").state)
        issue = self.read().issue("ASI-1")
        self.assertEqual(3, len(issue.dispositions))
        self.assertEqual(1, issue.occurrence_count)

    def test_a_recurrence_after_resolution_reopens_the_issue(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )
        self.assertEqual("resolved", self.read().issue("ASI-1").state)
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("open", issue.state)
        self.assertEqual(1, issue.reopened_count)
        self.assertEqual(2, issue.occurrence_count)

    def test_a_recurrence_after_dismissal_stays_closed(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1",
            "dismiss",
            note="Working as intended.",
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("dismissed", issue.state)
        self.assertEqual(0, issue.reopened_count)
        self.assertEqual(2, issue.occurrence_count)

    def test_a_recurrence_after_accepting_risk_stays_closed(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1",
            "accept-risk",
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("accepted-risk", issue.state)
        self.assertEqual(2, issue.occurrence_count)

    def test_disposing_an_unknown_issue_is_refused(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_disposition(
                "ASI-9", "resolve", state_dir=self.state_dir
            )
        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertFalse(self.ledger_path.exists())

    def test_a_disposition_never_rewrites_an_earlier_record(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )
        projection.record_disposition(
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        view = ledger.read_ledger(state_dir=self.state_dir)
        kinds = [record.kind for record in view.records]
        self.assertEqual(
            ["audit_completed", "issue_observed", "issue_disposition", "issue_disposition"],
            kinds,
        )
        self.assertEqual("dismissed", self.read().issue("ASI-1").state)


class MergeTests(ProjectionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_two_issues()

    def test_merge_moves_the_whole_history_to_the_target(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        target = projected.issue("ASI-2")
        self.assertEqual(("ASI-1",), target.merged_from)
        self.assertEqual(2, target.occurrence_count)
        self.assertEqual(2, len(target.fingerprints))

    def test_a_merged_reference_resolves_to_its_target(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        projected = self.read()
        self.assertEqual("ASI-2", projected.resolve("ASI-1"))
        self.assertEqual("ASI-2", projected.issue("ASI-1").issue_id)
        self.assertEqual({"ASI-1": "ASI-2"}, dict(projected.aliases))

    def test_later_observations_route_through_the_final_canonical_target(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        projection.record_merge("ASI-2", "ASI-3", state_dir=self.state_dir)
        # The original ASI-1 identity, seen again, lands on the end of the chain.
        result = self.publish(
            _publication(artifacts=_artifacts(OTHER_INPUT_DIGEST)),
            recorded_at=LAST_TIMESTAMP,
        )
        self.assertEqual(("ASI-3",), result.issue_ids)
        projected = self.read()
        self.assertEqual(["ASI-3"], [issue.issue_id for issue in projected.issues])
        self.assertEqual("ASI-3", projected.resolve("ASI-1"))
        self.assertEqual(("ASI-1", "ASI-2"), projected.issue("ASI-3").merged_from)
        self.assertEqual(4, projected.issue("ASI-3").occurrence_count)

    def test_a_disposition_on_a_merged_id_reaches_the_canonical_issue(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        record = projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.assertEqual("ASI-2", record.payload.issue_id)
        self.assertEqual("resolved", self.read().issue("ASI-1").state)

    def test_merge_rejects_an_issue_merging_into_itself(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_SELF, caught.exception.reason)

    def test_merge_rejects_an_unknown_source(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-9", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_UNKNOWN_SOURCE, caught.exception.reason)

    def test_merge_rejects_an_unknown_target(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-9", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_UNKNOWN_TARGET, caught.exception.reason)

    def test_merge_is_irreversible(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-3", state_dir=self.state_dir)
        self.assertEqual(
            projection.MERGE_SOURCE_ALREADY_MERGED, caught.exception.reason
        )

    def test_merge_rejects_an_already_merged_target(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-3", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(
            projection.MERGE_TARGET_ALREADY_MERGED, caught.exception.reason
        )

    def test_merge_rejects_a_cycle(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-2", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_CYCLE, caught.exception.reason)

    def test_a_refused_merge_writes_nothing(self) -> None:
        before = self.ledger_path.read_bytes()
        with self.assertRaises(ProjectionError):
            projection.record_merge("ASI-1", "ASI-9", state_dir=self.state_dir)
        self.assertEqual(before, self.ledger_path.read_bytes())


class UnavailableAuditTests(ProjectionTestCase):
    def _unavailable(self, run_id: str, reason: str, detail: str) -> AuditPublication:
        return AuditPublication(
            run_id=run_id,
            state="unavailable",
            eligible=True,
            reason=reason,
            attempts=(
                models.AuditAttempt(
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
        with self.assertRaises(models.AssuranceError):
            AuditPublication(run_id=RUN_ID, state="unavailable", eligible=True)


class RecordKindTests(ProjectionTestCase):
    def test_every_ledger_kind_reaches_the_projection(self) -> None:
        self.build_every_kind()
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(
            set(ledger.LEDGER_KINDS), {record.kind for record in view.records}
        )
        projected = projection.project(view)
        self.assertEqual((), projected.anomalies)
        self.assertEqual(["ASI-1"], [issue.issue_id for issue in projected.issues])
        issue = projected.issue("ASI-1")
        self.assertEqual("resolved", issue.state)
        self.assertEqual(("ASI-2",), issue.merged_from)
        self.assertEqual(1, len(issue.plans))
        self.assertEqual(
            {RUN_ID, OTHER_RUN_ID, THIRD_RUN_ID},
            {run.run_id for run in projected.runs},
        )
        run = projected.run(RUN_ID)
        self.assertEqual("findings", run.state)
        self.assertEqual("reworked", run.closeout.outcome)
        self.assertEqual(("ASI-1",), run.issue_ids)
        self.assertEqual("skipped", projected.run(OTHER_RUN_ID).state)
        self.assertEqual("unavailable", projected.run(THIRD_RUN_ID).state)


class ReplayTests(ProjectionTestCase):
    def test_replaying_only_the_ledger_reconstructs_identical_state(self) -> None:
        self.build_every_kind()
        first = self.read()
        data = self.ledger_path.read_bytes()
        replayed = projection.project(
            ledger.parse_ledger_bytes(data, self.ledger_path)
        )
        self.assertEqual(first, replayed)

    def test_a_copied_ledger_with_no_cache_reconstructs_identical_state(self) -> None:
        self.build_every_kind()
        first = self.read()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        copied = ledger.ledger_path(elsewhere)
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(self.ledger_path.read_bytes())
        self.assertFalse(ledger.ledger_index_path(elsewhere).exists())
        second = projection.read_projection(state_dir=elsewhere)
        self.assertEqual(first, second)

    def test_replay_is_stable_across_repeated_reads(self) -> None:
        self.build_every_kind()
        self.assertEqual(self.read(), self.read())


class AnomalyTests(ProjectionTestCase):
    def test_a_disposition_for_an_unobserved_issue_is_reported_not_applied(self) -> None:
        self.publish(_publication())
        self.force_record(
            ledger.KIND_ISSUE_DISPOSITION,
            models.Disposition(
                issue_id="ASI-7", kind="resolve", recorded_at=LATER_TIMESTAMP
            ),
        )
        projected = self.read()
        self.assertEqual("open", projected.issue("ASI-1").state)
        self.assertEqual(1, len(projected.anomalies))
        self.assertEqual(projection.UNKNOWN_ISSUE, projected.anomalies[0].reason)
        self.assertEqual(3, projected.anomalies[0].seq)

    def test_a_cyclic_merge_pair_on_disk_is_reported_and_terminates(self) -> None:
        self.open_two_issues()
        self.force_record(
            ledger.KIND_ISSUE_MERGED,
            models.IssueMerge(
                source_issue_id="ASI-1",
                target_issue_id="ASI-2",
                recorded_at=LAST_TIMESTAMP,
            ),
        )
        self.force_record(
            ledger.KIND_ISSUE_MERGED,
            models.IssueMerge(
                source_issue_id="ASI-2",
                target_issue_id="ASI-1",
                recorded_at=LAST_TIMESTAMP,
            ),
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        self.assertEqual(
            [projection.MERGE_CYCLE], [item.reason for item in projected.anomalies]
        )
        self.assertEqual("ASI-2", projected.resolve("ASI-1"))

    def test_a_closeout_without_an_audit_record_is_reported(self) -> None:
        self.force_record(
            ledger.KIND_HUMAN_CLOSEOUT,
            models.Closeout(
                run_id=RUN_ID, outcome="accepted", recorded_at=TIMESTAMP
            ),
        )
        projected = self.read()
        self.assertEqual((), projected.runs)
        self.assertEqual(
            [projection.UNKNOWN_RUN], [item.reason for item in projected.anomalies]
        )

    def test_an_observation_naming_a_merged_source_lands_on_the_target(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        ledger.append_transaction(
            lambda view: (
                ledger.PendingRecord(
                    kind=ledger.KIND_ISSUE_OBSERVED,
                    payload=models.IssueObservation(
                        run_id=THIRD_RUN_ID,
                        finding=_finding(),
                        observed_at=LAST_TIMESTAMP,
                    ),
                    issue_id="ASI-1",
                ),
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        self.assertEqual(3, len(projected.issue("ASI-2").occurrences))

    def test_rejected_lines_are_carried_through_to_the_projection(self) -> None:
        self.publish(_publication())
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        projected = self.read()
        self.assertEqual(1, len(projected.rejected))
        self.assertEqual(ledger.REJECT_INVALID_JSON, projected.rejected[0].reason)
        self.assertEqual(1, len(projected.issues))


class CloseoutTests(ProjectionTestCase):
    def closeout(self, **kwargs):
        kwargs.setdefault("recorded_at", LAST_TIMESTAMP)
        return projection.record_closeout(
            kwargs.pop("run_id", RUN_ID),
            kwargs.pop("outcome", "accepted"),
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_a_closeout_records_the_verdict_on_an_audited_run(self) -> None:
        self.publish(_publication())

        record = self.closeout(
            outcome="reworked",
            rework_minutes=25,
            issue_ids=("ASI-1",),
            note="clamped the counter",
            plan_id="b766d9cd537c-c70d18f6ec8e",
        )

        self.assertEqual(ledger.KIND_HUMAN_CLOSEOUT, record.kind)
        stored = self.read().run(RUN_ID).closeout
        self.assertEqual("reworked", stored.outcome)
        self.assertEqual(25.0, stored.rework_minutes)
        self.assertEqual(("ASI-1",), stored.issue_ids)
        self.assertEqual("b766d9cd537c-c70d18f6ec8e", stored.plan_id)
        self.assertIsNone(stored.replaces)

    def test_a_run_the_ledger_never_audited_cannot_be_closed_out(self) -> None:
        self.publish(_publication())

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(run_id=OTHER_RUN_ID)

        self.assertEqual(projection.UNKNOWN_RUN, caught.exception.reason)
        self.assertEqual(1, len(self.read().runs))

    def test_a_closeout_cannot_link_an_issue_the_inbox_never_opened(self) -> None:
        self.publish(_publication())

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(issue_ids=("ASI-99",))

        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertIsNone(self.read().run(RUN_ID).closeout)

    def test_a_linked_issue_that_was_merged_away_names_its_owner(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        self.closeout(issue_ids=("ASI-2", "ASI-1"))

        self.assertEqual(("ASI-1",), self.read().run(RUN_ID).closeout.issue_ids)

    def test_a_second_closeout_is_refused_unless_it_replaces_the_first(self) -> None:
        self.publish(_publication())
        self.closeout(outcome="accepted")

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(outcome="escaped")

        self.assertEqual(projection.DUPLICATE_CLOSEOUT, caught.exception.reason)
        self.assertEqual(1, len(self.read().run(RUN_ID).closeouts))

    def test_a_replacement_names_the_closeout_it_supersedes(self) -> None:
        self.publish(_publication())
        first = self.closeout(outcome="accepted")

        second = self.closeout(outcome="escaped", replace=True)

        self.assertEqual(projection.closeout_reference(first.seq), second.payload.replaces)
        recorded = self.read().run(RUN_ID)
        # Superseding appends: both verdicts, and the order they were reached
        # in, stay readable, and the later one is the one in force.
        self.assertEqual(
            ["accepted", "escaped"], [item.outcome for item in recorded.closeouts]
        )
        self.assertEqual("escaped", recorded.closeout.outcome)

    def test_replacing_when_nothing_was_closed_out_names_nothing(self) -> None:
        self.publish(_publication())

        record = self.closeout(replace=True)

        self.assertIsNone(record.payload.replaces)

    def test_unusable_closeout_values_are_refused_before_the_lock(self) -> None:
        self.publish(_publication())
        for kwargs in (
            {"outcome": "partly"},
            {"rework_minutes": -1},
            {"rework_minutes": float("nan")},
            {"rework_minutes": float("inf")},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(models.AssuranceError):
                    self.closeout(**kwargs)
        self.assertIsNone(self.read().run(RUN_ID).closeout)


if __name__ == "__main__":
    unittest.main()

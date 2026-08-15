"""Issue projection, deduplication, merge, and lifecycle tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
THIRD_INPUT_DIGEST = "c" * 64


_PUBLISH_WORKER_SOURCE = f"""
import sys
from pathlib import Path

from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection

state_dir = Path(sys.argv[1])
run_id = sys.argv[2]
input_digest = sys.argv[3]
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
    run_id=run_id,
    state="findings",
    eligible=True,
    findings=(finding,),
    artifacts=(
        models.EvidenceRef(
            kind="artifact",
            locator="supervisor-audit-input.json",
            digest=input_digest,
        ),
    ),
    completed_at="{TIMESTAMP}",
)
result = projection.publish_audit(
    publication, state_dir=state_dir, recorded_at="{TIMESTAMP}"
)
sys.stdout.write("created" if result.created else "existing")
"""


def _worker_run_id(index: int) -> str:
    return f"20260810T1011121234{index:02d}Z-43{index:02d}"


def _worker_digest(index: int) -> str:
    return f"{index:064x}"


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
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def read(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    def force_record(self, kind: str, payload) -> ledger.LedgerRecord:
        """Append a record the projection would refuse, to test replay tolerance."""
        return ledger.append_record(kind, payload, state_dir=self.state_dir)

    def bind(self, issue_id: str, key_of: str, source: str = "allocation") -> None:
        ledger.append_identity_binding(
            models.IssueIdentityBinding(
                identity_key=models.identity_key_for_issue(key_of),
                issue_id=issue_id,
                source=source,
                bound_at=LATER_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )

    def force_binding(self, issue_id: str, key_of: str) -> None:
        """Write a binding the ledger writer refuses, to test replay tolerance."""
        binding = models.IssueIdentityBinding(
            identity_key=models.identity_key_for_issue(key_of),
            issue_id=issue_id,
            source="reconcile",
            bound_at=LAST_TIMESTAMP,
        )
        line = json.dumps(
            {
                "version": ledger.LEDGER_SCHEMA_VERSION,
                "kind": ledger.KIND_ISSUE_IDENTITY_BOUND,
                "recorded_at": LAST_TIMESTAMP,
                "issue_id": None,
                "payload": binding.to_dict(),
            }
        )
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

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
        """Write one record of all ten ledger kinds across three runs."""
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
                    models.AuditAttempt(
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
            except BaseException as error:  # noqa: BLE001 - reported below
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


class IdentityResolutionTests(ProjectionTestCase):
    RENAMED = "scope.no-escape"

    def select(self, invariant_key: str, issue_id: str, *, promote=None, **overrides):
        """Publish one finding whose identity the audit says is ``issue_id``."""
        finding = _finding(invariant_key=invariant_key)
        payload = {
            "run_id": OTHER_RUN_ID,
            "findings": (finding,),
            "artifacts": _artifacts(OTHER_INPUT_DIGEST),
            "completed_at": LATER_TIMESTAMP,
            "identity_selections": {finding.fingerprint: issue_id},
        }
        payload.update(overrides)
        return self.publish(
            _publication(**payload),
            recorded_at=LATER_TIMESTAMP,
            **({} if promote is None else {"promote": promote}),
        )

    def test_allocation_binds_the_key_in_the_same_transaction(self) -> None:
        result = self.publish(_publication())

        binding = result.bindings[0].payload

        self.assertEqual(1, len(result.bindings))
        self.assertEqual("issue.asi-1", binding.identity_key)
        self.assertEqual("ASI-1", binding.issue_id)
        self.assertEqual("allocation", binding.source)
        self.assertEqual("issue.asi-1", self.read().issue("ASI-1").primary_identity_key)

    def test_an_exact_fingerprint_overrules_a_selection(self) -> None:
        self.open_two_issues()
        finding = _finding()

        result = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(finding,),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LAST_TIMESTAMP,
                identity_selections={finding.fingerprint: "ASI-2"},
            ),
            recorded_at=LAST_TIMESTAMP,
        )

        # ASL derived ASI-1 from this fingerprint itself, so no answer about the
        # same defect can move it somewhere else.
        self.assertEqual(("ASI-1",), result.issue_ids)
        self.assertEqual((), result.bindings)

    def test_a_selection_attaches_its_fingerprint_to_the_selected_issue(self) -> None:
        self.publish(_publication())

        selected = self.select(self.RENAMED, "ASI-1")
        recurred = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )

        self.assertEqual(("ASI-1",), selected.issue_ids)
        self.assertEqual((), selected.bindings)
        self.assertEqual(0, selected.summary.new_issue_count)
        # The third audit selected nothing and still lands on ASI-1: the
        # attachment made the rename an exact sighting from here on.
        self.assertEqual(("ASI-1",), recurred.issue_ids)
        self.assertEqual(1, len(self.read().issues))

    def test_a_selection_naming_a_merged_away_issue_follows_the_merge(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        result = self.select("commit.guarded", "ASI-1")

        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual((), result.bindings)

    def test_a_selection_naming_no_issue_refuses_the_whole_publication(self) -> None:
        self.publish(_publication())

        with self.assertRaises(ProjectionError) as caught:
            self.select(self.RENAMED, "ASI-99")

        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertEqual(3, len(ledger.read_ledger(state_dir=self.state_dir).records))
        self.assertEqual((RUN_ID,), tuple(run.run_id for run in self.read().runs))

    def test_a_selection_for_a_finding_the_publication_lacks_is_refused(self) -> None:
        with self.assertRaises(models.AssuranceError) as caught:
            _publication(identity_selections={"f" * 64: "ASI-1"})
        self.assertIn(
            "names a finding this publication does not carry", str(caught.exception)
        )

    def test_a_clear_new_finding_takes_the_next_id(self) -> None:
        self.publish(_publication())

        result = self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual("issue.asi-2", result.bindings[0].payload.identity_key)

    def test_a_decision_reviewed_against_a_moved_catalog_is_reported_refreshed(
        self,
    ) -> None:
        self.publish(_publication())
        stale = self.read().catalog.digest
        # Another publisher opens an issue between the review and the append,
        # which is exactly the state the decision could not have seen.
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

        result = self.select(self.RENAMED, "ASI-1", catalog_digest=stale)

        self.assertTrue(result.identity_refreshed)
        self.assertEqual(("ASI-1",), result.issue_ids)

    def test_a_decision_reviewed_against_the_current_catalog_is_not_refreshed(
        self,
    ) -> None:
        self.publish(_publication())

        result = self.select(
            self.RENAMED, "ASI-1", catalog_digest=self.read().catalog.digest
        )

        self.assertFalse(result.identity_refreshed)

    def test_promotion_runs_after_identity_is_settled_and_before_the_append(
        self,
    ) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()
        seen: list[bytes] = []

        def record() -> None:
            seen.append(self.ledger_path.read_bytes())

        result = self.select(self.RENAMED, "ASI-1", promote=record)

        # Promotion saw the ledger as it was, so the reports become canonical on
        # the strength of a decision that is settled but not yet durable.
        self.assertEqual([before], seen)
        self.assertEqual(("ASI-1",), result.issue_ids)
        self.assertNotEqual(before, self.ledger_path.read_bytes())

    def test_a_refused_promotion_leaves_the_ledger_untouched(self) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()

        def refuse() -> None:
            raise models.AssuranceError("the report could not be promoted")

        with self.assertRaises(models.AssuranceError):
            self.select(self.RENAMED, "ASI-1", promote=refuse)

        self.assertEqual(before, self.ledger_path.read_bytes())
        projected = self.read()
        self.assertEqual(["ASI-1"], [issue.issue_id for issue in projected.issues])
        self.assertEqual((RUN_ID,), tuple(run.run_id for run in projected.runs))

    def test_a_failed_append_mints_nothing(self) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("the ledger could not be extended")

        with (
            mock.patch.object(ledger, "_append_locked", refuse),
            self.assertRaises(OSError),
        ):
            self.publish(
                _publication(
                    run_id=OTHER_RUN_ID,
                    findings=(_finding(invariant_key=self.RENAMED),),
                    artifacts=_artifacts(OTHER_INPUT_DIGEST),
                    completed_at=LATER_TIMESTAMP,
                ),
                recorded_at=LATER_TIMESTAMP,
            )

        # The ID the failed publication would have taken is still free, so the
        # next publication takes it rather than skipping past an orphan.
        self.assertEqual(before, self.ledger_path.read_bytes())
        result = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual("issue.asi-2", result.bindings[0].payload.identity_key)

    def test_republishing_promotes_nothing(self) -> None:
        self.publish(_publication())
        promoted: list[bool] = []

        result = self.publish(_publication(), promote=lambda: promoted.append(True))

        # The audit is already in the ledger, so there is no record about to be
        # written for a canonical report to accompany.
        self.assertFalse(result.created)
        self.assertEqual([], promoted)


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


class LifecycleTests(ProjectionTestCase):
    def _observe_again(
        self, timestamp: str = LATER_TIMESTAMP, run_id: str = OTHER_RUN_ID
    ) -> None:
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
            projection.record_disposition("ASI-9", "resolve", state_dir=self.state_dir)
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
            [
                "audit_completed",
                "issue_observed",
                "issue_identity_bound",
                "issue_disposition",
                "issue_disposition",
            ],
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


class IdentityTests(ProjectionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_two_issues()

    def test_an_issue_opened_outside_a_publication_has_no_identity(self) -> None:
        # A guard that observes a defect directly mints an ID without a name for
        # it, which is what an issue carried over from a v1 ledger looks like
        # too. Both must stay readable rather than being treated as damaged.
        ledger.append_issue_observation(
            models.IssueObservation(
                run_id=THIRD_RUN_ID,
                finding=_finding(invariant_key="commit.guarded"),
                observed_at=LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        issue = self.read().issue("ASI-3")
        self.assertEqual((), issue.identity_keys)
        self.assertIsNone(issue.primary_identity_key)

    def test_a_bound_key_names_its_own_issue(self) -> None:
        projected = self.read()
        named = projected.issue_for_identity_key("issue.asi-1")
        self.assertEqual(
            {"issue.asi-1": "ASI-1", "issue.asi-2": "ASI-2"},
            dict(projected.identity_bindings),
        )
        self.assertEqual("ASI-1", named.issue_id)
        self.assertIsNone(projected.issue_for_identity_key("issue.asi-9"))

    def test_a_binding_resolves_through_a_later_merge(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        projected = self.read()

        self.assertEqual(
            {"issue.asi-1": "ASI-2", "issue.asi-2": "ASI-2"},
            dict(projected.identity_bindings),
        )
        self.assertEqual(
            "ASI-2", projected.issue_for_identity_key("issue.asi-1").issue_id
        )
        # The record still says what it said: the merge is followed at replay,
        # not written back over the binding.
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual("ASI-1", view.identity_bindings["issue.asi-1"])

    def test_an_issue_lists_every_key_that_resolves_to_it(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        issue = self.read().issue("ASI-2")
        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)

    def test_an_issue_is_named_by_its_own_key_before_one_it_absorbed(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.assertEqual("issue.asi-2", self.read().issue("ASI-2").primary_identity_key)

    def test_an_issue_named_only_by_absorbed_keys_takes_the_lowest(self) -> None:
        # ASI-3 is opened by a guard rather than by a publication, so it holds no
        # key of its own and can only be named by the keys it absorbs.
        ledger.append_issue_observation(
            models.IssueObservation(
                run_id=THIRD_RUN_ID,
                finding=_finding(invariant_key="commit.guarded"),
                observed_at=LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge(
            "ASI-1", "ASI-3", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projection.record_merge(
            "ASI-2", "ASI-3", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        issue = self.read().issue("ASI-3")

        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)
        self.assertEqual("issue.asi-1", issue.primary_identity_key)

    def test_a_stored_key_claiming_a_second_issue_is_reported_not_applied(self) -> None:
        self.force_binding("ASI-2", "ASI-1")

        projected = self.read()

        self.assertEqual(
            [projection.IDENTITY_KEY_CONFLICT],
            [item.reason for item in projected.anomalies],
        )
        self.assertEqual("ASI-1", projected.identity_bindings["issue.asi-1"])
        self.assertEqual(("issue.asi-2",), projected.issue("ASI-2").identity_keys)

    def test_a_restated_binding_is_reported_as_a_duplicate(self) -> None:
        self.force_binding("ASI-1", "ASI-1")

        projected = self.read()

        self.assertEqual(
            [projection.DUPLICATE_IDENTITY_BINDING],
            [item.reason for item in projected.anomalies],
        )
        self.assertEqual("ASI-1", projected.identity_bindings["issue.asi-1"])


class CatalogTests(ProjectionTestCase):
    def open_every_state(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(invariant_key="scope.no-escape"),
                    _finding(invariant_key="commit.guarded"),
                    _finding(invariant_key="wait.lease-held"),
                )
            )
        )
        for issue_id, kind in (
            ("ASI-2", "resolve"),
            ("ASI-3", "dismiss"),
            ("ASI-4", "accept-risk"),
        ):
            projection.record_disposition(
                issue_id, kind, state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
            )

    def test_the_catalog_covers_every_lifecycle_state(self) -> None:
        self.open_every_state()

        catalog = self.read().catalog

        self.assertEqual(
            ["ASI-1", "ASI-2", "ASI-3", "ASI-4"],
            [entry.issue_id for entry in catalog.entries],
        )
        self.assertEqual(
            ["open", "resolved", "dismissed", "accepted-risk"],
            [entry.state for entry in catalog.entries],
        )

    def test_an_entry_carries_the_finding_a_recurrence_is_matched_against(self) -> None:
        self.publish(_publication())

        entry = self.read().catalog.entry("ASI-1")

        self.assertEqual(_finding().fingerprint, entry.fingerprint)
        self.assertEqual("retry.budget-respected", entry.invariant_key)
        self.assertEqual("agent_session_loop/runner.py", entry.component)
        self.assertEqual("high", entry.severity)
        self.assertEqual(1, entry.occurrence_count)
        self.assertEqual(TIMESTAMP, entry.first_seen_at)

    def test_the_catalog_of_an_empty_inbox_is_empty(self) -> None:
        catalog = self.read().catalog
        self.assertEqual((), catalog.entries)
        self.assertIsNone(catalog.entry("ASI-1"))

    def test_every_fingerprint_a_merge_gathered_indexes_the_owner(self) -> None:
        self.open_two_issues()
        absorbed = _finding().fingerprint
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        catalog = self.read().catalog

        self.assertEqual("ASI-2", catalog.issue_for_fingerprint(absorbed))
        self.assertEqual(
            "ASI-2",
            catalog.issue_for_fingerprint(
                _finding(invariant_key="scope.no-escape").fingerprint
            ),
        )

    def test_a_merged_away_id_names_no_entry(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        catalog = self.read().catalog
        self.assertIsNone(catalog.entry("ASI-1"))
        self.assertIsNotNone(catalog.entry("ASI-2"))

    def test_an_identity_key_indexes_the_issue_that_owns_it(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        catalog = self.read().catalog

        self.assertEqual("ASI-2", catalog.issue_for_identity_key("issue.asi-1"))
        self.assertEqual("ASI-2", catalog.issue_for_identity_key("issue.asi-2"))
        self.assertIsNone(catalog.issue_for_identity_key("issue.asi-9"))

    def test_an_invariant_gathers_every_issue_that_broke_it(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(component="agent_session_loop/state.py"),
                    _finding(invariant_key="scope.no-escape"),
                )
            )
        )

        catalog = self.read().catalog

        self.assertEqual(
            ("ASI-1", "ASI-2"), catalog.issues_with_invariant("retry.budget-respected")
        )
        self.assertEqual(("ASI-3",), catalog.issues_with_invariant("scope.no-escape"))
        self.assertEqual((), catalog.issues_with_invariant("never.seen"))

    def test_localization_is_indexed_by_file_rather_than_by_line(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        localization=(
                            "agent_session_loop/runner.py:8129",
                            "agent_session_loop/runner.py:8140",
                        )
                    ),
                    _finding(
                        invariant_key="scope.no-escape",
                        localization=("agent_session_loop/runner.py:12",),
                    ),
                    _finding(
                        invariant_key="commit.guarded",
                        localization=("agent_session_loop/state.py:4",),
                    ),
                )
            )
        )

        catalog = self.read().catalog

        # One file named twice in one finding is one place to look.
        self.assertEqual(
            ("agent_session_loop/runner.py",), catalog.entry("ASI-1").localization
        )
        self.assertEqual(
            ("ASI-1", "ASI-2"),
            catalog.issues_localized_at("agent_session_loop/runner.py:9999"),
        )
        self.assertEqual(
            ("ASI-3",), catalog.issues_localized_at("agent_session_loop/state.py")
        )

    def test_a_finding_without_a_localization_is_indexed_by_nothing(self) -> None:
        self.publish(_publication())
        catalog = self.read().catalog
        self.assertEqual((), catalog.entry("ASI-1").localization)
        self.assertEqual({}, dict(catalog.by_localization))

    def test_summary_tokens_gather_two_wordings_of_one_defect(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(
                        invariant_key="scope.no-escape",
                        summary="The budget was exhausted before that retry.",
                    ),
                    _finding(
                        invariant_key="commit.guarded",
                        summary="A commit landed outside the write scope.",
                    ),
                )
            )
        )

        catalog = self.read().catalog

        self.assertEqual(
            ("after", "budget", "exhausted", "ran", "retry"),
            catalog.entry("ASI-1").summary_tokens,
        )
        self.assertEqual(
            ("ASI-1", "ASI-2"),
            catalog.issues_sharing_summary_tokens(("budget", "exhausted")),
        )
        self.assertEqual(
            ("ASI-3",), catalog.issues_sharing_summary_tokens(("scope", "commit"))
        )
        self.assertEqual((), catalog.issues_sharing_summary_tokens(()))

    def test_the_digest_survives_a_defect_merely_recurring(self) -> None:
        self.publish(_publication())
        before = self.read().catalog

        self.publish(
            _publication(run_id=OTHER_RUN_ID, completed_at=LATER_TIMESTAMP),
            recorded_at=LATER_TIMESTAMP,
        )
        after = self.read().catalog

        self.assertEqual(2, after.entry("ASI-1").occurrence_count)
        self.assertEqual(LATER_TIMESTAMP, after.entry("ASI-1").last_seen_at)
        self.assertEqual(before.digest, after.digest)

    def test_a_lifecycle_move_changes_the_digest(self) -> None:
        self.publish(_publication())
        before = self.read().catalog.digest

        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_a_new_issue_changes_the_digest(self) -> None:
        self.publish(_publication())
        before = self.read().catalog.digest

        self.open_two_issues()

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_a_binding_changes_the_digest(self) -> None:
        # A guard-opened issue starts unnamed, so binding its key is the one
        # remaining way identity moves without an issue being opened, and a
        # decision reviewed before the key existed was reviewed against an issue
        # nothing durable could name.
        ledger.append_issue_observation(
            models.IssueObservation(
                run_id=RUN_ID, finding=_finding(), observed_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
            recorded_at=TIMESTAMP,
        )
        before = self.read().catalog.digest

        self.bind("ASI-1", "ASI-1")

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_an_empty_catalog_still_has_a_digest(self) -> None:
        self.assertTrue(self.read().catalog.digest)

    def test_one_projection_builds_one_catalog(self) -> None:
        self.publish(_publication())
        projected = self.read()
        self.assertIs(projected.catalog, projected.catalog)

    def test_the_catalog_is_derived_and_never_part_of_projection_identity(
        self,
    ) -> None:
        self.build_every_kind()
        first = self.read()
        self.assertEqual(1, len(first.catalog.entries))
        self.assertEqual(first, self.read())


class CandidateMatchTests(ProjectionTestCase):
    """Matching proposes; it never decides. Near neighbours stay separate."""

    def test_an_exact_fingerprint_is_a_match_rather_than_a_candidate(self) -> None:
        self.publish(_publication())

        catalog = self.read().catalog

        self.assertEqual("ASI-1", catalog.issue_for_fingerprint(_finding().fingerprint))
        self.assertEqual((), catalog.candidates(_finding()))

    def test_a_renamed_component_leaves_the_invariant_as_a_candidate(self) -> None:
        self.publish(_publication())
        renamed = _finding(
            component="agent_session_loop/state.py", summary="The clamp never fired."
        )

        self.assertNotEqual(_finding().fingerprint, renamed.fingerprint)
        self.assertEqual(
            (
                projection.IdentityCandidate(
                    issue_id="ASI-1", reasons=(projection.MATCH_INVARIANT,)
                ),
            ),
            self.read().catalog.candidates(renamed),
        )

    def test_a_renamed_invariant_in_one_file_is_a_localization_candidate(self) -> None:
        self.publish(
            _publication(
                findings=(_finding(localization=("agent_session_loop/runner.py:12",)),)
            )
        )
        renamed = _finding(
            invariant_key="retry.budget-clamped",
            localization=("agent_session_loop/runner.py:409",),
            summary="Nothing alike here.",
        )

        self.assertEqual(
            (
                projection.IdentityCandidate(
                    issue_id="ASI-1", reasons=(projection.MATCH_LOCALIZATION,)
                ),
            ),
            self.read().catalog.candidates(renamed),
        )

    def test_a_shared_file_alone_is_not_enough_to_be_a_candidate(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        category="hygiene",
                        component="scripts/format.sh",
                        invariant_key="format.clean",
                        localization=("agent_session_loop/runner.py:99",),
                        summary="Formatting drifted.",
                    ),
                )
            )
        )
        elsewhere = _finding(
            localization=("agent_session_loop/runner.py:409",),
            summary="Nothing alike here.",
        )

        self.assertEqual(
            ("ASI-1",),
            self.read().catalog.issues_localized_at("agent_session_loop/runner.py"),
        )
        self.assertEqual((), self.read().catalog.candidates(elsewhere))

    def test_a_reworded_summary_at_the_threshold_is_a_candidate(self) -> None:
        self.publish(_publication())
        reworded = _finding(
            invariant_key="scope.no-escape",
            summary="The budget was exhausted after a retry.",
        )

        (candidate,) = self.read().catalog.candidates(reworded)

        self.assertEqual("ASI-1", candidate.issue_id)
        self.assertEqual((projection.MATCH_SUMMARY,), candidate.reasons)
        self.assertAlmostEqual(0.80, candidate.similarity)

    def test_a_summary_below_the_threshold_is_not_a_candidate(self) -> None:
        self.publish(_publication())
        distant = _finding(
            invariant_key="scope.no-escape", summary="The retry budget looked wrong."
        )

        catalog = self.read().catalog

        # Shared vocabulary, different subject: two of seven tokens overlap.
        self.assertEqual(
            ("ASI-1",),
            catalog.issues_sharing_summary_tokens(
                models.normalize_summary_tokens(distant.summary)
            ),
        )
        self.assertEqual((), catalog.candidates(distant))

    def test_near_neighbours_in_one_file_are_never_matched_to_each_other(self) -> None:
        retry = _finding(localization=("agent_session_loop/runner.py:8129",))
        scope = _finding(
            category="scope",
            component="agent_session_loop/write_scope.py",
            invariant_key="scope.no-escape",
            localization=("agent_session_loop/runner.py:8140",),
            summary="A write landed outside the declared scope.",
        )
        result = self.publish(_publication(findings=(retry, scope)))
        self.assertEqual(("ASI-1", "ASI-2"), result.issue_ids)

        catalog = self.read().catalog

        self.assertEqual((), catalog.candidates(retry))
        self.assertEqual((), catalog.candidates(scope))

    def test_a_candidate_is_never_the_issue_the_fingerprint_already_names(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(component="agent_session_loop/state.py"),
                )
            )
        )

        candidates = self.read().catalog.candidates(_finding())

        self.assertEqual(["ASI-2"], [item.issue_id for item in candidates])
        self.assertEqual(
            (projection.MATCH_INVARIANT, projection.MATCH_SUMMARY),
            candidates[0].reasons,
        )

    def test_corroborated_candidates_rank_ahead_of_single_reason_ones(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        invariant_key="retry.budget-clamped",
                        summary="The clamp never fired.",
                    ),
                    _finding(
                        component="agent_session_loop/state.py",
                        localization=("agent_session_loop/state.py:20",),
                    ),
                    _finding(
                        invariant_key="scope.no-escape",
                        summary="A retry ran after the budget was exhausted again.",
                    ),
                )
            )
        )
        query = _finding(
            component="agent_session_loop/mailbox.py",
            localization=("agent_session_loop/state.py:44",),
        )

        candidates = self.read().catalog.candidates(query)

        self.assertEqual(["ASI-2", "ASI-3"], [item.issue_id for item in candidates])
        self.assertEqual(
            (
                projection.MATCH_INVARIANT,
                projection.MATCH_LOCALIZATION,
                projection.MATCH_SUMMARY,
            ),
            candidates[0].reasons,
        )
        self.assertEqual((projection.MATCH_SUMMARY,), candidates[1].reasons)

    def test_the_candidate_list_is_bounded(self) -> None:
        self.publish(
            _publication(
                findings=tuple(
                    _finding(component=f"agent_session_loop/module_{index}.py")
                    for index in range(projection.MAX_IDENTITY_CANDIDATES + 3)
                )
            )
        )
        query = _finding(component="agent_session_loop/runner.py")

        catalog = self.read().catalog

        self.assertEqual(
            projection.MAX_IDENTITY_CANDIDATES + 3,
            len(catalog.issues_with_invariant("retry.budget-respected")),
        )
        self.assertEqual(
            projection.MAX_IDENTITY_CANDIDATES, len(catalog.candidates(query))
        )
        self.assertEqual(2, len(catalog.candidates(query, limit=2)))

    def test_a_candidate_renders_the_fields_a_prompt_shows(self) -> None:
        candidate = projection.IdentityCandidate(
            issue_id="ASI-7",
            reasons=(projection.MATCH_INVARIANT, projection.MATCH_SUMMARY),
            similarity=0.8333333,
        )

        self.assertEqual(
            {
                "issue_id": "ASI-7",
                "reasons": ["invariant", "summary"],
                "similarity": 0.83,
            },
            candidate.to_dict(),
        )

    def test_two_findings_matching_one_issue_merge_into_one_candidate(self) -> None:
        left = projection.IdentityCandidate(
            issue_id="ASI-7", reasons=(projection.MATCH_INVARIANT,)
        )
        right = projection.IdentityCandidate(
            issue_id="ASI-7", reasons=(projection.MATCH_SUMMARY,), similarity=0.7
        )

        merged = left.merged(right)

        self.assertEqual(
            (projection.MATCH_INVARIANT, projection.MATCH_SUMMARY), merged.reasons
        )
        self.assertEqual(0.7, merged.similarity)
        with self.assertRaises(models.AssuranceError):
            left.merged(
                projection.IdentityCandidate(issue_id="ASI-8", reasons=("invariant",))
            )

    def test_an_empty_catalog_names_no_candidate(self) -> None:
        self.assertEqual((), self.read().catalog.candidates(_finding()))


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
        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)
        self.assertEqual("issue.asi-1", issue.primary_identity_key)
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
        replayed = projection.project(ledger.parse_ledger_bytes(data, self.ledger_path))
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
    def test_a_disposition_for_an_unobserved_issue_is_reported_not_applied(
        self,
    ) -> None:
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
        self.assertEqual(4, projected.anomalies[0].seq)

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
            models.Closeout(run_id=RUN_ID, outcome="accepted", recorded_at=TIMESTAMP),
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

        self.assertEqual(
            projection.closeout_reference(first.seq), second.payload.replaces
        )
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
            with (
                self.subTest(**kwargs),
                self.assertRaises(models.AssuranceError),
            ):
                self.closeout(**kwargs)
        self.assertIsNone(self.read().run(RUN_ID).closeout)


if __name__ == "__main__":
    unittest.main()

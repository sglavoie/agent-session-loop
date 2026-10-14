"""Shared fixtures and base case for assurance projection tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_primitives,
    assurance_models_summary,
)
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication

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

from agent_session_loop import assurance_models_findings as findings
from agent_session_loop import assurance_projection as projection

state_dir = Path(sys.argv[1])
run_id = sys.argv[2]
input_digest = sys.argv[3]
finding = findings.Finding(
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
        findings.EvidenceRef(
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


def _finding(**overrides) -> assurance_models_findings.Finding:
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
    return assurance_models_findings.Finding(**payload)


def _artifacts(
    digest: str | None = INPUT_DIGEST,
) -> tuple[assurance_models_findings.EvidenceRef, ...]:
    return (
        assurance_models_findings.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
        ),
        assurance_models_findings.EvidenceRef(
            kind="artifact", locator="supervisor-audit.json"
        ),
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
            assurance_models_inbox.IssueIdentityBinding(
                identity_key=assurance_models_primitives.identity_key_for_issue(key_of),
                issue_id=issue_id,
                source=source,
                bound_at=LATER_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )

    def force_binding(self, issue_id: str, key_of: str) -> None:
        """Write a binding the ledger writer refuses, to test replay tolerance."""
        binding = assurance_models_inbox.IssueIdentityBinding(
            identity_key=assurance_models_primitives.identity_key_for_issue(key_of),
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

    def keep_distinct(self, left: str, right: str) -> None:
        catalog = self.read().catalog
        digests = tuple(catalog.identity_digest(issue) or "" for issue in (left, right))
        self.force_record(
            ledger.KIND_ISSUE_KEPT_DISTINCT,
            assurance_models_inbox.IssueKeptDistinct(
                issue_ids=(left, right),
                identity_digests=digests,
                recorded_at=LAST_TIMESTAMP,
            ),
        )

    def build_every_kind(self) -> None:
        """Write one record of all eleven ledger kinds across three runs."""
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
                    assurance_models_summary.AuditAttempt(
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
        self.keep_distinct("ASI-1", "ASI-2")
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.force_record(
            ledger.KIND_HUMAN_CLOSEOUT,
            assurance_models_inbox.Closeout(
                run_id=RUN_ID,
                outcome="reworked",
                recorded_at=LAST_TIMESTAMP,
                issue_ids=("ASI-1",),
                rework_minutes=25,
            ),
        )
        self.force_record(
            ledger.KIND_IMPROVEMENT_PLAN_DRAFTED,
            assurance_models_inbox.ImprovementPlanDraft(
                issue_id="ASI-1",
                harness="codex",
                output_path="/tmp/improvement.md",
                output_sha256="c" * 64,
                drafted_at=LAST_TIMESTAMP,
            ),
        )

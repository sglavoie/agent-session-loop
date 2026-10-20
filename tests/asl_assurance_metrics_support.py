"""Shared fixtures for the ``test_asl_assurance_metrics_*`` modules."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_vocab,
    assurance_report,
)
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication

RUN_A = "20260810T100000000000Z-4241"
RUN_B = "20260810T110000000000Z-4242"
RUN_C = "20260810T120000000000Z-4243"
RUN_D = "20260810T130000000000Z-4244"
RUN_E = "20260810T140000000000Z-4245"
RUN_F = "20260810T150000000000Z-4246"

OLD = "2026-08-01T10:00:00+00:00"
T0 = "2026-08-10T10:00:00+00:00"
T1 = "2026-08-10T11:00:00+00:00"
T2 = "2026-08-10T12:00:00+00:00"

SINCE = datetime(2026, 8, 10, tzinfo=UTC)

INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64


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
            kind="artifact",
            locator=f"run/{assurance_models_vocab.AUDIT_INPUT_FILENAME}",
            digest=digest,
        ),
        assurance_models_findings.EvidenceRef(
            kind="artifact",
            locator=f"run/{assurance_models_vocab.AUDIT_REPORT_FILENAME}",
        ),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_A,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": T0,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


def _assurance(**overrides) -> dict:
    payload = {
        "run_id": RUN_A,
        "state": "findings",
        "eligible": True,
        "issue_ids": ["ASI-1", "ASI-2"],
        "new_issue_count": 1,
        "recurring_issue_count": 1,
        "artifacts": [
            {
                "kind": "artifact",
                "locator": f"run/{assurance_models_vocab.AUDIT_REPORT_FILENAME}",
            }
        ],
    }
    payload.update(overrides)
    return payload


class AssuranceReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", T0)
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def metrics(self, since: datetime = SINCE) -> dict:
        return assurance_report.collect_assurance_metrics(
            state_dir=self.state_dir, since=since
        )

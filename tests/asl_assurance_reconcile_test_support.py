# ruff: noqa: F401, F403, F405, I001
"""Reconciliation tests: manifest schema, read-only preview, atomic idempotent apply."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_cli,
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_primitives,
    assurance_models_reconcile,
    cli,
)
from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_projection as projection
from agent_session_loop import assurance_reconcile as reconcile
from agent_session_loop.assurance_models_primitives import identity_key_for_issue
from agent_session_loop.assurance_projection import AuditPublication

RUN_ID = "20260810T101112123456Z-4242"
OTHER_RUN_ID = "20260810T101112999999Z-4243"
THIRD_RUN_ID = "20260810T101113111111Z-4244"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"
LAST_TIMESTAMP = "2026-08-10T12:11:12+00:00"
INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64
THIRD_INPUT_DIGEST = "c" * 64


def _finding(**overrides) -> assurance_models_findings.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the retry budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
        "localization": ("agent_session_loop/runner.py",),
    }
    payload.update(overrides)
    return assurance_models_findings.Finding(**payload)


def _artifacts(
    digest: str = INPUT_DIGEST,
) -> tuple[assurance_models_findings.EvidenceRef, ...]:
    return (
        assurance_models_findings.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
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


class ReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)
        self.parser = cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", TIMESTAMP)
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def observe(
        self,
        finding: assurance_models_findings.Finding,
        *,
        run_id: str,
        state_dir: Path | None = None,
    ) -> str:
        """Open an issue the way pre-identity history did: minted, never bound."""
        record = ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=run_id, finding=finding, observed_at=TIMESTAMP
            ),
            state_dir=state_dir or self.state_dir,
            recorded_at=TIMESTAMP,
        )
        assert record.issue_id is not None
        return record.issue_id

    def open_unbound_twins(self, *, state_dir: Path | None = None) -> tuple[str, str]:
        """Two unbound issues one reworded invariant key apart, plus a third."""
        first = self.observe(_finding(), run_id=RUN_ID, state_dir=state_dir)
        second = self.observe(
            _finding(
                invariant_key="retry.budget-respect",
                summary="A retry ran after the retry budget was fully exhausted.",
            ),
            run_id=OTHER_RUN_ID,
            state_dir=state_dir,
        )
        self.observe(
            _finding(
                component="agent_session_loop/supervisor_audit.py",
                invariant_key="audit.report-present",
                summary="The audit report never appeared in the run directory.",
                localization=("agent_session_loop/supervisor_audit.py",),
            ),
            run_id=THIRD_RUN_ID,
            state_dir=state_dir,
        )
        return first, second

    def inbox(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    def ledger_view(self) -> ledger.LedgerView:
        return ledger.read_ledger(state_dir=self.state_dir)

    def ledger_lines(self) -> int:
        if not self.ledger_path.exists():
            return 0
        return len(self.ledger_path.read_text(encoding="utf-8").splitlines())

    def write_manifest(self, payload: dict, name: str = "manifest.json") -> Path:
        path = Path(self._tmp.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # -- invocation -------------------------------------------------------

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        args = self.parser.parse_args([*argv, "--state-dir", str(self.state_dir)])
        handler = cli.resolve_handler(args)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handler(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, argv: list[str]) -> tuple[int, dict, str]:
        code, out, err = self.run_cli([*argv, "--json"])
        return code, json.loads(out), err

    def preview_manifest(self) -> dict:
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        return payload["manifest"]

    def resolve_reviews(self, manifest: dict, *, decision: str) -> dict:
        """Rewrite every open suggestion the way a reviewer would."""
        decisions = []
        for item in manifest["decisions"]:
            if item["decision"] != assurance_models_reconcile.DECISION_REVIEW:
                decisions.append(item)
            elif decision == assurance_models_reconcile.DECISION_MERGE:
                decisions.append(
                    {
                        "decision": assurance_models_reconcile.DECISION_MERGE,
                        "source_issue_id": item["issue_ids"][1],
                        "target_issue_id": item["issue_ids"][0],
                        "note": None,
                    }
                )
            else:
                decisions.append(
                    {
                        "decision": assurance_models_reconcile.DECISION_KEEP_DISTINCT,
                        "issue_ids": item["issue_ids"],
                        "note": None,
                    }
                )
        return {**manifest, "decisions": decisions}

    def keep_distinct_only_manifest(self) -> dict:
        """Rule every open pair distinct, and leave the bindings out of it."""
        manifest = self.resolve_reviews(
            self.preview_manifest(),
            decision=assurance_models_reconcile.DECISION_KEEP_DISTINCT,
        )
        return {
            **manifest,
            "decisions": [
                item
                for item in manifest["decisions"]
                if item["decision"] == assurance_models_reconcile.DECISION_KEEP_DISTINCT
            ],
        }

    def apply_path(self, path: Path) -> dict:
        """Apply one manifest file, asserting it was accepted."""
        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        return payload


__all__ = (
    "io",
    "json",
    "tempfile",
    "unittest",
    "redirect_stderr",
    "redirect_stdout",
    "Path",
    "mock",
    "assurance_cli",
    "assurance_models_findings",
    "assurance_models_inbox",
    "assurance_models_primitives",
    "assurance_models_reconcile",
    "cli",
    "ledger",
    "projection",
    "reconcile",
    "identity_key_for_issue",
    "AuditPublication",
    "RUN_ID",
    "OTHER_RUN_ID",
    "THIRD_RUN_ID",
    "TIMESTAMP",
    "LATER_TIMESTAMP",
    "LAST_TIMESTAMP",
    "INPUT_DIGEST",
    "OTHER_INPUT_DIGEST",
    "THIRD_INPUT_DIGEST",
    "_finding",
    "_artifacts",
    "_publication",
    "ReconcileTestCase",
)

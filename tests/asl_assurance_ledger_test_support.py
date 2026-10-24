"""Append-only assurance ledger, issue-ID allocation, and index-cache tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_primitives,
    assurance_models_summary,
    assurance_models_vocab,
    state,
)
from agent_session_loop.assurance_ledger import LedgerError
from tests.asl_assurance_models_support import _finding

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "20260810T101112123456Z-4242"
OTHER_RUN_ID = "20260810T101112999999Z-4243"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


_WORKER_SOURCE = f"""
import sys
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_findings as findings
from agent_session_loop import assurance_models_inbox as inbox

state_dir = Path(sys.argv[1])
finding = findings.Finding(
    category="lifecycle",
    component="agent_session_loop/runner.py",
    invariant_key=sys.argv[2],
    lane="lifecycle-retry-resume",
    severity="high",
    summary="A retry ran after the budget was exhausted.",
    recommendation="Clamp the retry counter before relaunching.",
    evidence=("events.jsonl",),
)
observation = inbox.IssueObservation(
    run_id="{RUN_ID}", finding=finding, observed_at="{TIMESTAMP}"
)
record = ledger.append_issue_observation(observation, state_dir=state_dir)
sys.stdout.write(record.issue_id)
"""


def _observation(**overrides) -> assurance_models_inbox.IssueObservation:
    payload = {
        "run_id": RUN_ID,
        "finding": _finding(),
        "observed_at": TIMESTAMP,
    }
    payload.update(overrides)
    return assurance_models_inbox.IssueObservation(**payload)


def _binding(**overrides) -> assurance_models_inbox.IssueIdentityBinding:
    payload = {
        "identity_key": "issue.asi-1",
        "issue_id": "ASI-1",
        "source": "allocation",
        "bound_at": TIMESTAMP,
    }
    payload.update(overrides)
    return assurance_models_inbox.IssueIdentityBinding(**payload)


def _summary(**overrides) -> assurance_models_summary.AssuranceSummary:
    payload = {"run_id": RUN_ID, "state": "pending", "eligible": True}
    payload.update(overrides)
    return assurance_models_summary.AssuranceSummary(**payload)


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)
        self.index_path = ledger.ledger_index_path(self.state_dir)

    def read(self) -> ledger.LedgerView:
        return ledger.read_ledger(state_dir=self.state_dir)

    def write_ledger_text(self, text: str) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(text, encoding="utf-8")


__all__ = (
    "json",
    "os",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "unittest",
    "Path",
    "mock",
    "ledger",
    "assurance_models_findings",
    "assurance_models_inbox",
    "assurance_models_primitives",
    "assurance_models_summary",
    "assurance_models_vocab",
    "state",
    "LedgerError",
    "REPO_ROOT",
    "RUN_ID",
    "OTHER_RUN_ID",
    "TIMESTAMP",
    "LATER_TIMESTAMP",
    "DIGEST",
    "OTHER_DIGEST",
    "_WORKER_SOURCE",
    "_finding",
    "_observation",
    "_binding",
    "_summary",
    "LedgerTestCase",
)

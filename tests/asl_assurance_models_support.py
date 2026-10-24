# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401
"""Assurance domain schema, fingerprint, and artifact-writer tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    assurance_models_artifacts,
    assurance_models_audit,
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_primitives,
    assurance_models_summary,
    assurance_models_vocab,
)
from agent_session_loop.assurance_models_primitives import AssuranceError

RUN_ID = "20260810T101112123456Z-4242"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
DIGEST = "a" * 64


def _evidence() -> tuple[assurance_models_findings.EvidenceRef, ...]:
    return (
        assurance_models_findings.EvidenceRef(kind="event", locator="events.jsonl"),
        assurance_models_findings.EvidenceRef(
            kind="state", locator="state.json", digest=DIGEST
        ),
        assurance_models_findings.EvidenceRef(
            kind="task-packet", locator="turns/1/task-packet.json", detail="turn 1"
        ),
    )


def _lanes(status: str = "pass") -> tuple[assurance_models_findings.LaneEvidence, ...]:
    return tuple(
        assurance_models_findings.LaneEvidence(
            lane=lane,
            status=status,
            summary=f"{lane} judged from the event ledger",
            evidence=("events.jsonl",),
        )
        for lane in assurance_models_vocab.RUBRIC_LANES
    )


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


def _audit(**overrides) -> assurance_models_audit.SupervisorAudit:
    payload = {
        "run_id": RUN_ID,
        "state": "all-clear",
        "input_digest": "b" * 64,
        "generated_at": TIMESTAMP,
        "evidence": _evidence(),
        "lanes": _lanes(),
    }
    payload.update(overrides)
    return assurance_models_audit.SupervisorAudit(**payload)


__all__ = [name for name in globals() if not name.startswith("__")]

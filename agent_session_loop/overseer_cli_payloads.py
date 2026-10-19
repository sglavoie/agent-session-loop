"""Overseer cli payloads."""

from __future__ import annotations

from typing import Any

from . import (
    clock,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_history,
    overseer_store_models,
    overseer_watch,
)
from . import (
    overseer_cli_common as common,
)


def _candidate_payload(
    candidate: overseer_discovery.Candidate, *, window_seconds: float
) -> dict[str, Any]:
    return {
        "plan_dir": candidate.plan_dir,
        "run_id": candidate.run_id,
        "workspace_root": candidate.workspace_root,
        "registry_digest": candidate.registry_digest,
        "lifecycle": candidate.lifecycle,
        "paused": candidate.paused,
        "pause_reason": candidate.pause_reason,
        "eligible": candidate.eligible,
        "in_window": candidate.paused_within(window_seconds),
        "observed": candidate.record_observed,
        "owned": not candidate.ownership.unowned(),
        "scope": overseer_context.scope_digest(candidate),
        "repositories": [
            {"alias": declaration.alias, "root": declaration.resolved_root}
            for declaration in candidate.repositories
        ],
        "ineligible_reasons": list(candidate.ineligible_reasons),
    }


def _sweep_payload(sweep: overseer_watch.Sweep) -> dict[str, Any]:
    return {
        "started_at": clock.isoformat(sweep.started_at),
        "finished_at": clock.isoformat(sweep.finished_at),
        "considered": sweep.considered,
        "serviced": sweep.serviced,
        "complete": sweep.complete,
        "inventory_complete": sweep.inventory_complete,
        "warnings": list(sweep.warnings),
        "enrolled": [
            {
                "plan_dir": item.plan_dir,
                "run_id": item.run_id,
                "lifecycle": item.lifecycle,
                "named": item.named,
                "took_over": item.took_over,
                "recoverable": item.recoverable,
                "lease_expires_at": item.lease_expires_at,
            }
            for item in sweep.enrolled
        ],
        "excluded": [
            {
                "plan_dir": item.plan_dir,
                "run_id": item.run_id,
                "reason": item.reason,
                "detail": item.detail,
            }
            for item in sweep.excluded
        ],
    }


def _packet_payload(packet: overseer_context.Packet) -> dict[str, Any]:
    return {
        "plan_dir": packet.plan_dir,
        "run_id": packet.run_id,
        "incident_id": packet.incident_id,
        "kind": packet.kind,
        "summary": packet.summary,
        "questions": list(packet.questions),
        "scope": packet.scope,
        "captured_at": packet.captured_at,
        "withheld": packet.withheld,
        "refusal": packet.refusal,
        "sources": [
            {
                "kind": source.kind,
                "space": source.space,
                "locator": source.locator,
                "summary": source.summary,
                "size": source.size,
                "truncated": source.truncated,
            }
            for source in packet.sources
        ],
        "gaps": [
            {"kind": gap.kind, "reason": gap.reason, "detail": gap.detail}
            for gap in packet.gaps
        ],
    }


def _excerpt_payload(excerpt: overseer_context.Excerpt) -> dict[str, Any]:
    return {
        "kind": excerpt.kind,
        "space": excerpt.space,
        "locator": excerpt.locator,
        "size": excerpt.size,
        "truncated": excerpt.truncated,
        "refusal": excerpt.refusal,
        "text": excerpt.text,
    }


def _statement_payload(
    statement: overseer_decisions.Statement,
) -> dict[str, Any]:
    return {
        "statement_id": statement.statement_id,
        "voice": statement.voice,
        "space": statement.space,
        "locator": statement.locator,
        "at": statement.at,
        "marker": statement.marker,
        "truncated": statement.truncated,
        "text": statement.text,
        "scope": common._scope_payload(statement.scope),
    }


def _history_payload(
    history: overseer_history.History | None,
) -> dict[str, Any] | None:
    """What the transcript boundary answered, including that it did not."""
    if history is None:
        return None
    return {
        "allowed": history.allowed,
        "available": history.available,
        "reason": history.reason,
        "failure": history.failure,
        "summary": history.summary,
        "examined": history.examined,
        "truncated": history.truncated,
        "conversations": len(history.conversations),
        "withheld": len(history.withheld),
        "warnings": list(history.warnings),
    }


def _decision_payload(
    decision: overseer_decisions.Decision,
) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "statement_id": decision.statement_id,
        "topic": decision.topic,
        "stance": decision.stance,
        "operations": list(decision.operations),
        "at": decision.at,
        "reading": decision.reading,
        "scope": common._scope_payload(decision.scope),
    }


def _ruling_payload(ruling: overseer_decisions.Ruling) -> dict[str, Any]:
    return {
        "operation": ruling.operation,
        "authorized": ruling.authorized,
        "refusal": ruling.refusal,
        "summary": ruling.summary,
        "scope": common._scope_payload(ruling.scope),
        "decision": (
            None if ruling.decision is None else _decision_payload(ruling.decision)
        ),
        "superseded": [_decision_payload(item) for item in ruling.superseded],
        "precedents": [_decision_payload(item) for item in ruling.precedents],
        "settlements": [
            {
                "topic": settlement.topic,
                "decision": (
                    None
                    if settlement.decision is None
                    else _decision_payload(settlement.decision)
                ),
                "superseded": [
                    _decision_payload(item) for item in settlement.superseded
                ],
            }
            for settlement in ruling.settlements
        ],
        "unresolved": list(ruling.unresolved),
    }


def _record_payload(
    record: overseer_store_models.ActionRecord | None,
) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "action_id": record.action_id,
        "plan_dir": record.plan_dir,
        "incident_id": record.incident_id,
        "operation": record.operation,
        "target": record.target,
        "status": record.status,
        "attempted_at": record.attempted_at,
        "settled_at": record.settled_at,
    }


def _receipt_payload(
    receipt: overseer_dispatch.Receipt | None,
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "outcome": receipt.outcome,
        "detail": receipt.detail,
        "locator": receipt.locator,
    }


def _dispatch_payload(dispatch: overseer_dispatch.Dispatch) -> dict[str, Any]:
    return {
        "operation": dispatch.action.payload.operation,
        "plan_dir": dispatch.action.plan_dir,
        "run_id": dispatch.action.run_id,
        "incident_id": dispatch.action.incident_id,
        "performed": dispatch.performed,
        "refusal": dispatch.refusal,
        "detail": dispatch.detail,
        "escalated": dispatch.escalated,
        "recoveries": dispatch.recoveries,
        "recovery_limit": overseer_dispatch.RECOVERY_LIMIT,
        "record": _record_payload(dispatch.record),
        "receipt": _receipt_payload(dispatch.receipt),
    }

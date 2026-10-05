"""Recovery Center cards: a read-only diagnosis of why a plan is stuck.

``build_workbench_recovery`` turns one plan's files, runner state, lock state,
commit artifacts, verification payloads and preflight repair hints into a
deterministic ``WorkbenchRecovery`` card: what kind of blocker it is, the
evidence behind that, and the actions and copyable commands that clear it.
Nothing here persists a diagnosis or calls a harness, so the card is recomputed
on every read.

The module sits below ``workbench``: the per-plan summary embeds the compact
card and shares a few state-reading helpers defined here, so ``workbench``
imports this module and never the reverse. ``DiscoveredPlan`` is imported for
type checking only for that reason.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import (
    events,
    intervention,
    plans,
    preflight,
    qna,
    redirect_cli,
    review_continuation,
    run_inputs,
    state,
    workbench_workspace,
)

if TYPE_CHECKING:
    from .workbench import DiscoveredPlan


RecoveryKind = Literal[
    "none",
    "review-continuation",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "conversation-recovery",
    "generic",
]
RECOVERY_KINDS: tuple[RecoveryKind, ...] = (
    "none",
    "review-continuation",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "conversation-recovery",
    "generic",
)


PAUSE_REASON_RECOVERY_KIND_BY_REASON: dict[str, RecoveryKind] = {
    "blocked": "reply-needed",
    "commit-failed": "commit-failed",
    "commit-preflight": "commit-preflight",
    # Both pauses hand the operator a decision about a conversation that cannot
    # be resumed, and `intervention.recovery_decision` is what says which one.
    # They share a kind rather than a generic card because the generic card's
    # advice -- inspect, then relaunch -- is wrong here: relaunching without the
    # one-use authorization re-derives the same pause, and relaunching beside an
    # unstopped predecessor is the thing the second pause exists to prevent.
    "conversation-recovery-required": "conversation-recovery",
    "dependent": "reply-needed",
    "malformed-contract": "malformed-contract",
    "no-contract": "no-contract",
    "not-loop-ready": "not-loop-ready",
    "redirect-successor-blocked": "conversation-recovery",
    "redirect-submission-unresolved": "conversation-recovery",
    "redirect-delivery-unrecordable": "conversation-recovery",
    "runner-pause": "reply-needed",
    "unresolved-prerequisite": "reply-needed",
    "verify-failed": "verify-failed",
}


GENERIC_RECOVERY_PAUSE_REASONS: tuple[str, ...] = (
    "nonzero-exit",
    "timeout",
    "no-status-change",
    "multiple-status-changes",
    "malformed-plan",
    "unknown-status",
    "lock-contention",
    "unexpected-status-change",
    "no-pending-but-incomplete",
    "harness-unavailable",
    "unsafe",
    "interrupted",
    "review-harness-required",
    "review-mutated-worktree",
    # The guard's git probes failed, so the review needs a working repository
    # before it can be relaunched — inspect, do not reply.
    "review-guard-unavailable",
    # Registered implementation turns remain unavailable until the workspace
    # write guard is installed — inspect or upgrade, do not request a reply.
    "workspace-write-guard-unavailable",
    # The retained repository changes need a durable scope repair plus fresh
    # expansion/preflight, not an operator reply to the paused harness turn.
    "workspace-write-scope-violation",
    # The plan must declare a repository registry and per-session scopes and be
    # re-prepared. No operator reply resolves it, and marking readiness cannot
    # either, so this is not the `not-loop-ready` recovery kind.
    "repository-registry-required",
    # Another live run claims a repository this plan registers. Only stopping
    # that run or narrowing this registry clears it; no reply does.
    "workspace-repository-contention",
    # Review repair-cycle materialization failed closed (Session 18); the
    # review artifacts or plan structure need inspection, not a reply.
    "malformed-review-repair",
    # Harness-failure classification categories (Session 5). These are
    # inspect/relaunch-from-the-workbench failures rather than reply-needed
    # question pauses; quota/auth/invalid-model are non-retryable and surface
    # generic recovery guidance, transport-failed is retryable upstream.
    "quota-exhausted",
    "auth-failed",
    "invalid-model",
    "transport-failed",
    # The turn rewrote another session's instructions. The plan needs inspecting
    # and the tampered artifact restoring before a relaunch; no reply undoes it.
    "plan-artifact-tampered",
    # The terminal adjudicator refused an unearned success. Relaunching re-derives
    # the same blocker from the same evidence, so the workbench offers the generic
    # inspect-then-relaunch guidance: the operator has to make the checks pass or
    # the review come back all-clear first, and no reply substitutes for either.
    "required-checks-incomplete",
    "review-not-clear",
    # A protected-path guard convicted the turn and the run deliberately kept
    # the retained changes. Only a human diffing the recorded before/after
    # evidence can decide whether to restore the path or keep the change, so
    # this is inspect-then-relaunch; no reply restores a file.
    "guard-protected-path-changed",
    # The turn-start snapshot the protected-path guard compares against was
    # unreadable, so the repair is to the evidence capture — a readable
    # worktree, a working git probe — not to anything an operator can say.
    "guard-protected-path-unavailable",
)


RecoverySeverity = Literal["none", "info", "warning", "error"]
RecoveryEvidenceSeverity = Literal["info", "warning", "error"]
RecoveryWarningSeverity = Literal["info", "warning", "error"]
RecoveryWarningCode = Literal[
    "state-missing-race",
    "state-unreadable",
    "state-malformed-json",
    "state-invalid-json-shape",
]
RecoveryActionKind = Literal["endpoint", "inspect", "navigate"]
RecoveryActionTarget = Literal[
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
]
RECOVERY_ACTION_TARGETS: tuple[RecoveryActionTarget, ...] = (
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
)


@dataclass(frozen=True)
class RecoveryWarning:
    """Structured warning metadata paired with legacy warning strings."""

    code: RecoveryWarningCode
    message: str
    severity: RecoveryWarningSeverity = "warning"

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryEvidenceRow:
    """Small diagnosis row suitable for a compact recovery card."""

    label: str
    value: str | None
    detail: str | None = None
    severity: RecoveryEvidenceSeverity = "info"

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryArtifactReference:
    """Reference to an existing ASL state, log, result, or artifact path."""

    label: str
    path: Path
    kind: str
    source: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": str(self.path),
            "kind": self.kind,
            "source": self.source,
        }


@dataclass(frozen=True)
class RecoveryCopyCommand:
    """Command text for the operator to copy; Workbench never executes it."""

    label: str
    command: str
    description: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "command": self.command,
            "description": self.description,
        }


@dataclass(frozen=True)
class RecoveryActionCard:
    """UI action backed only by existing Workbench affordances."""

    id: str
    label: str
    description: str
    kind: RecoveryActionKind
    target: RecoveryActionTarget | None = None
    endpoint: str | None = None
    method: str | None = None
    payload: Mapping[str, Any] | None = None
    disabled: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "target": self.target,
            "endpoint": self.endpoint,
            "method": self.method,
            "payload": dict(self.payload) if self.payload is not None else None,
            "disabled": self.disabled,
        }


@dataclass(frozen=True)
class WorkbenchRecovery:
    """Read-only recovery diagnosis for the Workbench detail view."""

    plan_dir: Path
    kind: RecoveryKind
    title: str
    summary: str
    severity: RecoverySeverity
    recoverable: bool
    evidence: tuple[RecoveryEvidenceRow, ...] = ()
    actions: tuple[RecoveryActionCard, ...] = ()
    commands: tuple[RecoveryCopyCommand, ...] = ()
    artifacts: tuple[RecoveryArtifactReference, ...] = ()
    run_id: str | None = None
    session_id: str | None = None
    turn_index: int | None = None
    commit_index: int | None = None
    warnings: tuple[str, ...] = ()
    warning_details: tuple[RecoveryWarning, ...] = ()
    review_resume: Mapping[str, Any] = field(default_factory=dict)
    workspace: Mapping[str, Any] = field(default_factory=dict)
    commit_groups: tuple[Mapping[str, Any], ...] = ()

    def compact_payload(self) -> dict[str, Any]:
        primary_action = self.actions[0] if self.actions else None
        return {
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "primary_action": (
                primary_action.to_payload() if primary_action is not None else None
            ),
            "artifact_count": len(self.artifacts),
            "command_count": len(self.commands),
            "warning_count": len(self.warnings),
            "review_resume": dict(self.review_resume),
            "workspace_root": self.workspace.get("workspace_root"),
            "repository_count": int(self.workspace.get("repository_count") or 0),
            "pending_commit_group_count": sum(
                row.get("status") != "complete" for row in self.commit_groups
            ),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "commit_index": self.commit_index,
            "evidence": [row.to_payload() for row in self.evidence],
            "actions": [action.to_payload() for action in self.actions],
            "commands": [command.to_payload() for command in self.commands],
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
            "warnings": list(self.warnings),
            "warning_details": [
                warning.to_payload() for warning in self.warning_details
            ],
            "review_resume": dict(self.review_resume),
            "workspace": dict(self.workspace),
            **workbench_workspace._workspace_payload(self.workspace),
            "commit_groups": [dict(group) for group in self.commit_groups],
            "compact": self.compact_payload(),
        }


@dataclass(frozen=True)
class _RecoveryBuildContext:
    """State gathered once for one recovery payload build."""

    snapshot: plans.PlanSnapshot
    state_path: Path
    events_path: Path
    progress_path: Path
    lock_path: Path
    lock_info: state.LockInfo | None
    runner_state: Mapping[str, Any]
    raw_state: Mapping[str, Any]
    pause_reason: str | None
    pause_details: Mapping[str, Any]
    pause_message: str
    base_artifacts: tuple[RecoveryArtifactReference, ...]
    plan_dir_payload: Mapping[str, Any]
    stale_unfinished: bool
    stale_reason: str | None
    warnings: tuple[str, ...]
    warning_details: tuple[RecoveryWarning, ...]
    review_resume: review_continuation.ReviewResume


def _coerce_path_value(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _session_identifier_from_payload(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("session_id")
    if isinstance(value, str) and value:
        return value
    fallback = payload.get("session")
    if isinstance(fallback, str) and fallback:
        return fallback
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback > 0:
        return str(fallback)
    return None


def _summary_base_paths(
    plan_dir: Path, state_dir: str | Path | None
) -> tuple[Path, Path, Path]:
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    return (
        plan_state / events.STATE_FILE_NAME,
        plan_state / events.EVENTS_FILE_NAME,
        plans.progress_path(plan_dir),
    )


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _payload_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _payload_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    return None


def _payload_mappings(
    payload: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _read_recovery_state_payload(
    state_path: Path,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[RecoveryWarning, ...]]:
    if not state_path.exists():
        return {}, (), ()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        message = "state.json disappeared before it could be read"
        warning = RecoveryWarning(code="state-missing-race", message=message)
        return {}, (message,), (warning,)
    except json.JSONDecodeError as error:
        message = f"state.json unreadable: {error}"
        warning = RecoveryWarning(code="state-malformed-json", message=message)
        return {}, (message,), (warning,)
    except OSError as error:
        message = f"state.json unreadable: {error}"
        warning = RecoveryWarning(code="state-unreadable", message=message)
        return {}, (message,), (warning,)
    if not isinstance(payload, dict):
        message = "state.json did not contain a JSON object"
        warning = RecoveryWarning(code="state-invalid-json-shape", message=message)
        return {}, (message,), (warning,)
    return payload, (), ()


def _recovery_base_artifacts(
    *,
    state_path: Path,
    events_path: Path,
    progress_path: Path,
) -> tuple[RecoveryArtifactReference, ...]:
    return (
        RecoveryArtifactReference(
            label="Runner state",
            path=state_path,
            kind="state",
            source="plan-state",
        ),
        RecoveryArtifactReference(
            label="Runner events",
            path=events_path,
            kind="events",
            source="plan-state",
        ),
        RecoveryArtifactReference(
            label="Progress journal",
            path=progress_path,
            kind="progress",
            source="plan",
        ),
    )


def _build_recovery_context(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None,
    *,
    reclaim: bool = True,
) -> _RecoveryBuildContext:
    state_path, events_path, progress_path = _summary_base_paths(
        snapshot.plan_dir, state_dir
    )
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state._read_lock_path(lock_path, snapshot.plan_dir, reclaim=reclaim)
    runner_state = state._runner_state_payload(
        state_path,
        lock_info=lock_info,
    )
    raw_state, warnings, warning_details = _read_recovery_state_payload(state_path)
    workspace = workbench_workspace._workspace_projection(
        snapshot.plan_dir,
        runtime=raw_state if raw_state.get("workspace_root") else runner_state,
    )
    stale_unfinished, stale_reason = state._stale_runner_state(runner_state, lock_info)
    base_artifacts = _recovery_base_artifacts(
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
    )
    reason = _pause_reason(raw_state, runner_state)
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, events_path
    )
    return _RecoveryBuildContext(
        snapshot=snapshot,
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
        lock_path=lock_path,
        lock_info=lock_info,
        runner_state=runner_state,
        raw_state=raw_state,
        pause_reason=reason,
        pause_details=_pause_details(raw_state),
        pause_message=_pause_message(raw_state) or reason or "Paused",
        base_artifacts=base_artifacts,
        plan_dir_payload={
            "plan_dir": str(snapshot.plan_dir),
            "cwd": workspace.get("workspace_root"),
            "workspace_root": workspace.get("workspace_root"),
            "registry_digest": workspace.get("registry_digest"),
            "repositories": workspace.get("repositories", []),
        },
        stale_unfinished=stale_unfinished,
        stale_reason=stale_reason,
        warnings=warnings,
        warning_details=warning_details,
        review_resume=review_resume,
    )


def _expired_wait_qna_context(
    snapshot: plans.PlanSnapshot,
    runner_state: Mapping[str, Any],
    state_dir: str | Path | None,
    *,
    reclaim: bool = True,
) -> qna.QnAContext | None:
    """Load the CLI's recovery projection for a nonterminal expired wait."""
    if runner_state.get("paused") is True:
        return None
    if (
        runner_state.get("waiting_for_reply_status")
        != state.WAITING_FOR_REPLY_STATUS_EXPIRED
    ):
        return None
    try:
        context = qna.load_qna_context(
            snapshot.plan_dir, state_dir=state_dir, reclaim=reclaim
        )
    except qna.QnAError:
        return None
    if context.pause_reason != qna.EXPIRED_WAIT_PAUSE_REASON:
        return None
    return context


_RECOVERY_ARTIFACT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("artifact_dir", "artifact-dir", "Artifact directory"),
    ("stdout_path", "stdout", "Stdout"),
    ("stderr_path", "stderr", "Stderr"),
    ("last_message_path", "last-message", "Last message"),
    ("result_path", "result", "Result JSON"),
)


def _artifact_references_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    source: str,
    label_prefix: str,
) -> tuple[RecoveryArtifactReference, ...]:
    if payload is None:
        return ()
    refs: list[RecoveryArtifactReference] = []
    for key, kind, label in _RECOVERY_ARTIFACT_FIELDS:
        path = _coerce_path_value(payload.get(key))
        if path is None:
            continue
        refs.append(
            RecoveryArtifactReference(
                label=f"{label_prefix} {label}".strip(),
                path=path,
                kind=kind,
                source=source,
            )
        )
    return tuple(refs)


def _latest_turn_matching(
    raw_state: Mapping[str, Any],
    predicate: Callable[[Mapping[str, Any]], bool],
) -> Mapping[str, Any] | None:
    active_turn = _payload_mapping(raw_state, "active_turn")
    if active_turn is not None and predicate(active_turn):
        return active_turn
    for turn in reversed(_payload_mappings(raw_state, "turns")):
        if predicate(turn):
            return turn
    return None


def _turn_has_verification_reason(
    turn: Mapping[str, Any],
    reason: str,
) -> bool:
    verification = _payload_mapping(turn, "verification")
    return verification is not None and verification.get("reason") == reason


def _turn_failed(turn: Mapping[str, Any]) -> bool:
    if _payload_mapping(turn, "verification") is not None:
        verification = _payload_mapping(turn, "verification")
        if verification is not None and verification.get("passed") is False:
            return True
    status_result = _payload_str(turn, "status_result")
    if status_result and status_result not in {"complete", "unknown"}:
        return True
    return (
        turn.get("timed_out") is True
        or turn.get("interrupted") is True
        or _payload_int(turn, "returncode") not in {None, 0}
        or _payload_str(turn, "plan_error") is not None
    )


def _turn_paused(turn: Mapping[str, Any]) -> bool:
    return (
        _payload_str(turn, "runner_pause") is not None
        or _payload_str(turn, "action_required") is not None
        or _payload_str(turn, "last_message_path") is not None
    )


def _latest_failed_commit(raw_state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    pause_details = _pause_details(raw_state)
    commit = _payload_mapping(pause_details, "commit")
    if commit is not None and commit.get("success") is False:
        return commit
    for candidate in reversed(_payload_mappings(raw_state, "commits")):
        if candidate.get("success") is False:
            return candidate
    return None


def _latest_relevant_turn(
    raw_state: Mapping[str, Any],
    *,
    kind: RecoveryKind,
    reason: str | None,
) -> Mapping[str, Any] | None:
    if kind in {"verify-failed", "no-contract", "malformed-contract"}:
        if reason:
            turn = _latest_turn_matching(
                raw_state,
                lambda candidate: _turn_has_verification_reason(candidate, reason),
            )
            if turn is not None:
                return turn
        turn = _latest_turn_matching(
            raw_state,
            lambda candidate: _payload_mapping(candidate, "verification") is not None,
        )
        if turn is not None:
            return turn
    if kind == "reply-needed":
        turn = _latest_turn_matching(raw_state, _turn_paused)
        if turn is not None:
            return turn
    if kind == "generic":
        active_turn = _payload_mapping(raw_state, "active_turn")
        if active_turn is not None:
            return active_turn
    return _latest_turn_matching(raw_state, _turn_failed)


def _pause_reason(
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
) -> str | None:
    for payload in (raw_state, runner_state):
        for key in ("pause_reason", "reason"):
            value = _payload_str(payload, key)
            if value is not None:
                return value
    return None


def _pause_reason_recovery_kind(reason: str | None) -> RecoveryKind | None:
    if reason is None:
        return None
    kind = PAUSE_REASON_RECOVERY_KIND_BY_REASON.get(reason)
    if kind is not None:
        return kind
    if reason in GENERIC_RECOVERY_PAUSE_REASONS:
        return "generic"
    return None


def _pause_message(raw_state: Mapping[str, Any]) -> str | None:
    return _payload_str(raw_state, "pause_message")


def _pause_details(raw_state: Mapping[str, Any]) -> Mapping[str, Any]:
    details = _payload_mapping(raw_state, "pause_details")
    if details is not None:
        return details
    details = _payload_mapping(raw_state, "details")
    if details is not None:
        return details
    return {}


def _runtime_failure_reason(
    *,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> str | None:
    for failure in _payload_mappings(details, "failures"):
        failure_reason = _payload_str(failure, "failure_reason")
        if failure_reason is not None:
            return failure_reason
    if verification is not None:
        for check in _payload_mappings(verification, "checks"):
            status = _payload_str(check, "status")
            failure_reason = _payload_str(check, "failure_reason")
            if failure_reason is not None and status != "pass":
                return failure_reason
        for check in _payload_mappings(verification, "checks"):
            failure_reason = _payload_str(check, "failure_reason")
            if failure_reason is not None:
                return failure_reason
    return None


def _hint_evidence_row(hint: preflight.RepairHint | None) -> RecoveryEvidenceRow | None:
    if hint is None:
        return None
    severity: RecoveryEvidenceSeverity
    if hint.severity_hint == "red":
        severity = "error"
    elif hint.severity_hint == "yellow":
        severity = "warning"
    else:
        severity = "info"
    return RecoveryEvidenceRow(
        label="Repair hint",
        value=hint.message,
        detail=hint.check_id,
        severity=severity,
    )


def _evidence_severity(value: Any) -> RecoveryEvidenceSeverity:
    if value == "red":
        return "error"
    if value == "yellow":
        return "warning"
    return "info"


def _readiness_gate_evidence(
    gate: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = [
        RecoveryEvidenceRow(
            label="Readiness state",
            value=(
                str(gate.get("readiness_state"))
                if gate.get("readiness_state") is not None
                else None
            ),
            severity="warning",
        )
    ]
    expected = _payload_str(gate, "expected_readiness_sha256")
    recorded = _payload_str(gate, "recorded_readiness_sha256")
    if expected is not None or recorded is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Readiness SHA",
                value=recorded,
                detail=f"expected {expected}" if expected else None,
                severity="warning" if expected != recorded else "info",
            )
        )
    counts = _payload_mapping(gate, "findings_by_severity")
    if counts is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Findings",
                value=(
                    f"red {counts.get('red', 0)}, "
                    f"yellow {counts.get('yellow', 0)}, "
                    f"green {counts.get('green', 0)}"
                ),
                severity=(
                    "error"
                    if counts.get("red")
                    else "warning"
                    if counts.get("yellow")
                    else "info"
                ),
            )
        )
    findings = _payload_mappings(gate, "findings")
    for finding in findings[:5]:
        category = _payload_str(finding, "category") or "finding"
        message = _payload_str(finding, "message")
        severity_value = _payload_str(finding, "severity")
        rows.append(
            RecoveryEvidenceRow(
                label=f"{severity_value or 'info'} finding",
                value=category,
                detail=message,
                severity=_evidence_severity(severity_value),
            )
        )
    if len(findings) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional findings",
                value=str(len(findings) - 5),
                severity="warning",
            )
        )
    return tuple(rows)


def _endpoint_action(
    *,
    action_id: str,
    label: str,
    description: str,
    endpoint: str,
    payload: Mapping[str, Any],
    target: RecoveryActionTarget | None = None,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="endpoint",
        target=target,
        endpoint=endpoint,
        method="POST",
        payload=payload,
    )


def _inspect_action(
    description: str,
    *,
    target: RecoveryActionTarget = "artifacts",
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id="inspect-artifacts",
        label="Inspect artifacts",
        description=description,
        kind="inspect",
        target=target,
    )


def _navigate_action(
    *,
    action_id: str,
    label: str,
    description: str,
    target: RecoveryActionTarget,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="navigate",
        target=target,
    )


def _retry_run_action(plan_dir_payload: Mapping[str, Any]) -> RecoveryActionCard:
    return _endpoint_action(
        action_id="retry-run",
        label="Retry run",
        description=(
            "Launch the existing runner flow after applying the recovery guidance."
        ),
        endpoint="/api/launch",
        payload=plan_dir_payload,
        target="launch",
    )


def _profile_action(description: str) -> RecoveryActionCard:
    return _navigate_action(
        action_id="review-profile",
        label="Review profile",
        description=description,
        target="profile",
    )


def _quote_command_path(path: Path | str) -> str:
    return shlex.quote(str(path))


def _preflight_repair_commands(plan_dir: Path) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    quoted_skill_plan = _quote_command_path(str(plan_dir) + os.sep)
    return (
        RecoveryCopyCommand(
            label="Repair readiness",
            command=f"asl preflight {quoted_plan} --mark-loop-ready",
            description=(
                "Copy this into a shell after repairing plan-readiness findings."
            ),
        ),
        RecoveryCopyCommand(
            label="Iterate preflight",
            command=f"$plan-preflight-iterate {quoted_skill_plan}",
            description=(
                "Copy this into the harness when preflight findings need an "
                "interactive repair pass."
            ),
        ),
    )


def _verification_repair_commands(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    commands = [
        RecoveryCopyCommand(
            label="Run preflight",
            command=f"asl preflight {quoted_plan} --json",
            description=(
                "Copy this into a shell to inspect current contract and "
                "readiness findings."
            ),
        )
    ]
    if session_id:
        commands.insert(
            0,
            RecoveryCopyCommand(
                label="Re-run verification",
                command=(
                    "asl verify "
                    f"{quoted_plan} --session {shlex.quote(session_id)} --json"
                ),
                description=(
                    "Copy this into a shell after repairing the session or "
                    "its Completion Contract."
                ),
            ),
        )
    return tuple(commands)


def _session_file_path(plan_dir: Path, session_id: str | None) -> Path | None:
    if not session_id:
        return None
    try:
        return plan_dir / plans.session_id_to_filename(session_id)
    except ValueError:
        return None


def _session_reference_evidence(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = []
    if session_id:
        rows.append(
            RecoveryEvidenceRow(
                label="Session",
                value=session_id,
                severity="error",
            )
        )
    session_path = _session_file_path(plan_dir, session_id)
    if session_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Session file",
                value=str(session_path),
                severity="error",
            )
        )
    return tuple(rows)


def _verification_check_evidence(
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    if verification is None:
        return ()
    rows: list[RecoveryEvidenceRow] = []
    failed_checks = [
        check
        for check in _payload_mappings(verification, "checks")
        if check.get("status") != "pass" or _payload_str(check, "failure_reason")
    ]
    for check in failed_checks[:5]:
        name = _payload_str(check, "name") or "check"
        failure_reason = _payload_str(check, "failure_reason")
        repository = _payload_str(check, "repo")
        rows.append(
            RecoveryEvidenceRow(
                label="Failed check",
                value=(f"{repository}:{name}" if repository else name),
                detail=failure_reason,
                severity="error",
            )
        )
    if len(failed_checks) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional failed checks",
                value=str(len(failed_checks) - 5),
                severity="error",
            )
        )
    return tuple(rows)


_VERIFICATION_ARTIFACT_STREAMS: tuple[tuple[str, str], ...] = (
    ("stdout_artifact_path", "stdout"),
    ("stderr_artifact_path", "stderr"),
)


def _verification_check_artifacts(
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryArtifactReference, ...]:
    """Follow v2 ``*_artifact_path`` references on failed verification checks.

    Schema v2 checks reference the full failed-command output as a gzip
    artifact instead of embedding it. Surfacing those paths lets the Recovery
    Center point operators at the recoverable evidence (readable through the
    bounded gzip-aware log API) rather than only the turn-level logs.
    """
    if verification is None:
        return ()
    refs: list[RecoveryArtifactReference] = []
    for check in _payload_mappings(verification, "checks"):
        if check.get("status") == "pass" and not _payload_str(check, "failure_reason"):
            continue
        name = _payload_str(check, "name") or "check"
        for key, stream in _VERIFICATION_ARTIFACT_STREAMS:
            path = _coerce_path_value(check.get(key))
            if path is None:
                continue
            refs.append(
                RecoveryArtifactReference(
                    label=f"Check {name} {stream}",
                    path=path,
                    kind="verification-output",
                    source="verification-check",
                )
            )
    return tuple(refs)


def _harness_config_artifacts(
    details: Mapping[str, Any],
) -> tuple[RecoveryArtifactReference, ...]:
    config_path = _coerce_path_value(details.get("config_path"))
    if config_path is None:
        return ()
    return (
        RecoveryArtifactReference(
            label="Harness config",
            path=config_path,
            kind="config",
            source="pause-details",
        ),
    )


def _harness_config_evidence(
    *,
    reason: str | None,
    message: str,
    details: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value=reason,
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Pause message",
            value=message,
            severity="error",
        ),
    ]
    requested_harness = _payload_str(details, "requested_harness")
    if requested_harness is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Requested harness",
                value=requested_harness,
                severity="error",
            )
        )
    config_path = _payload_str(details, "config_path")
    if config_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness config",
                value=config_path,
                severity="warning",
            )
        )
    error = _payload_str(details, "error")
    if error is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness error",
                value=error,
                severity="error",
            )
        )
    return tuple(rows)


def _recovery_common_context(
    *,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    selected_turn: Mapping[str, Any] | None,
    selected_commit: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, int | None, int | None]:
    run_id = (
        _payload_str(raw_state, "run_id")
        or _payload_str(runner_state, "run_id")
        or (
            _payload_str(selected_turn, "run_id") if selected_turn is not None else None
        )
        or (
            _payload_str(selected_commit, "run_id")
            if selected_commit is not None
            else None
        )
    )
    session_id = None
    if selected_turn is not None:
        session_id = _session_identifier_from_payload(selected_turn)
    if session_id is None and selected_commit is not None:
        session_id = _session_identifier_from_payload(selected_commit)
    if session_id is None:
        details = _pause_details(raw_state)
        session_id = _session_identifier_from_payload(details)
    turn_index = (
        _payload_int(selected_turn, "turn_index") if selected_turn is not None else None
    )
    commit_index = (
        _payload_int(selected_commit, "commit_index")
        if selected_commit is not None
        else None
    )
    return run_id, session_id, turn_index, commit_index


def _runtime_recovery_evidence(
    *,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [RecoveryEvidenceRow(label="Pause reason", value=reason, severity="error")]
    failure_reason = _runtime_failure_reason(details=details, verification=verification)
    if failure_reason is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=failure_reason,
                severity="error",
            )
        )
    hint = preflight.runtime_repair_hint(
        reason,
        failure_reason=failure_reason,
    )
    hint_row = _hint_evidence_row(hint)
    if hint_row is not None:
        rows.append(hint_row)
    if verification is not None:
        passed = verification.get("passed")
        if isinstance(passed, bool):
            rows.append(
                RecoveryEvidenceRow(
                    label="Verification passed",
                    value=str(passed).lower(),
                    severity="info" if passed else "error",
                )
            )
    return tuple(rows)


def _verification_recovery_evidence(
    *,
    plan_dir: Path,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    return (
        _runtime_recovery_evidence(
            reason=reason,
            details=details,
            verification=verification,
        )
        + _session_reference_evidence(plan_dir, session_id=session_id)
        + _verification_check_evidence(verification)
    )


_VERIFICATION_RECOVERY_TITLES: dict[str, str] = {
    "verify-failed": "Verification failed",
    "no-contract": "Completion contract missing",
    "malformed-contract": "Completion contract malformed",
}


def _verification_recovery_title(reason: str) -> str:
    return _VERIFICATION_RECOVERY_TITLES[reason]


def _selected_verification_context(
    raw_state: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    reason: str,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    selected_turn = _latest_relevant_turn(
        raw_state,
        kind=reason,  # type: ignore[arg-type]
        reason=reason,
    )
    verification = (
        _payload_mapping(selected_turn, "verification")
        if selected_turn is not None
        else None
    )
    selected_session_id = (
        _session_identifier_from_payload(selected_turn)
        if selected_turn is not None
        else _session_identifier_from_payload(details)
    )
    return selected_turn, verification, selected_session_id


def _commit_failed_evidence(
    failed_commit: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    status_result = (
        _payload_str(failed_commit, "status_result")
        if failed_commit is not None
        else None
    )
    returncode = (
        _payload_int(failed_commit, "returncode") if failed_commit is not None else None
    )
    repository = (
        _payload_str(failed_commit, "repository_alias")
        if failed_commit is not None
        else None
    )
    rows = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value="commit-failed",
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Commit status",
            value=status_result,
            detail=(f"return code {returncode}" if returncode is not None else None),
            severity="error",
        ),
    ]
    if repository is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Failed repository",
                value=repository,
                severity="error",
            )
        )
    return tuple(rows)


def _commit_group_evidence(
    raw_state: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = []
    groups = raw_state.get("commit_groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        return ()
    unresolved = [
        group
        for group in groups
        if isinstance(group, Mapping) and group.get("status") != "complete"
    ]
    for group in unresolved[:3]:
        successful = group.get("successful_repositories")
        shas = (
            [
                f"{row.get('alias') or row.get('key')}={row.get('commit_sha')}"
                for row in successful
                if isinstance(row, Mapping) and row.get("commit_sha")
            ]
            if isinstance(successful, Sequence)
            else []
        )
        remaining = group.get("remaining_aliases")
        remaining_text = (
            ", ".join(str(alias) for alias in remaining[:6])
            if isinstance(remaining, list)
            else None
        )
        detail_parts = []
        if shas:
            detail_parts.append("committed " + ", ".join(shas[:4]))
        if remaining_text:
            detail_parts.append("remaining " + remaining_text)
        rows.append(
            RecoveryEvidenceRow(
                label="Pending commit group",
                value=str(
                    group.get("failed_alias") or group.get("status") or "pending"
                ),
                detail="; ".join(detail_parts) or None,
                severity="error" if group.get("failed_alias") else "warning",
            )
        )
    if len(unresolved) > 3:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional pending commit groups",
                value=str(len(unresolved) - 3),
                severity="warning",
            )
        )
    return tuple(rows)


def _workspace_guard_recovery_evidence(
    details: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    guard = _payload_mapping(details, "workspace_write_guard")
    if guard is None:
        return ()
    rows: list[RecoveryEvidenceRow] = []
    for violation in _payload_mappings(guard, "violations")[:5]:
        alias = _payload_str(violation, "alias") or _payload_str(
            violation, "repository"
        )
        qualified = violation.get("qualified_paths")
        paths = (
            ", ".join(str(path) for path in qualified[:4])
            if isinstance(qualified, list)
            else None
        )
        rows.append(
            RecoveryEvidenceRow(
                label="Scope violation",
                value=alias,
                detail=paths,
                severity="error",
            )
        )
    unavailable = guard.get("unavailable")
    if isinstance(unavailable, Mapping):
        for repository, evidence in list(unavailable.items())[:5]:
            rows.append(
                RecoveryEvidenceRow(
                    label="Repository evidence unavailable",
                    value=str(repository),
                    detail=json.dumps(evidence, sort_keys=True)[:280],
                    severity="error",
                )
            )
    repair = guard.get("repair_required")
    if isinstance(repair, Mapping) and any(repair.values()):
        rows.append(
            RecoveryEvidenceRow(
                label="Rebind and scope guidance",
                value="durable scope + fresh expansion + preflight required",
                detail=(
                    "Workspace changes require `asl prepare PLAN_DIR --resume "
                    "--cwd NEW_WORKSPACE --rebind-workspace`; write authority "
                    "requires a plan edit and fresh preflight."
                ),
                severity="warning",
            )
        )
    return tuple(rows)


def _build_recovery_payload(
    *,
    plan_dir: Path,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
    warnings: tuple[str, ...] = (),
    warning_details: tuple[RecoveryWarning, ...] = (),
    review_resume: Mapping[str, Any] | None = None,
) -> WorkbenchRecovery:
    run_id, session_id, turn_index, commit_index = _recovery_common_context(
        raw_state=raw_state,
        runner_state=runner_state,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
    )
    return WorkbenchRecovery(
        plan_dir=plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        evidence=evidence,
        actions=actions,
        commands=commands,
        artifacts=artifacts,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        commit_index=commit_index,
        warnings=warnings,
        warning_details=warning_details,
        review_resume=review_resume or {},
        workspace=workbench_workspace._workspace_projection(
            plan_dir, runtime=raw_state
        ),
        commit_groups=tuple(
            dict(group)
            for group in raw_state.get("commit_groups", ())
            if isinstance(group, Mapping)
        ),
    )


_VERIFICATION_FAILURE_REASONS = frozenset(
    {"verify-failed", "no-contract", "malformed-contract"}
)


def _build_context_recovery_payload(
    context: _RecoveryBuildContext,
    *,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
) -> WorkbenchRecovery:
    return _build_recovery_payload(
        plan_dir=context.snapshot.plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        raw_state=context.raw_state,
        runner_state=context.runner_state,
        artifacts=artifacts,
        evidence=evidence,
        actions=actions,
        commands=commands,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        warnings=context.warnings,
        warning_details=context.warning_details,
        review_resume=context.review_resume.to_payload(),
    )


def _build_active_run_recovery(
    context: _RecoveryBuildContext,
    *,
    selected_turn: Mapping[str, Any] | None,
) -> WorkbenchRecovery:
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="active-turn",
        label_prefix="Active turn",
    )
    lock_info = context.lock_info
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="Run in progress",
        summary="No recovery action is required while the runner is active.",
        severity="none",
        recoverable=False,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Runner lock",
                value="active",
                detail=lock_info.reason if lock_info is not None else None,
                severity="info",
            ),
        ),
    )


def _build_not_loop_ready_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    gate = (
        _payload_mapping(context.raw_state, "readiness_gate") or context.pause_details
    )
    evidence = (
        RecoveryEvidenceRow(
            label="Pause message",
            value=context.pause_message,
            severity="warning",
        ),
    ) + _readiness_gate_evidence(gate)
    return _build_context_recovery_payload(
        context,
        kind="not-loop-ready",
        title="Plan is not loop-ready",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=evidence,
        actions=(_retry_run_action(context.plan_dir_payload),),
        commands=_preflight_repair_commands(context.snapshot.plan_dir),
    )


def _build_verification_failure_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str,
) -> WorkbenchRecovery:
    selected_turn, verification, selected_session_id = _selected_verification_context(
        context.raw_state,
        context.pause_details,
        reason=reason,
    )
    artifacts = (
        context.base_artifacts
        + _artifact_references_from_payload(
            selected_turn,
            source="failed-turn",
            label_prefix="Failed turn",
        )
        + _verification_check_artifacts(verification)
    )
    return _build_context_recovery_payload(
        context,
        kind=reason,  # type: ignore[arg-type]
        title=_verification_recovery_title(reason),
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_verification_recovery_evidence(
            plan_dir=context.snapshot.plan_dir,
            reason=reason,
            details=context.pause_details,
            verification=verification,
            session_id=selected_session_id,
        ),
        actions=(
            _inspect_action("Open the failed turn logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
        ),
        commands=_verification_repair_commands(
            context.snapshot.plan_dir,
            session_id=selected_session_id,
        ),
    )


def _build_commit_preflight_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="commit-preflight",
        title="Commit preflight failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value="commit-preflight",
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="error",
            ),
        ),
        actions=(
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch and commit settings before retrying."),
        ),
        commands=(
            RecoveryCopyCommand(
                label="Inspect worktree",
                command="git status --porcelain",
                description=(
                    "Copy this into the repository shell to inspect dirty files "
                    "before retrying commit mode."
                ),
            ),
        ),
    )


def _build_commit_failed_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    failed_commit = _latest_failed_commit(context.raw_state)
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        failed_commit,
        source="failed-commit",
        label_prefix="Failed commit",
    )
    return _build_context_recovery_payload(
        context,
        kind="commit-failed",
        title="Auto-commit failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_commit=failed_commit,
        artifacts=artifacts,
        evidence=(
            _commit_failed_evidence(failed_commit)
            + _commit_group_evidence(context.raw_state)
        ),
        actions=(
            _inspect_action("Open the failed commit logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review commit mode and launch profile before retrying."),
        ),
    )


def _is_harness_config_recovery(
    *,
    reason: str | None,
    details: Mapping[str, Any],
) -> bool:
    return (
        reason == "harness-unavailable"
        or _payload_str(details, "requested_harness") is not None
        or _payload_str(details, "config_path") is not None
    )


def _build_harness_config_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    artifacts = (
        context.base_artifacts
        + _harness_config_artifacts(context.pause_details)
        + _artifact_references_from_payload(
            selected_turn,
            source="failed-turn",
            label_prefix="Failed turn",
        )
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Harness configuration needs attention",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_harness_config_evidence(
            reason=reason,
            message=context.pause_message,
            details=context.pause_details,
        ),
        actions=(
            _profile_action("Review harness and profile fields before retrying."),
            _inspect_action("Open runner state, events, and harness config artifacts."),
            _retry_run_action(context.plan_dir_payload),
        ),
    )


def _build_reply_needed_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="reply-needed",
        reason=reason,
    )
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="paused-turn",
        label_prefix="Paused turn",
    )
    return _build_context_recovery_payload(
        context,
        kind="reply-needed",
        title="Reply needed",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value=reason,
                severity="warning",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="reply",
                label="Reply",
                description=(
                    "Send an operator reply through the existing reply endpoint."
                ),
                endpoint="/api/reply",
                payload=context.plan_dir_payload,
                target="reply",
            ),
        ),
    )


def _build_stale_state_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="stale-state",
        title="Runner state is stale",
        summary=context.stale_reason or "Runner state is unfinished.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Stale state",
                value=context.stale_reason,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear stale runner state through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_stale_lock_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    lock_info = context.lock_info
    if lock_info is None:
        return _build_clean_none_recovery(context)
    return _build_context_recovery_payload(
        context,
        kind="stale-lock",
        title="Runner lock is stale",
        summary=lock_info.reason or "Runner lock is stale.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts
        + (
            RecoveryArtifactReference(
                label="Runner lock",
                path=lock_info.path,
                kind="lock",
                source="plan-state",
            ),
        ),
        evidence=(
            RecoveryEvidenceRow(
                label="Lock reason",
                value=lock_info.reason,
                detail=(f"pid {lock_info.pid}" if lock_info.pid is not None else None),
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear the stale lock through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_failed_finished_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    failed_commit = _latest_failed_commit(context.raw_state)
    selected_commit = failed_commit if reason == "commit-failed" else None
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_commit or selected_turn,
        source="failed-commit" if selected_commit is not None else "failed-turn",
        label_prefix=(
            "Failed commit" if selected_commit is not None else "Failed turn"
        ),
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Run failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=reason,
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Failure message",
                value=context.pause_message,
                severity="error",
            ),
        )
        + _workspace_guard_recovery_evidence(context.pause_details)
        + _commit_group_evidence(context.raw_state),
        actions=(
            _inspect_action("Open the latest relevant logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch profile fields before retrying."),
        ),
    )


#: What each conversation recovery decision means in the card's own words, and
#: what the operator is actually being asked to weigh. Keyed by the decision
#: `intervention.recovery_decision` returns rather than by the pause reason, so
#: adding a pause that maps to an existing decision needs no change here.
_CONVERSATION_RECOVERY_TITLES: dict[str, str] = {
    intervention.RECOVERY_DECISION_FRESH_CONVERSATION: (
        "Conversation cannot be resumed"
    ),
    intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE: (
        "Predecessor run is not known to be stopped"
    ),
}
_CONVERSATION_RECOVERY_SUMMARIES: dict[str, str] = {
    intervention.RECOVERY_DECISION_FRESH_CONVERSATION: (
        "The durable conversation bound to the pending session cannot be resumed "
        "exactly. Inspect the recorded binding, then either repair it or relaunch "
        "with the one-use --fresh-conversation authorization -- which discards the "
        "agent's accumulated context, so it is a decision rather than a button."
    ),
    intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE: (
        "A correction was accepted against a turn whose run cannot be shown "
        "stopped, so no successor may be started beside it. Inspect the redirect "
        "journal and the predecessor process; there is no authorization that "
        "makes a second worker safe."
    ),
}


def _build_conversation_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
    state_dir: str | Path | None,
) -> WorkbenchRecovery:
    """Report which recovery an unresumable conversation is waiting on.

    Read-only by construction, and that is the point of the card. Every action
    it offers is an inspect or a copyable command, because both decisions this
    kind covers are ones a person has to make: one discards an agent's context
    and its authorization is one-use, and the other has no authorization at all.
    An endpoint action here would either persist a one-use decision in a
    replayable request or invite an operator to start a second worker beside a
    live one.

    The decision and both commands come from :mod:`.intervention` and
    :mod:`.redirect_cli`, so this card, ``asl control recover-conversation``,
    and the bridge's recovery read cannot name three different recoveries for
    the same pause.
    """
    if reason in (
        events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
        events.PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE,
    ):
        base = ["asl", "control"]
        extra = ["--state-dir", str(state_dir)] if state_dir is not None else []
        detail = context.pause_details.get("redirect_recovery")
        operation_id = (
            detail.get("operation_id", "ID") if isinstance(detail, dict) else "ID"
        )
        return _build_context_recovery_payload(
            context,
            kind="conversation-recovery",
            title="Submission outcome needs an explicit decision",
            summary=(
                "Inspect native history before deciding whether the correction "
                "was submitted. A retry cannot resolve this uncertainty."
            ),
            severity="error",
            recoverable=True,
            artifacts=context.base_artifacts,
            evidence=(
                RecoveryEvidenceRow(
                    label="Pause reason",
                    value=reason,
                    detail=context.pause_message,
                    severity="error",
                ),
            ),
            actions=(
                _inspect_action("Inspect native history and the redirect journal."),
            ),
            commands=(
                RecoveryCopyCommand(
                    label="Inspect redirect status",
                    command=shlex.join(
                        base
                        + ["redirect-status", str(context.snapshot.plan_dir)]
                        + extra
                        + ["--json"]
                    ),
                    description="Read the recorded delivery evidence; writes nothing.",
                ),
                RecoveryCopyCommand(
                    label="Record a submission decision",
                    command=shlex.join(
                        base
                        + [
                            "resolve-redirect",
                            str(context.snapshot.plan_dir),
                            "--operation-id",
                            str(operation_id),
                            "--outcome",
                            "OUTCOME",
                            "--reason",
                            "EXPLANATION",
                        ]
                        + extra
                    ),
                    description=(
                        "Choose submitted or not-submitted from native evidence "
                        "and replace EXPLANATION. Neither outcome is a default."
                    ),
                ),
            ),
        )
    classified = intervention.recovery_decision(reason, paused=True)
    decision = classified.decision
    commands = redirect_cli.recovery_commands(
        context.snapshot.plan_dir,
        str(state_dir) if state_dir is not None else None,
        decision=decision,
    )
    inspect_command = commands["inspect_command"]
    authorization_command = commands["authorization_command"]

    evidence: list[RecoveryEvidenceRow] = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value=reason,
            detail=context.pause_message or None,
            severity="warning",
        ),
        RecoveryEvidenceRow(
            label="Decision",
            value=decision,
            detail=f"authorized by {classified.authorized_by}",
            severity="warning",
        ),
        # Stated rather than implied: neither pause is recoverable by the run
        # loop, so a reader must not take "paused" for "relaunch and see".
        RecoveryEvidenceRow(
            label="Automatic resume",
            value="no",
            detail="the run loop cannot clear this pause on its own",
            severity="warning",
        ),
    ]
    if authorization_command is not None:
        evidence.append(
            RecoveryEvidenceRow(
                label="Authorization",
                value="one-use",
                detail=redirect_cli.FRESH_CONVERSATION_NOTE,
                severity="warning",
            )
        )
    else:
        evidence.append(
            RecoveryEvidenceRow(
                label="Authorization",
                value="none exists",
                detail=(
                    "no text an operator sends makes a second worker safe to "
                    "start beside an unstopped predecessor"
                ),
                severity="error",
            )
        )

    copy_commands: list[RecoveryCopyCommand] = []
    if inspect_command is not None:
        copy_commands.append(
            RecoveryCopyCommand(
                label="Inspect recovery",
                command=inspect_command,
                description="Read the recorded binding and journal; writes nothing.",
            )
        )
    if authorization_command is not None:
        copy_commands.append(
            RecoveryCopyCommand(
                label="Authorize fresh conversation",
                command=authorization_command,
                description=redirect_cli.FRESH_CONVERSATION_NOTE,
            )
        )

    return _build_context_recovery_payload(
        context,
        kind="conversation-recovery",
        title=_CONVERSATION_RECOVERY_TITLES.get(
            decision or "", "Conversation recovery required"
        ),
        summary=_CONVERSATION_RECOVERY_SUMMARIES.get(
            decision or "",
            context.pause_message or "A conversation decision is required.",
        ),
        severity="warning" if authorization_command is not None else "error",
        # Recoverable, but only by a person: the commands above are the recovery,
        # and `actions` deliberately carries no endpoint that performs one.
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=tuple(evidence),
        actions=(
            _inspect_action(
                "Inspect the recorded conversation binding and redirect journal "
                "before deciding."
            ),
        ),
        commands=tuple(copy_commands),
    )


def _build_clean_none_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="No recovery needed",
        summary="The plan has no structured blocker.",
        severity="none",
        recoverable=False,
        artifacts=context.base_artifacts,
        evidence=(),
    )


def _review_resume_artifacts(
    context: _RecoveryBuildContext,
) -> tuple[RecoveryArtifactReference, ...]:
    artifacts = list(context.base_artifacts)
    for filename, label, kind in (
        ("final-review.md", "Final review", "review-output"),
        ("final-review.json", "Structured review", "review-artifact"),
    ):
        path = context.snapshot.plan_dir / filename
        try:
            present = path.is_file()
        except OSError:
            present = False
        if present:
            artifacts.append(
                RecoveryArtifactReference(
                    label=label,
                    path=path,
                    kind=kind,
                    source="review-continuation",
                )
            )
    return tuple(artifacts)


def _build_review_continuation_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery | None:
    resume = context.review_resume
    if resume.action == "initial-review":
        return None
    if (
        resume.action == "terminal"
        and resume.blocker in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
    ):
        return None

    evidence = [
        RecoveryEvidenceRow(
            label="Review continuation",
            value=resume.action,
            detail=resume.review_session_id,
            severity="info",
        ),
        RecoveryEvidenceRow(
            label="Historical materializations",
            value=str(resume.historical_materializations),
            severity="info",
        ),
    ]
    actions: tuple[RecoveryActionCard, ...]

    def profile_guidance(*, include_cap: bool) -> str:
        guidance = (
            "Configure a review harness distinct from the implementation "
            "harness so the next launch supplies --review-harness."
        )
        if include_cap:
            guidance += (
                " Set max review-fix cycles to "
                f"{resume.minimum_required_cap} or higher so the next launch "
                "supplies --max-review-fix-cycles."
            )
        return guidance

    if resume.blocker == "higher-cap-required":
        if resume.selected_cap is None:
            title = "Review-fix cap required"
            summary = (
                "Review repairs can resume after supplying "
                "--max-review-fix-cycles with a value of "
                f"{resume.minimum_required_cap} or higher."
            )
            cap_detail = "No review-fix cap has been recorded"
            action_description = profile_guidance(include_cap=True)
        else:
            title = "Higher review-fix cap required"
            summary = (
                "Review repair budget is exhausted at the selected cap of "
                f"{resume.selected_cap}; increase --max-review-fix-cycles "
                f"to {resume.minimum_required_cap} or higher."
            )
            cap_detail = f"Selected cap: {resume.selected_cap}"
            action_description = profile_guidance(include_cap=True)
        summary += (
            " Configure a review harness distinct from the implementation "
            "harness; without --review-harness, review continuation will not "
            "run."
        )
        severity: RecoverySeverity = "warning"
        evidence.append(
            RecoveryEvidenceRow(
                label="Minimum review-fix cap",
                value=str(resume.minimum_required_cap),
                detail=cap_detail,
                severity="warning",
            )
        )
        actions = (_profile_action(action_description),)
    elif resume.blocker == "artifact-invalid":
        title = "Review artifact needs correction"
        summary = resume.artifact_error or (
            "The structured review artifact is not safely materializable."
        )
        severity = "error"
        evidence.append(
            RecoveryEvidenceRow(
                label="Artifact validation",
                value="invalid",
                detail=resume.artifact_error,
                severity="error",
            )
        )
        actions = (
            _inspect_action(
                "Inspect final-review.md and final-review.json, then rerun."
            ),
        )
    elif resume.action == "terminal":
        title = "Review continuation is inconsistent"
        summary = (
            "Recorded review repair history cannot be resumed safely: "
            f"{resume.blocker or 'unknown blocker'}."
        )
        severity = "error"
        evidence.append(
            RecoveryEvidenceRow(
                label="Continuation blocker",
                value=resume.blocker,
                severity="error",
            )
        )
        actions = (_inspect_action("Inspect review events and generated sessions."),)
    else:
        title = "Review continuation ready"
        if resume.action == "materialize-repairs":
            summary = (
                "Rerun the plan to materialize repairs from review session "
                f"{resume.review_session_id}."
            )
        elif resume.action == "run-repairs":
            summary = "Rerun the plan to execute pending review repairs."
        else:
            summary = "Rerun the plan to review the completed repair sessions again."
        summary += (
            " Configure a review harness distinct from the implementation "
            "harness before launching; without --review-harness, review "
            "continuation will not run."
        )
        cap_exceeds_default = (
            resume.action == "materialize-repairs"
            and resume.minimum_required_cap > run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES
        )
        exhausted_group = (
            review_continuation.exhausted_repair_group_details(resume.to_payload())
            if cap_exceeds_default
            else None
        )
        if exhausted_group is not None:
            repair_group, attempts = exhausted_group
            summary += (
                f' The recurring repair group "{repair_group}" already hit '
                f"{attempts} {'attempt' if attempts == 1 else 'attempts'}; "
                "fix it manually or re-split instead of raising "
                "--max-review-fix-cycles."
            )
        elif cap_exceeds_default:
            summary += (
                " Set --max-review-fix-cycles to "
                f"{resume.minimum_required_cap} or higher as well."
            )
        severity = "info"
        actions = (
            (
                _inspect_action(
                    "Inspect the recurring repair group and repair it "
                    "manually or re-split before relaunching."
                ),
            )
            if exhausted_group is not None
            else (_profile_action(profile_guidance(include_cap=cap_exceeds_default)),)
        )

    return _build_context_recovery_payload(
        context,
        kind="review-continuation",
        title=title,
        summary=summary,
        severity=severity,
        recoverable=True,
        artifacts=_review_resume_artifacts(context),
        evidence=tuple(evidence),
        actions=actions,
    )


def build_workbench_recovery(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchRecovery:
    """Build a deterministic, read-only recovery diagnosis for one plan.

    ``reclaim=False`` suppresses the stale-lock hygiene the shared lock reader
    performs by default, so an observational caller leaves the corpus untouched.
    """
    if not plan.exists:
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=f"Plan directory does not exist: {plan.plan_dir}",
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan directory",
                    value=str(plan.plan_dir),
                    severity="error",
                ),
            ),
        )

    try:
        snapshot = plans.inspect_plan(plan.plan_dir)
    except plans.PlanError as error:
        state_path, events_path, progress_path = _summary_base_paths(
            plan.plan_dir, state_dir
        )
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=str(error),
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan error",
                    value=str(error),
                    severity="error",
                ),
            ),
            artifacts=_recovery_base_artifacts(
                state_path=state_path,
                events_path=events_path,
                progress_path=progress_path,
            ),
        )

    try:
        context = _build_recovery_context(snapshot, state_dir, reclaim=reclaim)
    except (plans.PlanError, OSError, ValueError) as error:
        state_path, events_path, progress_path = _summary_base_paths(
            snapshot.plan_dir, state_dir
        )
        return WorkbenchRecovery(
            plan_dir=snapshot.plan_dir,
            kind="generic",
            title="Workspace metadata cannot be inspected",
            summary=str(error),
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Workspace metadata error",
                    value=str(error),
                    severity="error",
                ),
            ),
            artifacts=_recovery_base_artifacts(
                state_path=state_path,
                events_path=events_path,
                progress_path=progress_path,
            ),
        )
    expired_wait_context = _expired_wait_qna_context(
        snapshot,
        context.runner_state,
        state_dir,
        reclaim=reclaim,
    )
    if expired_wait_context is not None:
        context = replace(
            context,
            pause_reason=expired_wait_context.pause_reason,
            pause_details=expired_wait_context.pause_details,
            pause_message=(
                expired_wait_context.pause_message or qna.EXPIRED_WAIT_PAUSE_REASON
            ),
        )

    active_turn = _payload_mapping(context.raw_state, "active_turn")
    active_verification = _payload_mapping(context.raw_state, "active_verification")
    lock_info = context.lock_info
    if (
        lock_info is not None
        and lock_info.active
        and (active_turn is not None or active_verification is not None)
    ):
        return _build_active_run_recovery(context, selected_turn=active_turn)

    reason = context.pause_reason
    details = context.pause_details
    recovery_kind = _pause_reason_recovery_kind(reason)

    if expired_wait_context is not None:
        return _build_reply_needed_recovery(context, reason=reason)

    if context.runner_state.get("paused") is True:
        if reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR:
            continuation = _build_review_continuation_recovery(context)
            if continuation is not None:
                return continuation

        if recovery_kind == "not-loop-ready":
            return _build_not_loop_ready_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(context, reason=recovery_kind)

        if recovery_kind == "commit-preflight":
            return _build_commit_preflight_recovery(context)

        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if recovery_kind == "conversation-recovery":
            return _build_conversation_recovery(
                context, reason=reason, state_dir=state_dir
            )

        if _is_harness_config_recovery(reason=reason, details=details):
            return _build_harness_config_recovery(context, reason=reason)

        if recovery_kind == "generic":
            return _build_failed_finished_recovery(context, reason=reason)

        return _build_reply_needed_recovery(context, reason=reason)

    if context.stale_unfinished:
        return _build_stale_state_recovery(context)

    if lock_info is not None and lock_info.stale:
        return _build_stale_lock_recovery(context)

    if (
        context.runner_state.get("present")
        and context.runner_state.get("run_finished_at")
        and context.runner_state.get("ok") is False
    ):
        if reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR:
            continuation = _build_review_continuation_recovery(context)
            if continuation is not None:
                return continuation

        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(context, reason=recovery_kind)

        return _build_failed_finished_recovery(context, reason=reason)

    continuation = _build_review_continuation_recovery(context)
    if continuation is not None:
        return continuation
    return _build_clean_none_recovery(context)

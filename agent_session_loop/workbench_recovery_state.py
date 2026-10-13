"""Read-only readers over a plan's runner-state payload for the recovery cards."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_models,
    plans_progress,
    qna,
    review_continuation,
    state,
    state_abandonment,
    state_locks,
    state_payloads,
    workbench_workspace,
)
from .workbench_recovery_models import (
    GENERIC_RECOVERY_PAUSE_REASONS,
    PAUSE_REASON_RECOVERY_KIND_BY_REASON,
    RecoveryArtifactReference,
    RecoveryKind,
    RecoveryWarning,
    _RecoveryBuildContext,
)


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
        plans_progress.progress_path(plan_dir),
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
    snapshot: plans_models.PlanSnapshot,
    state_dir: str | Path | None,
    *,
    reclaim: bool = True,
) -> _RecoveryBuildContext:
    state_path, events_path, progress_path = _summary_base_paths(
        snapshot.plan_dir, state_dir
    )
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state_locks._read_lock_path(
        lock_path, snapshot.plan_dir, reclaim=reclaim
    )
    runner_state = state_payloads._runner_state_payload(
        state_path,
        lock_info=lock_info,
    )
    raw_state, warnings, warning_details = _read_recovery_state_payload(state_path)
    workspace = workbench_workspace._workspace_projection(
        snapshot.plan_dir,
        runtime=raw_state if raw_state.get("workspace_root") else runner_state,
    )
    stale_unfinished, stale_reason = state_abandonment._stale_runner_state(
        runner_state, lock_info
    )
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
    snapshot: plans_models.PlanSnapshot,
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
        != state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED
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

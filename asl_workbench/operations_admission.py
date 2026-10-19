"""Focused portion of asl_workbench/operations.py."""

from .operations_records import *


@dataclass(frozen=True)
class Admission:
    """What may happen to one submitted mutation, and why.

    Exactly three outcomes, and the middle one is the reason this type exists:
    a *replay* is not a second attempt that happened to be deduplicated, it is
    the first attempt's own record handed back. A caller that treats
    :attr:`admitted` and :attr:`replayed` alike will start a second run.
    """

    decision: str
    operation_id: str
    method: str
    scope: str
    fingerprint: str
    record: dict[str, Any] | None = None
    reason: str | None = None
    conflict: str | None = None
    message: str = ""

    @property
    def admitted(self) -> bool:
        return self.decision == DECISION_ADMIT

    @property
    def replayed(self) -> bool:
        return self.decision == DECISION_REPLAY

    @property
    def refused(self) -> bool:
        return self.decision == DECISION_REFUSE

    def error(self) -> OperationError:
        """The refusal this admission carries, as the exception to raise."""
        if not self.refused or self.reason is None:
            raise OperationError(
                REASON_INVALID_STATUS,
                "admission was not a refusal",
                details={"decision": self.decision},
            )
        details: dict[str, Any] = {
            "operation_id": self.operation_id,
            "method": self.method,
            "scope": self.scope,
        }
        if self.conflict is not None:
            details["conflict"] = self.conflict
        if self.record is not None:
            details["operation"] = export_operation(self.record)
        return OperationError(self.reason, self.message, details=details)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision": self.decision,
            "operation_id": self.operation_id,
            "method": self.method,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
        }
        if self.record is not None:
            payload["operation"] = export_operation(self.record)
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.conflict is not None:
            payload["conflict"] = self.conflict
        if self.message:
            payload["message"] = self.message
        return payload


def conflicting_reuse(
    recorded: Mapping[str, Any], *, method: str, target: OperationTarget
) -> str | None:
    """Name what a resubmission changed about an operation already recorded.

    The comparison is on identity, never on words: the fingerprint names the
    action and its target together, and the method is compared separately only
    so the answer can say which half disagrees. Returns ``None`` when the
    resubmission *is* the recorded operation.
    """
    resolved = require_method(method)
    if str(recorded.get("method", "")) != resolved:
        return CONFLICT_METHOD
    if str(recorded.get("fingerprint", "")) != operation_fingerprint(resolved, target):
        return CONFLICT_TARGET
    return None


def admit(
    records: Sequence[Mapping[str, Any]],
    *,
    operation_id: str,
    method: str,
    target: OperationTarget,
    now: float,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_key: str | None = None,
    question_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> Admission:
    """Decide what happens to one submitted mutation, given what is recorded.

    Pure, and deliberately so: the caller holds the cross-process lock, reads
    the journal under it, calls this, and writes the result under the same
    lock. Deciding outside that lock would validate against a view another
    producer has already invalidated, which is the whole failure idempotent
    submission has to survive.
    """
    resolved = require_method(method)
    identifier = require_operation_id(operation_id)
    scope = method_scope(resolved)
    fingerprint = operation_fingerprint(resolved, target)

    for recorded in records:
        if str(recorded.get("operation_id", "")) != identifier:
            continue
        conflict = conflicting_reuse(recorded, method=resolved, target=target)
        if conflict is None:
            return Admission(
                decision=DECISION_REPLAY,
                operation_id=identifier,
                method=resolved,
                scope=scope,
                fingerprint=fingerprint,
                record=dict(recorded),
            )
        return Admission(
            decision=DECISION_REFUSE,
            operation_id=identifier,
            method=resolved,
            scope=scope,
            fingerprint=fingerprint,
            record=dict(recorded),
            reason=REASON_OPERATION_CONFLICT,
            conflict=conflict,
            message=(
                f"operation {identifier!r} already names a different "
                f"{conflict}; it cannot be reused for this request"
            ),
        )

    for recorded in records:
        status = str(recorded.get("status", ""))
        if str(recorded.get("scope", "")) != scope or not outstanding(status):
            continue
        if status == OPERATION_UNCONFIRMED:
            return Admission(
                decision=DECISION_REFUSE,
                operation_id=identifier,
                method=resolved,
                scope=scope,
                fingerprint=fingerprint,
                record=dict(recorded),
                reason=REASON_OPERATION_UNCERTAIN,
                message=(
                    "an earlier mutation on this plan has an outcome nobody "
                    "can establish; resolve it before starting another"
                ),
            )
        return Admission(
            decision=DECISION_REFUSE,
            operation_id=identifier,
            method=resolved,
            scope=scope,
            fingerprint=fingerprint,
            record=dict(recorded),
            reason=REASON_OPERATION_OUTSTANDING,
            message=(
                f"another {scope} operation on this plan is still "
                f"{status}: {recorded.get('operation_id')}"
            ),
        )

    return Admission(
        decision=DECISION_ADMIT,
        operation_id=identifier,
        method=resolved,
        scope=scope,
        fingerprint=fingerprint,
        record=new_record(
            operation_id=identifier,
            method=resolved,
            target=target,
            now=now,
            deliver_by=deliver_by,
            command_id=command_id,
            run_key=run_key,
            question_id=question_id,
            pid=pid,
            hostname=hostname,
        ),
    )


# -- Reconciliation ---------------------------------------------------------


__all__ = ["hashlib", "json", "Mapping", "Sequence", "dataclass", "Any", "METHOD_LAUNCH", "METHOD_REPLY", "METHOD_QUESTION_REPLY", "METHOD_STOP_AFTER_CURRENT", "METHOD_ABORT", "METHOD_SELECT_HARNESS", "METHOD_REDIRECT", "METHOD_UNLOCK", "METHOD_PLAN_ARCHIVE", "METHOD_ARCHIVE_RESTORE", "METHOD_ARCHIVE_DELETE", "METHOD_ARCHIVE_SETTINGS", "OPERATION_METHODS", "UNSCOPED_MUTATIONS", "REMOTE_METHOD_ALIASES", "RUN_STARTING_METHODS", "SCOPE_RUN_START", "REPLY_BEARING_METHODS", "MAX_OPERATION_ID_CHARS", "OPERATION_PENDING", "OPERATION_DISPATCHED", "OPERATION_CANCELLED", "OPERATION_ACCEPTED", "OPERATION_CONSUMED", "OPERATION_REJECTED", "OPERATION_EXPIRED", "OPERATION_SUPERSEDED", "OPERATION_UNCONFIRMED", "ASL_SETTLED_STATUSES", "OPERATION_STATUSES", "OPERATION_OUTSTANDING_STATUSES", "OPERATION_TERMINAL_STATUSES", "OPERATION_TRANSITIONS", "DETAIL_DEADLINE_PASSED", "DETAIL_OWNER_CANCELLED", "DETAIL_CONNECTOR_RESTARTED", "DETAIL_NO_RESULT", "DETAIL_BRIDGE_UNAVAILABLE", "DETAIL_ACTION_TOKEN_REFUSED", "DETAIL_RECONCILED", "DETAIL_LAUNCH_REFUSED", "DETAIL_REPLACED_UNCERTAIN", "DETAIL_REPLY_WITNESSED", "OPERATION_DETAIL_CODES", "OPERATION_PERSIST_FIELDS", "OPERATION_EXPORT_FIELDS", "FORBIDDEN_PERSISTED_KEYS", "REASON_INVALID_METHOD", "REASON_INVALID_OPERATION_ID", "REASON_INVALID_STATUS", "REASON_INVALID_TRANSITION", "REASON_BODY_LEAK", "REASON_UNKNOWN_OPERATION", "REASON_OPERATION_CONFLICT", "REASON_OPERATION_OUTSTANDING", "REASON_OPERATION_UNCERTAIN", "_STATUS_BY_REASON", "CONFLICT_METHOD", "CONFLICT_TARGET", "OperationError", "OperationTarget", "operation_fingerprint", "require_method", "require_operation_id", "method_scope", "next_status", "attemptable", "outstanding", "terminal", "assert_body_free", "persist_operation", "export_operation", "_project", "new_record", "advance_record", "DECISION_ADMIT", "DECISION_REPLAY", "DECISION_REFUSE", "Admission", "conflicting_reuse", "admit"]  # fmt: skip

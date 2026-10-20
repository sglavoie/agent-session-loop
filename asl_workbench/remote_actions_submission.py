"""Reading one ``operation.submit`` into a command the lane can hold.

Validation only: which method, which target, which authority, which window.
Anything that fails raises ``RemoteActionError`` carrying the receipt to send
back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .operations import (
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    OperationError,
    require_method,
    require_operation_id,
)
from .remote_actions_models import (
    BODY_BEARING_METHODS,
    DELIVERABLE_METHODS,
    FRESHNESS_PROVEN_METHODS,
    MAX_DELIVERY_WINDOW_SECONDS,
    MutationRequest,
    RemoteActionError,
)
from .transport import (
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNAUTHORIZED,
    ERROR_WRONG_TARGET,
    MAX_IDENTIFIER_CHARS,
    RECEIPT_EXPIRED,
    REQUEST_MUTATE,
)


def _optional_identifier(payload: Mapping[str, Any], name: str) -> str | None:
    """Read an optional identifier field, or refuse a malformed one."""
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, f"{name} must be a non-empty string"
        )
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD,
            f"{name} exceeds {MAX_IDENTIFIER_CHARS} characters",
        )
    return value


def parse_submission(
    payload: Mapping[str, Any],
    *,
    now: float,
    deadline: float | None = None,
    window: float = MAX_DELIVERY_WINDOW_SECONDS,
) -> MutationRequest:
    """Read one ``operation.submit`` into a command this lane can hold.

    The envelope has already been validated for shape by the transport; what is
    decided here is whether *this* connector will carry *this* method, with the
    authority attached to it, inside a window it is willing to honour.
    """
    if payload.get("request") != REQUEST_MUTATE:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "this lane carries operation.submit only"
        )

    raw_method = payload.get("method")
    if not isinstance(raw_method, str):
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, "a submission needs a method")
    try:
        method = require_method(raw_method)
    except OperationError as error:
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, str(error)) from error
    if method not in DELIVERABLE_METHODS:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, f"this connector does not deliver {method}"
        )

    raw_id = payload.get("operation_id")
    if not isinstance(raw_id, str):
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "a submission needs an operation_id"
        )
    try:
        operation_id = require_operation_id(raw_id)
    except OperationError as error:
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, str(error)) from error

    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise RemoteActionError(ERROR_WRONG_TARGET, "a submission needs a target")
    archive_id = _optional_identifier(target, "archive_id")
    archive = _optional_identifier(target, "archive")
    plan_key = _optional_identifier(target, "plan_key")
    if method in (METHOD_ARCHIVE_RESTORE, METHOD_ARCHIVE_DELETE):
        if archive_id is None or plan_key is not None or archive is not None:
            raise RemoteActionError(
                ERROR_WRONG_TARGET, "archive action needs one archive_id"
            )
        plan_key = "archive:" + archive_id
    elif method == METHOD_ARCHIVE_SETTINGS:
        if archive != "settings" or plan_key is not None or archive_id is not None:
            raise RemoteActionError(
                ERROR_WRONG_TARGET, "archive settings needs archive=settings"
            )
        plan_key = "archive:settings"
    elif plan_key is None or archive_id is not None or archive is not None:
        raise RemoteActionError(ERROR_WRONG_TARGET, "plan action needs one plan_key")
    retention_days = payload.get("archive_retention_days")
    if method == METHOD_ARCHIVE_SETTINGS:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise RemoteActionError(
                ERROR_FORBIDDEN_METHOD, "archive_retention_days must be positive"
            )
    elif retention_days is not None:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "retention is only for archive settings"
        )

    action_token = _optional_identifier(payload, "action_token") or ""
    reply_ref = _optional_identifier(payload, "reply_ref")
    bridge_token = _optional_identifier(payload, "bridge_token") or ""
    harness = _optional_identifier(payload, "harness")

    # A method that needs prose and arrived without a way to fetch it would be
    # carried out against an empty reply, which is not the mutation that was
    # submitted. Refuse it while the service can still resubmit.
    if method in BODY_BEARING_METHODS and (not reply_ref or not action_token):
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD,
            f"{method} needs a reply_ref and the token that redeems it",
        )
    # Acting on a live run without the proof ASL handed out with the state the
    # caller read is acting on a guess about what that run is doing now.
    if method in FRESHNESS_PROVEN_METHODS and not bridge_token:
        raise RemoteActionError(
            ERROR_UNAUTHORIZED, f"{method} needs the freshness token ASL issued"
        )

    if window <= 0:
        raise RemoteActionError(
            ERROR_DEADLINE, "this connector holds no command", outcome=RECEIPT_EXPIRED
        )
    deliver_by = now + min(window, MAX_DELIVERY_WINDOW_SECONDS)
    if deadline is not None:
        deliver_by = min(deliver_by, deadline)
    if deliver_by <= now:
        raise RemoteActionError(
            ERROR_DEADLINE,
            "the delivery window had already closed on arrival",
            outcome=RECEIPT_EXPIRED,
        )

    reason = payload.get("reason")
    return MutationRequest(
        operation_id=operation_id,
        method=method,
        plan_key=plan_key,
        requested_at=now,
        deliver_by=deliver_by,
        run_key=_optional_identifier(target, "run_key"),
        run_id=_optional_identifier(target, "run_id"),
        question_id=_optional_identifier(target, "question_id"),
        session_id=_optional_identifier(target, "session_id"),
        harness=harness,
        reason=reason if isinstance(reason, str) and reason.strip() else None,
        archive_id=archive_id,
        archive=archive,
        archive_retention_days=retention_days,
        reply_ref=reply_ref,
        action_token=action_token,
        bridge_token=bridge_token,
    )

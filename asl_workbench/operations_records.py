"""Focused portion of asl_workbench/operations.py."""

from .operations_vocab import *


class OperationError(Exception):
    """A mutation refused before anything was attempted.

    Carries the status an HTTP surface answers with, so a refusal decided in
    this module is the refusal a client sees whether it reached the local
    server or the remote connector.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = (
            status if status is not None else _STATUS_BY_REASON.get(reason, 400)
        )
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason, "error": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


# -- Identity ---------------------------------------------------------------


@dataclass(frozen=True)
class OperationTarget:
    """What a mutation acts on, as identifiers rather than paths or prose.

    ``plan_key`` is the digest :func:`asl_workbench.catalog.plan_key` mints, so
    a target names a plan the same way a published catalog entry does.
    ``selector`` narrows it where a method needs narrowing -- the question id a
    reply answers, the harness a selection picks, the turn a correction aims
    at -- and is empty for a method that acts on the plan as a whole.

    It is part of the fingerprint, so an operation id reused against a
    different question or a different turn is a conflict rather than a replay.
    """

    plan_key: str
    selector: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not self.plan_key:
            raise OperationError(
                REASON_INVALID_OPERATION_ID, "an operation target needs a plan_key"
            )

    def to_payload(self) -> dict[str, Any]:
        return {"plan_key": self.plan_key, "selector": self.selector}


def operation_fingerprint(method: str, target: OperationTarget) -> str:
    """The digest that binds an operation id to the action and its target.

    Short and stable: a canonical JSON rendering of the method and the target,
    digested. It names the request without carrying it, which is what lets a
    journal row refuse a conflicting reuse while holding nothing an operator
    typed.
    """
    canonical = json.dumps(
        {
            "method": require_method(method),
            "plan_key": target.plan_key,
            "selector": target.selector,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def require_method(method: str) -> str:
    """Return ``method``, resolving a remote alias, or refuse an unknown one."""
    resolved = REMOTE_METHOD_ALIASES.get(method, method)
    if resolved not in OPERATION_METHODS:
        raise OperationError(
            REASON_INVALID_METHOD,
            f"unknown operation method: {method!r}",
            details={"method": method},
        )
    return resolved


def require_operation_id(operation_id: str) -> str:
    """Return ``operation_id`` when it is a bounded, non-empty identifier."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise OperationError(
            REASON_INVALID_OPERATION_ID, "operation_id must be a non-empty string"
        )
    if len(operation_id) > MAX_OPERATION_ID_CHARS:
        raise OperationError(
            REASON_INVALID_OPERATION_ID,
            f"operation_id exceeds {MAX_OPERATION_ID_CHARS} characters",
            details={"length": len(operation_id)},
        )
    if any(character.isspace() for character in operation_id):
        raise OperationError(
            REASON_INVALID_OPERATION_ID, "operation_id must not contain whitespace"
        )
    return operation_id


def method_scope(method: str) -> str:
    """The resource this method contends for.

    The two spawning methods share :data:`SCOPE_RUN_START`; every other method
    is its own scope, so a queued stop does not refuse an abort while still
    refusing a second identical stop.
    """
    resolved = require_method(method)
    return SCOPE_RUN_START if resolved in RUN_STARTING_METHODS else resolved


# -- Transitions ------------------------------------------------------------


def next_status(current: str, proposed: str) -> str:
    """Return ``proposed`` when the transition is legal, otherwise refuse it."""
    if current not in OPERATION_STATUSES:
        raise OperationError(REASON_INVALID_STATUS, f"unknown status: {current!r}")
    if proposed not in OPERATION_STATUSES:
        raise OperationError(REASON_INVALID_STATUS, f"unknown status: {proposed!r}")
    if proposed not in OPERATION_TRANSITIONS[current]:
        raise OperationError(
            REASON_INVALID_TRANSITION,
            f"{current} cannot become {proposed}",
            details={"status": current, "proposed": proposed},
        )
    return proposed


def attemptable(status: str) -> bool:
    """Whether the mutation behind this row may be carried out.

    Exactly one state qualifies, and it is the one that has never been tried.
    There is no state from which an attempt may be *repeated*: on this side of
    the wire the mutation is local, so once dispatch has been recorded nothing
    can establish that it did not happen, and a second attempt would be the
    repeat this whole module exists to prevent.
    """
    return status == OPERATION_PENDING


def outstanding(status: str) -> bool:
    """Whether this state still blocks a second mutation in the same scope."""
    return status in OPERATION_OUTSTANDING_STATUSES


def terminal(status: str) -> bool:
    """Whether this state is closed to further evidence."""
    return status in OPERATION_TERMINAL_STATUSES


# -- Projection -------------------------------------------------------------


def assert_body_free(
    document: Any,
    *,
    where: str = "operation record",
    allow: frozenset[str] = frozenset(),
) -> None:
    """Refuse any nested key that could carry question or reply prose.

    ``allow`` exists for the one frame whose whole purpose is a body. Every
    other caller passes nothing, so the check is a property of the document
    rather than of the caller's intentions.
    """
    pending: list[Any] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            leaked = sorted((FORBIDDEN_PERSISTED_KEYS - allow) & set(node))
            if leaked:
                raise OperationError(
                    REASON_BODY_LEAK,
                    f"{where} carries body fields: {', '.join(leaked)}",
                    details={"fields": leaked},
                )
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def persist_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one operation onto the fields a journal is allowed to store."""
    return _project(record, OPERATION_PERSIST_FIELDS, "persisted operation")


def export_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one operation onto the fields a cockpit may read back."""
    projected = _project(record, OPERATION_EXPORT_FIELDS, "exported operation")
    if projected.get("method") in (
        METHOD_LAUNCH,
        METHOD_PLAN_ARCHIVE,
        METHOD_ARCHIVE_RESTORE,
        METHOD_ARCHIVE_DELETE,
        METHOD_ARCHIVE_SETTINGS,
    ):
        # The remote service's legacy journal requires a non-null run_key. For
        # these plan-scoped operations it stores the real plan key there; that
        # storage adapter must never be presented as a run identity.
        projected.pop("run_key", None)
    return projected


def _project(
    record: Mapping[str, Any], allowed: frozenset[str], where: str
) -> dict[str, Any]:
    projected = {key: value for key, value in record.items() if key in allowed}
    # The allowlist already removed every declared body field. This catches the
    # undeclared ones -- a nested object smuggling prose under an allowed key.
    assert_body_free(projected, where=where)
    return projected


def new_record(
    *,
    operation_id: str,
    method: str,
    target: OperationTarget,
    now: float,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_key: str | None = None,
    run_id: str | None = None,
    question_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Build the ``pending`` row for one freshly admitted mutation.

    ``pending`` rather than ``dispatched``: admission and attempt are separate
    steps so that the window between them -- the interruption that leaves a
    record of something that never happened -- is a window a test can stand
    inside and an operator can be told about precisely.
    """
    resolved = require_method(method)
    return persist_operation(
        {
            "operation_id": require_operation_id(operation_id),
            "fingerprint": operation_fingerprint(resolved, target),
            "method": resolved,
            "scope": method_scope(resolved),
            "status": OPERATION_PENDING,
            "plan_key": target.plan_key,
            "run_key": run_key,
            "run_id": run_id,
            "question_id": question_id,
            "command_id": command_id,
            "requested_at": now,
            "deliver_by": deliver_by,
            "dispatched_at": None,
            "settled_at": None,
            "detail_code": None,
            "pid": pid,
            "hostname": hostname,
        }
    )


def advance_record(
    record: Mapping[str, Any],
    status: str,
    *,
    now: float,
    detail_code: str | None = None,
    run_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Return ``record`` moved to ``status``, or refuse an illegal move.

    ``dispatched`` stamps ``dispatched_at`` and every settled status stamps
    ``settled_at``; ``unconfirmed`` does neither on purpose, because it is not
    settled -- it is the absence of an answer, still open to late evidence.
    """
    current = str(record.get("status", ""))
    updated = dict(record)
    if current != status:
        updated["status"] = next_status(current, status)
    if status == OPERATION_DISPATCHED:
        updated["dispatched_at"] = now
    if status in ASL_SETTLED_STATUSES or status == OPERATION_CANCELLED:
        updated["settled_at"] = now
    if detail_code is not None:
        updated["detail_code"] = detail_code
    if run_id is not None:
        updated["run_id"] = run_id
    if pid is not None:
        updated["pid"] = pid
    if hostname is not None:
        updated["hostname"] = hostname
    return persist_operation(updated)


# -- Admission --------------------------------------------------------------

DECISION_ADMIT = "admit"
DECISION_REPLAY = "replay"
DECISION_REFUSE = "refuse"


__all__ = ["hashlib", "json", "Mapping", "Sequence", "dataclass", "Any", "METHOD_LAUNCH", "METHOD_REPLY", "METHOD_QUESTION_REPLY", "METHOD_STOP_AFTER_CURRENT", "METHOD_ABORT", "METHOD_SELECT_HARNESS", "METHOD_REDIRECT", "METHOD_UNLOCK", "METHOD_PLAN_ARCHIVE", "METHOD_ARCHIVE_RESTORE", "METHOD_ARCHIVE_DELETE", "METHOD_ARCHIVE_SETTINGS", "OPERATION_METHODS", "UNSCOPED_MUTATIONS", "REMOTE_METHOD_ALIASES", "RUN_STARTING_METHODS", "SCOPE_RUN_START", "REPLY_BEARING_METHODS", "MAX_OPERATION_ID_CHARS", "OPERATION_PENDING", "OPERATION_DISPATCHED", "OPERATION_CANCELLED", "OPERATION_ACCEPTED", "OPERATION_CONSUMED", "OPERATION_REJECTED", "OPERATION_EXPIRED", "OPERATION_SUPERSEDED", "OPERATION_UNCONFIRMED", "ASL_SETTLED_STATUSES", "OPERATION_STATUSES", "OPERATION_OUTSTANDING_STATUSES", "OPERATION_TERMINAL_STATUSES", "OPERATION_TRANSITIONS", "DETAIL_DEADLINE_PASSED", "DETAIL_OWNER_CANCELLED", "DETAIL_CONNECTOR_RESTARTED", "DETAIL_NO_RESULT", "DETAIL_BRIDGE_UNAVAILABLE", "DETAIL_ACTION_TOKEN_REFUSED", "DETAIL_RECONCILED", "DETAIL_LAUNCH_REFUSED", "DETAIL_REPLACED_UNCERTAIN", "DETAIL_REPLY_WITNESSED", "OPERATION_DETAIL_CODES", "OPERATION_PERSIST_FIELDS", "OPERATION_EXPORT_FIELDS", "FORBIDDEN_PERSISTED_KEYS", "REASON_INVALID_METHOD", "REASON_INVALID_OPERATION_ID", "REASON_INVALID_STATUS", "REASON_INVALID_TRANSITION", "REASON_BODY_LEAK", "REASON_UNKNOWN_OPERATION", "REASON_OPERATION_CONFLICT", "REASON_OPERATION_OUTSTANDING", "REASON_OPERATION_UNCERTAIN", "_STATUS_BY_REASON", "CONFLICT_METHOD", "CONFLICT_TARGET", "OperationError", "OperationTarget", "operation_fingerprint", "require_method", "require_operation_id", "method_scope", "next_status", "attemptable", "outstanding", "terminal", "assert_body_free", "persist_operation", "export_operation", "_project", "new_record", "advance_record", "DECISION_ADMIT", "DECISION_REPLAY", "DECISION_REFUSE"]  # fmt: skip

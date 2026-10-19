"""Focused portion of asl_workbench/operations.py."""

from .operations_admission import *


@dataclass(frozen=True)
class Evidence:
    """What the authorities say about one operation's target, right now.

    Assembled by the provider from ASL's own records -- the plan lock, the run
    state, a receipt a dispatcher returned -- because ASL is the authority on
    what actually happened and this module is not. Every field defaults to the
    weakest claim, so evidence nobody gathered can only ever produce an
    uncertain answer rather than a confident wrong one.
    """

    owner_alive: bool = False
    run_active: bool = False
    run_id: str | None = None
    receipt_status: str | None = None
    receipt_detail_code: str | None = None
    #: A run recorded reading the reply this operation carried. Gathered from
    #: the plan's own event log by the provider, because that log is the only
    #: place the claim exists -- the plan lock says a run is there, and a run
    #: being there is not the same as an operator's words having been read.
    reply_consumed: bool = False
    #: Which run read it. A resumed run is a *new* process with a new id, so
    #: this is reported rather than matched against the id the operation named:
    #: requiring a match would refuse every successful resume.
    consuming_run_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "owner_alive": self.owner_alive,
            "run_active": self.run_active,
            "run_id": self.run_id,
            "receipt_status": self.receipt_status,
            "receipt_detail_code": self.receipt_detail_code,
            "reply_consumed": self.reply_consumed,
            "consuming_run_id": self.consuming_run_id,
        }


NO_EVIDENCE = Evidence()


@dataclass(frozen=True)
class Reconciliation:
    """The status an open operation should be moved to, and why."""

    status: str
    detail_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"status": self.status, "detail_code": self.detail_code}


def reconcile(
    record: Mapping[str, Any],
    evidence: Evidence = NO_EVIDENCE,
    *,
    now: float,
    superseding: bool = False,
) -> Reconciliation | None:
    """What to do about one open operation, or ``None`` to leave it alone.

    One step at a time, so every move is a legal transition on its own and a
    caller that stops after the first one has still written something true. A
    caller that wants the settled end state applies this until it returns
    ``None``.

    The rules, in the order they are applied:

    * A receipt wins. ASL is the authority on what happened, so a status it
      settled is carried through unchanged when the transition is legal.
    * An operation whose owning process is still alive is left alone. That
      process is mid-attempt, and settling under it would publish an answer
      about work still in progress. A *witnessed* reply is the one exception,
      because it is not an inference about the owner's progress: the run
      recorded reading the operator's words, which is the whole of what the
      operation promised, and the owner is in no position to contradict it.
      Without the exception a long-lived surface -- a Workbench server that
      answers a question and then goes back to serving pages -- would leave
      every reply it delivered sitting at ``accepted`` for its own lifetime.
    * ``pending`` from a dead owner settles as ``expired``: intent was recorded
      and nothing was attempted, so the mutation demonstrably did not run.
      This is the good case, and it is why dispatch is a separate write.
    * ``dispatched`` from a dead owner becomes ``accepted`` only when the run
      it names is *there to be seen* -- the recorded run id matching the run
      ASL currently holds the plan for. Every other shape settles as
      ``unconfirmed``: a mutation may have run and nobody can say, which is the
      honest answer and never a reason to try again.
    * ``accepted`` is ``consumed`` on the same positive evidence, and
      ``unconfirmed`` when the run it names is gone. Success is inferred from
      presence, never from absence: a run that has vanished may have finished,
      crashed, or been killed, and only its own records tell those apart.
    * A :data:`REPLY_BEARING_METHODS` operation is the exception to the two
      rules above, and in the strict direction. What it promised is that an
      operator's words were read, so a run being *there* does not settle it --
      only :attr:`Evidence.reply_consumed`, which is a run's own record of
      taking that reply into a turn, does. That evidence also resolves an
      earlier ``unconfirmed`` result: uncertainty is deliberately open to a
      late receipt. A resumed run is a new process with a new id, so the
      presence test those rules use cannot apply to it anyway: it would call
      every successful resume somebody else's run.

    ``superseding`` is what a *new* submission passes, and it does one thing:
    an ``unconfirmed`` operation whose target is demonstrably idle stops
    holding its scope, as ``superseded``. That is not a finding about whether
    it ran -- nothing can produce one -- it is the record that an operator
    submitted something in its place. Without it an interrupted process would
    make a plan permanently unmutatable, which would turn "never repeat
    automatically" into "never act again"; with it, the only thing that can
    start a second mutation is a person deciding to, which is the line this
    module actually draws.
    """
    status = str(record.get("status", ""))
    if not outstanding(status):
        return None

    receipt = evidence.receipt_status
    if receipt is not None and receipt in OPERATION_TRANSITIONS.get(
        status, frozenset()
    ):
        return Reconciliation(
            receipt, evidence.receipt_detail_code or DETAIL_RECONCILED
        )

    deliver_by = record.get("deliver_by")
    overdue = isinstance(deliver_by, int | float) and now >= float(deliver_by)
    recorded_run = record.get("run_id")
    bears_reply = str(record.get("method", "")) in REPLY_BEARING_METHODS
    #: Only a row that was actually dispatched can have been read, so a
    #: ``pending`` row is never witnessed however loud the evidence is.
    witnessed = (
        bears_reply
        and evidence.reply_consumed
        and status in (OPERATION_DISPATCHED, OPERATION_ACCEPTED, OPERATION_UNCONFIRMED)
    )

    if evidence.owner_alive and not witnessed:
        return None

    identified = (
        not bears_reply
        and evidence.run_active
        and evidence.run_id is not None
        and recorded_run == evidence.run_id
    )

    if status == OPERATION_PENDING:
        return Reconciliation(
            OPERATION_EXPIRED,
            DETAIL_DEADLINE_PASSED if overdue else DETAIL_CONNECTOR_RESTARTED,
        )

    if status == OPERATION_DISPATCHED:
        if witnessed:
            return Reconciliation(OPERATION_ACCEPTED, DETAIL_REPLY_WITNESSED)
        if identified:
            return Reconciliation(OPERATION_ACCEPTED, DETAIL_RECONCILED)
        if bears_reply and evidence.run_active and not overdue:
            # Something is running on this plan and has not said it read the
            # reply. It may be about to, so waiting is what a look this early
            # is for, and the deadline is what ends the wait.
            return None
        return Reconciliation(
            OPERATION_UNCONFIRMED,
            DETAIL_NO_RESULT if overdue else DETAIL_CONNECTOR_RESTARTED,
        )

    if status == OPERATION_ACCEPTED:
        if witnessed:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_REPLY_WITNESSED)
        if identified:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_RECONCILED)
        if bears_reply and evidence.run_active and not overdue:
            return None
        if evidence.run_active and recorded_run is None:
            # Something holds the plan and this row cannot say whether it is
            # the thing it started. Waiting costs nothing; guessing costs the
            # only guarantee here worth having.
            return None
        return Reconciliation(OPERATION_UNCONFIRMED, DETAIL_NO_RESULT)

    if status == OPERATION_UNCONFIRMED:
        if witnessed:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_REPLY_WITNESSED)
        if superseding and not evidence.run_active:
            return Reconciliation(OPERATION_SUPERSEDED, DETAIL_REPLACED_UNCERTAIN)

    # `unconfirmed` with no receipt and nothing replacing it: this is already
    # the most that can be said.
    return None


__all__ = ["hashlib", "json", "Mapping", "Sequence", "dataclass", "Any", "METHOD_LAUNCH", "METHOD_REPLY", "METHOD_QUESTION_REPLY", "METHOD_STOP_AFTER_CURRENT", "METHOD_ABORT", "METHOD_SELECT_HARNESS", "METHOD_REDIRECT", "METHOD_UNLOCK", "METHOD_PLAN_ARCHIVE", "METHOD_ARCHIVE_RESTORE", "METHOD_ARCHIVE_DELETE", "METHOD_ARCHIVE_SETTINGS", "OPERATION_METHODS", "UNSCOPED_MUTATIONS", "REMOTE_METHOD_ALIASES", "RUN_STARTING_METHODS", "SCOPE_RUN_START", "REPLY_BEARING_METHODS", "MAX_OPERATION_ID_CHARS", "OPERATION_PENDING", "OPERATION_DISPATCHED", "OPERATION_CANCELLED", "OPERATION_ACCEPTED", "OPERATION_CONSUMED", "OPERATION_REJECTED", "OPERATION_EXPIRED", "OPERATION_SUPERSEDED", "OPERATION_UNCONFIRMED", "ASL_SETTLED_STATUSES", "OPERATION_STATUSES", "OPERATION_OUTSTANDING_STATUSES", "OPERATION_TERMINAL_STATUSES", "OPERATION_TRANSITIONS", "DETAIL_DEADLINE_PASSED", "DETAIL_OWNER_CANCELLED", "DETAIL_CONNECTOR_RESTARTED", "DETAIL_NO_RESULT", "DETAIL_BRIDGE_UNAVAILABLE", "DETAIL_ACTION_TOKEN_REFUSED", "DETAIL_RECONCILED", "DETAIL_LAUNCH_REFUSED", "DETAIL_REPLACED_UNCERTAIN", "DETAIL_REPLY_WITNESSED", "OPERATION_DETAIL_CODES", "OPERATION_PERSIST_FIELDS", "OPERATION_EXPORT_FIELDS", "FORBIDDEN_PERSISTED_KEYS", "REASON_INVALID_METHOD", "REASON_INVALID_OPERATION_ID", "REASON_INVALID_STATUS", "REASON_INVALID_TRANSITION", "REASON_BODY_LEAK", "REASON_UNKNOWN_OPERATION", "REASON_OPERATION_CONFLICT", "REASON_OPERATION_OUTSTANDING", "REASON_OPERATION_UNCERTAIN", "_STATUS_BY_REASON", "CONFLICT_METHOD", "CONFLICT_TARGET", "OperationError", "OperationTarget", "operation_fingerprint", "require_method", "require_operation_id", "method_scope", "next_status", "attemptable", "outstanding", "terminal", "assert_body_free", "persist_operation", "export_operation", "_project", "new_record", "advance_record", "DECISION_ADMIT", "DECISION_REPLAY", "DECISION_REFUSE", "Admission", "conflicting_reuse", "admit", "Evidence", "NO_EVIDENCE", "Reconciliation", "reconcile"]  # fmt: skip

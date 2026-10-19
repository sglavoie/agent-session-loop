"""Read owner liveness and reply-consumption evidence for operation settlement."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from asl_workbench import actions as portable_actions
from asl_workbench import operations

from . import events, intervention, mailbox, mailbox_models, state
from .workbench_operation_journal import _hostname

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def owner_alive(record: Mapping[str, Any]) -> bool:
    """Whether the process that recorded this operation is still running.

    Public for the one caller outside this module that needs the same rule: a
    connector deciding, on the way up, which open rows are its predecessor's to
    close out. Settling a row whose owner is still going would report an
    outcome for a mutation that is currently happening.
    """
    return _owner_alive(record)


def _owner_alive(record: Mapping[str, Any]) -> bool:
    """Whether the process that recorded this operation is still running.

    Unknowable cases answer *alive*, which is the conservative direction: an
    operation whose owner cannot be checked keeps holding its scope, so the
    worst outcome is a refusal an operator can resolve explicitly through
    :func:`settle_operation`. Answering *gone* instead would convert "we cannot
    tell" into "nothing happened", and that is the one claim this module must
    never make on no evidence.
    """
    pid = record.get("pid")
    hostname = record.get("hostname")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if not isinstance(hostname, str) or not hostname:
        return True
    if hostname != _hostname():
        # Another host's process table is not ours to read.
        return True
    return state.is_pid_running(pid)


def _run_facts(
    plan_dir: str | Path, state_dir: str | Path | None
) -> intervention.RunFacts:
    return intervention.inspect_run(plan_dir, state_dir=state_dir)


def events_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Where one plan's event log lives.

    The log ASL's own runs append to, not a copy: the record that settles a
    reply-bearing operation has to be the run's own statement that it read the
    reply, and a projection of that statement is somebody's summary of it.
    """
    return state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME


def _reply_evidence(
    record: Mapping[str, Any],
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> portable_actions.ConsumptionEvidence:
    """Look in the plan's event log for a run that read this row's reply.

    Only the tail is read, and only back to
    :data:`asl_workbench.actions.MAX_WITNESS_BYTES_PER_LOOK`. A reply is taken
    into the next turn a run starts, so the evidence is at the end of the log
    when it is anywhere; a busy plan that has buried it answers "nothing found",
    which reconciles to ``unconfirmed`` rather than to a guess.

    Attribution is by the dispatch moment this row persisted, because the
    process that wrote the row may be gone and its byte marks with it. That is
    the weaker of the two tests :mod:`asl_workbench.actions` offers and it is
    the only one a restarted reconciler has.
    """
    method = str(record.get("method", ""))
    origin = portable_actions.consuming_origin(method)
    if origin is None:
        return portable_actions.NO_CONSUMPTION
    question_id = str(record.get("question_id") or "") or None
    if origin == portable_actions.REPLY_ORIGIN_MAILBOX and question_id is not None:
        try:
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        except OSError, ValueError, mailbox_models.MailboxError:
            view = None
        if view is not None:
            closure = view.reply_for(question_id)
            if closure is not None and view.receipt_for_closure(closure.id) is not None:
                # A live `asl ask --wait` consumes its closure in the waiting
                # process and writes this receipt. It does not start another
                # harness turn, so no `reply_provided` event follows. The
                # receipt is the direct, question-bound statement that the
                # operator's words were read and is therefore stronger than
                # inferring consumption from a later turn event.
                return portable_actions.ConsumptionEvidence(
                    consumed=True,
                    run_id=closure.run_id,
                    question_id=question_id,
                )
    dispatched_at = record.get("dispatched_at")
    if isinstance(dispatched_at, bool) or not isinstance(dispatched_at, int | float):
        # Admitted and never dispatched: there is nothing to have been read.
        return portable_actions.NO_CONSUMPTION
    # Only a mailbox reply is bound to a question id by construction. A reply
    # streamed into a resumed run's stdin records one only when the pause named
    # one, so requiring it there would discard evidence rather than check it.
    question_id = (
        question_id if origin == portable_actions.REPLY_ORIGIN_MAILBOX else None
    )
    budget = portable_actions.MAX_WITNESS_BYTES_PER_LOOK
    path = events_path_for_plan(plan_dir, state_dir)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > budget:
                handle.seek(size - budget)
                # Drop whatever the seek landed in the middle of: half a record
                # is not a record.
                handle.readline()
            chunk = handle.read(budget)
    except OSError:
        return portable_actions.ConsumptionEvidence(readable=False)
    evidence, _ = portable_actions.scan_replies(
        chunk,
        origin=origin,
        question_id=question_id,
        since=float(dispatched_at),
    )
    return evidence


def _evidence_for(
    record: Mapping[str, Any],
    facts: intervention.RunFacts,
    *,
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> operations.Evidence:
    reply = _reply_evidence(record, plan_dir, state_dir)
    return operations.Evidence(
        owner_alive=_owner_alive(record),
        run_active=facts.run_active,
        run_id=facts.run_id,
        reply_consumed=reply.consumed,
        consuming_run_id=reply.run_id,
    )

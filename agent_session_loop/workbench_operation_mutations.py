"""Reconcile, admit, dispatch, and settle operations under the journal lock."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from asl_workbench import catalog as portable_catalog
from asl_workbench import operations

from .workbench_operation_evidence import _evidence_for, _run_facts
from .workbench_operation_journal import (
    _append_locked,
    _hostname,
    _locked_journal,
    _moment,
    _Replay,
    journal_path_for_plan,
    plan_target,
)

#: How many times one record may be advanced in a single reconciliation pass.
#: ``dispatched -> accepted -> consumed`` is the longest real chain and
#: :data:`asl_workbench.operations.OPERATION_TRANSITIONS` is acyclic, so this is
#: a backstop against a future table change rather than a limit anything
#: legitimate reaches.
_MAX_RECONCILE_STEPS = 4


def _reconcile_locked(
    fd: int,
    replay: _Replay,
    *,
    journal_path: Path,
    plan_dir: str | Path,
    state_dir: str | Path | None,
    now: float,
    superseding: bool = False,
    resubmitted: str = "",
) -> list[dict[str, Any]]:
    """Settle what the authorities can settle, under the lock already held.

    Called before every admission decision, which is what stops a process that
    died mid-launch from holding a plan's run-start scope forever. The evidence
    comes from ASL -- the plan lock through ``intervention.inspect_run``, and
    the owning pid through ``state.is_pid_running`` -- because ASL is the
    authority on what actually happened and this journal is only the record of
    what was asked for.

    A record is advanced one legal step at a time until nothing more can be
    said about it, and only the resulting state is appended. The intermediate
    steps are not history anyone can use: they are all conclusions from the
    same single reading of the evidence, drawn in the same instant.

    ``resubmitted`` names the operation the caller is submitting right now, and
    it is the one row superseding never touches. A resend of the same id is the
    same submission, so letting it replace its own uncertain record would mean
    a retry answering itself with "you were replaced by you" -- and would hand
    the resend a fresh attempt at a mutation that may already have run.
    """
    outstanding_rows = [
        record
        for record in replay.ordered()
        if operations.outstanding(str(record.get("status", "")))
    ]
    if not outstanding_rows:
        return []
    facts = _run_facts(plan_dir, state_dir)
    changed: list[dict[str, Any]] = []
    for record in outstanding_rows:
        evidence = _evidence_for(record, facts, plan_dir=plan_dir, state_dir=state_dir)
        replaceable = superseding and record.get("operation_id") != resubmitted
        settled: dict[str, Any] | None = None
        current: Mapping[str, Any] = record
        for _ in range(_MAX_RECONCILE_STEPS):
            verdict = operations.reconcile(
                current, evidence, now=now, superseding=replaceable
            )
            if verdict is None:
                break
            settled = operations.advance_record(
                current,
                verdict.status,
                now=now,
                detail_code=verdict.detail_code,
            )
            current = settled
        if settled is not None:
            changed.append(
                _append_locked(fd, replay, settled, journal_path=journal_path)
            )
    return changed


def reconcile_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Reconcile one plan's open operations and return the rows that moved.

    Safe to call at any time and idempotent by construction: an operation the
    evidence cannot settle is left exactly as it was. It never supersedes --
    replacing an operation nobody could account for takes a person submitting
    something in its place, and a passive sweep is not that.
    """
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    if not journal_path.exists():
        return []
    moment = _moment(now)
    with _locked_journal(journal_path) as (fd, replay):
        return _reconcile_locked(
            fd,
            replay,
            journal_path=journal_path,
            plan_dir=plan_dir,
            state_dir=state_dir,
            now=moment,
        )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def admit_operation(
    plan_dir: str | Path,
    *,
    operation_id: str,
    method: str,
    selector: str = "",
    state_dir: str | Path | None = None,
    now: float | None = None,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_id: str | None = None,
    question_id: str | None = None,
) -> operations.Admission:
    """Decide and durably record what happens to one submitted mutation.

    Reconcile, decide, and append all happen under one exclusive lock on the
    journal. That is the whole cross-process guard: two Workbench processes, or
    a Workbench and the connector, submitting at the same moment serialize
    here, so exactly one of them is admitted and the other is told why not --
    replay for the same request, refusal for a different one.

    An admitted operation is written ``pending``, not ``dispatched``. The
    caller is expected to call :func:`mark_dispatched` immediately before the
    side effect, so an interruption between the two writes is distinguishable
    from an interruption after them.
    """
    resolved = operations.require_method(method)
    identifier = operations.require_operation_id(operation_id)
    target = plan_target(plan_dir, selector=selector)
    moment = _moment(now)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        _reconcile_locked(
            fd,
            replay,
            journal_path=journal_path,
            plan_dir=plan_dir,
            state_dir=state_dir,
            now=moment,
            superseding=True,
            resubmitted=identifier,
        )
        admission = operations.admit(
            replay.ordered(),
            operation_id=identifier,
            method=resolved,
            target=target,
            now=moment,
            deliver_by=deliver_by,
            command_id=command_id,
            run_key=portable_catalog.run_key(target.plan_key, run_id),
            question_id=question_id,
            pid=os.getpid(),
            hostname=_hostname(),
        )
        if admission.admitted and admission.record is not None:
            stored = _append_locked(
                fd, replay, admission.record, journal_path=journal_path
            )
            return operations.Admission(
                decision=admission.decision,
                operation_id=admission.operation_id,
                method=admission.method,
                scope=admission.scope,
                fingerprint=admission.fingerprint,
                record=stored,
            )
        return admission


def _update(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None,
    mutate: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
) -> dict[str, Any]:
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        record = replay.records.get(operation_id)
        if record is None:
            raise operations.OperationError(
                operations.REASON_UNKNOWN_OPERATION,
                f"no operation {operation_id!r} is recorded for this plan",
                details={"operation_id": operation_id},
            )
        updated = mutate(record)
        if updated is None:
            return dict(record)
        return _append_locked(fd, replay, updated, journal_path=journal_path)


def mark_dispatched(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None = None,
    now: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Record that the mutation is about to be attempted.

    Called immediately before the side effect and never after it. The gap this
    write closes is the one that matters: a process killed between admission
    and this write leaves a ``pending`` row, which reconciles to ``expired``
    because the mutation demonstrably did not run; killed after it, the row is
    ``dispatched`` and reconciles to ``unconfirmed``, because it may have. The
    ordering is what buys the first answer, and nothing else can.
    """
    moment = _moment(now)
    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=lambda record: operations.advance_record(
            record,
            operations.OPERATION_DISPATCHED,
            now=moment,
            run_id=run_id,
            pid=os.getpid(),
            hostname=_hostname(),
        ),
    )


def settle_operation(
    plan_dir: str | Path,
    operation_id: str,
    status: str,
    *,
    detail_code: str | None = None,
    run_id: str | None = None,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Move one operation to a settled status, refusing an illegal move.

    The route calls this with what actually happened: ``accepted`` when ASL
    started the run, ``rejected`` when ASL refused it. It is also how an
    operator resolves an ``unconfirmed`` operation that no evidence can settle
    -- deliberately an explicit act, because deciding on someone's behalf that
    an uncertain mutation did not happen is the decision this module refuses to
    make automatically.

    A delayed acceptance preserves an already consumed receipt. A live reply
    can be witnessed by reconciliation before its delivery call returns; that
    stronger evidence must neither be overwritten nor turn delivery into an
    error. The check shares the journal lock with the update.
    """
    moment = _moment(now)

    def mutate(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if (
            status == operations.OPERATION_ACCEPTED
            and record.get("status") == operations.OPERATION_CONSUMED
        ):
            return None
        return operations.advance_record(
            record,
            status,
            now=moment,
            detail_code=detail_code,
            run_id=run_id,
        )

    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=mutate,
    )


def confirm_operation(
    plan_dir: str | Path,
    operation_id: str,
    *,
    run_id: str | None = None,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record that the mutation ran and its receipt is in hand.

    Write ``consumed`` directly. Acceptance and consumption are separate states
    because on the Companion's side they are separated by a network: a command
    the bridge acknowledged is not yet a command ASL carried out. Here the
    mutation is local and synchronous, so the call that returns the receipt
    establishes both at once, and writing one row that says ``consumed`` is a
    truer record than two rows implying a gap that did not exist.

    Direct confirmation also admits late evidence for an ``unconfirmed`` row,
    which cannot return to ``accepted``. An already consumed row retains its
    original receipt unchanged when confirmation is retried.

    ``consumed`` is terminal, which is what stops an accounted-for mutation
    from holding its scope: the plan lock, not this journal, is what refuses a
    second run while the first is still going.
    """
    moment = _moment(now)
    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=lambda record: (
            None
            if record.get("status") == operations.OPERATION_CONSUMED
            else operations.advance_record(
                record,
                operations.OPERATION_CONSUMED,
                now=moment,
                run_id=run_id,
            )
        ),
    )

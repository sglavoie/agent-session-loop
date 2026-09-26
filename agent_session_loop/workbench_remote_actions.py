"""Carrying out a delivered mutation through the paths the browser uses.

:mod:`asl_workbench.remote_actions` decides what a connector will hold, for how
long, and what it may say about it. This is the half that actually acts, and the
whole of its job is to *not be a second implementation*: every method routes
into :mod:`agent_session_loop.workbench_actions`,
:mod:`agent_session_loop.workbench_controls` or
:mod:`agent_session_loop.workbench_archive` -- the same functions ``POST
/api/launch``, ``POST /api/control/abort`` and ``POST /api/plans/archive``
already call, with the same freshness checks, the same durable journal, and the
same refusals.

What is genuinely different about arriving over a link, and what this module
therefore adds:

* **A key, never a path.** A submission names a ``plan_key``, and
  :class:`~agent_session_loop.workbench_remote_reads.PlanIndex` is the only
  thing that turns one into a directory. The set of plans the wire can act on is
  exactly the set this Mac already publishes, which is not a property a path
  argument could ever have.

* **A proof, or a refusal.** Every method that acts on a live run is dispatched
  under ``AUTHORIZATION_STRICT``. A remote operator acted on a rendered
  snapshot, so the freshness token ASL issued with that snapshot is the only
  evidence that what they saw is what is there now -- and the portable half
  already refuses to submit one of those methods without it. Selecting a future
  harness is the one control that is *not* about a live turn, so ASL gives it no
  action to validate a proof against and it goes the open way the browser and
  the CLI have always sent it.

* **Archiving takes the bulk route.** ``archive_one`` needs the operator to
  retype the plan directory, which a caller holding a ``plan_key`` structurally
  cannot do and must not be given a way to fake. The delivered form goes through
  :func:`~agent_session_loop.workbench_archive.archive_many` instead, one plan
  per operation, and :func:`bulk_archive_result` reassembles a set of them into
  exactly the ``results``/``archived_count``/``failed_count`` payload the local
  bulk route answers with -- so per-entry reporting survives the trip rather
  than collapsing into one status word.

* **A restart closes rows rather than replaying them.** :meth:`recover` first
  applies any durable ASL evidence, then settles what a previous connector left
  open. It considers only rows that carry a ``command_id`` *and* whose owner
  process is gone: the first keeps it away from operations the local browser
  submitted, the second keeps it away from ones that are still happening.
  Nothing is re-attempted, ever.

Every moment is an argument here too, so a test drives the restart and
lost-response cases at the moments it chooses rather than waiting for them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive
from asl_workbench import controls as portable_controls
from asl_workbench import operations as portable_operations
from asl_workbench.details import DetailError
from asl_workbench.operations import (
    METHOD_ABORT,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.remote_actions import (
    MAX_BODY_CHARS,
    BodyUnavailable,
    MutationRequest,
    MutationResult,
    Settlement,
    reportable_status,
    restart_settlement,
    settlement_payload,
)

from . import (
    intervention,
    workbench,
    workbench_actions,
    workbench_archive,
    workbench_controls,
    workbench_operations,
)
from .workbench_remote_reads import PlanIndex

__all__ = [
    "AslMutationPerformer",
    "CONTROL_METHODS",
    "MutationRefused",
    "bulk_archive_result",
    "bulk_outcome",
]


#: Which submitted methods are run controls, and which control each names. The
#: three retained ones, and the mapping is a lookup rather than a string split
#: so a method whose name happens to look like a control cannot become one.
CONTROL_METHODS: Mapping[str, str] = {
    METHOD_STOP_AFTER_CURRENT: portable_controls.CONTROL_STOP_AFTER_CURRENT,
    METHOD_ABORT: portable_controls.CONTROL_ABORT,
    METHOD_SELECT_HARNESS: portable_controls.CONTROL_SELECT_HARNESS,
}


class MutationRefused(Exception):
    """ASL declined to carry out a delivered mutation.

    Carries ASL's own ``reason`` word unrenamed, the same way every other
    surface's refusal does. It becomes :attr:`MutationResult.reason`, which stays
    on this Mac: the transport's operation shape has nowhere to put it and its
    vocabulary is closed, so what goes out is ``rejected`` and what stays here is
    why.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def bulk_outcome(
    request: MutationRequest, result: MutationResult
) -> portable_archive.BulkOutcome:
    """One delivered ``plan.archive`` settlement, as the bulk route's own row.

    ``target`` is the ``plan_key``, never the directory: the far end named a key
    and gets a key back, so a batch report cannot become a way to learn this
    Mac's layout. ``reason`` matches the local route's word for word: ``"ok"``
    on success, and ASL's own refusal word otherwise, so a reader cannot tell
    the two routes apart by the shape of what came back.
    """
    archived = result.status == OPERATION_CONSUMED
    return portable_archive.BulkOutcome(
        target=request.plan_key,
        ok=archived,
        reason="ok"
        if archived
        else (result.reason or portable_archive.REASON_STATE_ERROR),
        error=None,
    )


def bulk_archive_result(
    outcomes: Sequence[portable_archive.BulkOutcome],
) -> workbench_archive.BulkArchiveResult:
    """A set of delivered archive settlements as one bulk answer.

    The delivered form of a bulk archive is N operations rather than one, so
    that each plan is independently admitted, journalled, windowed and settled.
    This puts the rows back together in the shape ``POST /api/plans/archive``
    answers with, which is what keeps "every retained control, including bulk
    archive results" true of the remote lane as well as the local one.
    """
    return workbench_archive.BulkArchiveResult(outcomes=tuple(outcomes))


class AslMutationPerformer:
    """Carries out delivered mutations through ASL's own shared actions.

    Built by :mod:`agent_session_loop.workbench_connector` when -- and only when
    -- the operator asked for remote actions. Holding one is what makes a
    connector stop calling itself observation-only, so its construction is the
    single place that decision is recorded.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        registry_path: str | Path | None = None,
        index: PlanIndex | None = None,
        profile: workbench.LaunchProfile | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.registry_path = registry_path
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self.profile = profile
        self._index = index or PlanIndex(
            state_dir=state_dir,
            plan_roots=self.plan_roots,
            include_default_dirs=include_default_dirs,
        )

    # -- performing ------------------------------------------------------
    def perform(
        self, request: MutationRequest, *, body: str | None, now: float
    ) -> MutationResult:
        """Carry out one delivered mutation and report what ASL settled it as.

        Raises nothing the connector has to interpret except
        :exc:`~asl_workbench.remote_actions.BodyUnavailable`, which the
        connector settles as ``expired`` because a body that never arrived is a
        command that certainly never ran. An ASL refusal comes back as
        ``rejected`` with ASL's own word alongside it, and anything that might
        have half-happened comes back ``unconfirmed``. The connector turns an
        escaping exception into ``unconfirmed`` too, so the two agree.
        """
        if request.method in (
            METHOD_ARCHIVE_RESTORE,
            METHOD_ARCHIVE_DELETE,
            METHOD_ARCHIVE_SETTINGS,
        ):
            try:
                return self._archive_collection(request, now=now)
            except (MutationRefused, workbench_archive.ArchiveRefused) as error:
                return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
            except OSError:
                return MutationResult(status=OPERATION_UNCONFIRMED)
        try:
            plan_dir = self._index.resolve(request.plan_key, now=now)
        except DetailError as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        try:
            if request.bears_body:
                body = _require_body(body)
            return self._route(request, plan_dir, body=body, now=now)
        except MutationRefused as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        except workbench_actions.ActionRefused as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        except workbench.LaunchError as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        except workbench_controls.ControlRefused as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        except workbench_archive.ArchiveRefused as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)
        except intervention.InterventionError as error:
            return MutationResult(status=OPERATION_REJECTED, reason=error.reason)

    def _route(
        self,
        request: MutationRequest,
        plan_dir: Path,
        *,
        body: str | None,
        now: float,
    ) -> MutationResult:
        """Send one method to the path the browser's own route would take."""
        method = request.method
        if method == METHOD_LAUNCH:
            return self._launch(request, plan_dir)
        if method == METHOD_REPLY:
            return self._resume(request, plan_dir, body=body)
        if method == METHOD_QUESTION_REPLY:
            return self._reply(request, plan_dir, body=body)
        if method in CONTROL_METHODS:
            return self._control(request, plan_dir, now=now)
        if method == METHOD_REDIRECT:
            return self._redirect(request, plan_dir, body=body, now=now)
        if method == METHOD_UNLOCK:
            return self._unlock(request, plan_dir, now=now)
        if method == METHOD_PLAN_ARCHIVE:
            return self._archive(request, plan_dir, now=now)
        raise MutationRefused(
            portable_operations.REASON_INVALID_METHOD,
            f"this performer does not carry {method}",
        )

    # -- the shared actions ----------------------------------------------
    def _launch(self, request: MutationRequest, plan_dir: Path) -> MutationResult:
        """``plan.launch``, through the action ``POST /api/launch`` calls."""
        outcome = workbench_actions.launch_run(
            plan_dir,
            operation_id=request.operation_id,
            profile=self.profile,
            state_dir=self.state_dir,
            deliver_by=request.deliver_by,
            command_id=request.command_id,
        )
        return _from_action(outcome)

    def _resume(
        self, request: MutationRequest, plan_dir: Path, *, body: str | None
    ) -> MutationResult:
        """``plan.reply``, through the action ``POST /api/reply`` calls."""
        outcome = workbench_actions.resume_with_reply(
            plan_dir,
            reply_text=body or "",
            operation_id=request.operation_id,
            profile=self.profile,
            state_dir=self.state_dir,
            action_token=request.bridge_token or None,
            question_id=request.question_id,
            run_id=request.run_id,
            deliver_by=request.deliver_by,
            command_id=request.command_id,
        )
        return _from_action(outcome)

    def _reply(
        self, request: MutationRequest, plan_dir: Path, *, body: str | None
    ) -> MutationResult:
        """``question.reply``, answering the live question of a running plan."""
        outcome = workbench_actions.reply_to_question(
            plan_dir,
            text=body or "",
            operation_id=request.operation_id,
            state_dir=self.state_dir,
            action_token=request.bridge_token or None,
            question_id=request.question_id,
            run_id=request.run_id,
            deliver_by=request.deliver_by,
            command_id=request.command_id,
        )
        return _from_action(outcome)

    def _control(
        self, request: MutationRequest, plan_dir: Path, *, now: float
    ) -> MutationResult:
        """One retained run control, journalled before it is queued.

        ``dispatch_control`` has its own idempotency -- the ``command_id`` is
        the queue's -- but no durable row of its own, so the operations journal
        is wrapped around it here. That is what puts a dispatch record in front
        of the side effect, which is the only thing that lets a restart tell a
        control that was never queued from one that may have been.

        Strict authorization wherever there is a live turn to be strict about,
        which is the two controls that interrupt one. Selecting a future harness
        has no ASL action to validate a proof against, so holding it to a proof
        would refuse it every time rather than check anything; it goes the open
        way, which is the way the local browser and the CLI have always sent it.
        """
        kind = CONTROL_METHODS[request.method]
        authorization = (
            portable_controls.AUTHORIZATION_STRICT
            if portable_controls.asl_action_for(kind) is not None
            else portable_controls.AUTHORIZATION_OPEN
        )
        with _journalled(self, request, plan_dir, now=now) as journal:
            if journal.replayed is not None:
                return journal.replayed
            workbench_controls.dispatch_control(
                plan_dir,
                kind,
                state_dir=self.state_dir,
                command_id=request.command_id,
                authorization=authorization,
                action_token=request.bridge_token or None,
                run_id=request.run_id,
                harness=request.harness,
                reason=request.reason,
            )
        # ``accepted`` rather than ``consumed``: the command is durably queued
        # and a runner has been woken, and whether it acted on it is a fact
        # about the next turn rather than about this call.
        return MutationResult(status=OPERATION_ACCEPTED, run_id=request.run_id)

    def _redirect(
        self, request: MutationRequest, plan_dir: Path, *, body: str | None, now: float
    ) -> MutationResult:
        """``plan.redirect``, through the correction the browser submits.

        Wrapped in the operations journal like the other controls even though
        ``submit_correction`` keeps a durable redirect journal of its own. The
        two answer different questions -- that one decides whether this
        correction is a replay of itself, this one decides whether the plan has
        an outstanding mutation at all -- and a delivered redirect needs both.
        """
        with _journalled(self, request, plan_dir, now=now) as journal:
            if journal.replayed is not None:
                return journal.replayed
            submission = workbench_controls.submit_correction(
                plan_dir,
                operation_id=request.operation_id,
                text=body,
                state_dir=self.state_dir,
                authorization=portable_controls.AUTHORIZATION_STRICT,
                action_token=request.bridge_token or None,
                run_id=request.run_id,
            )
        if not submission.persisted:
            raise MutationRefused(
                submission.reason or portable_controls.REASON_INVALID_REQUEST,
                "the correction was not persisted",
            )
        return MutationResult(status=OPERATION_ACCEPTED, run_id=request.run_id)

    def _unlock(
        self, request: MutationRequest, plan_dir: Path, *, now: float
    ) -> MutationResult:
        """``plan.unlock``, releasing a lock an operator decided is dead.

        Never forced. A forced unlock takes a plan away from a process that may
        still be running it, and the operator who would accept that risk is the
        one sitting at the machine -- not a caller whose evidence is a snapshot
        that crossed a network.
        """
        with _journalled(self, request, plan_dir, now=now) as journal:
            if journal.replayed is not None:
                return journal.replayed
            workbench_archive.unlock(
                str(plan_dir), state_dir=self.state_dir, force=False
            )
            journal.terminal = True
        return MutationResult(status=OPERATION_CONSUMED)

    def _archive(
        self, request: MutationRequest, plan_dir: Path, *, now: float
    ) -> MutationResult:
        """``plan.archive``, through the bulk route rather than the confirmed one.

        ``archive_one`` asks the operator to retype the plan directory, which is
        a confirmation a caller holding only a ``plan_key`` cannot give and must
        not be handed a way to forge. ``archive_many`` is the route whose
        confirmation is the selection itself, and it is the one a delivered
        archive belongs on.
        """
        with _journalled(self, request, plan_dir, now=now) as journal:
            if journal.replayed is not None:
                return journal.replayed
            bulk = workbench_archive.archive_many(
                [str(plan_dir)],
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                plan_roots=self.plan_roots,
                include_default_dirs=self.include_default_dirs,
            )
            row = bulk.outcomes[0]
            if not row.ok:
                raise MutationRefused(
                    row.reason or portable_archive.REASON_STATE_ERROR,
                    row.error or "the plan was not archived",
                )
            journal.terminal = True
        return MutationResult(status=OPERATION_CONSUMED)

    def _archive_collection(
        self, request: MutationRequest, *, now: float
    ) -> MutationResult:
        """Run one archive command through a durable collection journal."""
        journal_subject = workbench.archive_root(self.state_dir)
        with _journalled(self, request, journal_subject, now=now) as journal:
            if journal.replayed is not None:
                return journal.replayed
            if request.method == METHOD_ARCHIVE_RESTORE:
                assert request.archive_id is not None
                workbench_archive.restore_entry(
                    request.archive_id,
                    state_dir=self.state_dir,
                    registry_path=self.registry_path,
                )
            elif request.method == METHOD_ARCHIVE_DELETE:
                assert request.archive_id is not None
                result = workbench_archive.purge_entries(
                    [request.archive_id], 1, state_dir=self.state_dir
                )
                if result.removed_count != 1:
                    raise MutationRefused("not-found", "archive entry was not removed")
            else:
                assert request.archive_retention_days is not None
                workbench_archive.set_retention(
                    request.archive_retention_days,
                    registry_path=self.registry_path,
                )
            journal.terminal = True
        return MutationResult(status=OPERATION_CONSUMED)

    # -- restart ---------------------------------------------------------
    def recover(self, *, now: float) -> list[dict[str, Any]]:
        """Settle and report what a previous connector left open.

        Two filters, and dropping either one would make this dangerous. A row
        without a ``command_id`` was submitted by a local surface, and settling
        it would have the connector answer for a browser's launch. A row whose
        owner process is still alive is a mutation that is *happening*, and
        settling it would report an outcome for something in flight.

        What survives both is a row this connector's predecessor admitted and
        nobody is carrying out. Durable evidence is reconciled first, so a
        consumed mailbox reply remains consumed; only an otherwise unresolved
        row is closed with the word its recorded status earns: ``expired`` when
        it never reached dispatch, ``unconfirmed`` when it did. Never
        re-attempted.
        """
        settled: list[dict[str, Any]] = []
        subjects = [
            *self._index.items(now=now),
            (None, workbench.archive_root(self.state_dir)),
        ]
        for plan_key, plan_dir in subjects:
            try:
                rows = workbench_operations.open_operations(
                    plan_dir, state_dir=self.state_dir
                )
            except OSError:
                continue
            eligible = {
                str(record["operation_id"])
                for record in rows
                if record.get("command_id")
                and not workbench_operations.owner_alive(record)
            }
            if not eligible:
                continue
            try:
                reconciled = (
                    workbench_operations.reconcile_plan(
                        plan_dir, state_dir=self.state_dir, now=now
                    )
                    if plan_key is not None
                    else []
                )
            except OSError:
                reconciled = []
            for record in reconciled:
                operation_id = str(record.get("operation_id") or "")
                if operation_id not in eligible:
                    continue
                settled.append(
                    settlement_payload(
                        Settlement(
                            operation_id=operation_id,
                            status=str(record["status"]),
                            detail_code=str(
                                record.get("detail_code")
                                or portable_operations.DETAIL_NO_RESULT
                            ),
                            method=str(record.get("method") or "") or None,
                        ),
                        observed_at=now,
                        target={"plan_key": plan_key} if plan_key else None,
                    )
                )
                eligible.discard(operation_id)
            if not eligible:
                continue
            try:
                rows = workbench_operations.open_operations(
                    plan_dir, state_dir=self.state_dir
                )
            except OSError:
                continue
            for record in rows:
                if str(record.get("operation_id") or "") not in eligible:
                    continue
                settlement = restart_settlement(record, now=now)
                if settlement is None:
                    continue
                try:
                    workbench_operations.settle_operation(
                        plan_dir,
                        settlement.operation_id,
                        settlement.status,
                        detail_code=settlement.detail_code,
                        state_dir=self.state_dir,
                        now=now,
                    )
                except portable_operations.OperationError, OSError:
                    # The row moved between the read and the write, or the
                    # journal is unreadable. Either way somebody else's answer
                    # is now the record, and overwriting it would be worse than
                    # saying nothing.
                    continue
                settled.append(
                    settlement_payload(
                        settlement,
                        observed_at=now,
                        target={"plan_key": plan_key} if plan_key else None,
                    )
                )
        return settled

    def reconcile(
        self, request: MutationRequest, *, now: float
    ) -> MutationResult | None:
        """Report when ASL's evidence advances one accepted remote mutation.

        The first result says only that a live reply was appended.  The runner
        later records that it consumed those words.  Reconcile the local
        operation journal against that record and return a second result only
        when the operation has reached a terminal state.
        """
        try:
            plan_dir = self._index.resolve(request.plan_key, now=now)
        except DetailError:
            return None
        try:
            workbench_operations.reconcile_plan(
                plan_dir, state_dir=self.state_dir, now=now
            )
            record = next(
                (
                    row
                    for row in workbench_operations.read_operations(
                        plan_dir, state_dir=self.state_dir
                    )
                    if row.get("operation_id") == request.operation_id
                ),
                None,
            )
        except OSError, portable_operations.OperationError:
            return None
        if record is None:
            return None
        status = str(record.get("status", ""))
        if status in {
            portable_operations.OPERATION_PENDING,
            portable_operations.OPERATION_DISPATCHED,
            portable_operations.OPERATION_ACCEPTED,
        }:
            return None
        return MutationResult(
            status=reportable_status(status),
            detail_code=_detail_of(record),
            run_id=request.run_id,
        )


# ---------------------------------------------------------------------------
# Journalling the actions that have no journal of their own
# ---------------------------------------------------------------------------


class _Journal:
    """The operations row wrapped around one action that keeps none itself.

    A context manager rather than a pair of calls, because the ordering it
    enforces is the whole point: admit, mark dispatched, *then* act, and close
    the row out as ``unconfirmed`` if the action raises something that might
    have landed anyway.
    """

    def __init__(
        self,
        performer: AslMutationPerformer,
        request: MutationRequest,
        plan_dir: Path,
        *,
        now: float,
    ) -> None:
        self._performer = performer
        self._request = request
        self._plan_dir = plan_dir
        self._now = now
        #: The result a replay is owed, or ``None`` when this call is the first.
        self.replayed: MutationResult | None = None
        #: Set by the body when the effect is complete rather than merely
        #: queued, which is the difference between ``consumed`` and ``accepted``.
        self.terminal = False

    def __enter__(self) -> _Journal:
        admission = workbench_operations.admit_operation(
            self._plan_dir,
            operation_id=self._request.operation_id,
            method=self._request.method,
            state_dir=self._performer.state_dir,
            now=self._now,
            deliver_by=self._request.deliver_by,
            command_id=self._request.command_id,
            run_id=self._request.run_id,
            question_id=self._request.question_id,
        )
        if not admission.admitted:
            if admission.replayed and admission.record is not None:
                # The first attempt's record answers the resend, exactly as it
                # does for the browser. Nothing is carried out a second time.
                status = str(admission.record.get("status", ""))
                self.replayed = MutationResult(
                    status=reportable_status(status),
                    detail_code=_detail_of(admission.record),
                    run_id=self._request.run_id,
                )
                return self
            raise MutationRefused(
                portable_operations.REASON_OPERATION_OUTSTANDING,
                "another mutation is outstanding for this plan",
            )
        workbench_operations.mark_dispatched(
            self._plan_dir,
            self._request.operation_id,
            state_dir=self._performer.state_dir,
            now=self._now,
            run_id=self._request.run_id,
        )
        return self

    def __exit__(self, kind: object, error: object, traceback: object) -> None:
        """Close the row and never swallow: the caller decides what to report."""
        if self.replayed is not None:
            return
        if error is None:
            self._settle()
            return
        # A refusal ASL stated is a refusal: it decided not to act. Anything
        # else was attempted and may have landed, and ``unconfirmed`` is the
        # only word that is not a guess about which.
        if isinstance(error, _STATED_REFUSALS):
            self._close(OPERATION_REJECTED, None)
        else:
            self._close(OPERATION_UNCONFIRMED, portable_operations.DETAIL_NO_RESULT)

    def _settle(self) -> None:
        if self.terminal:
            try:
                workbench_operations.confirm_operation(
                    self._plan_dir,
                    self._request.operation_id,
                    run_id=self._request.run_id,
                    state_dir=self._performer.state_dir,
                    now=self._now,
                )
            except portable_operations.OperationError, OSError:
                # Archiving takes the plan's state directory -- and with it this
                # journal -- along with the plan, so the row a confirmation
                # would amend is gone by the time there is anything to confirm.
                # The effect happened and the caller is told so; the record of
                # it moved into the archive with everything else about the plan.
                return
            return
        self._close(OPERATION_ACCEPTED, None)

    def _close(self, status: str, detail_code: str | None) -> None:
        try:
            workbench_operations.settle_operation(
                self._plan_dir,
                self._request.operation_id,
                status,
                detail_code=detail_code,
                run_id=self._request.run_id,
                state_dir=self._performer.state_dir,
                now=self._now,
            )
        except portable_operations.OperationError, OSError:
            # The row is already settled, or the journal cannot be written. The
            # original outcome is what the caller needs to hear about, so this
            # is not allowed to replace it.
            return


_STATED_REFUSALS = (
    MutationRefused,
    workbench.LaunchError,
    workbench_actions.ActionRefused,
    workbench_archive.ArchiveRefused,
    workbench_controls.ControlRefused,
)


def _journalled(
    performer: AslMutationPerformer,
    request: MutationRequest,
    plan_dir: Path,
    *,
    now: float,
) -> _Journal:
    """Wrap one action that keeps no durable row of its own in the journal."""
    return _Journal(performer, request, plan_dir, now=now)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _require_body(body: str | None) -> str:
    """The prose a body-bearing method needs, bounded the way the Pi bounds it.

    A body over the limit is refused whole rather than truncated: a reply cut
    off at eight thousand characters is prose a run would act on as though it
    were the whole instruction.
    """
    if body is None:
        raise BodyUnavailable("this command's body was not redeemed")
    if len(body) > MAX_BODY_CHARS:
        raise MutationRefused(
            portable_controls.REASON_INVALID_REQUEST,
            f"the redeemed body exceeds {MAX_BODY_CHARS} characters",
        )
    return body


def _from_action(outcome: workbench_actions.ActionOutcome) -> MutationResult:
    """One shared action's settled row, in the words a connector may report."""
    return MutationResult(
        status=reportable_status(outcome.status),
        detail_code=_detail_of(outcome.operation),
        run_id=outcome.facts.run_id,
    )


def _detail_of(record: Mapping[str, Any]) -> str | None:
    """The detail code a settled row carries, when it carries one."""
    code = record.get("detail_code")
    return code if isinstance(code, str) and code else None

"""The operations journal wrapped around delivered actions, and their projections.

Split out of :mod:`agent_session_loop.workbench_remote_actions`, which keeps the
performer that uses it and re-exports these names.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from asl_workbench import controls as portable_controls
from asl_workbench import operations as portable_operations
from asl_workbench.operations import (
    OPERATION_ACCEPTED,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.remote_actions import (
    MAX_BODY_CHARS,
    BodyUnavailable,
    MutationRequest,
    MutationResult,
    reportable_status,
)

from . import (
    workbench_actions,
    workbench_archive,
    workbench_controls,
    workbench_launch,
    workbench_operations,
)

if TYPE_CHECKING:
    from .workbench_remote_actions import AslMutationPerformer


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
    workbench_launch.LaunchError,
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

"""The bounded mailbox reply wait: prepare, poll, finish, and its coordinator."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    clock,
    mailbox,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
    plans_models,
    qna,
    run_coordination_constants,
    run_coordination_mailbox_question,
    run_coordination_seams,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_coordination_wait_state,
)


def prepare_mailbox_reply_wait(
    *,
    snapshot: plans_models.PlanSnapshot,
    payload: dict[str, Any],
    plan_state: Path,
    state_path: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
    started_at: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> run_coordination_wait_models.PreparedMailboxWait:
    """Validate turn context, ensure its question, and prepare persisted state."""
    try:
        lease = mailbox_leases.read_lease(
            snapshot.plan_dir,
            state_dir=state_dir_arg,
        )
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_LEASE_READ
        )
        raise run_coordination_wait_models.MailboxWaitUnavailable(reason) from error
    if not lease.fresh:
        raise run_coordination_wait_models.MailboxWaitUnavailable(
            lease.reason or mailbox_constants.ERROR_LEASE_READ
        )

    settings = run_coordination_wait_models.mailbox_reply_wait_settings(environ)
    started_monotonic = monotonic()
    resolved_started_at = clock.utc_now() if started_at is None else started_at
    question_id = run_coordination_mailbox_question.mailbox_question_id(
        plan_dir=snapshot.plan_dir,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
    )
    waiting_for_reply = run_coordination_wait_record.waiting_for_reply_payload(
        question_id=question_id,
        session_id=session_id,
        turn_index=turn_index,
        started_at=resolved_started_at,
        base_deadline=(resolved_started_at + timedelta(seconds=settings.base_seconds)),
        unsupervised_deadline_at=(
            resolved_started_at
            + timedelta(seconds=settings.unsupervised_ceiling_seconds)
        ),
        unsupervised_ceiling_seconds=settings.unsupervised_ceiling_seconds,
        last_lease_activity_at=None,
    )
    try:
        context = qna.build_qna_context(
            snapshot,
            payload,
            plan_state=plan_state,
            state_path=state_path,
            waiting_for_reply=waiting_for_reply,
        )
    except qna.QnAError as error:
        raise run_coordination_wait_models.MailboxWaitUnavailable(
            error.reason
        ) from error

    question = run_coordination_mailbox_question.ensure_mailbox_question(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=state_dir_arg,
        question_id=question_id,
        text=run_coordination_mailbox_question.mailbox_question_text(context),
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
    )
    try:
        prepared_view = mailbox.read_mailbox(
            snapshot.plan_dir,
            state_dir=state_dir_arg,
        )
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise run_coordination_wait_models.MailboxWaitUnavailable(reason) from error
    wait_start = prepared_view.active_wait_for(question.id)
    if wait_start is None:
        raise run_coordination_wait_models.MailboxWaitUnavailable(
            mailbox_constants.REJECT_QUESTION_NOT_WAITING
        )
    return run_coordination_wait_models.PreparedMailboxWait(
        context=context,
        question=question,
        wait_start=wait_start,
        waiting_for_reply=waiting_for_reply,
        pending_outbox=len(prepared_view.open_questions(run_id)),
        settings=settings,
        started_monotonic=started_monotonic,
    )


def wait_for_mailbox_reply(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    payload: dict[str, Any],
    question_id: str,
    run_id: str,
    settings: run_coordination_wait_models.MailboxWaitSettings,
    persistence: run_coordination_seams.CoordinationPersistence,
    poll_controls: Callable[[], None] | None = None,
    started_monotonic: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] = clock.utc_now,
) -> run_coordination_wait_models.MailboxWaitResult:
    """Poll one closure under monotonic base, lease-extension, and ceiling bounds."""
    started = monotonic() if started_monotonic is None else started_monotonic
    cursor = run_coordination_wait_models.SupervisionCursor(
        settings=settings,
        started=started,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    io_failure_started: float | None = None
    pending_outbox: int | None = None
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir_arg)
    last_successful_view: mailbox_snapshot.MailboxView | None = None
    last_read_token: object = run_coordination_constants.MAILBOX_TOKEN_UNSEEN

    while True:
        if poll_controls is not None:
            poll_controls()

        rebased = cursor.observe_clock()
        if rebased is not None:
            base_deadline, unsupervised_deadline_at = rebased
            run_coordination_wait_state.rebase_waiting_deadlines(
                payload,
                question_id=question_id,
                base_deadline=base_deadline,
                unsupervised_deadline_at=unsupervised_deadline_at,
                persistence=persistence,
            )

        io_failure_reason: str | None = None
        current_token = mailbox.mailbox_change_token(mailbox_path)
        view = last_successful_view
        if (
            last_read_token is run_coordination_constants.MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            try:
                view = mailbox.read_mailbox(
                    plan_dir,
                    state_dir=state_dir_arg,
                )
            except (OSError, mailbox_models.MailboxError) as error:
                view = None
                io_failure_reason = (
                    error.reason
                    if isinstance(error, mailbox_models.MailboxError)
                    else mailbox_constants.ERROR_MAILBOX_READ
                )
            else:
                last_successful_view = view
                # Retain the token captured before the locked read. An append
                # racing that read will conservatively trigger another parse.
                last_read_token = current_token

        if view is not None:
            pending_outbox = len(view.open_questions(run_id))
            closure = view.closure_for(question_id)
            if closure is not None and closure.kind == mailbox_vocabulary.KIND_REPLY:
                return run_coordination_wait_models.MailboxWaitResult(
                    reason=run_coordination_constants.WAIT_REASON_REPLY,
                    pending_outbox=pending_outbox,
                    reply=closure,
                )
            if (
                closure is not None
                and closure.kind == mailbox_vocabulary.KIND_RESOLUTION
            ):
                return run_coordination_wait_models.MailboxWaitResult(
                    reason=run_coordination_constants.WAIT_REASON_RESOLUTION,
                    pending_outbox=pending_outbox,
                    resolution=closure,
                )
            if not view.accepts_wait_closures:
                return run_coordination_wait_models.MailboxWaitResult.unclosed(
                    mailbox_constants.ERROR_MAILBOX_FULL,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )
            question = next(
                (
                    message
                    for message in view.messages
                    if message.id == question_id
                    and message.kind == mailbox_vocabulary.KIND_QUESTION
                ),
                None,
            )
            if question is None:
                return run_coordination_wait_models.MailboxWaitResult.unclosed(
                    qna.REASON_MISSING_QUESTION_CONTEXT,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )

        now = monotonic()
        if (
            io_failure_reason is None
            and now < cursor.unsupervised_deadline()
            and now >= cursor.active_deadline
        ):
            try:
                lease = mailbox_leases.read_lease(
                    plan_dir,
                    state_dir=state_dir_arg,
                )
            except (OSError, mailbox_models.MailboxError) as error:
                io_failure_reason = (
                    error.reason
                    if isinstance(error, mailbox_models.MailboxError)
                    else mailbox_constants.ERROR_LEASE_READ
                )
            else:
                if lease.updated_at is not None:
                    run_coordination_wait_state.record_waiting_lease_activity(
                        payload,
                        question_id=question_id,
                        updated_at=lease.updated_at,
                        persistence=persistence,
                    )
                if cursor.observe_lease(lease, now) == "lost":
                    return run_coordination_wait_models.MailboxWaitResult.unclosed(
                        run_coordination_constants.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
                        pending_outbox=pending_outbox,
                        evidence=cursor.evidence(),
                    )

        if io_failure_reason is not None:
            if io_failure_started is None:
                io_failure_started = now
            if (
                now - io_failure_started >= settings.io_grace_seconds
                or now >= cursor.unsupervised_deadline()
            ):
                return run_coordination_wait_models.MailboxWaitResult.unclosed(
                    io_failure_reason,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )
            sleep(
                max(
                    0.0,
                    min(
                        settings.poll_seconds,
                        (io_failure_started + settings.io_grace_seconds) - now,
                        cursor.unsupervised_deadline() - now,
                    ),
                )
            )
            continue
        io_failure_started = None

        if now >= cursor.unsupervised_deadline():
            return run_coordination_wait_models.MailboxWaitResult.unclosed(
                run_coordination_constants.WAIT_REASON_TIMEOUT,
                pending_outbox=pending_outbox,
                evidence=cursor.evidence(),
            )

        # During a post-suspend lease grace the active deadline stays in the
        # past, so it bounds the sleep only while it is still ahead — pacing
        # on a passed deadline would busy-spin at zero-length sleeps.
        sleep_bound = min(settings.poll_seconds, cursor.unsupervised_deadline() - now)
        if now < cursor.active_deadline:
            sleep_bound = min(sleep_bound, cursor.active_deadline - now)
        sleep(max(0.0, sleep_bound))


def finish_mailbox_reply_wait_lifecycle(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    question_id: str,
    wait_id: str,
    result: run_coordination_wait_models.MailboxWaitResult,
) -> run_coordination_wait_models.MailboxWaitResult:
    """Commit wait termination and let a lock-winning closure override timeout.

    This is the recovery half of the exactly-once reply guarantee: the poll
    loop reads the mailbox without its lock, so a reply appended in the instant
    the wait gave up is invisible to it. ``mailbox_waits.finish_wait`` takes the
    lock, and whatever closure it finds there wins over the reason the loop
    arrived with.
    """
    completion = mailbox_waits.finish_wait(
        plan_dir,
        question_id,
        wait_id,
        reason=(
            mailbox_vocabulary.WAIT_END_REASON_TIMEOUT
            if result.reason == run_coordination_constants.WAIT_REASON_TIMEOUT
            else mailbox_vocabulary.WAIT_END_REASON_ERROR
        ),
        state_dir=state_dir_arg,
    )
    closure = completion.closure
    if closure is None:
        return result
    # The evidence the loop gathered is kept verbatim and only the outcome is
    # rewritten, so a timeout that lost the race still reports the lease it saw.
    # Both closure fields are named on both branches: a wait can end on exactly
    # one, and carrying the other over would describe two endings.
    if closure.kind == mailbox_vocabulary.KIND_REPLY:
        return dataclass_replace(
            result,
            reason=run_coordination_constants.WAIT_REASON_REPLY,
            reply=closure,
            resolution=None,
        )
    return dataclass_replace(
        result,
        reason=run_coordination_constants.WAIT_REASON_RESOLUTION,
        reply=None,
        resolution=closure,
    )


class MailboxWaitCoordinator:
    """One reply-capable pause's whole live wait, start to cleared.

    The ordering this class exists to guarantee is that the durable wait record
    and the lifecycle notification bracket the poll on *both* sides: a wait that
    starts is always announced before it can block, and a wait that stops is
    always cleared and announced even when the poll raises. Doing that inline
    meant a hundred-line ``finally`` whose every field had to repeat "if the
    result is not None"; here the absence of a result is one named case.
    """

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir_arg: str | None,
        prepared: run_coordination_wait_models.PreparedMailboxWait,
        run_id: str,
        store: run_coordination_wait_state.WaitStateStore,
        persistence: run_coordination_seams.CoordinationPersistence,
        notifier: run_coordination_seams.WaitNotifier | None = None,
        poll_controls: Callable[[], None] | None = None,
        wait_for_reply: Callable[
            ..., run_coordination_wait_models.MailboxWaitResult
        ] = wait_for_mailbox_reply,
        finish_lifecycle: Callable[
            ..., run_coordination_wait_models.MailboxWaitResult
        ] = (finish_mailbox_reply_wait_lifecycle),
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._prepared = prepared
        self._run_id = run_id
        self._store = store
        self._persistence = persistence
        self._notifier = notifier
        self._poll_controls = poll_controls
        # Injected rather than called directly so a caller's patch is heard:
        # in-tree callers and tests patch ``wait_for_mailbox_reply`` on this
        # module to assert a wait was or was not entered, and a coordinator that
        # bound the function at construction would ignore the patch. The run
        # loop resolves both names here at the call site for the same reason.
        self._wait_for_reply = wait_for_reply
        self._finish_lifecycle = finish_lifecycle

    @property
    def question(self) -> mailbox_models.MailboxMessage:
        return self._prepared.question

    @property
    def context(self) -> qna.QnAContext:
        return self._prepared.context

    def run(self) -> run_coordination_wait_models.MailboxWaitResult:
        """Announce, poll, commit, and clear — in that order, always."""
        prepared = self._prepared
        self._store.begin(
            prepared.waiting_for_reply,
            pending_outbox=prepared.pending_outbox,
        )
        if self._notifier is not None:
            self._notifier.wait_started(
                question_id=prepared.question.id,
                question_text=prepared.question.text,
            )
        result: run_coordination_wait_models.MailboxWaitResult | None = None
        lifecycle_ended = False
        try:
            result = self._wait_for_reply(
                plan_dir=self._plan_dir,
                state_dir_arg=self._state_dir_arg,
                payload=self._store.payload,
                question_id=prepared.question.id,
                run_id=self._run_id,
                settings=prepared.settings,
                persistence=self._persistence,
                poll_controls=self._poll_controls,
                started_monotonic=prepared.started_monotonic,
            )
            try:
                result = self._finish_lifecycle(
                    plan_dir=self._plan_dir,
                    state_dir_arg=self._state_dir_arg,
                    question_id=prepared.question.id,
                    wait_id=prepared.wait_start.id,
                    result=result,
                )
            except mailbox_models.MailboxError as error:
                # The wait-end could not be committed -- most often because the
                # mailbox is full. That is a wait that ended without an answer,
                # so it becomes the reason the caller pauses on rather than an
                # exception that crashes the run. The poll's own answer is
                # dropped with it: an answer whose receipt was never written
                # was not consumed, and acting on it would deliver it twice.
                result = dataclass_replace(
                    result, reason=error.reason, reply=None, resolution=None
                )
            lifecycle_ended = True
        finally:
            if not lifecycle_ended:
                self._end_abandoned_wait()
            reason = self._store.clear(result)
            if self._notifier is not None:
                self._notifier.wait_ended(
                    question_id=prepared.question.id,
                    wait_reason=reason,
                    closure_id=None if result is None else result.closure_id,
                )
        return result

    def _end_abandoned_wait(self) -> None:
        """Best-effort close of a wait the poll left by raising.

        An interrupt or a state-write failure leaves the poll without a result,
        and without this the mailbox keeps the question waiting for a run that
        has stopped listening: ``asl inbox reply`` would accept an answer no
        one will consume and ``--wait-consumed`` would hang on it. An answer
        that already arrived is left unconsumed rather than receipted, because
        this run never delivered it.
        """
        with contextlib.suppress(OSError, mailbox_models.MailboxError):
            mailbox_waits.finish_wait(
                self._plan_dir,
                self._prepared.question.id,
                self._prepared.wait_start.id,
                reason=mailbox_vocabulary.WAIT_END_REASON_ERROR,
                state_dir=self._state_dir_arg,
                consume_closure=False,
            )

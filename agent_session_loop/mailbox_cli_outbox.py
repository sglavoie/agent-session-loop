"""The ``outbox wait`` and ``outbox supervise`` commands."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    mailbox,
    mailbox_cli_agent_mux,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_models,
    mailbox_cli_output,
    mailbox_cli_projections,
    mailbox_cli_status,
    mailbox_cli_waits,
    mailbox_cursors,
    mailbox_models,
    responses,
)


def outbox_wait_from_args(args: argparse.Namespace) -> int:
    """Long-poll selected worker records after a stateless physical cursor."""
    since = mailbox_cli_args.validated_since(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if since is None:
        return 1
    include = mailbox_cli_args.validated_include(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if include is None:
        return 1
    supervision_id, supervision_id_ok = mailbox_cli_args.validated_supervision_id(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if not supervision_id_ok:
        return 1
    timeout = mailbox_cli_args.validated_timeout(
        args,
        value=args.timeout,
        option="--timeout",
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if timeout is None:
        return 1
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if context is None:
        return 1
    supervisor_id = str(uuid.uuid4())
    try:
        wait_result = mailbox_cli_waits.wait_for_questions(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
            since=since,
            timeout=timeout,
            supervisor_id=supervisor_id,
            include=include,
        )
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
            text_label="outbox wait",
            error=error,
        )
        return 1

    question_projection = mailbox_cli_projections.outbox_question_projection(
        context,
        args,
        wait_result,
    )
    mailbox_cli_agent_mux.notify_agent_mux_mailbox_question(
        context, question_projection
    )

    # Without a correlation filter every terminal record reads as terminal,
    # which is exactly what an overlapping helper's record must not do.
    own_supervision_ends = tuple(
        supervision_end
        for supervision_end in wait_result.supervision_ends
        if supervision_id is None or supervision_end.supervision_id == supervision_id
    )
    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_OUTBOX_WAIT,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        lease_path=str(
            mailbox_cursors.lease_path_for_plan(
                context.snapshot.plan_dir, args.state_dir
            )
        ),
        supervisor_id=wait_result.supervisor_id,
        since=since,
        next_since=wait_result.next_since,
        timeout=timeout,
        timed_out=wait_result.timed_out,
        include=wait_result.include,
        questions=[
            mailbox_cli_output.message_payload(question)
            for question in wait_result.questions
        ],
        notices=[
            mailbox_cli_output.message_payload(notice) for notice in wait_result.notices
        ],
        supervision_ends=[
            mailbox_cli_output.message_payload(supervision_end)
            for supervision_end in wait_result.supervision_ends
        ],
        supervision_ended=bool(own_supervision_ends),
        supervision_id=supervision_id,
        lease_supervision=wait_result.lease_supervision.to_payload(),
    )
    if args.json:
        responses.print_json(payload)
    else:
        failures = wait_result.lease_supervision.renewal_failures
        if failures:
            health = (
                "the latest renewal remains degraded"
                if wait_result.lease_supervision.degraded
                else "a later renewal recovered"
            )
            print(
                "Warning: supervisor lease renewal failed "
                f"{failures} time(s); {health}.",
                file=sys.stderr,
            )
        if (
            not wait_result.questions
            and not wait_result.notices
            and not wait_result.supervision_ends
        ):
            selected_label = {
                mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS: "questions",
                mailbox_cli_constants.OUTBOX_INCLUDE_NOTICES: "notices",
                mailbox_cli_constants.OUTBOX_INCLUDE_ALL: (
                    "questions, notices, or supervision ends"
                ),
            }[wait_result.include]
            print(f"No {selected_label} after seq {since}; timed out.")
        else:
            if wait_result.questions:
                print(
                    "\n".join(
                        mailbox_cli_output.outbox_question_text_lines(
                            wait_result.questions
                        )
                    )
                )
            if wait_result.notices:
                print(
                    "\n".join(
                        mailbox_cli_output.outbox_notice_text_lines(wait_result.notices)
                    )
                )
            if wait_result.supervision_ends:
                print(
                    "\n".join(
                        mailbox_cli_output.outbox_supervision_end_text_lines(
                            wait_result.supervision_ends
                        )
                    )
                )
                if supervision_id is not None and not own_supervision_ends:
                    print(
                        "No supervision end record matches supervision id "
                        f"{supervision_id}; supervision continues."
                    )
            print(f"Next cursor: {wait_result.next_since}")
    return 0


def _open_questions_in(
    wait_result: mailbox_cli_models.OutboxWaitResult,
    plan_dir: Path,
    state_dir: str | None,
) -> tuple[mailbox_models.MailboxMessage, ...]:
    """Return the woken questions a reply can still close.

    Scoped like ``asl status``: a question from an earlier run, or a ``--wait``
    question whose wait already ended, refuses every reply, so reporting it as
    ACTION would hand the supervisor a question it cannot answer.
    """
    if not wait_result.questions:
        return ()
    from . import intervention_facts

    open_ids = intervention_facts.open_question_ids(
        plan_dir, state_dir=state_dir, view=wait_result.mailbox_view
    )
    return tuple(
        question for question in wait_result.questions if question.id in open_ids
    )


def _carried_question_ids(
    plan_dir: Path, state_dir: str | None, since: int
) -> frozenset[str]:
    """The current run's open questions an earlier call already moved past.

    The cursor advances on every wake, before anyone has replied, so a reply
    that failed or a call that was cut off would leave the question behind the
    cursor and every later call IDLE. Scoped to the current run, so a question
    a crashed run left open never returns.
    """
    from . import intervention_facts

    open_ids = intervention_facts.open_question_ids(plan_dir, state_dir=state_dir)
    if not open_ids or since <= 0:
        return frozenset()
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except OSError, ValueError, mailbox_models.MailboxError:
        return frozenset()
    return frozenset(
        message.id
        for message in view.unanswered_questions
        if message.id in open_ids and message.seq <= since
    )


def outbox_supervise_from_args(args: argparse.Namespace) -> int:
    """Long-poll one launch's supervision cursor and print a single token.

    Every wakeup that a supervisor cannot act on — another launch's terminal
    record, or a question batch that already closed — advances the persisted
    cursor and re-enters the wait on the remaining budget instead of returning.
    Absorbing them here is what keeps a healthy run silent. A question already
    behind the cursor that is still open is reported again after
    :data:`~.mailbox_cli_constants.SUPERVISE_CARRIED_QUESTION_DELAY_SECONDS`.
    """
    supervision_id, supervision_id_ok = mailbox_cli_args.validated_supervision_id(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if not supervision_id_ok:
        return 1
    if supervision_id is None:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
            text_label="outbox supervise",
            reason="invalid-supervision-id",
            error="--supervision-id is required",
        )
        return 1
    timeout = mailbox_cli_args.validated_timeout(
        args,
        value=args.timeout,
        option="--timeout",
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if timeout is None:
        return 1
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if context is None:
        return 1

    plan_dir = context.snapshot.plan_dir
    mailbox_cursors.prune_supervision_cursors(plan_dir, state_dir=args.state_dir)
    since = mailbox_cursors.read_supervision_cursor(
        plan_dir,
        supervision_id,
        state_dir=args.state_dir,
    )
    first_seen_at = mailbox_cursors.read_supervision_first_seen_at(
        plan_dir,
        supervision_id,
        state_dir=args.state_dir,
    )
    if first_seen_at is None:
        # Anchor the grace window before the first wait rather than after it:
        # a poll that dies mid-wait would otherwise restart the window and let
        # a mis-launch stay invisible for as long as polling continues.
        first_seen_at = datetime.now(UTC)
        mailbox_cursors.write_supervision_cursor(
            plan_dir,
            supervision_id,
            since,
            state_dir=args.state_dir,
            first_seen_at=first_seen_at,
        )
    supervisor_id = str(uuid.uuid4())
    liveness_probe = mailbox_cli_status.supervision_liveness_probe(
        plan_dir,
        args.state_dir,
        supervision_id,
        first_seen_at=first_seen_at,
    )
    cursor = since
    deadline = time.monotonic() + timeout
    carried_ids = _carried_question_ids(plan_dir, args.state_dir, since)
    if carried_ids:
        deadline = min(
            deadline,
            time.monotonic()
            + mailbox_cli_constants.SUPERVISE_CARRIED_QUESTION_DELAY_SECONDS,
        )
    renewal_failures = 0
    renewal_recoveries = 0
    lease_degraded = False
    while True:
        try:
            wait_result = mailbox_cli_waits.wait_for_questions(
                plan_dir,
                state_dir=args.state_dir,
                since=cursor,
                timeout=max(0.0, deadline - time.monotonic()),
                supervisor_id=supervisor_id,
                include=mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION,
                liveness_probe=liveness_probe,
            )
        except mailbox_models.MailboxError as error:
            mailbox_cli_output.print_mailbox_error(
                args,
                command_name=mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
                text_label="outbox supervise",
                error=error,
            )
            return 1
        renewal_failures += wait_result.lease_supervision.renewal_failures
        renewal_recoveries += wait_result.lease_supervision.renewal_recoveries
        lease_degraded = wait_result.lease_supervision.degraded
        cursor = wait_result.next_since
        mailbox_cursors.write_supervision_cursor(
            plan_dir,
            supervision_id,
            cursor,
            state_dir=args.state_dir,
            first_seen_at=first_seen_at,
        )
        mailbox_cli_agent_mux.notify_agent_mux_mailbox_question(
            context,
            mailbox_cli_projections.outbox_question_projection(
                context, args, wait_result
            ),
        )
        own_supervision_ends = tuple(
            supervision_end
            for supervision_end in wait_result.supervision_ends
            if supervision_end.supervision_id == supervision_id
        )
        open_questions = _open_questions_in(wait_result, plan_dir, args.state_dir)
        if own_supervision_ends or open_questions:
            break
        if wait_result.lost_reason is not None:
            break
        if time.monotonic() >= deadline:
            break

    if (
        carried_ids
        and not own_supervision_ends
        and not open_questions
        and wait_result.lost_reason is None
        and wait_result.mailbox_view is not None
    ):
        from . import intervention_facts

        # The carry delay can outlive a ``--wait`` question's wait, so recheck
        # that a reply can still close each carried question.
        still_open = intervention_facts.open_question_ids(
            plan_dir, state_dir=args.state_dir, view=wait_result.mailbox_view
        )
        open_questions = tuple(
            question
            for question in wait_result.mailbox_view.unanswered_questions
            if question.id in carried_ids and question.id in still_open
        )

    run_evidence: dict[str, Any] | None = None
    if own_supervision_ends:
        terminal = own_supervision_ends[-1]
        state_token = mailbox_cli_constants.SUPERVISE_TOKEN_DONE
        token = (
            f"{mailbox_cli_constants.SUPERVISE_TOKEN_DONE} outcome="
            f"{terminal.supervision_outcome} seq={terminal.seq}"
        )
        text_lines = mailbox_cli_output.outbox_supervision_end_text_lines(
            own_supervision_ends
        )
    elif open_questions:
        state_token = mailbox_cli_constants.SUPERVISE_TOKEN_ACTION
        token = (
            f"{mailbox_cli_constants.SUPERVISE_TOKEN_ACTION} "
            f"questions={len(open_questions)}"
        )
        text_lines = mailbox_cli_output.outbox_question_text_lines(open_questions)
    elif wait_result.lost_reason is not None:
        state_token = mailbox_cli_constants.SUPERVISE_TOKEN_LOST
        token = (
            f"{mailbox_cli_constants.SUPERVISE_TOKEN_LOST} "
            f"reason={wait_result.lost_reason}"
        )
        run_evidence = mailbox_cli_status.supervision_run_evidence(
            plan_dir, args.state_dir
        )
        text_lines = mailbox_cli_output.outbox_lost_text_lines(
            plan_dir,
            wait_result.lost_reason,
            run_evidence,
        )
    else:
        state_token = mailbox_cli_constants.SUPERVISE_TOKEN_IDLE
        token = mailbox_cli_constants.SUPERVISE_TOKEN_IDLE
        text_lines = []

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_OUTBOX_SUPERVISE,
        plan_dir=str(plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        lease_path=str(mailbox_cursors.lease_path_for_plan(plan_dir, args.state_dir)),
        cursor_path=str(
            mailbox_cursors.supervision_cursor_path_for_plan(
                plan_dir,
                supervision_id,
                args.state_dir,
            )
        ),
        supervisor_id=wait_result.supervisor_id,
        since=since,
        next_since=cursor,
        timeout=timeout,
        timed_out=state_token == mailbox_cli_constants.SUPERVISE_TOKEN_IDLE,
        include=mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION,
        state=state_token,
        questions=[
            mailbox_cli_output.message_payload(question) for question in open_questions
        ],
        notices=[],
        supervision_ends=[
            mailbox_cli_output.message_payload(supervision_end)
            for supervision_end in own_supervision_ends
        ],
        supervision_ended=bool(own_supervision_ends),
        supervision_id=supervision_id,
        lost_reason=wait_result.lost_reason,
        run_evidence=run_evidence,
        lease_supervision=mailbox_cli_models.LeaseSupervision(
            degraded=lease_degraded,
            renewal_failures=renewal_failures,
            renewal_recoveries=renewal_recoveries,
        ).to_payload(),
    )
    if args.json:
        responses.print_json(payload)
        return 0
    if renewal_failures:
        health = (
            "the latest renewal remains degraded"
            if lease_degraded
            else "a later renewal recovered"
        )
        print(
            "Warning: supervisor lease renewal failed "
            f"{renewal_failures} time(s); {health}.",
            file=sys.stderr,
        )
    print(token)
    if text_lines:
        print("\n".join(text_lines))
    return 0

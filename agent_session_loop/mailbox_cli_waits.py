"""The outbox question wait loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from . import (
    mailbox,
    mailbox_cli_constants,
    mailbox_cli_models,
    mailbox_cli_status,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
)


def wait_for_questions(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None,
    since: int,
    timeout: float,
    supervisor_id: str,
    include: str = mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS,
    liveness_probe: Callable[[], str | None] | None = None,
) -> mailbox_cli_models.OutboxWaitResult:
    started = time.monotonic()
    deadline = started + timeout
    next_lease_at = started + mailbox_cli_constants.LEASE_RENEW_INTERVAL_SECONDS
    # The probe forks `ps`, so it rides the lease-renewal interval rather than
    # the mailbox poll. It starts due, because a short wait that never reaches
    # the first renewal must still be able to report a helper that is gone.
    next_probe_at = started
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
    last_successful_view: mailbox_snapshot.MailboxView | None = None
    last_read_error: mailbox_models.MailboxError | None = None
    last_read_token: object = mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
    lease_degraded = False
    lease_renewal_failures = 0
    lease_renewal_recoveries = 0
    kinds = {
        mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS: (
            mailbox_vocabulary.KIND_QUESTION,
        ),
        mailbox_cli_constants.OUTBOX_INCLUDE_NOTICES: (mailbox_vocabulary.KIND_NOTICE,),
        mailbox_cli_constants.OUTBOX_INCLUDE_ALL: (
            mailbox_vocabulary.KIND_QUESTION,
            mailbox_vocabulary.KIND_NOTICE,
            mailbox_vocabulary.KIND_SUPERVISION_END,
        ),
        mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION: (
            mailbox_vocabulary.KIND_QUESTION,
            mailbox_vocabulary.KIND_SUPERVISION_END,
        ),
    }[include]

    # Initial establishment remains fail-closed: without it the caller has no
    # signal that supervision ever started. Only later renewals may degrade.
    landed_lease = mailbox_cli_status.write_supervisor_lease(
        plan_dir,
        state_dir,
        supervisor_id,
    )
    landed_supervisor_id = landed_lease.supervisor_id
    while True:
        current_token = mailbox.mailbox_change_token(mailbox_path)
        if (
            last_read_token is mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            try:
                view = mailbox.read_mailbox(
                    plan_dir,
                    state_dir=state_dir,
                )
            except mailbox_models.MailboxError as error:
                if error.reason != mailbox_constants.ERROR_MAILBOX_READ:
                    raise
                last_read_error = error
            else:
                last_successful_view = view
                last_read_error = None
                last_read_token = current_token
                selected = view.messages_since(since, kinds=kinds)
                if selected:
                    return mailbox_cli_models.OutboxWaitResult(
                        questions=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox_vocabulary.KIND_QUESTION
                        ),
                        notices=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox_vocabulary.KIND_NOTICE
                        ),
                        supervision_ends=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox_vocabulary.KIND_SUPERVISION_END
                        ),
                        include=include,
                        supervisor_id=landed_supervisor_id,
                        next_since=max(since, view.last_seq),
                        timed_out=False,
                        lease_supervision=mailbox_cli_models.LeaseSupervision(
                            degraded=lease_degraded,
                            renewal_failures=lease_renewal_failures,
                            renewal_recoveries=lease_renewal_recoveries,
                        ),
                        mailbox_view=view,
                    )

        now = time.monotonic()
        # After the mailbox read, so a woken record always outranks a lost
        # helper: the reader above has already returned anything selected.
        if liveness_probe is not None and now >= next_probe_at:
            lost_reason = liveness_probe()
            next_probe_at = now + mailbox_cli_constants.LEASE_RENEW_INTERVAL_SECONDS
            if lost_reason is not None:
                return mailbox_cli_models.OutboxWaitResult(
                    questions=(),
                    notices=(),
                    supervision_ends=(),
                    include=include,
                    supervisor_id=landed_supervisor_id,
                    next_since=max(
                        since,
                        0
                        if last_successful_view is None
                        else last_successful_view.last_seq,
                    ),
                    timed_out=False,
                    lease_supervision=mailbox_cli_models.LeaseSupervision(
                        degraded=lease_degraded,
                        renewal_failures=lease_renewal_failures,
                        renewal_recoveries=lease_renewal_recoveries,
                    ),
                    mailbox_view=last_successful_view,
                    lost_reason=lost_reason,
                )

        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            next_since = max(
                since,
                0 if last_successful_view is None else last_successful_view.last_seq,
            )
            return mailbox_cli_models.OutboxWaitResult(
                questions=(),
                notices=(),
                supervision_ends=(),
                include=include,
                supervisor_id=landed_supervisor_id,
                next_since=next_since,
                timed_out=True,
                lease_supervision=mailbox_cli_models.LeaseSupervision(
                    degraded=lease_degraded,
                    renewal_failures=lease_renewal_failures,
                    renewal_recoveries=lease_renewal_recoveries,
                ),
                mailbox_view=last_successful_view,
            )

        if now >= next_lease_at:
            try:
                landed_lease = mailbox_cli_status.write_supervisor_lease(
                    plan_dir,
                    state_dir,
                    supervisor_id,
                )
            except OSError, mailbox_models.MailboxError:
                lease_degraded = True
                lease_renewal_failures += 1
            else:
                if lease_degraded:
                    lease_renewal_recoveries += 1
                lease_degraded = False
                landed_supervisor_id = landed_lease.supervisor_id
            next_lease_at = now + mailbox_cli_constants.LEASE_RENEW_INTERVAL_SECONDS
        sleep_for = min(
            mailbox_cli_constants.OUTBOX_WAIT_POLL_INTERVAL_SECONDS,
            deadline - now,
            max(0.0, next_lease_at - now),
        )
        if sleep_for > 0:
            time.sleep(sleep_for)

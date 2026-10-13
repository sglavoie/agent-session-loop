"""Mailbox watcher that relays runner questions and notices during supervision."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from asl_autopilot_command_parse import _canonical_path
from asl_autopilot_constants import MAILBOX_LEASE_RENEW_INTERVAL, MAILBOX_POLL_INTERVAL
from asl_autopilot_lock_state import (
    _lock_created_at,
    _parse_timestamp,
    _runner_run_id,
    _state_written_after_lock,
)
from asl_autopilot_records import (
    _notice_progress_record,
    _question_progress_record,
    _receipt_progress_record,
    _wait_end_progress_record,
)

from agent_session_loop import mailbox as _mailbox
from agent_session_loop import mailbox_leases as _mailbox_leases
from agent_session_loop import mailbox_models as _mailbox_models
from agent_session_loop import mailbox_vocabulary as _mailbox_vocabulary


class _MailboxWatcher:
    """Poll one plan mailbox with a durable cursor and renewable lease.

    The authoritative ``asl status`` payload supplies the canonical plan path,
    per-plan state directory, mailbox paths, and initial physical sequence.
    The watcher advances its cursor through every physical line while tracking
    questions and notices independently, so filtered or malformed lines cannot
    make a later poll repeat work. Initial and renewal lease failures keep
    observation armed, schedule a later retry, and emit bounded progress
    without affecting child process ownership. Mailbox read failures defer that
    poll so the runner can still fall back to ordinary terminal pause/resume.
    """

    def __init__(
        self,
        *,
        plan_dir: str,
        state_root: str,
        state_path: str,
        mailbox_path: str,
        lease_path: str,
        since: int,
        clock=time.monotonic,
        read_mailbox=_mailbox.read_mailbox,
        write_lease=_mailbox_leases.write_lease,
        supervisor_id: str | None = None,
        poll_interval: float = MAILBOX_POLL_INTERVAL,
        lease_interval: float = MAILBOX_LEASE_RENEW_INTERVAL,
        include_unanswered_backlog: bool = False,
    ) -> None:
        self.plan_dir = plan_dir
        self.state_root = state_root
        self.state_path = state_path
        self.mailbox_path = mailbox_path
        self.lease_path = lease_path
        self.supervisor_id = supervisor_id or str(uuid.uuid4())
        self.initial_seq = since
        self.last_seq = since
        self._clock = clock
        self._read_mailbox = read_mailbox
        self._write_lease = write_lease
        self._poll_interval = poll_interval
        self._lease_interval = lease_interval
        self._include_unanswered_backlog = include_unanswered_backlog
        self._next_poll_at: float | None = None
        self._next_lease_at: float | None = None
        self._questions: list[dict[str, Any]] = []
        self._notices: list[dict[str, Any]] = []
        self._observed_question_ids: set[str] = set()
        self._observed_notice_ids: set[str] = set()
        self._pending_questions: tuple[_mailbox_models.MailboxMessage, ...] = ()
        self._projection_seq = since
        self.run_id: str | None = None
        self._requires_lock_correlation = False
        self._progress_records: list[str] = []
        self._lease_degraded = False
        self._lease_renewal_failures = 0
        self._lease_renewal_recoveries = 0
        self.enabled = False

    def _open_questions(
        self,
        view,
    ) -> tuple[_mailbox_models.MailboxMessage, ...]:
        """Return unanswered questions the bound run can still consume."""
        if self.run_id is None:
            return ()
        return view.open_questions(self.run_id)

    def set_run_id(
        self,
        run_id: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """Bind the lease projection to one authoritative runner identity."""
        if not isinstance(run_id, str) or not run_id:
            return False
        if run_id == self.run_id:
            return True
        if self.run_id is not None:
            # A watcher can survive a bounded relaunch, but questions observed
            # for the previous run must never be projected onto its successor.
            self._observed_question_ids.clear()
            self._pending_questions = ()
        self.run_id = run_id
        self._requires_lock_correlation = False
        if self.enabled:
            resolved_now = self._clock() if now is None else now
            self._renew_lease(resolved_now)
        return True

    def observe_status(
        self,
        status: object,
        *,
        now: float | None = None,
    ) -> bool:
        """Adopt a run id only when status attributes state to the live lock."""
        if not isinstance(status, dict):
            return False
        run_id = _runner_run_id(status)
        if run_id is None:
            return False
        lock_created_at = _lock_created_at(status)
        if self._requires_lock_correlation and (
            _parse_timestamp(lock_created_at) is None
            or not _state_written_after_lock(status, lock_created_at)
        ):
            return False
        if _parse_timestamp(
            lock_created_at
        ) is not None and not _state_written_after_lock(status, lock_created_at):
            return False
        return self.set_run_id(run_id, now=now)

    @classmethod
    def from_status(
        cls,
        status: object,
        **kwargs,
    ) -> _MailboxWatcher | None:
        """Resolve a watcher from canonical paths advertised by status."""
        if not isinstance(status, dict) or not status.get("ok"):
            return None
        plan_dir = status.get("plan_dir")
        state_path = status.get("state_dir")
        mailbox_status = status.get("mailbox")
        if (
            not isinstance(plan_dir, str)
            or not plan_dir
            or not isinstance(state_path, str)
            or not state_path
            or not isinstance(mailbox_status, dict)
        ):
            return None
        mailbox_path = mailbox_status.get("path")
        lease_path = mailbox_status.get("lease_path")
        since = mailbox_status.get("last_seq")
        if (
            not isinstance(mailbox_path, str)
            or not mailbox_path
            or not isinstance(lease_path, str)
            or not lease_path
            or not isinstance(since, int)
            or isinstance(since, bool)
            or since < 0
        ):
            return None

        canonical_plan = _canonical_path(plan_dir)
        canonical_state = _canonical_path(state_path)
        canonical_mailbox = _canonical_path(mailbox_path)
        canonical_lease = _canonical_path(lease_path)
        if None in (
            canonical_plan,
            canonical_state,
            canonical_mailbox,
            canonical_lease,
        ):
            return None
        assert canonical_plan is not None
        assert canonical_state is not None
        assert canonical_mailbox is not None
        assert canonical_lease is not None
        # Both advertised mailbox files must live in the advertised per-plan
        # state directory. Refuse a contradictory payload rather than writing a
        # lease through a guessed root.
        if (
            os.path.dirname(canonical_mailbox) != canonical_state
            or os.path.dirname(canonical_lease) != canonical_state
        ):
            return None
        return cls(
            plan_dir=canonical_plan,
            state_root=os.path.dirname(canonical_state),
            state_path=canonical_state,
            mailbox_path=canonical_mailbox,
            lease_path=canonical_lease,
            since=since,
            **kwargs,
        )

    def establish(self, *, now: float | None = None) -> bool:
        """Arm observation and attempt the initial lease before launch.

        A transient write failure follows the periodic-renewal path: the
        watcher remains usable by the active chunk, reports degraded lease
        health, and retries at the normal renewal deadline.
        """
        resolved_now = self._clock() if now is None else now
        self.enabled = True
        self._next_poll_at = (
            resolved_now
            if self._include_unanswered_backlog
            else resolved_now + self._poll_interval
        )
        self._next_lease_at = resolved_now + self._lease_interval
        self._renew_lease(resolved_now)
        return True

    def prepare_for_launch(
        self,
        *,
        now: float | None = None,
        renew_lease: bool = True,
    ) -> bool:
        """Unbind stale run evidence immediately before a self-launched child.

        The durable physical cursor survives so old mailbox records are not
        replayed, but the previous run's identity and pending-question set must
        disappear before a successor can start. The next identity is accepted
        only from state proven to have been written after that child's live
        lock. A newly established watcher already wrote the unbound lease, so
        its caller may set ``renew_lease=False`` to avoid a duplicate write.
        """
        if not self.enabled:
            return False
        resolved_now = self._clock() if now is None else now
        self.run_id = None
        self._observed_question_ids.clear()
        self._pending_questions = ()
        self._include_unanswered_backlog = False
        self._requires_lock_correlation = True
        if not renew_lease:
            return True
        return self._renew_lease(resolved_now)

    def _renew_lease(self, resolved_now: float) -> bool:
        """Renew now while preserving observation after a write failure."""
        projection: dict[str, Any] = {}
        if self.run_id is not None:
            projection = {
                "run_id": self.run_id,
                "projection_seq": self._projection_seq,
                "unanswered_questions": len(self._pending_questions),
            }
            if self._pending_questions:
                latest = max(
                    self._pending_questions,
                    key=lambda question: question.seq,
                )
                projection.update(
                    {
                        "latest_unanswered_question_id": latest.id,
                        "latest_unanswered_question_created_at": (latest.created_at),
                    }
                )
        try:
            self._write_lease(
                self.plan_dir,
                self.supervisor_id,
                state_dir=self.state_root,
                **projection,
            )
        except OSError, _mailbox_models.MailboxError:
            self._lease_renewal_failures += 1
            if not self._lease_degraded:
                self._progress_records.append(
                    "MAILBOX WARN lease renewal failed; retry scheduled"
                )
            self._lease_degraded = True
            self._next_lease_at = resolved_now + self._lease_interval
            return False

        if self._lease_degraded:
            self._lease_renewal_recoveries += 1
            self._progress_records.append("MAILBOX lease renewal recovered")
        self._lease_degraded = False
        self._next_lease_at = resolved_now + self._lease_interval
        return True

    def next_deadline(self) -> float | None:
        """Return the next mailbox/lease wake deadline, if observation is live."""
        if (
            not self.enabled
            or self._next_poll_at is None
            or self._next_lease_at is None
        ):
            return None
        return min(self._next_poll_at, self._next_lease_at)

    def stop(self) -> None:
        """Stop future observation without deleting the last supervisor lease.

        The lease is intentionally left in place to age out through the normal
        freshness window.  Clearing the local deadlines makes supervision
        lifetime explicit: once the autopilot returns, this watcher can no
        longer renew the lease or read more mailbox records.
        """
        self.enabled = False
        self._next_poll_at = None
        self._next_lease_at = None

    def poll(
        self,
        *,
        now: float | None = None,
        force_read: bool = False,
    ) -> tuple[_mailbox_models.MailboxMessage, ...]:
        """Renew when due and return each newly observed question once.

        Attach-mode watchers use one initial immutable unanswered-question view
        so questions that predate the status cursor survive supervisor
        reconnects without replaying already-answered history. Notices are
        independently selected from the bound live run and deduplicated for the
        lifetime of this watcher, which replays that run's backlog on attach but
        permits at-least-once replay after a supervisor restart. ``force_read``
        supports the final drain after a supervised child exits, even when the
        regular poll deadline has not arrived yet.
        """
        if not self.enabled:
            return ()
        resolved_now = self._clock() if now is None else now
        assert self._next_poll_at is not None
        assert self._next_lease_at is not None

        questions: tuple[_mailbox_models.MailboxMessage, ...] = ()
        read_succeeded = False
        if force_read or resolved_now >= self._next_poll_at:
            self._next_poll_at = resolved_now + self._poll_interval
            try:
                view = self._read_mailbox(
                    self.plan_dir,
                    state_dir=self.state_root,
                )
            except OSError, _mailbox_models.MailboxError:
                pass
            else:
                read_succeeded = True
                prior_seq = self.last_seq
                if self._include_unanswered_backlog:
                    # The backlog is only meaningful once an identity is bound,
                    # so stay armed until then rather than dropping it.
                    questions = self._open_questions(view)
                    self._include_unanswered_backlog = self.run_id is None
                else:
                    questions = view.messages_since(
                        self.last_seq,
                        kinds=(_mailbox_vocabulary.KIND_QUESTION,),
                    )
                notices = tuple(
                    message
                    for message in view.messages
                    if message.kind == _mailbox_vocabulary.KIND_NOTICE
                    and message.run_id == self.run_id
                    and message.id not in self._observed_notice_ids
                )
                lifecycle = tuple(
                    message
                    for message in view.messages
                    if message.seq > prior_seq
                    and message.kind
                    in (
                        _mailbox_vocabulary.KIND_WAIT_END,
                        _mailbox_vocabulary.KIND_RECEIPT,
                    )
                )
                self.last_seq = max(self.last_seq, view.last_seq)
                self._observed_question_ids.update(
                    question.id for question in questions
                )
                self._observed_notice_ids.update(notice.id for notice in notices)
                pending_questions = tuple(
                    question
                    for question in self._open_questions(view)
                    if question.id in self._observed_question_ids
                )
                previous_pending_ids = {
                    question.id for question in self._pending_questions
                }
                pending_ids = {question.id for question in pending_questions}
                if pending_ids != previous_pending_ids:
                    # The reducer generation advances only when the projected
                    # pending set changes. Guidance, acknowledgements, rejected
                    # lines, and unchanged polls must not reassert a question
                    # the user explicitly cleared.
                    self._projection_seq = max(self._projection_seq, view.last_seq)
                self._pending_questions = pending_questions
                self._questions.extend(question.to_payload() for question in questions)
                self._notices.extend(notice.to_payload() for notice in notices)
                self._progress_records.extend(
                    _notice_progress_record(notice) for notice in notices
                )
                self._progress_records.extend(
                    _wait_end_progress_record(message)
                    if message.kind == _mailbox_vocabulary.KIND_WAIT_END
                    else _receipt_progress_record(message)
                    for message in lifecycle
                )
        if read_succeeded or resolved_now >= self._next_lease_at:
            # Every successful mailbox read republishes the projection. This
            # both bounds detection latency and repairs a lease temporarily
            # replaced by another supervisor mailbox command.
            self._renew_lease(resolved_now)
        return questions

    def drain_progress_records(self) -> tuple[str, ...]:
        """Return and clear watcher health records for the progress sink."""
        records = tuple(self._progress_records)
        self._progress_records.clear()
        return records

    def outbox_payload(self) -> dict[str, Any]:
        """Return the full-text structured response accumulated so far."""
        return {
            "command": "outbox-wait",
            "include": "all",
            "plan_dir": self.plan_dir,
            "mailbox_path": self.mailbox_path,
            "lease_path": self.lease_path,
            "supervisor_id": self.supervisor_id,
            "since": self.initial_seq,
            "next_since": self.last_seq,
            "questions": list(self._questions),
            "notices": list(self._notices),
            "lease_supervision": {
                "degraded": self._lease_degraded,
                "renewal_failures": self._lease_renewal_failures,
                "renewal_recoveries": self._lease_renewal_recoveries,
            },
        }


def _poll_mailbox_watcher(
    watcher: _MailboxWatcher,
    *,
    now: float,
    heartbeat,
    force_read: bool = False,
) -> tuple[_mailbox_models.MailboxMessage, ...]:
    """Poll one watcher and emit health plus bounded message previews."""
    if force_read:
        questions = watcher.poll(now=now, force_read=True)
    else:
        questions = watcher.poll(now=now)
    drain_progress = getattr(watcher, "drain_progress_records", None)
    if callable(drain_progress):
        for record in drain_progress():
            heartbeat(record)
    for question in questions:
        heartbeat(_question_progress_record(question))
    return questions

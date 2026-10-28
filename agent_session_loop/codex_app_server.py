"""A bounded JSON-RPC client for the Codex app-server transport.

``codex exec`` is a one-shot process: a prompt goes in on argv, output comes
back on stdout, and there is no way to say anything to the model once the turn
has begun. The app-server speaks a long-lived JSON-RPC conversation over the
same two pipes instead, and that conversation has ``turn/steer`` -- the one
call that hands a running turn new input while it is still running. Same-turn
guidance delivery is why this module exists; everything else here is the cost
of keeping the rest of the turn's guarantees intact while the transport
changes underneath them.

The protocol surface this module uses, all of it verified against
``codex-cli`` and the app-server JSON schema bundle:

- ``initialize`` request, then the ``initialized`` notification.
- ``thread/start``, whose response echoes the *effective* sandbox policy,
  model, and reasoning effort. That echo is checked, not trusted: an audit
  turn that did not get a read-only sandbox never gets to send a turn.
- ``thread/resume``, which restores one named thread and answers in the same
  shape. Its echo is checked harder than ``thread/start``'s, because a
  restored thread carries settings from the thread it restores rather than
  from the call that asked for it.
- ``turn/start``, whose response carries the accepted ``turnId``. Acceptance
  is the point of no return for the fallback in :mod:`.harnesses`.
- Streamed notifications (``turn/started``, ``item/*``, ``error``) until the
  terminal ``turn/completed``.
- ``turn/steer`` with a required ``expectedTurnId`` precondition.
- ``turn/interrupt``, which names the thread and the turn it stops. It is how
  a cancelled turn is recorded as *interrupted* by the side that owns the
  conversation, rather than as a turn that stopped answering.

Continuity. The thread identifier is learned from ``thread/start`` or
``thread/resume`` and handed to ``on_conversation`` *between* that call and
``turn/start``, which is the only window in which it can be made durable before
any work exists to be lost. A sink that raises, or a ``before_submit`` gate that
refuses, ends the conversation with nothing submitted -- and neither is a
protocol failure, so neither licenses the fallback in :mod:`.harnesses`.

Threading. Three threads touch a session and each has one job:

- the harness's stdout reader calls :meth:`feed` and nothing else;
- a driver thread owned by this module runs the handshake and the orderly
  shutdown;
- the mailbox poller calls :meth:`steer`.

``_state_lock`` guards the mutable state and the pending-request table and is
never held across a blocking wait. ``_write_lock`` serializes stdin writes,
which is the only resource the driver and the poller share.

Privacy. Prompts and guidance bodies go *out* over this transport and must not
come back in through the log: the app-server echoes every user message,
including a steered one, as a ``userMessage`` item. :meth:`feed` therefore
renders no user message at all. Nothing derived from a guidance body is
rendered, returned, or persisted.
"""

from __future__ import annotations

import threading

from . import guidance_delivery
from .codex_app_server_protocol import AppServerProtocol
from .codex_app_server_types import (
    _LIVENESS_POLL_SECONDS,
    _RESTORED_SETTINGS,
    _WORK_NOTIFICATIONS,
    ACTIVE_TURN_NOT_STEERABLE,
    CLIENT_NAME,
    CLIENT_TITLE,
    INTERRUPT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SANDBOX_DANGER_FULL_ACCESS,
    SANDBOX_READ_ONLY,
    SANDBOX_WORKSPACE_WRITE,
    SHUTDOWN_GRACE_SECONDS,
    STEER_TIMEOUT_SECONDS,
    TRANSPORT_APP_SERVER,
    TRANSPORT_EXEC,
    TURN_STATUS_COMPLETED,
    TURN_STATUS_FAILED,
    TURN_STATUS_INTERRUPTED,
    AppServerError,
    ConversationSink,
    RestoreRefused,
    SubmissionGate,
    SubmissionInFlightSink,
    SubmissionSink,
    TurnPlan,
    _ConversationNotRecorded,
    _LineDecoder,
    _Pending,
    _redacted_steer_detail,
    _RequestRejected,
    _steer_deferral_reason,
    _SubmissionRefused,
)

__all__ = [
    "TRANSPORT_APP_SERVER",
    "TRANSPORT_EXEC",
    "CLIENT_NAME",
    "CLIENT_TITLE",
    "REQUEST_TIMEOUT_SECONDS",
    "STEER_TIMEOUT_SECONDS",
    "INTERRUPT_TIMEOUT_SECONDS",
    "SHUTDOWN_GRACE_SECONDS",
    "_LIVENESS_POLL_SECONDS",
    "TURN_STATUS_COMPLETED",
    "TURN_STATUS_INTERRUPTED",
    "TURN_STATUS_FAILED",
    "SANDBOX_READ_ONLY",
    "SANDBOX_WORKSPACE_WRITE",
    "SANDBOX_DANGER_FULL_ACCESS",
    "ACTIVE_TURN_NOT_STEERABLE",
    "_RESTORED_SETTINGS",
    "ConversationSink",
    "SubmissionGate",
    "SubmissionSink",
    "SubmissionInFlightSink",
    "AppServerError",
    "_RequestRejected",
    "RestoreRefused",
    "_ConversationNotRecorded",
    "_SubmissionRefused",
    "_Pending",
    "_LineDecoder",
    "TurnPlan",
    "_WORK_NOTIFICATIONS",
    "_steer_deferral_reason",
    "_redacted_steer_detail",
    "CodexAppServerSession",
]


class CodexAppServerSession(AppServerProtocol):
    """One app-server conversation, scoped to exactly one harness turn.

    Constructed around a spawned process, driven by :meth:`start`, fed by the
    harness reader, and torn down by :meth:`shutdown`. The turn it runs is the
    only turn it will ever run: the thread is started or resumed, one turn is
    sent, and the session closes its own stdin as soon as that turn completes.

    ``resume_conversation_id`` names the thread to restore instead of starting
    a fresh one. It is a requirement, not a preference: a resume that cannot be
    honoured fails the turn, because the caller asking for it has state in that
    conversation and a fresh thread would silently discard it.
    """

    def start(self) -> None:
        """Begin the conversation on a background thread.

        Backgrounded because the answers arrive on the harness reader thread,
        which the caller has not started yet: the handshake has to be able to
        block without blocking the pipe it is waiting on.
        """
        driver = threading.Thread(
            target=self._drive,
            name="codex-app-server",
            daemon=True,
        )
        with self._state_lock:
            self._driver = driver
        driver.start()

    def _drive(self) -> None:
        try:
            self._handshake()
        except _ConversationNotRecorded as error:
            self._abandon_before_submission(conversation_error=str(error))
        except _SubmissionRefused as error:
            self._abandon_before_submission(submission_refused=str(error))
        except RestoreRefused as error:
            self._note_restore_refused(str(error))
            self._turn_finished.set()
        except AppServerError as error:
            self._note_protocol_error(str(error))
            self._turn_finished.set()
        else:
            self._turn_finished.wait()
        # A failed or cancelled handshake can leave the server alive too.
        # Give every outcome the same EOF grace and termination policy.
        self._end_process()

    def steer(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        """Hand ``request`` to the running turn and report what happened.

        Every steer states the turn it believes is active. The app-server
        rejects the call outright when that belief is wrong, which is what
        makes a ``delivered-live`` receipt a claim about *this* turn rather
        than about whichever turn happened to be running.
        """
        with self._state_lock:
            thread_id = self._thread_id
            turn_id = self._turn_id
            finished = self._turn_status is not None
        if thread_id is None or turn_id is None:
            return guidance_delivery.deferred(guidance_delivery.REASON_NO_ACTIVE_TURN)
        if finished or self._turn_finished.is_set():
            return guidance_delivery.deferred(guidance_delivery.REASON_STALE_TURN)
        try:
            self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": request.text}],
                },
                timeout=self._steer_timeout,
            )
        except AppServerError as error:
            return guidance_delivery.deferred(
                _steer_deferral_reason(str(error)),
                detail=_redacted_steer_detail(str(error), turn_id=turn_id),
            )
        return guidance_delivery.DELIVERED

    def shutdown(self) -> None:
        """Release the conversation once the harness process is gone.

        Runs on the turn's own thread from the harness ``finally``, so it must
        not wait on anything unbounded: the driver is joined with a bound, and
        a driver that outlives it is a daemon thread whose only remaining act
        would be closing an already-closed pipe.

        A turn still running at this point is one the harness stopped waiting
        for, so it is interrupted before the pipe is taken away -- bounded by
        the interrupt timeout, and skipped outright (in the usual case) when
        the turn has a terminal status or the process is already gone.
        """
        self.interrupt()
        self._turn_finished.set()
        self._fail_pending("app-server session is closed")
        self._close_stdin()
        with self._state_lock:
            driver = self._driver
        if driver is not None:
            driver.join(timeout=self._shutdown_grace)

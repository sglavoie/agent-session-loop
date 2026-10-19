"""App-server protocol constants, errors, request state, and turn plans."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import guidance_delivery, restore_diagnosis

#: The transport discriminator persisted alongside ``argv``.
TRANSPORT_APP_SERVER = "app-server"
#: The legacy one-shot ``codex exec`` transport.
TRANSPORT_EXEC = "exec"

#: Client identity sent with ``initialize``. The app-server folds this into its
#: user agent, so it is what a Codex-side log shows the turn was launched by.
CLIENT_NAME = "agent-session-loop"
CLIENT_TITLE = "ASL"

#: How long a handshake request may take before the turn is declared unusable.
#: Generous, because ``thread/start`` boots the configured MCP servers.
REQUEST_TIMEOUT_SECONDS = 120.0

#: How long ``turn/steer`` may take. Deliberately far tighter than the
#: handshake: the caller is a poll tick holding the live-delivery endpoint
#: lock, so a slow steer delays closing the endpoint at end of turn.
STEER_TIMEOUT_SECONDS = 10.0

#: How long ``turn/interrupt`` may take. Bounded well under the shutdown grace
#: because every caller has already decided the turn is over: the interrupt is
#: how the conversation is *told* so, and a forced termination stands behind it
#: either way.
INTERRUPT_TIMEOUT_SECONDS = 5.0

#: How long the app-server gets to exit on its own after its stdin is closed
#: at end of turn. It exits in milliseconds in practice; this only bounds what
#: a wedged one costs before the driver terminates it.
SHUTDOWN_GRACE_SECONDS = 10.0

#: How often a bounded wait re-checks whether the process is still alive. A
#: dead app-server can never answer, so noticing quickly is what keeps a
#: handshake failure from costing the full request timeout.
_LIVENESS_POLL_SECONDS = 0.05

#: Turn statuses the app-server reports on ``turn/completed``.
TURN_STATUS_COMPLETED = "completed"
TURN_STATUS_INTERRUPTED = "interrupted"
TURN_STATUS_FAILED = "failed"

#: Sandbox policy discriminators as the ``thread/start`` response echoes them.
SANDBOX_READ_ONLY = "readOnly"
SANDBOX_WORKSPACE_WRITE = "workspaceWrite"
SANDBOX_DANGER_FULL_ACCESS = "dangerFullAccess"

#: The app-server's own name for "this turn exists but cannot take steering",
#: e.g. a `/review` or a manual `/compact` turn.
ACTIVE_TURN_NOT_STEERABLE = "activeTurnNotSteerable"

#: Requested thread settings re-checked against a ``thread/resume`` echo. Only
#: the ones whose disagreement changes what the turn is allowed to do: a
#: restored thread that runs somewhere else, or under a different approval
#: policy, is not the turn that was asked for. ``model`` is deliberately absent
#: -- the server canonicalizes it, so it is reported through ``observed_model``
#: rather than enforced.
_RESTORED_SETTINGS = ("cwd", "approvalPolicy")

#: What a session calls with the native thread identifier it just learned,
#: before it submits anything. Raising from it abandons the turn.
ConversationSink = Callable[[str], None]

#: What a session calls immediately before submitting work, as its last chance
#: to be told not to. Raising from it abandons the turn.
SubmissionGate = Callable[[], None]

#: What a session calls once the app-server has accepted ``turn/start``, which
#: is the first moment the prompt is demonstrably the worker's rather than this
#: process's. Unlike the two above it cannot refuse anything -- the turn is
#: already running -- so a failure is swallowed by the caller's own seam.
SubmissionSink = Callable[[], None]

#: What a session calls in the instant before ``turn/start`` goes out, which is
#: the last moment this process can still say the prompt has not left it. The
#: acceptance that follows may never arrive, so this is what keeps the silence
#: after it from reading as a turn that submitted nothing.
SubmissionInFlightSink = Callable[[], None]


class AppServerError(Exception):
    """The app-server conversation cannot continue.

    Raised for a transport failure (a dead process, a closed pipe, a request
    that never answered) and for a protocol failure (an error response, an
    unparseable or contradictory message). One exception type, because every
    caller does the same thing with both: stop driving the turn and record why.
    """


class _RequestRejected(AppServerError):
    """The app-server answered a request with an error response.

    Distinct from the transport failures that share :class:`AppServerError` --
    a dead process, a closed pipe, a request that never answered -- because only
    this one is the app-server *stating* something. What it states can outlive
    the turn; a broken pipe says nothing about the conversation behind it.

    It carries the error structured rather than only rendered, because the one
    caller that acts durably on a rejection has to weigh the code against the
    message instead of pattern-matching the sentence the two were folded into.
    """

    def __init__(
        self, message: str, *, rejection: restore_diagnosis.ProviderRejection
    ) -> None:
        super().__init__(message)
        self.rejection = rejection


class RestoreRefused(AppServerError):
    """The app-server would not restore the thread this turn was told to resume.

    A subclass because every ``AppServerError`` handler should keep treating it
    as one: nothing ran, the turn is over, and the fallback stays shut. What the
    separate type adds is durability -- this failure is a statement about the
    provider's own history, not about this turn, so retrying the same resume
    later cannot succeed either.

    Deliberately narrow, and narrow twice over. Only the ``thread/resume``
    request itself answering with an error can raise it, and only when that
    error is a supported missing-history diagnosis under
    :mod:`~agent_session_loop.restore_diagnosis` -- so the internal-error code a
    server wraps every unhandled failure in, a temporary outage, an expired
    credential, and a refused workspace all stay ordinary failures that leave
    the saved conversation exactly where it was. A thread that came back under a
    different id, a widened sandbox, and drifted settings are excluded for the
    same reason: they are thread-level disagreements about a conversation the
    provider still holds, and discarding that history because this turn could
    not use it would throw away recoverable work.
    """


class _ConversationNotRecorded(Exception):
    """The thread identifier could not be made durable, so nothing was sent.

    Deliberately *not* an :class:`AppServerError`. The app-server did nothing
    wrong here, and re-running the turn on the legacy transport would spend the
    very work this refusal exists to withhold -- so this must never become a
    ``protocol_error`` and must never open the fallback window.
    """


class _SubmissionRefused(Exception):
    """The turn was cancelled before ``turn/start``, so nothing was sent.

    Not an :class:`AppServerError` either, and for the same reason: the
    conversation is healthy, and re-running the turn would defeat the
    cancellation that stopped it.
    """


@dataclass
class _Pending:
    """One in-flight request, waiting for the reader thread to answer it.

    A request can stop waiting for two unrelated reasons, and they are kept in
    two fields rather than one string: the app-server *answered* with an error,
    or the transport failed underneath it. Only the first is the app-server
    stating something, and folding both into ``error`` is what let a broken pipe
    during ``thread/resume`` read as a provider disowning its own history.
    """

    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None
    rejection: restore_diagnosis.ProviderRejection | None = None

    def resolve(
        self,
        *,
        result: Any = None,
        error: str | None = None,
        rejection: restore_diagnosis.ProviderRejection | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.rejection = rejection
        self.event.set()


class _LineDecoder:
    """Reassemble complete lines from arbitrarily fragmented reads.

    The harness reader hands over whatever ``readline`` produced, which is
    normally one whole line but is not guaranteed to be: a long JSON message
    can arrive split across reads. Buffering here means a fragment is never
    parsed as a message and never mistaken for passthrough text.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self._buffer += chunk
        if "\n" not in self._buffer:
            return []
        *lines, self._buffer = self._buffer.split("\n")
        return lines

    def flush(self) -> list[str]:
        """Return whatever the last read left behind without a newline."""
        remainder, self._buffer = self._buffer, ""
        return [remainder] if remainder else []


@dataclass(frozen=True)
class TurnPlan:
    """The prompt-free description of the conversation one turn will have.

    Persisted and printed in place of the argv that used to carry the whole
    invocation: with the app-server, argv says only which binary runs, and
    *this* is where the access profile, model, effort, and writable roots
    actually live. It never contains the prompt or any guidance body, so it is
    safe everywhere argv was safe and safer than argv ever was.
    """

    thread_params: Mapping[str, Any]
    turn_params: Mapping[str, Any]
    #: The sandbox discriminator this turn requires the app-server to confirm.
    expected_sandbox: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "thread_params": json.loads(json.dumps(self.thread_params)),
            "turn_params": json.loads(json.dumps(self.turn_params)),
            "expected_sandbox": self.expected_sandbox,
        }


#: Notifications that prove the turn itself has begun doing something. Any
#: ``item/*`` notification counts too; these are the ones that do not match
#: that prefix.
_WORK_NOTIFICATIONS = frozenset(
    {
        "turn/started",
        "turn/completed",
        "turn/diff/updated",
        "turn/plan/updated",
        "command/exec/outputDelta",
        "process/outputDelta",
        "process/exited",
        "error",
    }
)


def _steer_deferral_reason(detail: str) -> str:
    """Map one steer failure onto the ledger's fixed deferral vocabulary.

    The three outcomes are kept distinct because they mean different things to
    a supervisor: the turn moved on, the turn is one that cannot be steered at
    all, or the channel itself failed.
    """
    if ACTIVE_TURN_NOT_STEERABLE in detail:
        return guidance_delivery.REASON_TRANSPORT_UNAVAILABLE
    if "expected active turn id" in detail:
        return guidance_delivery.REASON_STALE_TURN
    return guidance_delivery.REASON_TRANSPORT_UNAVAILABLE


def _redacted_steer_detail(detail: str, *, turn_id: str) -> str:
    """Return a diagnosis string that cannot carry a guidance body.

    ``detail`` is never persisted, but it is the one value on this path that
    is derived from a failed request, and that request carried the guidance
    text. Only the app-server's own vocabulary survives.
    """
    if ACTIVE_TURN_NOT_STEERABLE in detail:
        return ACTIVE_TURN_NOT_STEERABLE
    if "expected active turn id" in detail:
        return f"expected turn {turn_id} is no longer active"
    if "timed out" in detail:
        return "turn/steer timed out"
    return "turn/steer failed"

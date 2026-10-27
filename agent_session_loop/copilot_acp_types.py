# mypy: ignore-errors
# ruff: noqa
"""A bounded Agent Client Protocol client for the Copilot worker transport.

``copilot -p <prompt>`` is a one-shot process: the prompt goes out on argv, the
answer comes back as prose on stdout, and the only thing the turn's outcome can
be read from afterwards is an exit code. ``copilot --acp`` speaks
newline-delimited JSON-RPC 2.0 over the same two pipes instead, and that
conversation says out loud what the one-shot form could only be inferred from:
which session the turn ran in, which model and reasoning effort the server
actually applied, every tool call as it happens, and a terminal stop reason.
Turning those into an honest :class:`~.harnesses.HarnessResult` is why this
module exists.

The protocol surface used here, all of it verified against ``copilot`` 1.0.82
except where noted:

- ``initialize``, which negotiates the protocol version and states what *this*
  client can do. It advertises no filesystem and no terminal capability, so the
  agent has no way to ask ASL to read, write, or run anything on its behalf --
  the turn's access stays exactly what the launch argv granted it.
- ``session/new``, whose response carries the session identifier and echoes the
  effective ``model`` and ``reasoning_effort`` config options. Those are the
  launch flags coming back, which is the only place their parity can be
  checked rather than assumed.
- ``session/prompt``, which submits the turn's work and answers once with a
  terminal ``stopReason``.
- ``session/update`` notifications, streamed until the prompt answers.
- ``session/request_permission``, a server-to-client *request*: see
  :class:`PermissionPolicy` for why it is answered explicitly and why it can
  never grant more than the launch argv already did.
- ``session/load``, which restores one *named* session instead of creating a
  fresh one, restating the same configuration ``session/new`` states. It is
  gated on the ``loadSession`` capability ``initialize`` advertises: an agent
  that does not offer it makes the turn fail rather than quietly start a new
  conversation under an old conversation's name.
- ``session/cancel``, a notification that stops the turn in flight. There is
  nothing to answer it with; the acknowledgment is the pending
  ``session/prompt`` coming back with ``stopReason: "cancelled"``.

Replay. ``session/load`` streams the whole prior conversation back as
``session/update`` notifications *before* it answers, and none of it is this
turn. Everything that arrives while the load is in flight is counted and
rendered as nothing: it is not the turn's log, not the turn's final message,
not new activity by the resumed worker, and not an acknowledgment of anything
this turn asked for. Only the ``session/prompt`` response can complete a turn,
and replay cannot produce one.

Threading. Two threads touch a session and each has one job:

- the harness's stdout reader calls :meth:`feed`, which renders updates and
  answers server-to-client requests inline;
- a driver thread owned by this module runs the handshake, the submission, and
  the orderly shutdown.

``_state_lock`` guards the mutable state and the pending-request table and is
never held across a blocking wait. ``_write_lock`` serializes stdin writes,
which is the only resource the driver and the reader share.

Privacy. The prompt goes *out* over this transport and must not come back in
through the log. Copilot echoes it twice -- as a ``user_message_chunk`` and as
the ``title`` of a ``session_info_update`` -- and :meth:`feed` renders neither.
"""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import __version__, restore_diagnosis

#: The transport discriminator persisted alongside ``argv``.
TRANSPORT_ACP = "acp"

#: The ACP major version this client speaks. ``initialize`` sends it and the
#: agent answers with the version it will actually use; anything else is a
#: conversation this client cannot hold.
PROTOCOL_VERSION = 1

#: Client identity sent with ``initialize``.
CLIENT_NAME = "agent-session-loop"
CLIENT_TITLE = "ASL"

#: The agent capability that decides whether ``session/load`` may be called at
#: all. Read from ``initialize``'s own answer rather than assumed from the
#: version, because it is the agent -- not the protocol -- that either kept the
#: session on disk or did not.
AGENT_CAPABILITY_LOAD_SESSION = "loadSession"

#: How long a handshake request may take before the turn is declared unusable.
#: Generous, because ``session/new`` boots the agent's configured MCP servers.
REQUEST_TIMEOUT_SECONDS = 120.0

#: How long the agent gets to exit on its own after its stdin is closed at end
#: of turn. It exits in milliseconds in practice; this only bounds what a
#: wedged one costs before the driver terminates it.
SHUTDOWN_GRACE_SECONDS = 10.0

#: How often a bounded wait re-checks whether the process is still alive. A
#: dead agent can never answer, so noticing quickly is what keeps a handshake
#: failure from costing the full request timeout.
_LIVENESS_POLL_SECONDS = 0.05

#: ``session/prompt`` is the turn, so it gets no deadline of its own: the
#: harness timeout is what bounds a turn, and a second bound here would kill
#: long work that was proceeding normally.
_NO_DEADLINE = math.inf

#: Terminal ``stopReason`` values ``session/prompt`` answers with. ``end_turn``
#: is the only one that means the agent finished what it was asked to do.
STOP_REASON_END_TURN = "end_turn"
STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_REFUSAL = "refusal"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_MAX_TURN_REQUESTS = "max_turn_requests"

#: Permission option kinds, as ``session/request_permission`` offers them. The
#: two ``_always`` kinds are named here only so the responder can refuse to
#: pick them.
PERMISSION_ALLOW_ONCE = "allow_once"
PERMISSION_ALLOW_ALWAYS = "allow_always"
PERMISSION_REJECT_ONCE = "reject_once"
PERMISSION_REJECT_ALWAYS = "reject_always"

#: Option kinds this client will never select, whatever the request. Both make
#: one answer stand for every later request, which is the blanket approval an
#: unattended turn must not be able to hand out -- and ``reject_always`` is
#: refused for the same reason in the other direction: a standing refusal is a
#: decision about calls nobody has seen yet.
_STANDING_KINDS = frozenset({PERMISSION_ALLOW_ALWAYS, PERMISSION_REJECT_ALWAYS})

#: The decisions the responder records, in the vocabulary the turn's log uses.
DECISION_ALLOWED = "allowed"
DECISION_REJECTED = "rejected"
DECISION_CANCELLED = "cancelled"

#: JSON-RPC's "no such method", answered to any client-side method this turn
#: did not advertise a capability for.
_METHOD_NOT_FOUND = -32601

#: JSON-RPC's "internal error", answered to a request this client failed to
#: handle, so the agent is never left waiting on an answer that is not coming.
_INTERNAL_ERROR = -32603

#: What a session calls immediately before submitting work, as its last chance
#: to be told not to. Raising from it abandons the turn.
SubmissionGate = Callable[[], None]

#: What a session calls with the identifier of the session it will run in,
#: between learning it and submitting any work. Raising from it abandons the
#: turn: a conversation nothing recorded is one no later run could continue.
ConversationSink = Callable[[str], None]

#: What a session calls once ``session/prompt`` has actually been written to the
#: agent, which is the first moment the prompt is demonstrably the worker's
#: rather than this process's. Unlike the two above it cannot refuse anything --
#: the request is already on the wire -- so a failure is swallowed.
SubmissionSink = Callable[[], None]


class AcpError(Exception):
    """The ACP conversation cannot continue.

    Raised for a transport failure (a dead process, a closed pipe, a request
    that never answered) and for a protocol failure (an error response, an
    unparseable or contradictory message). One exception type, because every
    caller does the same thing with both: stop driving the turn and record why.
    """


class _RequestRejected(AcpError):
    """The agent answered a request with an error response.

    Distinct from the transport failures that share :class:`AcpError` -- a dead
    process, a closed pipe, a request that never answered -- because only this
    one is the agent *stating* something. What it states can outlive the turn; a
    broken pipe says nothing about the conversation behind it.

    It carries the error structured rather than only rendered, because the one
    caller that acts durably on a rejection has to weigh the code against the
    message instead of pattern-matching the sentence the two were folded into.
    """

    def __init__(
        self, message: str, *, rejection: restore_diagnosis.ProviderRejection
    ) -> None:
        super().__init__(message)
        self.rejection = rejection


class RestoreRefused(AcpError):
    """The agent would not restore the session this turn was told to continue.

    A subclass because every ``AcpError`` handler should keep treating it as
    one: nothing was submitted, the turn is over. What the separate type adds is
    durability -- the agent no longer holds this history, so retrying the same
    ``session/load`` later cannot succeed either.

    Only a rejected ``session/load`` raises it, and only when the rejection is a
    supported missing-history diagnosis under
    :mod:`~agent_session_loop.restore_diagnosis`: an agent that answers with an
    internal error, a temporary outage, an expired credential, or a refused
    workspace has not said the session is gone, and reading it that way would
    retire a history it is still holding. The other three restore refusals are
    excluded for the same reason -- an unadvertised ``loadSession`` capability
    is this client being unable to ask, a substituted session id is a
    disagreement about which conversation came back, and an unreadable replay is
    this client being unable to trust what did.
    """


class _SubmissionRefused(Exception):
    """The turn was cancelled before ``session/prompt``, so nothing was sent.

    Deliberately *not* an :class:`AcpError`. The agent is healthy and is simply
    being let go without work; recording a protocol error here would restate a
    cancellation as a harness that broke.
    """


class _ConversationNotRecorded(Exception):
    """The session identifier could not be persisted, so nothing was sent.

    Also not an :class:`AcpError`, and for the same reason: the agent answered
    everything it was asked. What failed is on this side of the pipe, and a turn
    submitted anyway would run its work into a conversation no later run could
    find again.
    """


@dataclass(frozen=True)
class PermissionPolicy:
    """What this turn may answer ``session/request_permission`` with.

    The responder exists to be explicit, not to be generous. It restates the
    grant the launch argv already made and can never exceed it:

    - an audit turn grants nothing, so every request is rejected;
    - a worker turn without ``--allow-all-tools --allow-all-paths`` grants
      nothing either -- ``--no-ask-user`` means there is no operator at this
      turn to approve anything, and approving on their behalf is precisely the
      blanket approval an unattended run must not invent;
    - a worker turn launched with those flags has already granted the tools,
      so ``allow_once`` restates that grant for one call rather than making a
      new one.

    Guard deny rules are deliberately not re-derived here. They are command
    *text* rules (``shell(git push)``) that the agent applies to its own tool
    calls before it would ever ask, so matching them again against a permission
    payload could only ever be a weaker guess at the same rule -- and a weaker
    guess that appeared to enforce something would be worse than not guessing.
    Copilot's guard lane remains its argv, exactly as ``guards.guard_coverage``
    reports it.
    """

    #: Whether the launch argv already granted the tools this turn may use.
    may_allow: bool
    #: The access profile's name, recorded with each decision so the turn's log
    #: says which rule answered rather than only what it answered.
    profile: str

    @classmethod
    def for_turn(cls, *, is_audit: bool, unsafe_bypass: bool) -> PermissionPolicy:
        if is_audit:
            return cls(may_allow=False, profile="audit-read-only")
        return cls(may_allow=unsafe_bypass, profile="worker")


@dataclass(frozen=True)
class PermissionDecision:
    """One answered permission request, as the turn recorded it."""

    decision: str
    reason: str
    tool: str
    option_id: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "tool": self.tool,
            "option_id": self.option_id,
        }


@dataclass
class _Pending:
    """One in-flight request, waiting for the reader thread to answer it.

    A request can stop waiting for two unrelated reasons, and they are kept in
    two fields rather than one string: the agent *answered* with an error, or
    the transport failed underneath it. Only the first is the agent stating
    something, and folding both into ``error`` is what let a broken pipe during
    ``session/load`` read as an agent disowning its own history.
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


# The focused ACP modules deliberately share the protocol vocabulary, including
# private wire-level constants and transport helpers.
__all__ = [name for name in globals() if not name.startswith("__")] + ["__version__"]

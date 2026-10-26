"""Shared harness result, invocation, access-profile, and callback contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    claude_stream,
    codex_app_server,
    copilot_acp,
)

HarnessName = Literal["codex", "claude", "copilot"]


HARNESS_NAMES: tuple[HarnessName, ...] = ("codex", "claude", "copilot")


StreamName = Literal["stdout", "stderr"]


StreamCallback = Callable[[StreamName, str], None]


AccessProfileName = Literal["worker", "audit-read-only"]


ACCESS_PROFILE_NAMES: tuple[AccessProfileName, ...] = ("worker", "audit-read-only")


# A dead harness process closes its pipes, so the reader threads see EOF at
# once. This bound only exists for a descendant that outlives the harness and
# keeps the inherited pipes open. The post-turn sweep runs after this join and
# kills such a descendant if it is still in the turn's process group, but one
# that started a session of its own outlives both, so this caps what a turn
# pays to walk away from it (mirrors
# verification_process._ABANDONED_READER_JOIN_SECONDS).
_ABANDONED_READER_JOIN_SECONDS = 5.0


#: How often a turn with an abort signal attached re-checks its deadlines. The
#: wait is otherwise a single blocking call, so this is the whole cost of being
#: cancellable and the upper bound on how late a kill lands.
_ABORT_POLL_INTERVAL_SECONDS = 0.1


#: The four windows a cancellation request can land in, named on the result and
#: on the signal itself. A caller that only saw "aborted" could not tell a turn
#: that was killed mid-flight from one that never ran, and the two mean
#: different things to a continuation: nothing was submitted in the first two
#: windows, so there is no interrupted work to resume from.
CancelStage = Literal["unstarted", "handshake", "execution", "shutdown"]


#: Requested before the harness process was spawned. Nothing ever ran.
CANCEL_STAGE_UNSTARTED: CancelStage = "unstarted"


#: Requested while the turn's conversation was being opened, before its work was
#: submitted. The process existed; the turn did not.
CANCEL_STAGE_HANDSHAKE: CancelStage = "handshake"


#: Requested against a running turn. This is the window that kills a process
#: group and the only one that interrupts work in progress.
CANCEL_STAGE_EXECUTION: CancelStage = "execution"


#: Requested once the turn's outcome was already decided. Nothing was killed and
#: the verdict stands; the request is complete because the turn already is.
CANCEL_STAGE_SHUTDOWN: CancelStage = "shutdown"


CANCEL_STAGES: tuple[CancelStage, ...] = (
    CANCEL_STAGE_UNSTARTED,
    CANCEL_STAGE_HANDSHAKE,
    CANCEL_STAGE_EXECUTION,
    CANCEL_STAGE_SHUTDOWN,
)


#: Exit code for a turn abandoned because its conversation was not recorded.
#: Distinct from the cancellation and timeout codes because it is neither: the
#: turn was refused before it submitted anything, and a caller reading only the
#: return code still has to be able to tell that apart from a turn that ran.
_CONVERSATION_ABANDON_RETURNCODE = 70


class HarnessError(Exception):
    """Raised when a harness adapter cannot build or run a command."""


class ConversationCaptureError(HarnessError):
    """Raised when a turn's native conversation could not be recorded.

    The one failure a transport may not work around. Continuity requires the
    native identifier to be durable *before* work is submitted, so a transport
    that cannot record it has to abandon the turn rather than run one that
    could never be continued.
    """


class TurnCancelled(HarnessError):
    """Raised inside a transport when the turn it is opening was cancelled.

    Never escapes :meth:`HarnessAdapter.run`, which turns it into a cancelled
    result. It exists so a handshake can stop at its next checkpoint without
    submitting anything, which is the difference between a cancellation that
    costs nothing and one that has to interrupt work already in flight.
    """


#: What a turn calls with the native conversation identifier its harness
#: reported. Unlike ``on_spawn`` and the other turn hooks, an exception from
#: this one is *not* suppressed: it is the persistence failure that must stop
#: the turn before it submits work.
ConversationCallback = Callable[[str], None]


@dataclass(frozen=True)
class AccessProfile:
    """What one harness turn is allowed to reach.

    This is deliberately separate from ``unsafe_bypass``: bypass widens a worker
    turn, while the profile decides which access model the turn runs under at
    all. Folding the audit profile into the bypass flag would make "not
    bypassed" mean two different things and let a worker default leak into an
    audit.
    """

    name: AccessProfileName
    read_roots: tuple[Path, ...] = ()

    @property
    def is_audit(self) -> bool:
        return self.name == "audit-read-only"


#: The profile every implementation and review turn runs under.
WORKER_ACCESS = AccessProfile(name="worker")


def audit_read_only_access(read_roots: tuple[Path, ...] | list[Path]) -> AccessProfile:
    """Build the restricted profile an auditor runs under.

    ``read_roots`` are the directories the auditor may read outside its own
    working directory — the evidence bundle and the read-only ASL and skill
    trees. They are never writable roots, and the application workspace is
    never among them.
    """
    return AccessProfile(name="audit-read-only", read_roots=tuple(read_roots))


#: Transport discriminators. ``argv`` alone stopped describing a turn once a
#: harness could be driven over a protocol instead of a command line, so every
#: invocation and every result now says which of the two it was.
TRANSPORT_EXEC = codex_app_server.TRANSPORT_EXEC


TRANSPORT_APP_SERVER = codex_app_server.TRANSPORT_APP_SERVER


TRANSPORT_STREAM_JSON = claude_stream.TRANSPORT_STREAM_JSON


TRANSPORT_ACP = copilot_acp.TRANSPORT_ACP


@dataclass(frozen=True)
class HarnessInvocation:
    """A concrete subprocess invocation for one harness turn.

    ``argv`` remains the invocation's identity for every caller that had one.
    Under a protocol transport it says only which binary is spawned, and
    ``turn_plan`` carries what argv used to: the access profile, the writable
    roots, the model, and the effort. Neither ever carries the prompt.
    """

    name: HarnessName
    argv: list[str]
    cwd: Path
    last_message_path: Path | None = None
    model: str | None = None
    effort: str | None = None
    transport: str = TRANSPORT_EXEC
    turn_plan: codex_app_server.TurnPlan | None = None
    #: The turn's ephemeral harness-configuration directory, when it has one.
    #: It holds settings a turn needs and must not outlive — a hook overlay and
    #: its guidance spool — and is created by ``run`` and deleted by it, so a
    #: turn's private channel cannot be reached from the next turn.
    overlay_dir: Path | None = None
    #: The native conversation this turn intends to continue, when the caller
    #: has one to continue. Intent only: an adapter that cannot honour it
    #: refuses the turn rather than quietly starting a fresh conversation, so
    #: this never silently degrades into "resumed nothing".
    resume_conversation_id: str | None = None
    #: The native conversation this turn will actually run on, when its
    #: transport states that on the command line instead of learning it from a
    #: handshake. Distinct from ``resume_conversation_id``, which is a request:
    #: this is the identity, whether freshly minted or restored, and it is
    #: known before the process is spawned — which is what lets such a turn
    #: persist its conversation before submitting any work.
    assigned_conversation_id: str | None = None


@dataclass(frozen=True)
class HarnessResult:
    """Captured result from a single harness turn."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    last_message: str = ""
    name: HarnessName | None = None
    timed_out: bool = False
    aborted: bool = False
    interrupted: bool = False
    process_sweep: dict[str, Any] | None = None
    observed_model: str | None = None
    stream_io: dict[str, int | float] | None = None
    #: Every error a stream reader or its callback raised while the turn ran,
    #: as ``"<Type>: <message>"``. Recorded rather than raised: the readers keep
    #: their own copy of the output, so the turn's result stands without them.
    stream_errors: tuple[str, ...] = ()
    transport: str = TRANSPORT_EXEC
    #: The native conversation this turn actually ran on, when its transport
    #: reported one, or ``None`` for a turn that never named one — because its
    #: transport cannot report one, because it stopped before its handshake got
    #: there, or because its output never stated it.
    conversation_id: str | None = None
    #: Why this turn has no conversation to continue, when one was asked for
    #: and none could be had. Set only where the work ran anyway: a legacy
    #: one-shot turn submits its prompt on argv, so continuity can only be
    #: observed after the fact and its absence cannot retract the turn. A turn
    #: that could have refused instead reports ``conversation_error``.
    continuity_unavailable: str | None = None
    #: Which window a cancellation was acted on in, or ``None`` when the turn
    #: was not cancelled. Distinguishes the cancellations that cost nothing
    #: (``unstarted``, ``handshake``) from the ones that had to interrupt work
    #: already submitted (``execution``, ``shutdown``).
    cancel_stage: CancelStage | None = None
    #: Why the turn refused to submit work, when a conversation could not be
    #: recorded. Present only on a turn abandoned by that failure.
    conversation_error: str | None = None
    #: Why the provider would not restore the native conversation this turn was
    #: told to continue. Set only when the provider itself refused the restore
    #: request -- the history is gone on its side, so no later launch can
    #: continue it either. Settings drift, a substituted conversation, and a
    #: transport that cannot restore are all excluded: those leave the history
    #: intact and are ordinary failures of this turn.
    conversation_restore_refused: str | None = None
    #: What the turn's own submission seam says about whether its prompt went
    #: out: ``True`` submitted, ``False`` ended without submitting, ``None`` no
    #: statement either way. The three values are not two, and ``None`` is
    #: reached from both ends: a turn whose process died before a result could
    #: be assembled leaves the field where it started, and a turn stopped while
    #: its prompt was crossing the wire has a seam that deliberately refuses to
    #: answer. Only a turn that *declined* reads ``False``, which is why the
    #: absence of a statement is its own value rather than a default of
    #: ``False``.
    prompt_submitted: bool | None = None


def prompt_provably_unsubmitted(result: HarnessResult) -> bool:
    """Whether this result *proves* its turn put no prompt in front of a worker.

    Not the negation of :attr:`TurnSubmission.submitted` as a caller observes
    it. A caller sees that flag only through a callback, and the absence of a
    callback covers two very different things: a turn that refused before
    sending anything, and a turn that sent something and then lost the thread
    that would have said so. A caller holding an operator's words has to tell
    those apart, because acting on the second as if it were the first hands the
    same words to a second worker.

    ``prompt_submitted`` is the whole answer wherever there is one: the adapter
    stamps it from the submission seam as it returns, so it reports what this
    turn actually did rather than what its exit path implies. That matters in
    both directions -- it covers the protocol handshakes that fail without a
    refusal field of their own, a provider that was merely unreachable most of
    all, and it is the only field that knows a one-shot turn submitted its
    prompt on argv before the window its cancellation was named for. A seam that
    answers ``None`` has answered: a turn stopped with its prompt on the wire
    proves nothing, and that is the answer, not a missing one.

    A result nothing stamped falls back to what a turn sets when it *declines*,
    each written on a path that has not sent a prompt -- and each, for that
    reason, unreachable once a transport has begun writing the prompt out, so
    reading them under a ``None`` seam cannot resurrect the proof the seam
    withheld:

    - a cancellation acted on in the ``unstarted`` or ``handshake`` window, the
      two windows :data:`CancelStage` documents as costing nothing;
    - ``conversation_error``, set only where a turn refused to submit because it
      could not record the conversation it would have run on;
    - ``conversation_restore_refused``, set only where the provider declined to
      restore the history the prompt was going to continue.

    Everything else -- a nonzero exit, a timeout, a transport that died at an
    unknown point, and every result assembled by something other than an
    adapter -- answers ``False``, which means "not proven", not "it was
    submitted". The caller's job on ``False`` is to leave the question open.
    """
    if result.prompt_submitted is not None:
        return not result.prompt_submitted
    if result.cancel_stage in (CANCEL_STAGE_UNSTARTED, CANCEL_STAGE_HANDSHAKE):
        return True
    if result.conversation_error is not None:
        return True
    return result.conversation_restore_refused is not None

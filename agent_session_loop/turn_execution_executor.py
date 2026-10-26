"""The typed boundary between the run loop and one executed turn.

A turn is the run's only irreversible act: it launches an agent against the
plan, watches it, and writes the artifacts every later guard, audit and
supervisor reads. This module composes launch, heartbeat, hang detection,
process result, artifact capture and the two terminal transitions
(publication and rejection) -- so the run loop is left deciding *whether* a
turn may proceed rather than reimplementing *how* one runs.

Every transition hands back a frozen record (:class:`TurnExecution`, and
:class:`turn_execution_commit.TurnFinalization` / ``TurnCommit``) instead of
mutating the caller's dictionaries in place, so what a phase produced is
readable from its return value rather than reconstructed from whichever keys
appeared.

Nothing here imports the runner."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    guidance_delivery,
    harnesses,
    mailbox,
    mailbox_models,
    plans_models,
    plans_snapshot,
    turn_execution_artifacts,
    turn_execution_commit,
    turn_execution_constants,
    turn_execution_live,
    turn_execution_protocols,
)


def worker_input_wait_probe(
    *,
    plan_dir: str | Path,
    run_id: str,
    session_id: str | None,
    turn_index: int,
    state_dir: str | Path | None = None,
) -> bool:
    """Whether this exact worker turn is intentionally waiting for an answer."""
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except mailbox_models.MailboxError:
        # A read failure cannot safely suppress stall detection.
        return False
    return (
        view.active_worker_wait(
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
        )
        is not None
    )


@dataclass(frozen=True)
class TurnIdentity:
    """Which turn of which run against which session this is."""

    run_id: str
    turn_index: int
    session_id: str


@dataclass(frozen=True)
class TurnLaunch:
    """The harness invocation one turn is, decided before anything starts.

    ``build_invocation`` is deferred rather than an ``argv`` field because it
    is only needed on the interrupt path, where no process ever reported one;
    building it eagerly would render a command line for every turn to describe
    the few that are interrupted.

    What it renders there is a description of the turn rather than the command
    the turn ran: the real invocation is built inside ``HarnessAdapter.run``,
    which adds what only it knows. For claude that now includes the conversation
    identifier, so a rebuilt argv names a fresh session the interrupted turn
    never used. Read the conversation off ``HarnessResult.conversation_id``,
    never off an argv rendered here.
    """

    harness: harnesses.HarnessName
    cwd: Path
    plan_dir: Path
    prompt: str
    unsafe_bypass: bool
    timeout: int | None
    model: str | None
    effort: str | None
    abort: harnesses.AbortSignal
    #: The turn-scoped channel live guidance is delivered through. Handed over
    #: unopened: the harness opens it around the process it owns and closes it
    #: when that process is gone, so this object never outlives its turn.
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None
    stream_callback: harnesses.StreamCallback | None
    build_invocation: Callable[[], harnesses.HarnessInvocation]
    #: The complete child environment for this turn, or ``None`` to inherit
    #: this process's. It carries the run's guard-bin ``PATH`` prefix, which is
    #: what makes a command guard reach the harness and its descendants.
    env: Mapping[str, str] | None = None
    #: Called once this turn's prompt has actually been handed to the worker,
    #: which is where the words stop being something the run undertook to
    #: deliver and start being something a worker holds. For a harness that
    #: takes its prompt on argv that is the spawn; for one that speaks a
    #: protocol it is the accepted turn, on the far side of a handshake that can
    #: refuse. A turn that spawned and then refused never calls it, which is
    #: what keeps a correction the worker never saw recoverable.
    on_prompt_submitted: harnesses.SubmissionCallback | None = None
    #: Called once the harness process exists, whatever its transport does next.
    #: It is for bookkeeping about the prompt this turn was *built* from, which
    #: is settled before the spawn and unaffected by what the handshake decides.
    #: Anything that records words as having reached a worker belongs on
    #: ``on_prompt_submitted`` instead.
    on_spawned: Callable[[], None] | None = None
    #: Called with the harness process's pid the moment it exists, which is
    #: earlier than ``on_prompt_submitted`` and deliberately so. It is how a
    #: turn-scoped controller learns which process it is permitted to stop, and
    #: it has to be stated at spawn rather than looked up later: a pid
    #: discovered at a deadline names whatever holds that number by then.
    #: Failures propagate -- a controller that could not be bound would leave an
    #: accepted correction with nothing to interrupt.
    on_worker: Callable[[int], None] | None = None
    #: The native conversation this turn continues, or ``None`` to let the
    #: harness open one. Exact-or-refusal: an adapter that cannot restore this
    #: identifier fails the turn rather than running a fresh conversation, so a
    #: caller is never told it continued something it did not.
    resume_conversation_id: str | None = None
    #: Called with the identifier the turn actually runs on, on the harness's
    #: own driver thread and before any work is submitted. The durable write
    #: belongs here, ahead of the turn, so a crash mid-turn still leaves the
    #: conversation findable. Never called by an adapter that cannot report one.
    on_conversation: harnesses.ConversationCallback | None = None


@dataclass(frozen=True)
class TurnExecution:
    """What a launched turn left behind once its process was gone.

    ``live_turn`` is the finished live record, not the mutable one the loop
    kept during the turn: the heartbeat thread is joined and ``active_turn``
    is cleared by the time this exists, so nothing can still be writing to it.
    """

    result: harnesses.HarnessResult
    live_turn: dict[str, Any]
    finished_at: datetime


class TurnExecutor:
    """Runs one turn from launch to publication behind one typed boundary.

    Constructed per turn, because ``identity`` and ``paths`` are what make a
    turn that turn. The three methods are the run loop's whole vocabulary for
    executing one: :meth:`launch` spends the turn, :meth:`finalize` collects
    the exited process into artifacts, and :meth:`commit` publishes the
    accepted result. Everything the loop does between them -- the guards --
    is orchestration this class deliberately knows nothing about.
    """

    def __init__(
        self,
        *,
        identity: TurnIdentity,
        paths: dict[str, Path],
        event_path: Path,
        persistence: turn_execution_protocols.TurnPersistence,
        progress: turn_execution_protocols.TurnProgress,
        plan_cache: plans_snapshot.PlanSnapshotCache,
    ) -> None:
        self.identity = identity
        self.paths = paths
        self.event_path = event_path
        self.persistence = persistence
        self.progress = progress
        self.plan_cache = plan_cache

    def launch(
        self,
        launch: TurnLaunch,
        *,
        live_turn: turn_execution_live.LiveTurn,
        active_turn: dict[str, Any],
        payload: dict[str, Any],
        after_stop: Callable[[], None] | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> TurnExecution:
        """Spend the turn: start the harness, watch it, and collect its exit.

        The logs are materialized, ``turn_started`` appended and the live turn
        started in that order, so a reader that has seen the event finds the
        files the event names. An interrupt is turned into a result rather
        than propagated -- the run has to record what it spent before it stops
        -- while any other exception tears the live turn down through
        ``on_failure`` and re-raises, because the loop cannot describe a turn
        whose failure it does not understand.
        """
        turn_execution_artifacts.create_turn_logs(self.paths)
        self.persistence.append_event(self.event_path, "turn_started", active_turn)
        live_turn.start()
        turn_execution_protocols.notify_turn_started(
            payload, active_turn, self.persistence
        )

        def on_spawn(pid: int) -> None:
            live_turn.note_harness_pid(pid)
            if launch.on_worker is not None:
                launch.on_worker(pid)
            if launch.on_spawned is not None:
                # Suppressed rather than propagated: recording what the prompt
                # was built from is bookkeeping about a turn that has already
                # started, and a ledger failure must not become a spawn failure.
                with contextlib.suppress(Exception):
                    launch.on_spawned()

        try:
            result = harnesses.run_harness(
                launch.harness,
                cwd=launch.cwd,
                plan_dir=launch.plan_dir,
                prompt=launch.prompt,
                unsafe_bypass=launch.unsafe_bypass,
                last_message_path=self.paths["last_message"],
                timeout=launch.timeout,
                stream_callback=launch.stream_callback,
                model=launch.model,
                effort=launch.effort,
                on_spawn=on_spawn,
                on_final_result=live_turn.note_final_result,
                on_transport=live_turn.note_transport,
                on_observed_model=live_turn.note_observed_model,
                abort=launch.abort,
                live_delivery=launch.live_delivery,
                env=launch.env,
                on_conversation=launch.on_conversation,
                # Handed to the harness rather than fired from ``on_spawn``:
                # only the adapter knows where its prompt actually goes out, and
                # for a protocol transport that is past a handshake the provider
                # can still refuse. The harness's own seam swallows a failure
                # here, so a ledger that cannot be written never becomes a turn
                # that did not run.
                on_prompt_submitted=launch.on_prompt_submitted,
                resume_conversation_id=launch.resume_conversation_id,
            )
        except KeyboardInterrupt:
            result = harnesses.HarnessResult(
                name=launch.harness,
                argv=launch.build_invocation().argv,
                returncode=turn_execution_constants.INTERRUPTED_EXIT,
                stdout="",
                stderr="",
                interrupted=True,
            )
        except Exception:
            try:
                live_turn.stop()
                live_turn.finish(clock.utc_now())
            finally:
                # The state write in ``finish`` can fail for the same reason
                # the launch did; the turn's interactive surfaces still go.
                if on_failure is not None:
                    on_failure()
            raise
        live_turn.stop()
        # The poll thread is joined by now, so no later tick can arm a signal
        # this finished turn would never read. ``after_stop`` is where the
        # caller disarms whatever it handed the turn, and it runs before
        # ``finish`` clears ``active_turn``.
        if after_stop is not None:
            after_stop()
        finished_at = clock.utc_now()
        live_turn_payload = live_turn.finish(finished_at)
        run_started_at = clock.parse_datetime(payload.get("run_started_at"))
        if run_started_at is not None:
            live_turn_payload["total_duration_seconds"] = clock.duration_seconds(
                run_started_at,
                finished_at,
            )
        return TurnExecution(
            result=result,
            live_turn=live_turn_payload,
            finished_at=finished_at,
        )

    def finalize(
        self,
        execution: TurnExecution,
        *,
        before: plans_models.PlanSnapshot,
        unredacted_prompt: str | None = None,
        pending_exception_guard: MutableMapping[str, Any] | None = None,
    ) -> turn_execution_commit.TurnFinalization:
        """Collect the exited process into artifacts and a re-read plan."""
        return turn_execution_commit.finalize_turn_execution(
            event_path=self.event_path,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            result=execution.result,
            before=before,
            paths=self.paths,
            run_id=self.identity.run_id,
            turn_index=self.identity.turn_index,
            session_id=self.identity.session_id,
            live_turn=execution.live_turn,
            unredacted_prompt=unredacted_prompt,
            pending_exception_guard=pending_exception_guard,
        )

    def commit(
        self,
        *,
        payload: dict[str, Any],
        turn_payload: dict[str, Any],
        live_turn: dict[str, Any],
        accepted: plans_models.PlanSnapshot,
    ) -> turn_execution_commit.TurnCommit:
        """Publish the accepted turn, or raise :class:`TurnCommitRejected`."""
        return turn_execution_commit.commit_turn(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
            progress=self.progress,
            turn_payload=turn_payload,
            live_turn=live_turn,
            accepted=accepted,
            plan_cache=self.plan_cache,
            paths=self.paths,
        )

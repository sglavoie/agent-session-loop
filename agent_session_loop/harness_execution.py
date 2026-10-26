"""Execute one harness turn and settle its transport, process, and lifecycle."""

from __future__ import annotations

import contextlib
import dataclasses
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    guidance_delivery,
    harness_conversation,
    harness_lifecycle,
    harness_process,
    harness_streams,
    harness_transport,
    harness_types,
)

if TYPE_CHECKING:
    from . import harness_adapter


def _report_stream_errors(
    harness_name: str, reader_errors: Sequence[BaseException]
) -> tuple[str, ...]:
    """Describe what went wrong while the turn's output was being read.

    A failed stream callback -- one ``ENOSPC`` appending to the provisional
    ``stdout.log`` -- used to raise here and discard a turn that had already
    finished, exit code and all. The readers keep their own copy of every
    chunk, so the turn's result is still whole; the failure is recorded on it
    and announced on stderr once instead.
    """
    errors = tuple(f"{type(error).__name__}: {error}" for error in reader_errors)
    if errors:
        print(
            f"agent-session-loop: {harness_name} stream reading degraded "
            f"({len(errors)} error(s)); first: {errors[0]}",
            file=sys.stderr,
        )
    return errors


def run_adapter(
    self: harness_adapter.HarnessAdapter,
    *,
    cwd: Path,
    plan_dir: Path | None,
    prompt: str,
    unsafe_bypass: bool = False,
    last_message_path: Path | None = None,
    timeout: int | None = None,
    stream_callback: harness_types.StreamCallback | None = None,
    model: str | None = None,
    effort: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_final_result: Callable[[], None] | None = None,
    on_observed_model: Callable[[str], None] | None = None,
    access: harness_types.AccessProfile = harness_types.WORKER_ACCESS,
    abort: harness_process.AbortSignal | None = None,
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
    invocation: harness_types.HarnessInvocation | None = None,
    on_transport: Callable[[harness_transport.HarnessTransport | None], None]
    | None = None,
    env: Mapping[str, str] | None = None,
    on_conversation: harness_types.ConversationCallback | None = None,
    on_prompt_submitted: harness_conversation.SubmissionCallback | None = None,
    resume_conversation_id: str | None = None,
) -> harness_types.HarnessResult:
    if invocation is None:
        invocation = self.build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            # The turn environment is where argv learns about guards: an
            # adapter whose only pre-execution lane is its command line
            # reads the policy this env points at. Built here rather than
            # by the caller so the argv that runs is the argv the guards
            # were compiled into.
            env=env,
            resume_conversation_id=resume_conversation_id,
        )
    elif (
        resume_conversation_id is not None
        and resume_conversation_id != invocation.resume_conversation_id
    ):
        # A prebuilt invocation is the argv that will actually run, so it
        # wins — but it would win silently. A caller that named one
        # conversation while handing over an invocation naming another (or
        # none) would be told it continued something it did not, which is
        # the substitution this whole path exists to prevent. Saying
        # nothing is still allowed: a caller that passes only an invocation
        # asked for nothing, so the invocation contradicts nothing.
        raise harness_types.HarnessError(
            f"{self.name} was asked to resume {resume_conversation_id} but "
            "the invocation it was given resumes "
            f"{invocation.resume_conversation_id or 'no conversation'}"
        )
    # Both refusals are the same rule read from both ends: a caller must
    # not be told a conversation was captured or continued by an adapter
    # that cannot do it. Raising is deliberate — a turn that ran anyway
    # would be one the caller believed was continuable and is not.
    if on_conversation is not None and not self.captures_conversations:
        raise harness_types.HarnessError(
            f"{self.name} cannot report the conversation a turn runs on"
        )
    if invocation.resume_conversation_id is not None and (
        not self.resumes_conversations
    ):
        raise harness_types.HarnessError(
            f"{self.name} cannot resume conversation "
            f"{invocation.resume_conversation_id}"
        )
    if invocation.last_message_path is not None:
        invocation.last_message_path.parent.mkdir(parents=True, exist_ok=True)

    lifecycle = harness_lifecycle.TurnLifecycle(
        conversation=harness_conversation.TurnConversation(on_conversation),
        abort=abort,
        submission=harness_conversation.TurnSubmission(on_prompt_submitted),
    )

    def stated(result: harness_types.HarnessResult) -> harness_types.HarnessResult:
        """Answer, on the way out, whether this turn's prompt went out.

        For the early returns, which build their result before there is a
        turn to describe; the ones further down state it beside the other
        facts they read off the lifecycle. Every return from this method
        answers one way or the other, because the caller that has to know is
        holding words it will otherwise hand to somebody else, and the only
        results that stay silent are the ones this method never produced.
        Read from the seam rather than from the exit path, so a failure mode
        nobody anticipated reports the submission it actually made rather
        than the one its branch assumed.
        """
        return dataclasses.replace(result, prompt_submitted=lifecycle.prompt_submitted)

    lifecycle.begin()
    if lifecycle.cancel_due:
        # Cancelled before spawn: the cheapest window there is. Nothing is
        # started, so nothing has to be killed, and the completion the
        # caller waits on is satisfied immediately.
        lifecycle.note_cancelled(harness_types.CANCEL_STAGE_UNSTARTED)
        lifecycle.close()
        return stated(
            self._cancelled_result(invocation, harness_types.CANCEL_STAGE_UNSTARTED)
        )

    try:
        process = subprocess.Popen(
            invocation.argv,
            cwd=invocation.cwd,
            # A protocol transport talks back to the process it spawned, so
            # it needs a pipe of its own. A one-shot turn leaves stdin
            # inherited, exactly as it always was.
            stdin=(
                subprocess.PIPE
                if invocation.transport != harness_types.TRANSPORT_EXEC
                else None
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A harness that prints one byte which is not UTF-8 must not kill
            # the reader thread draining its pipe, and with it the turn.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Its own session, so every descendant that inherits the turn's
            # process group can be killed at once on timeout and enumerated
            # by the post-turn sweep. Descendants that start a session of
            # their own are out of reach: see :func:`_kill_process_group`.
            start_new_session=True,
            # ``None`` inherits this process's environment, which is what
            # every turn did before command guards existed and what every
            # turn still does when none are active. A supplied mapping is
            # the complete child environment, already derived from the
            # parent's, so a guard's ``PATH`` prefix reaches the harness and
            # everything it spawns.
            env=None if env is None else dict(env),
        )
    except OSError as error:
        # Nothing started, so nothing is left to stop -- but a caller
        # waiting on a cancellation must be answered rather than left
        # waiting for a turn that does not exist. Any spawn failure counts:
        # a non-executable binary or an argv over the kernel's limit leaves
        # the same nothing behind as a missing one.
        lifecycle.close()
        if isinstance(error, FileNotFoundError):
            message = f"{self.name} executable not found: {self.executable}"
        else:
            message = f"{self.name} could not be started: {error}"
        raise harness_types.HarnessError(message) from error

    if on_spawn is not None:
        with contextlib.suppress(Exception):
            on_spawn(process.pid)

    if invocation.transport == harness_types.TRANSPORT_EXEC:
        # The one transport for which the spawn *is* the submission: the
        # prompt is on the argv this process was started with, so by the
        # time there is a pid the worker already has the words. Every other
        # transport still has a handshake to get through -- and a handshake
        # that can refuse -- so each reports its own submission from the
        # point where its prompt actually goes out.
        lifecycle.note_prompt_submitted()

    # The conversation and the live-delivery endpoint are both opened here,
    # inside the boundary that owns the process, and both are closed in the
    # ``finally`` below on every path out of the turn. A delivery can
    # therefore only ever reach this exact live process: once the turn is
    # over the endpoint answers ``stale-turn`` instead of injecting into a
    # dead PID or into whatever turn started next.
    try:
        transport = self.open_transport(
            process=process,
            invocation=invocation,
            prompt=prompt,
            lifecycle=lifecycle,
        )
    except harness_types.TurnCancelled:
        # Cancelled mid-handshake: the process exists but the transport
        # stopped short of submitting, so this stops a spawn rather than
        # interrupting work.
        lifecycle.note_cancelled(harness_types.CANCEL_STAGE_HANDSHAKE)
        sweep = self._stop_before_submission(process)
        lifecycle.close()
        return stated(
            self._cancelled_result(
                invocation,
                harness_types.CANCEL_STAGE_HANDSHAKE,
                process_sweep=sweep,
            )
        )
    except harness_types.ConversationCaptureError as error:
        # The turn is abandoned rather than run: a conversation that was
        # not recorded before submission is one no later session could
        # continue, and running the work anyway would spend it into a
        # conversation nothing can find again.
        sweep = self._stop_before_submission(process)
        lifecycle.close()
        return stated(
            harness_types.HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                returncode=harness_types._CONVERSATION_ABANDON_RETURNCODE,
                stdout="",
                stderr=f"{self.name} turn abandoned: {error}\n",
                process_sweep=sweep,
                conversation_error=str(error),
            )
        )
    except BaseException:
        # A handshake that failed for any other reason leaves a spawned
        # process nobody will ever wait on, and a lifecycle nobody will
        # ever close. Both are ended here before the failure is reported.
        self._stop_before_submission(process)
        lifecycle.close()
        raise
    try:
        try:
            if on_transport is not None:
                on_transport(transport)
            if live_delivery is not None:
                live_delivery.open(
                    self.live_delivery_transport(
                        process=process,
                        invocation=invocation,
                        transport=transport,
                    )
                )
            output_processor = (
                self.output_processor()
                if transport is None
                else transport.output_processor()
            )
            streams = harness_streams.start_turn_streams(
                process,
                output_processor,
                harness_name=self.name,
                stream_callback=stream_callback,
                on_final_result=on_final_result,
                on_observed_model=on_observed_model,
            )
        except BaseException:
            # Nothing below waits on or kills the process until the streams
            # are running, and it lives in a session of its own, so the
            # terminal's SIGINT never reached it. Left alone it would keep
            # working after the run reported the failure and released the plan
            # lock; it is taken down with its group before the error surfaces.
            self._stop_before_submission(process)
            raise
        stdout_chunks = streams.stdout_chunks
        stderr_chunks = streams.stderr_chunks
        stream_io = streams.stream_io
        reader_errors = streams.reader_errors
        stdout_thread = streams.stdout_thread
        stderr_thread = streams.stderr_thread
        abandon_readers = streams.abandon

        timed_out = False
        aborted = False
        prior_snapshot: tuple[Mapping[str, Any], ...] = ()
        process_sweep: dict[str, Any] | None = None
        try:
            expired = harness_process._wait_for_turn_exit(
                process, timeout=timeout, abort=abort
            )
            if expired is not None:
                timed_out = expired == "timeout"
                aborted = expired == "abort"
                if aborted:
                    # Work was already submitted, so this is the expensive
                    # window: the kill below is what stops it.
                    lifecycle.note_cancelled(harness_types.CANCEL_STAGE_EXECUTION)
                    harness_transport._request_native_interrupt(transport)
                prior_snapshot = harness_process._snapshot_descendants(
                    process.pid, harness_process._sweep_process_table()
                )
                harness_process._kill_process_group(process)
                process.wait()

            harness_process._join_readers(
                (stdout_thread, stderr_thread),
                timeout=harness_types._ABANDONED_READER_JOIN_SECONDS,
            )
            abandon_readers()
            # After the join, so an in-group straggler gets to flush what it
            # was writing before the sweep takes it down.
            process_sweep = harness_process._sweep_turn_descendants(
                process, prior_snapshot
            )
        except KeyboardInterrupt:
            # The turn runs in a session of its own, so the terminal's SIGINT
            # never reached it or anything it spawned. It is taken down the
            # way a timeout or abort takes it down: the descendants are
            # snapshotted while the root still lives, then the whole group is
            # killed, so a helper that left the group is still in reach of the
            # sweep below rather than reparented out of it.
            interrupt_snapshot: tuple[Mapping[str, Any], ...] = ()
            if process.poll() is None:
                interrupt_snapshot = harness_process._snapshot_descendants(
                    process.pid, harness_process._sweep_process_table()
                )
                harness_process._kill_process_group(process)
                process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            abandon_readers()
            # This branch returns early, so it needs its own sweep: the one on
            # the normal path is never reached from here.
            interrupt_sweep = harness_process._sweep_turn_descendants(
                process, interrupt_snapshot
            )
            return harness_types.HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                returncode=130,
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
                last_message=output_processor.last_message(
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    last_message_path=invocation.last_message_path,
                ),
                interrupted=True,
                process_sweep=interrupt_sweep,
                stream_errors=_report_stream_errors(self.name, reader_errors),
                observed_model=output_processor.observed_model,
                stream_io=dict(stream_io),
                conversation_id=lifecycle.conversation.native_id,
                cancel_stage=lifecycle.cancel_stage,
                prompt_submitted=lifecycle.prompt_submitted,
            )

        stream_errors = _report_stream_errors(self.name, reader_errors)
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if aborted:
            return harness_types.HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                # 128 + SIGKILL, not the timeout path's 124: a caller reading
                # only the returncode still has to tell the two apart.
                returncode=137,
                stdout=stdout,
                stderr=stderr,
                last_message=output_processor.last_message(
                    stdout=stdout,
                    stderr=stderr,
                    last_message_path=invocation.last_message_path,
                ),
                aborted=True,
                process_sweep=process_sweep,
                stream_errors=stream_errors,
                observed_model=output_processor.observed_model,
                stream_io=dict(stream_io),
                conversation_id=lifecycle.conversation.native_id,
                cancel_stage=lifecycle.cancel_stage,
                prompt_submitted=lifecycle.prompt_submitted,
            )
        if timed_out:
            return harness_types.HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                last_message=output_processor.last_message(
                    stdout=stdout,
                    stderr=stderr,
                    last_message_path=invocation.last_message_path,
                ),
                timed_out=True,
                process_sweep=process_sweep,
                stream_errors=stream_errors,
                observed_model=output_processor.observed_model,
                stream_io=dict(stream_io),
                conversation_id=lifecycle.conversation.native_id,
                cancel_stage=lifecycle.cancel_stage,
                prompt_submitted=lifecycle.prompt_submitted,
            )

        interrupted = process.returncode == -signal.SIGINT
        return harness_types.HarnessResult(
            name=self.name,
            argv=invocation.argv,
            transport=invocation.transport,
            returncode=130 if interrupted else process.returncode,
            stdout=stdout,
            stderr=stderr,
            last_message=output_processor.last_message(
                stdout=stdout,
                stderr=stderr,
                last_message_path=invocation.last_message_path,
            ),
            interrupted=interrupted,
            process_sweep=process_sweep,
            stream_errors=stream_errors,
            observed_model=output_processor.observed_model,
            stream_io=dict(stream_io),
            conversation_id=lifecycle.conversation.native_id,
            cancel_stage=lifecycle.cancel_stage,
            prompt_submitted=lifecycle.prompt_submitted,
        )
    finally:
        # Endpoint first: closing it waits out any delivery already in
        # flight, so no steer can still be using the conversation the
        # transport shutdown is about to tear down. The lifecycle closes
        # last, so a request arriving in this window is staged
        # ``shutdown`` and completed rather than left waiting: by the time
        # completion is reported there is no turn left to cancel, and a
        # protocol thread that outlives the shutdown can no longer record
        # a conversation against it either.
        if live_delivery is not None:
            live_delivery.close()
        if transport is not None:
            transport.shutdown()
        lifecycle.close()

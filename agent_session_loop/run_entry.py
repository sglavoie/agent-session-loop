"""Where a run begins: the flags it resumes and the gate it must pass to start.

Everything else in this package runs *inside* a run. This boundary owns the
outside of it — the stretch between the CLI handing over a parsed namespace and
the loop taking the plan's lock, plus the single line that returns its exit
code. It is the package's public entry: ``cli.py`` reaches it by the string
``handler_ref="runner:run_from_args"``, and ``workbench_launch.py`` and
``skills/asl/scripts/asl_autopilot.py`` start an ``asl run`` child process that
arrives through that same string, so the name and the signature are a contract,
not an implementation detail.

Two halves, in three modules.

* **Resume rehydration** lives in ``run_entry_resume``: ``asl run --resume``
  restores the flags of the plan's last run instead of asking the operator to
  retype them, and explicit flags win. When no usable config exists,
  ``_ResumeUnavailable`` carries a best-effort ``asl run`` command built by
  ``run_entry_resume_command`` from the top-level fields the state document has
  always written. This module imports ``_apply_resume_config`` and
  ``_ResumeUnavailable`` and applies them first.
* **The admission sequence.** :func:`run_from_args` is the ordered gate a run
  passes before the loop exists: validate the flags, resolve the reply (the long
  refusals live in ``run_entry_admission``), inspect
  the plan, resolve the workspace and the repository intent, mint the run id,
  open the wakeup endpoint, take the lock, check for a contending run under
  that claim, and only then call the locked run. Every refusal on that path
  builds a ``run_outcome._error_payload`` and emits it, because before the lock
  there is no ``state.json`` to write the failure into.

Durable records this boundary writes: **the plan's run lock and the wakeup
endpoint**, and nothing else. :func:`state.acquire_lock` publishes this
process's claim on the plan and its repository roots, and the endpoint is
opened before the lock that names it and closed after that lock is gone so
every producer that can find the path finds this process holding the read end.
The run state, the events and the turn records all belong to the locked run;
this module hands them a lock and reads back an exit code and a payload.

Nothing here imports the runner, and nothing here imports the loop either.
``run_locked`` lives in ``run_loop`` since session 37, and it still enters
:func:`run_from_args` as a required keyword-only :class:`LockedRun` seam that
``runner.run_from_args`` supplies — the ``CoordinationPersistence``/
``WaitNotifier`` pattern, kept after the move because a direct import here
would put the admission gate and the run it admits back in one cycle. Every
other collaborator
is read at its true owner: admission at ``run_inputs``, the review-guard
allowlist at ``review_guard``, the reply at ``run_prompts``, the final emission
and the error payload at ``run_outcome``, the flag spec at ``run_flags``, the
run id at ``clock``, the interrupted exit code at ``turn_execution_constants``, and the
lock, endpoint and contention checks at ``state``, ``control_signal`` and
``run_contention``.

Invariants that survive the move:

* **An explicit flag always beats a persisted one.** The overlay skips any dest
  :func:`_explicit_resume_dests` names, and the deprecated
  ``--max-review-fix-plans`` alias marks its canonical dest explicit too, so
  passing either one on a resume is never clobbered by the stored value.
* **A dest missing from the persisted block is skipped silently.** The overlay
  leaves such a flag at its parser default rather than failing, which is why
  ``run_payloads._run_config_payload`` must seed every dest in
  :data:`_RESUME_OVERLAY_DESTS` — a contract
  ``tests/test_asl_run_payloads.py`` pins on the writer's side.
* **The registry gate is independent of ``--allow-not-loop-ready``.** The
  repository-intent refusal runs after root and workspace resolution and before
  anything this run launches: the flag waives readiness, and no readiness
  marking can stand in for the registry that would have bound a second
  repository in the first place.
* **Contention is checked after acquiring, never before.** A check before the
  acquire leaves two racing starters blind to each other and both proceed;
  checking under our own published claim means the worst case is both refusing,
  which only happens on a genuine overlap.
* **The lock is released in a ``finally``, inside the endpoint.** The release
  runs whatever the locked run raises, and the interrupt, run-error and
  contention branches all still emit a payload, so a run that dies before its
  loop leaves no lock behind and still says why.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from . import (
    clock,
    config,
    control_signal,
    events,
    harnesses,
    plans_errors,
    plans_snapshot,
    review_guard,
    run_contention,
    run_entry_admission,
    run_inputs,
    run_outcome,
    run_prompts,
    state,
    state_locks,
    turn_execution_constants,
    verification_models,
)
from .run_entry_resume import _apply_resume_config, _ResumeUnavailable
from .run_errors import RunError, RunTerminatedBySignal, interrupt_message

#: Signals that end a locked run the way Ctrl-C does. SIGTERM is what `kill`,
#: a process supervisor or a logout sends; SIGHUP is a closed terminal. Left at
#: their defaults they end the process without a terminal record, a restored
#: TTY or a killed harness group -- and the harness runs in a session of its
#: own, so nothing else would ever stop it.
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def _termination_signals_interrupt() -> Iterator[None]:
    """Raise :class:`RunTerminatedBySignal` on a termination signal, then undo.

    Only the main thread can install a handler, and Python runs handlers there
    anyway, so from any other thread this changes nothing. A signal already set
    to be ignored -- ``nohup asl run`` -- stays ignored. The previous handlers
    come back on the way out, so a second signal while the interrupt is being
    reported takes its default action, like a second Ctrl-C.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _interrupt(signum: int, _frame: FrameType | None) -> None:
        raise RunTerminatedBySignal(signal.Signals(signum).name)

    previous: dict[signal.Signals, Any] = {}
    try:
        for signum in TERMINATION_SIGNALS:
            try:
                if signal.getsignal(signum) is signal.SIG_IGN:
                    continue
                previous[signum] = signal.signal(signum, _interrupt)
            except ValueError, OSError:
                continue
        yield
    finally:
        for restored, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(restored, handler)


class LockedRun(Protocol):
    """The locked run, as the entry point needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns ``run_locked``. The caller has already taken
    the plan's lock and opened the wakeup endpoint before this is invoked, and
    it returns the run's exit code paired with the payload the final emission
    prints; it never emits that payload itself.
    """

    def __call__(
        self,
        *,
        args: argparse.Namespace,
        cwd: Path,
        timeout: int | None,
        max_sessions: int | None,
        commit_mode: str | None,
        lock_info: state.LockInfo,
        run_id: str,
        reply_text: str | None,
        reply_origin: str,
        verification_context: verification_models.VerificationContext | None,
        wakeup: threading.Event | None,
        redirect_wakeup: threading.Event | None,
    ) -> tuple[int, dict[str, Any]]: ...


def run_from_args(
    args: argparse.Namespace,
    *,
    run_argv: list[str] | None = None,
    run_locked: LockedRun,
    reclaim_stale_locks: bool = True,
) -> int:
    """Run one or more pending sessions from CLI arguments.

    ``run_argv`` is the raw ``run`` subcommand argv used to detect which flags
    the operator passed explicitly during ``--resume`` rehydration. When it is
    not supplied, the argv the CLI parser recorded on the namespace
    (``args._run_argv``) is used, and only if that is absent does detection fall
    back to the process-global ``sys.argv``. This keeps explicit-flag provenance
    correct through ``cli.main(argv)`` and direct resolved-handler dispatch,
    where ``sys.argv`` may name an unrelated command.

    ``run_locked`` is the :class:`LockedRun` seam and is required rather than
    defaulted: it is called only once the plan's lock is held, and a default
    would let a caller start a run this module cannot finish.

    ``reclaim_stale_locks`` defaults to true, which is the contention check ASL
    has always performed: the scan reclaims every lock in the state root whose
    owner this host has proven dead. A launcher whose authorization covers only
    *this* plan passes false, because that hygiene reaches plans it was never
    granted — see ``overseer_isolation.launch``. It narrows the writing, never
    the checking: the overlap question, the refusal and its payload are
    identical either way. A launcher that spawns the run as a child process
    cannot pass that argument at all, so it sets
    ``run_contention.LOCK_ISOLATION_ENV_VAR`` on the child's environment
    instead; the parameter is narrowed from that marker below, which makes an
    in-process isolated call and a spawned isolated child the same run.
    """
    if run_contention.lock_isolation_requested():
        reclaim_stale_locks = False
    if run_argv is None:
        run_argv = getattr(args, "_run_argv", None)
    if getattr(args, "resume", False):
        try:
            args = _apply_resume_config(args, run_argv)
        except _ResumeUnavailable as error:
            payload = run_outcome._error_payload(
                args,
                reason="resume-unavailable",
                message=str(error),
                paused=False,
            )
            payload["resume_command"] = error.command
            payload["details"] = {"resume_command": error.command}
            run_outcome._emit_run_payload(args, payload)
            return 1

    try:
        cwd, timeout, max_sessions, commit_mode = run_inputs._validate_run_inputs(
            args,
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
    except (plans_errors.PlanError, RunError) as error:
        payload = run_outcome._error_payload(
            args,
            reason="input-error",
            message=str(error),
            paused=False,
        )
        run_outcome._emit_run_payload(args, payload)
        return 1

    reply_text, reply_origin, reply_refused = run_entry_admission.resolve_reply(
        args, cwd
    )
    if reply_refused:
        return 1

    try:
        initial_snapshot = plans_snapshot.inspect_plan(args.plan_dir)
    except plans_errors.PlanError as error:
        # A plan directory that is not there is an input error, not a pause:
        # nothing ran, and there is no plan a reply or a resume could reach.
        missing = isinstance(error, plans_errors.PlanNotFoundError)
        payload = run_outcome._error_payload(
            args,
            reason=plans_errors.plan_error_reason(error, "malformed-plan"),
            message=str(error),
            paused=not missing,
            cwd=cwd,
        )
        run_outcome._emit_run_payload(args, payload)
        return 1

    try:
        verification_context = run_inputs._resolve_run_verification_context(
            initial_snapshot.plan_dir,
            cwd,
        )
        run_inputs._validate_resume_workspace_binding(args, verification_context)
    except plans_errors.PlanError as error:
        payload = run_outcome._error_payload(
            args,
            reason="workspace-error",
            message=str(error),
            paused=True,
            cwd=cwd,
            snapshot=initial_snapshot,
        )
        run_outcome._emit_run_payload(args, payload)
        return 1

    # After root/workspace resolution and before anything this run launches.
    # Deliberately independent of `--allow-not-loop-ready`: the flag waives
    # readiness, and no readiness marking can stand in for the registry that
    # would have bound a second repository in the first place.
    intent_report = run_inputs._run_repository_intent_report(
        initial_snapshot.plan_dir,
        verification_context,
        state_dir_arg=args.state_dir,
    )
    if intent_report is not None:
        run_entry_admission.emit_repository_intent_refusal(
            args, initial_snapshot, verification_context, intent_report
        )
        return 1

    run_id = clock.utc_run_id()
    claimed_repository_roots = run_prompts._claimed_repository_roots(
        verification_context
    )
    # The endpoint opens before the lock that names it and closes after that
    # lock is gone, so every producer that can find the path finds this
    # process holding the read end. Ordering it this way is what lets the
    # producer skip authenticating an owner: it writes to a descriptor rather
    # than acting on a pid, and an endpoint outliving its reader refuses the
    # write instead of reaching whatever took the pid over.
    #
    # The urgent redirect endpoint opens on the same terms and inside the same
    # window, on its own FIFO and its own event. Publishing it is what makes
    # `redirect_admission` able to admit a submission at all: that module
    # refuses anything it cannot deliver urgently, so a lock without this path
    # means redirects against this run are declined rather than queued behind a
    # drain checkpoint. `redirect_wakeup` is served by the endpoint's reader
    # thread and is the entry point for the urgent controller.
    wakeup = threading.Event()
    redirect_wakeup = threading.Event()
    plan_state_dir = state.plan_state_dir(initial_snapshot.plan_dir, args.state_dir)
    with (
        control_signal.open_wakeup_endpoint(plan_state_dir, wakeup) as wakeup_endpoint,
        control_signal.open_redirect_endpoint(
            plan_state_dir, redirect_wakeup
        ) as redirect_endpoint,
    ):
        try:
            lock_info = state_locks.acquire_lock(
                initial_snapshot.plan_dir,
                args.state_dir,
                repository_roots=claimed_repository_roots,
                wakeup_endpoint=wakeup_endpoint,
                redirect_endpoint=redirect_endpoint,
            )
        except state.StateError as error:
            reason, paused = run_outcome._state_error_reason(error)
            payload = run_outcome._error_payload(
                args,
                reason=reason,
                message=str(error),
                paused=paused,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            run_outcome._emit_run_payload(args, payload)
            return 1

        try:
            with _termination_signals_interrupt():
                try:
                    # Checked after acquiring, not before: a check-before-acquire
                    # leaves two racing starters blind to each other and both
                    # proceed, whereas checking under our own published claim means
                    # the worst case is both refusing, which only happens on a
                    # genuine overlap.
                    if verification_context.workspace is not None:
                        conflicts = run_contention.detect_conflicts(
                            plan_dir=initial_snapshot.plan_dir,
                            repository_roots=claimed_repository_roots,
                            state_dir=args.state_dir,
                            reclaim=reclaim_stale_locks,
                        )
                        if conflicts:
                            payload = run_outcome._error_payload(
                                args,
                                reason=(
                                    events.PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION
                                ),
                                message=run_contention.conflict_message(
                                    initial_snapshot.plan_dir, conflicts
                                ),
                                paused=True,
                                cwd=verification_context.root,
                                snapshot=initial_snapshot,
                            )
                            contention_details = run_contention.conflict_details(
                                conflicts
                            )
                            payload["details"] = contention_details
                            payload["pause_details"] = dict(contention_details)
                            run_outcome._emit_run_payload(args, payload)
                            return 1
                    exit_code, payload = run_locked(
                        args=args,
                        cwd=verification_context.root,
                        verification_context=verification_context,
                        timeout=timeout,
                        max_sessions=max_sessions,
                        commit_mode=commit_mode,
                        lock_info=lock_info,
                        run_id=run_id,
                        reply_text=reply_text,
                        reply_origin=reply_origin,
                        wakeup=wakeup,
                        redirect_wakeup=redirect_wakeup,
                    )
                finally:
                    state_locks.release_lock(initial_snapshot.plan_dir, args.state_dir)
        except KeyboardInterrupt as interrupt:
            payload = run_outcome._error_payload(
                args,
                reason="interrupted",
                message=interrupt_message(interrupt),
                paused=True,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            run_outcome._emit_run_payload(args, payload)
            return turn_execution_constants.INTERRUPTED_EXIT
        except (
            config.ConfigError,
            harnesses.HarnessError,
            plans_errors.PlanError,
            RunError,
            state.StateError,
            OSError,
        ) as error:
            payload = run_outcome._error_payload(
                args,
                reason="run-error",
                message=str(error),
                paused=False,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            run_outcome._emit_run_payload(args, payload)
            return 1

    run_outcome._emit_run_payload(args, payload)
    return exit_code

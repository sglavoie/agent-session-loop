"""The locked run itself: one plan, one lock, one loop, one exit code.

Every other module in this package answers a question about a *part* of a run
-- how a turn is prepared, how its guards are enforced, how its outcome is
settled, how one handshake with the outside world stays correct. This module is
where those answers are sequenced into a run. It opens with the setup that a
run cannot start without, walks the readiness gates in the order the plan
requires, registers the control surfaces an operator can reach, and then enters
the ``while True`` skeleton that drives session after session until the plan
completes, pauses, or fails. When the loop leaves -- normally, by refusal, or
through a crash -- the finalization block below is what turns whatever happened
into a terminal record and an exit code.

:class:`RunContext` is the loop's memory. It is deliberately a plain dataclass
rather than a Protocol: the delegate modules each redeclare the *narrow*
structural view they need, so what lives here is the union of every field the
loop itself carries across a pass -- run-start collaborators that never change,
loop-pass bookkeeping reset or advanced per session, and the per-turn slots the
delegates read and write through the context instead of through 20 more
keyword arguments.

Nothing here imports the runner. That is not incidental: this module *is* the
run loop, so a runner import would be a cycle by construction rather than a
layering slip. Everything the loop needs enters as a parameter (``args``,
``cwd``, ``lock_info``, ``run_id``), through a package module that owns the
behaviour, or through the seams the delegate modules already publish. The
runner is left holding two argv entry points that call :func:`run_locked` and
nothing else.

Four properties of the loop are load-bearing and easy to break by accident:

* **The lock is already held.** :func:`run_locked` is called with the run lock
  taken and ``lock_info`` describing it. Nothing here acquires it, and every
  durable write below assumes it. Re-entering the runner's lock acquisition
  from inside this module would deadlock rather than fail loudly.
* **The exception guard is armed for the whole loop.** ``pending_exception_guard``
  is armed before the first pass and disarmed only on a clean terminalization.
  A ``BaseException`` escaping the loop body -- including ``KeyboardInterrupt``
  -- must reach the crash-path handler below, which is the only writer of the
  terminal record for an unplanned exit. Swallowing an exception inside the
  loop silently converts a crash into a hung, non-terminal run.
* **The retained-work settlement has one arming site and one call site.** A
  turn that clears the post-turn write guards arms ``ctx.retained_work``; the
  convergence point every loop exit falls through to is where that work is
  committed, if the pause the run recorded is one a machine can recover from
  and a commit mode makes the commit this run's to make. Both halves are
  placements rather than conditions: arming below the guards is what keeps a
  write-scope or protected-path refusal from ever reaching the settlement, and
  calling after the loop is what keeps the commit behind the revert that a
  repository-wide stage would otherwise commit the wrong side of.
* **The ``finally`` block is the only shutdown.** The mailbox poll's abort hook,
  the run guards' expiry, the control surfaces, and the inline tail are all
  stopped there. Returning early from a branch above without going through it
  leaks the poll thread and the control listener into the caller's process.


The phases the loop sequences live in the ``run_loop_*`` modules: setup in
``run_loop_setup_state`` and ``run_loop_setup_gates``, one pass of the loop in
``run_loop_turn_selection``, ``run_loop_turn_preparation``,
``run_loop_turn_execution`` and ``run_loop_turn_outcome``, and the crash handler
and final settlement in ``run_loop_finalization``. They share the
:class:`.run_loop_frame.RunFrame` this module builds."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

from . import (
    run_prompts,
    state,
    verification_models,
)
from .run_loop_finalization import (
    _finalize_run,
    _handle_run_crash,
    _record_run_crash,
    _stop_run_controls,
)
from .run_loop_frame import LoopSignal, RunFrame, RunResult
from .run_loop_setup_gates import _pass_readiness_gate, _prepare_review_and_resume
from .run_loop_setup_state import _open_run_state, _register_control_surfaces
from .run_loop_turn_execution import _execute_turn
from .run_loop_turn_outcome import _settle_turn_outcome
from .run_loop_turn_preparation import _prepare_turn_prompt, _stage_turn_launch
from .run_loop_turn_selection import _select_pending_session, _select_turn_harness

_SETUP_STEPS = (
    _open_run_state,
    _prepare_review_and_resume,
    _pass_readiness_gate,
    _register_control_surfaces,
)


_PASS_STEPS = (
    _select_pending_session,
    _select_turn_harness,
    _prepare_turn_prompt,
    _stage_turn_launch,
    _execute_turn,
    _settle_turn_outcome,
)


def _run_loop_pass(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Run one pass of the session loop through its steps in order."""
    for step in _PASS_STEPS:
        outcome = step(frame)
        if outcome is not None:
            return outcome
    return None


def run_locked(
    *,
    args: argparse.Namespace,
    cwd: Path,
    timeout: int | None,
    max_sessions: int | None,
    commit_mode: str | None,
    lock_info: state.LockInfo,
    run_id: str,
    reply_text: str | None = None,
    reply_origin: str = run_prompts.REPLY_ORIGIN_ARGV,
    verification_context: verification_models.VerificationContext | None = None,
    wakeup: threading.Event | None = None,
    redirect_wakeup: threading.Event | None = None,
) -> tuple[int, dict[str, Any]]:
    # The caller holds this run's lock and opened the wakeup endpoint before
    # publishing it, so `wakeup` is already being set by producers from here
    # on. A wakeup that lands outside the poll loop below only leaves the
    # event set for the next waiter to observe.
    frame = RunFrame(
        args=args,
        cwd=cwd,
        timeout=timeout,
        max_sessions=max_sessions,
        commit_mode=commit_mode,
        lock_info=lock_info,
        run_id=run_id,
        reply_text=reply_text,
        reply_origin=reply_origin,
        verification_context=verification_context,
        wakeup=wakeup,
        redirect_wakeup=redirect_wakeup,
    )
    # Setup and the settlement run outside the loop's crash handler, so this
    # outer guard is what still writes a terminal record and stops the
    # keyboard listener when either of them -- or the handler's re-raise --
    # lets an exception out.
    try:
        for setup_step in _SETUP_STEPS:
            early = setup_step(frame)
            if early is not None:
                return early
        try:
            while True:
                outcome = _run_loop_pass(frame)
                if isinstance(outcome, LoopSignal):
                    if outcome is LoopSignal.BREAK:
                        break
                    continue
                if outcome is not None:
                    return outcome
        except BaseException as error:  # noqa: BLE001
            _handle_run_crash(frame, error)
        # The one call site, at the point every one of the loop's breaks
        # converges on, so the settlement keeps the ordering it depends on.
        result = _finalize_run(frame)
        assert result is not None
        return result
    except BaseException as error:
        _record_run_crash(frame, error)
        raise
    finally:
        _stop_run_controls(frame)

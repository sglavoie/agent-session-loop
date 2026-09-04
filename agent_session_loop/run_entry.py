"""Where a run begins: the flags it resumes and the gate it must pass to start.

Everything else in this package runs *inside* a run. This boundary owns the
outside of it — the stretch between the CLI handing over a parsed namespace and
the loop taking the plan's lock, plus the single line that returns its exit
code. It is the package's public entry: ``cli.py`` reaches it by the string
``handler_ref="runner:run_from_args"``, and ``workbench.py``,
``maintenance.py`` and ``skills/asl/scripts/asl_autopilot.py`` call the runner's
delegating shim by name, so the name and the signature are a contract, not an
implementation detail.

Two halves live here.

* **Resume rehydration.** ``asl run --resume`` restores the flags of the plan's
  last run instead of asking the operator to retype them.
  :func:`_load_resume_state` reads the persisted ``state.json``,
  :func:`_apply_resume_config` overlays its ``run_config`` block onto the
  namespace for every dest in :data:`_RESUME_OVERLAY_DESTS`, and
  :func:`_explicit_resume_dests` decides which flags the operator passed *this*
  time so those win. When no usable config exists,
  :class:`_ResumeUnavailable` carries a best-effort ``asl run`` command that
  :func:`_reconstruct_resume_command` and :func:`_reconstructed_audit_flags`
  rebuild from the top-level fields the state document has always written, so
  the refusal hands the operator something to run rather than only a reason.
* **The admission sequence.** :func:`run_from_args` is the ordered gate a run
  passes before the loop exists: validate the flags, resolve the reply, inspect
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
run id at ``clock``, the interrupted exit code at ``turn_execution``, and the
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
import json
import shlex
import sys
import threading
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    config,
    control_signal,
    events,
    harnesses,
    plans,
    preflight,
    repository_registration,
    review_guard,
    run_contention,
    run_flags,
    run_inputs,
    run_outcome,
    run_prompts,
    state,
    supervisor_audit,
    turn_execution,
    verification,
)
from .run_errors import RunError


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
        verification_context: verification.VerificationContext | None,
        wakeup: threading.Event | None,
    ) -> tuple[int, dict[str, Any]]: ...


# Flag dests rehydrated from a persisted ``run_config`` by ``asl run --resume``.
# Derived from ``RUN_FLAG_SPEC`` so new ordinary run flags join the overlay
# automatically. Exclusions are flags that are positional, helper-owned, or
# persisted under a different key (``state_dir_override``) / canonical alias.
_RESUME_OVERLAY_EXCLUDED_DESTS = frozenset(
    {
        "dry_run",
        "json",
        "max_review_fix_plans",
        "plan_dir",
        "reply",
        "reply_stdin",
        "resume",
        "state_dir",
    }
)


def _resume_overlay_dests() -> tuple[str, ...]:
    seen: set[str] = set()
    dests: list[str] = []
    for flag in run_flags.RUN_FLAG_SPEC:
        if (
            flag.positional
            or flag.dest in _RESUME_OVERLAY_EXCLUDED_DESTS
            or flag.dest in seen
        ):
            continue
        seen.add(flag.dest)
        dests.append(flag.dest)
    return tuple(dests)


_RESUME_OVERLAY_DESTS = _resume_overlay_dests()


class _ResumeUnavailable(Exception):
    """Raised when ``--resume`` cannot rehydrate flags from persisted state.

    Carries a best-effort ``asl run`` command reconstructed from whatever fields
    the state file does persist, so the operator can re-run with explicit flags.
    """

    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


def _resume_run_tokens(run_argv: list[str] | None) -> list[str]:
    """Return the ``run`` subcommand's argv tokens, minus the subcommand name."""
    tokens = list(sys.argv[1:] if run_argv is None else run_argv)
    if tokens and tokens[0] == "run":
        tokens = tokens[1:]
    return tokens


def _explicit_resume_dests(run_argv: list[str] | None) -> set[str]:
    """Return the flag dests the operator passed explicitly on this resume.

    Re-parsing with ``mode="resume"`` (``argparse.SUPPRESS`` defaults) leaves
    only operator-supplied flags on the namespace, so ``vars()`` names exactly
    the flags whose values must win over the persisted ``run_config``.
    """
    parser = argparse.ArgumentParser(prog="asl run", add_help=False)
    run_flags.add_run_arguments(parser, mode="resume")
    try:
        namespace, _unknown = parser.parse_known_args(_resume_run_tokens(run_argv))
    except SystemExit:
        return set()
    explicit = set(vars(namespace))
    # The deprecated alias shares intent with the canonical flag; treat passing
    # either as an explicit cap so the persisted value never clobbers it.
    if "max_review_fix_plans" in explicit:
        explicit.add("max_review_fix_cycles")
    return explicit


def _reconstruct_resume_command(
    plan_dir: str,
    state_dir_override: str | None,
    payload: dict[str, Any],
) -> str:
    """Best-effort ``asl run`` command from top-level persisted state fields.

    Used only when ``run_config`` is absent (legacy or missing state): it re-emits
    the flags recoverable from the fields ``run_payloads._initial_payload`` has
    always written at the top level, so the operator can resume manually with
    explicit flags.
    """
    argv = ["asl", "run", str(plan_dir)]
    if state_dir_override:
        argv += ["--state-dir", str(state_dir_override)]
    cwd = payload.get("cwd")
    if cwd:
        argv += ["--cwd", str(cwd)]
    harness = payload.get("harness")
    if harness:
        argv += ["--harness", str(harness)]
    review_harness = payload.get("review_harness")
    if review_harness:
        argv += ["--review-harness", str(review_harness)]
    model = payload.get("model")
    if model:
        argv += ["--harness-model", str(model)]
    effort = payload.get("effort")
    if effort:
        argv += ["--effort", str(effort)]
    commit_mode = payload.get("commit_mode")
    if commit_mode == "per-session":
        argv.append("--commit-per-session")
    elif commit_mode == "end":
        argv.append("--commit-at-end")
    max_pending = payload.get("max_pending_sessions")
    if isinstance(max_pending, int):
        argv += ["--max-pending-sessions", str(max_pending)]
    note = payload.get("note")
    if note:
        argv += ["--note", str(note)]
    if payload.get("allow_not_loop_ready") is True:
        argv.append("--allow-not-loop-ready")
    argv += _reconstructed_audit_flags(payload.get("assurance"))
    return shlex.join(argv)


def _reconstructed_audit_flags(assurance: Any) -> list[str]:
    """Recover the audit flags from the assurance object state persisted.

    An inherited role needs no ``--audit-harness``: rebuilding ``--review-harness``
    reproduces it. Its resolved model is re-stated explicitly, which yields the
    same effective role because an explicit audit model wins over an inherited one.
    """
    if not isinstance(assurance, Mapping):
        return []
    argv: list[str] = []
    if assurance.get("state") == "skipped" and (
        assurance.get("reason") == supervisor_audit.REASON_OPTED_OUT
    ):
        return ["--no-supervisor-audit"]
    role = assurance.get("role")
    if not isinstance(role, Mapping):
        return argv
    if role.get("source") == "explicit" and role.get("harness"):
        argv += ["--audit-harness", str(role["harness"])]
    if role.get("model"):
        argv += ["--audit-harness-model", str(role["model"])]
    if role.get("effort"):
        argv += ["--audit-effort", str(role["effort"])]
    timeout = role.get("timeout_seconds")
    if (
        isinstance(timeout, (int, float))
        and int(timeout) != supervisor_audit.DEFAULT_AUDIT_TIMEOUT_SECONDS
    ):
        argv += ["--audit-timeout", str(int(timeout))]
    return argv


def _load_resume_state(
    plan_dir: str,
    state_dir_arg: str | None,
) -> dict[str, Any] | None:
    """Best-effort read of a plan's persisted ``state.json`` payload."""
    plan_state = state.plan_state_dir(plan_dir, state_dir_arg)
    state_path = plan_state / events.STATE_FILE_NAME
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _apply_resume_config(
    args: argparse.Namespace,
    run_argv: list[str] | None,
) -> argparse.Namespace:
    """Overlay a persisted ``run_config`` onto ``args`` for ``asl run --resume``.

    Explicitly-passed flags win; every other flag is restored from the
    ``run_config`` the plan's last run persisted. Raises :class:`_ResumeUnavailable`
    when no usable config exists so the caller can emit a structured
    ``resume-unavailable`` error.
    """
    state_dir_arg = getattr(args, "state_dir", None)
    payload = _load_resume_state(args.plan_dir, state_dir_arg)
    if payload is None:
        raise _ResumeUnavailable(
            "no runner state found for this plan; run it once so its "
            "run_config is persisted before resuming",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, {}),
        )
    run_config = payload.get("run_config")
    if not isinstance(run_config, dict):
        raise _ResumeUnavailable(
            "runner state predates run_config persistence; re-run with "
            "explicit flags instead of --resume",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, payload),
        )
    explicit = _explicit_resume_dests(run_argv)
    args._resume_workspace_config = {
        "workspace_root": run_config.get("workspace_root"),
        "registry_digest": run_config.get("registry_digest"),
        "repositories": run_config.get("repositories"),
    }
    for dest in _RESUME_OVERLAY_DESTS:
        if dest in explicit or dest not in run_config:
            continue
        setattr(args, dest, run_config[dest])
    return args


def run_from_args(
    args: argparse.Namespace,
    *,
    run_argv: list[str] | None = None,
    run_locked: LockedRun,
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
    """
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
    except (plans.PlanError, RunError) as error:
        payload = run_outcome._error_payload(
            args,
            reason="input-error",
            message=str(error),
            paused=False,
        )
        run_outcome._emit_run_payload(args, payload)
        return 1

    reply_text = getattr(args, "reply", None)
    reply_via_stdin = bool(getattr(args, "reply_stdin", False))
    reply_origin = run_prompts.REPLY_ORIGIN_ARGV
    if reply_via_stdin:
        if reply_text is not None:
            payload = run_outcome._error_payload(
                args,
                reason="conflicting-reply",
                message="--reply and --reply-stdin are mutually exclusive",
                paused=False,
                cwd=cwd,
            )
            run_outcome._emit_run_payload(args, payload)
            return 1
        try:
            reply_text = run_prompts.read_reply_from_stdin()
        except OSError as error:
            payload = run_outcome._error_payload(
                args,
                reason="reply-read-error",
                message=str(error),
                paused=False,
                cwd=cwd,
            )
            run_outcome._emit_run_payload(args, payload)
            return 1
        reply_origin = run_prompts.REPLY_ORIGIN_STDIN
    if reply_text is not None and not reply_text.strip():
        flag = "--reply-stdin" if reply_via_stdin else "--reply"
        payload = run_outcome._error_payload(
            args,
            reason="empty-reply",
            message=f"{flag} requires non-whitespace content",
            paused=False,
            cwd=cwd,
        )
        run_outcome._emit_run_payload(args, payload)
        return 1

    try:
        initial_snapshot = plans.inspect_plan(args.plan_dir)
    except plans.PlanError as error:
        payload = run_outcome._error_payload(
            args,
            reason="malformed-plan",
            message=str(error),
            paused=True,
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
    except plans.PlanError as error:
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
        registration, registration_error = run_inputs._apply_repository_registry(
            initial_snapshot,
            cwd=verification_context.root,
        )
        payload = run_outcome._error_payload(
            args,
            reason=events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            message=run_inputs._repository_intent_message(
                intent_report, registration=registration
            ),
            paused=True,
            cwd=verification_context.root,
            snapshot=initial_snapshot,
        )
        details: dict[str, Any] = {"repository_intent": intent_report}
        if registration is not None:
            details["repository_registration"] = repository_registration.result_payload(
                registration
            )
            if registration.status == "applied":
                details["auto_repairs"] = [
                    asdict(
                        preflight.AutoRepair(
                            action="repository-registry",
                            category="repository-registry-required",
                            path=str(initial_snapshot.plan_dir),
                            entries_written=len(registration.files),
                            completed_session_ids=(),
                            journal_replaced=False,
                        )
                    )
                ]
        if registration_error is not None:
            details["repository_registration_error"] = registration_error
        payload["details"] = details
        payload["pause_details"] = dict(details)
        run_outcome._emit_run_payload(args, payload)
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
    wakeup = threading.Event()
    with control_signal.open_wakeup_endpoint(
        state.plan_state_dir(initial_snapshot.plan_dir, args.state_dir),
        wakeup,
    ) as wakeup_endpoint:
        try:
            lock_info = state.acquire_lock(
                initial_snapshot.plan_dir,
                args.state_dir,
                repository_roots=claimed_repository_roots,
                wakeup_endpoint=wakeup_endpoint,
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
                        contention_details = run_contention.conflict_details(conflicts)
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
                )
            finally:
                state.release_lock(initial_snapshot.plan_dir, args.state_dir)
        except KeyboardInterrupt:
            payload = run_outcome._error_payload(
                args,
                reason="interrupted",
                message="run interrupted by user",
                paused=True,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            run_outcome._emit_run_payload(args, payload)
            return turn_execution.INTERRUPTED_EXIT
        except (
            config.ConfigError,
            harnesses.HarnessError,
            plans.PlanError,
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

# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Control dispatch and paused-run recovery for ASL interventions."""

from __future__ import annotations

from dataclasses import dataclass

from . import intervention_actions as _actions
from . import intervention_facts as _facts
from . import intervention_questions as _questions

for _module in (_facts, _actions, _questions):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )
del _actions, _facts, _module, _questions


@dataclass(frozen=True)
class ControlDispatch:
    """One durable control command plus its best-effort wakeup."""

    command: control_queue.ControlCommand
    queue_path: Path
    signal: control_signal.NotifyResult


def dispatch_control_command(
    plan_dir: str | Path,
    command: control_queue.CommandName,
    *,
    state_dir: str | Path | None = None,
    command_id: str | None = None,
    harness: str | None = None,
    harness_model: str | None = None,
    clear_harness_model: bool = False,
    effort: str | None = None,
    clear_effort: bool = False,
    grace_seconds: Any = None,
    reason: Any = None,
    deny_commands: Sequence[str] | None = None,
    protect_paths: Sequence[str] | None = None,
    guard_id: str | None = None,
    expected_run_id: str | None = None,
) -> ControlDispatch:
    """Append one control command, then wake the runner.

    The append is the delivery guarantee; the signal only decides how soon the
    runner notices, so a failed wakeup is reported rather than raised.
    """
    queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
    queued = control_queue.append_command(
        plan_dir,
        command,
        state_dir=state_dir,
        harness=harness,
        harness_model=harness_model,
        clear_harness_model=clear_harness_model,
        effort=effort,
        clear_effort=clear_effort,
        grace_seconds=grace_seconds,
        reason=reason,
        deny_commands=deny_commands,
        protect_paths=protect_paths,
        guard_id=guard_id,
        expected_run_id=expected_run_id,
        command_id=command_id,
    )
    signal = control_signal.notify_runner(plan_dir, state_dir=state_dir)
    return ControlDispatch(command=queued, queue_path=queue_path, signal=signal)


def pending_command_ids(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> frozenset[str]:
    """Return the command ids still sitting in the queue, read without draining."""
    queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
    try:
        text = queue_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    identifiers: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = record.get("id") if isinstance(record, Mapping) else None
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    return frozenset(identifiers)


def command_observed(
    plan_dir: str | Path,
    command_id: str,
    *,
    state_dir: str | Path | None = None,
) -> bool:
    """Whether the runner recorded its delivery receipt for one command."""
    events_path = state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A line still being appended is read whole on the next poll.
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("name") != events.RUN_CONTROL_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("action") == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
            and payload.get("command_id") == command_id
        ):
            return True
    return False


# --------------------------------------------------------------------------
# Paused-run resume service
# --------------------------------------------------------------------------


def resume_paused_run(
    plan_dir: str | Path,
    *,
    reply_text: str,
    state_dir: str | Path | None = None,
    profile: Any = None,
    **launch_kwargs: Any,
) -> Any:
    """Spawn ``asl run --resume --reply-stdin`` for a paused plan.

    ``--resume`` is what rehydrates the harness, model, effort, state dir, and
    workspace binding the original run persisted, so a resume never silently
    downgrades to this process's defaults. The reply reaches the child through
    stdin and therefore never appears in argv.
    """
    from . import workbench_launch

    return workbench_launch.launch_plan(
        plan_dir,
        profile=profile,
        reply_text=reply_text,
        state_dir=state_dir,
        resume=True,
        **launch_kwargs,
    )


def resume_recovered_run(
    plan_dir: str | Path,
    *,
    profile_fields: Mapping[str, Any] | None = None,
    state_dir: str | Path | None = None,
    **launch_kwargs: Any,
) -> Any:
    """Spawn ``asl run --resume`` for a paused plan, carrying no reply at all.

    The environmental-recovery counterpart to :func:`resume_paused_run`, added
    beside it rather than in place of it. A run paused by a checkpoint, a
    transport failure, or an unmet workspace precondition has nothing to answer,
    so the reply-oriented path could only be reached by inventing text --- and
    invented text becomes part of that run's own record. Here ``reply_text`` is
    ``None``, which is what makes the omission structural: ``launch_plan`` adds
    ``--reply-stdin`` and opens a pipe only when a reply is present, so there is
    no channel for words nobody said to travel down.

    ``profile_fields`` is a mapping rather than a ``LaunchProfile`` so a caller
    restoring a launch out of persisted run metadata does not have to import the
    launcher to reach one. An unknown key is refused rather than dropped: a
    field this service quietly ignored would be a launch setting the caller
    believes it restored and did not.

    ``isolate_locks=True`` is passed here rather than left to callers, for the
    same structural reason. This path exists for a recovery an overseer drives,
    and an overseer is authorized one plan at a time while the plans it was
    refused sit in the same state root; corpus-wide stale-lock reclamation in
    the spawned child would delete their lock files. A keyword every caller has
    to remember is a keyword one caller forgets, and the forgetting is silent —
    so the narrowing belongs to the route, not to its callers. The resumed
    plan's own lock and the overlap refusal are untouched by it, and
    :func:`resume_paused_run` keeps the ordinary hygiene an operator-driven
    launch should have.

    Launcher refusals are re-raised as :class:`InterventionError` under the
    launcher's own reason, so a caller behind this seam handles one error type
    and never needs the launcher in its own imports.
    """
    from . import workbench_launch, workbench_registry_models

    values = dict(profile_fields or {})
    known = {
        declared.name for declared in fields(workbench_registry_models.LaunchProfile)
    }
    unknown = sorted(set(values) - known)
    if unknown:
        raise InterventionError(
            REASON_UNKNOWN_PROFILE_FIELD,
            f"not launch profile fields: {', '.join(unknown)}",
            details={"fields": unknown},
        )
    try:
        return workbench_launch.launch_plan(
            plan_dir,
            profile=workbench_registry_models.LaunchProfile(**values),
            reply_text=None,
            state_dir=state_dir,
            resume=True,
            isolate_locks=True,
            **launch_kwargs,
        )
    except workbench_launch.LaunchError as error:
        raise InterventionError(error.reason, error.message) from error

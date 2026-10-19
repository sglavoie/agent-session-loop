"""Run-scoped control guard handlers."""

from __future__ import annotations

import argparse
from typing import Any

from . import cli_common, control_queue, events, guards, responses
from .control_cli_enqueue import _enqueue_and_report
from .control_cli_status import _run_active
from .control_cli_vocab import (
    COMMAND_GUARD_ADD,
    COMMAND_GUARD_LIST,
    COMMAND_GUARD_REMOVE,
    GUARD_ADD_NOTE,
    GUARD_REMOVE_NOTE,
)


def _active_run_id(status: dict[str, Any]) -> str | None:
    """The run ID a guard would be scoped to, or ``None`` if none is live."""
    if not _run_active(status):
        return None
    runner_state = status.get("runner_state")
    if not isinstance(runner_state, dict):
        return None
    run_id = runner_state.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _guard_no_active_run_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> int:
    """Refuse a guard mutation that has no run to scope itself to.

    Unlike every other control command, a guard cannot usefully wait in the
    queue: it applies to one run's process environment, and a queue line that
    outlives that run is refused at drain anyway. Refusing here is the same
    answer, delivered before anything is written.
    """
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason=guards.REJECT_NO_ACTIVE_RUN,
        error="no active run holds this plan, so there is nothing to guard",
        hint="start a run first; guards apply to one run and expire with it",
    )
    return 1


def plan_path_qualifiers(status: dict[str, Any]) -> tuple[str, ...]:
    """The repository qualifiers a protected-path rule may name for this plan.

    A registered plan's vocabulary is exactly its declared aliases. A legacy
    plan has one repository and no name for it, so ``.`` is the only qualifier
    — spelled explicitly rather than made optional, because a rule that reads
    ``docs/api.md`` in a plan that later gains a registry would silently mean
    something different.
    """
    repositories = status.get("repositories")
    if isinstance(repositories, list) and repositories:
        aliases = tuple(
            repository["alias"]
            for repository in repositories
            if isinstance(repository, dict) and isinstance(repository.get("alias"), str)
        )
        if aliases:
            return aliases
    return (guards.LEGACY_PATH_QUALIFIER,)


def control_guard_add_from_args(args: argparse.Namespace) -> int:
    """Add run-scoped command and protected-path guards to this plan's run."""
    deny_commands = list(getattr(args, "deny_command", None) or [])
    protect_paths = list(getattr(args, "protect_path", None) or [])
    reason = getattr(args, "reason", None)
    if not deny_commands and not protect_paths:
        cli_common.print_error(
            args,
            command_name=COMMAND_GUARD_ADD,
            text_label="control guard add",
            reason=control_queue.REJECT_MISSING_GUARD_RULE,
            error=(
                "guard add requires at least one --deny-command or --protect-path rule"
            ),
        )
        return 1
    # Parsed before the plan is even resolved: a malformed rule is the
    # operator's typo, and naming which token is wrong beats a generic
    # `invalid-command` from the enqueue path.
    for rule in deny_commands:
        try:
            guards.parse_command_rule(rule)
        except guards.GuardRuleError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_GUARD_ADD,
                text_label="control guard add",
                reason=error.reason,
                error=error.message,
                details={"deny_command": rule},
            )
            return 1
    # Syntax only, for the same reason: the alias check needs the plan's
    # registry, which is resolved below, and a backslash or a `..` is worth
    # naming before any of that work happens.
    for rule in protect_paths:
        try:
            guards.parse_protected_path_rule(rule)
        except guards.GuardRuleError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_GUARD_ADD,
                text_label="control guard add",
                reason=error.reason,
                error=error.message,
                details={"protect_path": rule},
            )
            return 1
    if reason is not None and (not isinstance(reason, str) or reason == ""):
        cli_common.print_error(
            args,
            command_name=COMMAND_GUARD_ADD,
            text_label="control guard add",
            reason=control_queue.REJECT_INVALID_REASON,
            error="--reason must be non-empty text when provided",
            details={"reason": repr(reason)},
        )
        return 1

    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_GUARD_ADD,
        text_label="control guard add",
    )
    if context is None:
        return 1
    qualifiers = plan_path_qualifiers(context.status)
    # Now that the plan is known, the alias half of the rule is checked against
    # the registry the run will actually enforce against. Doing it here rather
    # than at drain time means the operator learns about the typo while they
    # are still looking at the terminal they made it in.
    for rule in protect_paths:
        try:
            guards.parse_protected_path_rule(rule, aliases=qualifiers)
        except guards.GuardRuleError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_GUARD_ADD,
                text_label="control guard add",
                reason=error.reason,
                error=error.message,
                details={
                    "protect_path": rule,
                    "known_qualifiers": list(qualifiers),
                },
                hint=(
                    "name a repository alias this plan declares, or `.` for a "
                    "single-repository plan"
                ),
            )
            return 1
    run_id = _active_run_id(context.status)
    if run_id is None:
        return _guard_no_active_run_error(
            args,
            command_name=COMMAND_GUARD_ADD,
            text_label="control guard add",
        )

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        fields = {
            "run_id": run_id,
            "deny_commands": [list(rule) for rule in queued.deny_commands],
            "protect_paths": [path.rendered for path in queued.protect_paths],
            "reason": queued.reason,
            "note": GUARD_ADD_NOTE,
        }
        summary = (
            f"Queued guard add for {status['plan_dir']} (command {queued.id}); "
            f"{GUARD_ADD_NOTE}"
        )
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_GUARD_ADD,
        text_label="control guard add",
        queue_command=control_queue.COMMAND_ADD_GUARD,
        deny_commands=deny_commands,
        protect_paths=protect_paths,
        expected_run_id=run_id,
        reason=reason,
        terminal_actions=events.GUARD_TERMINAL_ACTIONS,
        terminal_requires_command_id=True,
        terminal_is_final=True,
        failure_actions=(events.RUN_CONTROL_ACTION_GUARD_REJECTED,),
        report=report,
    )


def control_guard_remove_from_args(args: argparse.Namespace) -> int:
    """Remove one run-scoped command guard by ID."""
    guard_id = getattr(args, "id", None)
    if not isinstance(guard_id, str) or not guard_id:
        cli_common.print_error(
            args,
            command_name=COMMAND_GUARD_REMOVE,
            text_label="control guard remove",
            reason=control_queue.REJECT_MISSING_GUARD_ID,
            error="guard remove requires --id",
        )
        return 1

    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_GUARD_REMOVE,
        text_label="control guard remove",
    )
    if context is None:
        return 1
    run_id = _active_run_id(context.status)
    if run_id is None:
        return _guard_no_active_run_error(
            args,
            command_name=COMMAND_GUARD_REMOVE,
            text_label="control guard remove",
        )

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        fields = {
            "run_id": run_id,
            "guard_id": guard_id,
            "note": GUARD_REMOVE_NOTE,
        }
        summary = (
            f"Queued guard remove {guard_id} for {status['plan_dir']} "
            f"(command {queued.id}); {GUARD_REMOVE_NOTE}"
        )
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_GUARD_REMOVE,
        text_label="control guard remove",
        queue_command=control_queue.COMMAND_REMOVE_GUARD,
        guard_id=guard_id,
        expected_run_id=run_id,
        terminal_actions=events.GUARD_TERMINAL_ACTIONS,
        terminal_requires_command_id=True,
        terminal_is_final=True,
        failure_actions=(events.RUN_CONTROL_ACTION_GUARD_REJECTED,),
        report=report,
    )


def control_guard_list_from_args(args: argparse.Namespace) -> int:
    """Report the guards the active run is currently enforcing.

    A pure read: it never enqueues, so it answers for a finished run too, where
    the honest answer is an empty set and expired enforcement.
    """
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_GUARD_LIST,
        text_label="control guard list",
    )
    if context is None:
        return 1
    status = context.status
    run_id = _active_run_id(status)
    runner_state = status.get("runner_state")
    control = runner_state.get("control") if isinstance(runner_state, dict) else None
    active = control.get("guards") if isinstance(control, dict) else None
    active = [entry for entry in active if isinstance(entry, dict)] if active else []
    if run_id is None:
        active = []
    payload = responses.success_payload(
        COMMAND_GUARD_LIST,
        plan_dir=str(context.snapshot.plan_dir),
        run_active=run_id is not None,
        run_id=run_id,
        guards=active,
        command_gate=dict(guards.COMMAND_GATE_COVERAGE),
        protected_paths=dict(guards.PROTECTED_PATH_COVERAGE),
    )
    if args.json:
        responses.print_json(payload)
    elif not active:
        print(
            f"No active guards for {status['plan_dir']}"
            if run_id is not None
            else f"No active run for {status['plan_dir']}; no guards are enforced"
        )
    else:
        print(f"Active guards for {status['plan_dir']} (run {run_id}):")
        for entry in active:
            print(f"  {entry.get('id')}  {_rendered_guard(entry)}")
            coverage = entry.get("coverage")
            if isinstance(coverage, dict):
                for lane in guards.COVERAGE_LANES:
                    verdict = coverage.get(lane)
                    if isinstance(verdict, dict):
                        print(
                            f"    {lane}: {verdict.get('status')} "
                            f"({verdict.get('reason')})"
                        )
    return 0


def _rendered_guard(entry: dict[str, Any]) -> str:
    """One line naming what a persisted guard entry forbids.

    Read from the persisted payload rather than reconstructed from a rule
    object: the entry may have been written by an older runner, and rendering
    ``deny:`` for a path guard would misreport which lane enforces it.
    """
    protected = entry.get("protected_path")
    if isinstance(protected, dict):
        return f"protect: {protected.get('qualifier')}:{protected.get('pattern')}"
    command = entry.get("command")
    rendered = (
        " ".join(str(token) for token in command) if isinstance(command, list) else ""
    )
    return f"deny: {rendered}"

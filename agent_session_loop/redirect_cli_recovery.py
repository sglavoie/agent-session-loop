# ruff: noqa: F403, F405
from .formatting import count_noun
from .redirect_cli_projection import *
from .redirect_cli_projection import _live_target
from .redirect_cli_vocab import *


def fresh_conversation_command(
    plan_dir: Path | str,
    state_dir: str | None = None,
) -> str:
    """Render the exact one-use relaunch an unresumable conversation needs."""
    parts = ["asl", "run", str(plan_dir), "--resume"]
    if state_dir:
        parts.extend(["--state-dir", state_dir])
    parts.append("--fresh-conversation")
    return shlex.join(parts)


def _state_dir_args(state_dir: str | None) -> list[str]:
    return ["--state-dir", state_dir] if state_dir else []


def recovery_commands(
    plan_dir: Path | str,
    state_dir: str | None = None,
    *,
    decision: str | None,
) -> dict[str, str | None]:
    """Render what an operator runs to inspect, then authorize, one recovery.

    The single renderer for both commands, for the same reason
    :func:`fresh_conversation_command` is the single renderer of the relaunch:
    the supervision helper, this CLI, and the bridge all report these two pauses,
    and three surfaces printing three slightly different commands for the same
    decision is how an operator ends up running the wrong one. The decision
    itself comes from :func:`intervention.recovery_decision`, so the mapping is
    not restated here either.
    """
    if decision == intervention.RECOVERY_DECISION_FRESH_CONVERSATION:
        return {
            "inspect_command": shlex.join(
                [
                    "asl",
                    "control",
                    "recover-conversation",
                    str(plan_dir),
                    *_state_dir_args(state_dir),
                    "--json",
                ]
            ),
            "authorization_command": fresh_conversation_command(plan_dir, state_dir),
        }
    if decision == intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE:
        return {
            "inspect_command": shlex.join(
                [
                    "asl",
                    "control",
                    "redirect-status",
                    str(plan_dir),
                    *_state_dir_args(state_dir),
                    "--json",
                ]
            ),
            # No authorization exists for this one: a predecessor that cannot be
            # shown stopped is a fact about the machine, and no text an operator
            # sends makes a second worker safe to start beside it.
            "authorization_command": None,
        }
    return {"inspect_command": None, "authorization_command": None}


def control_recover_conversation_from_args(args: argparse.Namespace) -> int:
    """Report whether a fresh conversation is needed, and how to authorize one.

    A read, and only a read. Performing the recovery here would persist the
    operator's one-use authorization in the one place it must never live: a
    command that can be repeated.
    """
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_RECOVER_CONVERSATION,
        text_label=TEXT_LABEL_RECOVER_CONVERSATION,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)

    runner_state = context.status.get("runner_state")
    runner_state = runner_state if isinstance(runner_state, dict) else {}
    pause_reason = runner_state.get("reason")
    recovery_required = bool(runner_state.get("paused")) and (
        pause_reason == events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
    )

    try:
        ledger = redirect_recovery.inspect_conversations(plan_dir, state_dir=state_dir)
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_RECOVER_CONVERSATION,
            text_label=TEXT_LABEL_RECOVER_CONVERSATION,
            reason="os-error",
            error=str(error),
        )
        return EXIT_REFUSED

    retained: list[dict[str, Any]] = []
    report = None
    try:
        report = redirect_recovery.inspect_plan_recovery(plan_dir, state_dir=state_dir)
    except redirect_store_models.RedirectStoreError, OSError:
        # The corrections a fresh start would carry are worth reporting when
        # they can be read and are not what this command is for when they
        # cannot: an unreadable journal must not hide the recovery route.
        report = None
    if report is not None:
        live_target = _live_target(plan_dir, state_dir)
        retained = [
            operation_payload(operation, live_target=live_target)
            for operation in (*report.unfinished, *report.uncertain)
        ]

    command = fresh_conversation_command(plan_dir, state_dir)
    payload = responses.success_payload(
        COMMAND_RECOVER_CONVERSATION,
        plan_dir=str(plan_dir),
        recovery_required=recovery_required,
        pause_reason=pause_reason if runner_state.get("paused") else None,
        conversations=ledger.to_payload(),
        fresh_conversation_command=command,
        one_use=True,
        note=FRESH_CONVERSATION_NOTE,
        retained_operations=retained,
        exit_code=EXIT_OK,
    )
    if args.json:
        responses.print_json(payload)
    else:
        if recovery_required:
            print(f"{plan_dir} is paused on {pause_reason}.")
        else:
            print(
                f"{plan_dir} is not paused on "
                f"{events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED}; "
                "the command below is what would authorize a fresh start."
            )
        print(
            "  Conversation ledger: "
            f"readable {'yes' if ledger.readable else 'no'}, "
            f"trustworthy {'yes' if ledger.trustworthy else 'no'}, "
            f"{count_noun(ledger.records, 'record')}"
        )
        print(f"  Authorize one fresh conversation: {command}")
        print(f"  {FRESH_CONVERSATION_NOTE}.")
        if retained:
            print(
                "  Corrections a fresh start still carries: "
                + ", ".join(
                    f"{operation['operation_id']} ({operation['stage']})"
                    for operation in retained
                )
            )
    return EXIT_OK


def control_resolve_redirect_from_args(args: argparse.Namespace) -> int:
    """An operator decision, serialized against runner startup by the plan lock."""
    from . import state

    command = "control-resolve-redirect"
    label = "control resolve-redirect"
    if args.outcome not in RESOLUTION_OUTCOMES:
        cli_common.print_error(
            args,
            command_name=command,
            text_label=label,
            reason=ERROR_INVALID_OUTCOME,
            error=(
                f"--outcome must be one of {', '.join(RESOLUTION_OUTCOMES)}; "
                f"got {args.outcome!r}"
            ),
        )
        return EXIT_REFUSED
    context = cli_common.resolve_plan_context(
        args, command_name=command, text_label=label
    )
    if context is None:
        return EXIT_REFUSED
    try:
        payload = redirect_restart.resolve_submission(
            context.snapshot.plan_dir,
            args.operation_id,
            outcome=args.outcome,
            reason=args.reason,
            state_dir=getattr(args, "state_dir", None),
        )
    except (
        OSError,
        ValueError,
        state.StateError,
        redirect_store_models.RedirectStoreError,
    ) as error:
        cli_common.print_error(
            args,
            command_name=command,
            text_label=label,
            reason="redirect-resolution-refused",
            error=str(error),
        )
        return EXIT_REFUSED
    if args.json:
        responses.print_json({"ok": True, "command": command, "resolution": payload})
    else:
        print(f"Recorded {args.outcome} for {args.operation_id}")
    return EXIT_OK

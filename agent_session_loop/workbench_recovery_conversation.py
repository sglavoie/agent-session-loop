"""Recovery builder for conversation-turn pauses and their resume commands."""

from __future__ import annotations

import shlex
from pathlib import Path

from . import (
    events,
    intervention,
    redirect_cli,
)
from .workbench_recovery_actions import _inspect_action
from .workbench_recovery_models import (
    RecoveryCopyCommand,
    RecoveryEvidenceRow,
    WorkbenchRecovery,
    _RecoveryBuildContext,
)
from .workbench_recovery_payload import _build_context_recovery_payload

#: What each conversation recovery decision means in the card's own words, and
#: what the operator is actually being asked to weigh. Keyed by the decision
#: `intervention.recovery_decision` returns rather than by the pause reason, so
#: adding a pause that maps to an existing decision needs no change here.
_CONVERSATION_RECOVERY_TITLES: dict[str, str] = {
    intervention.RECOVERY_DECISION_FRESH_CONVERSATION: (
        "Conversation cannot be resumed"
    ),
    intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE: (
        "Predecessor run is not known to be stopped"
    ),
}
_CONVERSATION_RECOVERY_SUMMARIES: dict[str, str] = {
    intervention.RECOVERY_DECISION_FRESH_CONVERSATION: (
        "The durable conversation bound to the pending session cannot be resumed "
        "exactly. Inspect the recorded binding, then either repair it or relaunch "
        "with the one-use --fresh-conversation authorization -- which discards the "
        "agent's accumulated context, so it is a decision rather than a button."
    ),
    intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE: (
        "A correction was accepted against a turn whose run cannot be shown "
        "stopped, so no successor may be started beside it. Inspect the redirect "
        "journal and the predecessor process; there is no authorization that "
        "makes a second worker safe."
    ),
}


def _build_conversation_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
    state_dir: str | Path | None,
) -> WorkbenchRecovery:
    """Report which recovery an unresumable conversation is waiting on.

    Read-only by construction, and that is the point of the card. Every action
    it offers is an inspect or a copyable command, because both decisions this
    kind covers are ones a person has to make: one discards an agent's context
    and its authorization is one-use, and the other has no authorization at all.
    An endpoint action here would either persist a one-use decision in a
    replayable request or invite an operator to start a second worker beside a
    live one.

    The decision and both commands come from :mod:`.intervention` and
    :mod:`.redirect_cli`, so this card, ``asl control recover-conversation``,
    and the bridge's recovery read cannot name three different recoveries for
    the same pause.
    """
    if reason in (
        events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
        events.PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE,
    ):
        base = ["asl", "control"]
        extra = ["--state-dir", str(state_dir)] if state_dir is not None else []
        detail = context.pause_details.get("redirect_recovery")
        operation_id = (
            detail.get("operation_id", "ID") if isinstance(detail, dict) else "ID"
        )
        return _build_context_recovery_payload(
            context,
            kind="conversation-recovery",
            title="Submission outcome needs an explicit decision",
            summary=(
                "Inspect native history before deciding whether the correction "
                "was submitted. A retry cannot resolve this uncertainty."
            ),
            severity="error",
            recoverable=True,
            artifacts=context.base_artifacts,
            evidence=(
                RecoveryEvidenceRow(
                    label="Pause reason",
                    value=reason,
                    detail=context.pause_message,
                    severity="error",
                ),
            ),
            actions=(
                _inspect_action("Inspect native history and the redirect journal."),
            ),
            commands=(
                RecoveryCopyCommand(
                    label="Inspect redirect status",
                    command=shlex.join(
                        base
                        + ["redirect-status", str(context.snapshot.plan_dir)]
                        + extra
                        + ["--json"]
                    ),
                    description="Read the recorded delivery evidence; writes nothing.",
                ),
                RecoveryCopyCommand(
                    label="Record a submission decision",
                    command=shlex.join(
                        base
                        + [
                            "resolve-redirect",
                            str(context.snapshot.plan_dir),
                            "--operation-id",
                            str(operation_id),
                            "--outcome",
                            "OUTCOME",
                            "--reason",
                            "EXPLANATION",
                        ]
                        + extra
                    ),
                    description=(
                        "Choose submitted or not-submitted from native evidence "
                        "and replace EXPLANATION. Neither outcome is a default."
                    ),
                ),
            ),
        )
    classified = intervention.recovery_decision(reason, paused=True)
    decision = classified.decision
    commands = redirect_cli.recovery_commands(
        context.snapshot.plan_dir,
        str(state_dir) if state_dir is not None else None,
        decision=decision,
    )
    inspect_command = commands["inspect_command"]
    authorization_command = commands["authorization_command"]

    evidence: list[RecoveryEvidenceRow] = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value=reason,
            detail=context.pause_message or None,
            severity="warning",
        ),
        RecoveryEvidenceRow(
            label="Decision",
            value=decision,
            detail=f"authorized by {classified.authorized_by}",
            severity="warning",
        ),
        # Stated rather than implied: neither pause is recoverable by the run
        # loop, so a reader must not take "paused" for "relaunch and see".
        RecoveryEvidenceRow(
            label="Automatic resume",
            value="no",
            detail="the run loop cannot clear this pause on its own",
            severity="warning",
        ),
    ]
    if authorization_command is not None:
        evidence.append(
            RecoveryEvidenceRow(
                label="Authorization",
                value="one-use",
                detail=redirect_cli.FRESH_CONVERSATION_NOTE,
                severity="warning",
            )
        )
    else:
        evidence.append(
            RecoveryEvidenceRow(
                label="Authorization",
                value="none exists",
                detail=(
                    "no text an operator sends makes a second worker safe to "
                    "start beside an unstopped predecessor"
                ),
                severity="error",
            )
        )

    copy_commands: list[RecoveryCopyCommand] = []
    if inspect_command is not None:
        copy_commands.append(
            RecoveryCopyCommand(
                label="Inspect recovery",
                command=inspect_command,
                description="Read the recorded binding and journal; writes nothing.",
            )
        )
    if authorization_command is not None:
        copy_commands.append(
            RecoveryCopyCommand(
                label="Authorize fresh conversation",
                command=authorization_command,
                description=redirect_cli.FRESH_CONVERSATION_NOTE,
            )
        )

    return _build_context_recovery_payload(
        context,
        kind="conversation-recovery",
        title=_CONVERSATION_RECOVERY_TITLES.get(
            decision or "", "Conversation recovery required"
        ),
        summary=_CONVERSATION_RECOVERY_SUMMARIES.get(
            decision or "",
            context.pause_message or "A conversation decision is required.",
        ),
        severity="warning" if authorization_command is not None else "error",
        # Recoverable, but only by a person: the commands above are the recovery,
        # and `actions` deliberately carries no endpoint that performs one.
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=tuple(evidence),
        actions=(
            _inspect_action(
                "Inspect the recorded conversation binding and redirect journal "
                "before deciding."
            ),
        ),
        commands=tuple(copy_commands),
    )

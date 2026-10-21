"""The paused-run ``interactive_help`` payload that tells an operator how to answer."""

from __future__ import annotations

import argparse
import shlex
from typing import Any

from . import plans_errors, qna


def _questions_command_text(plan_dir: str, state_dir_arg: str | None) -> str:
    parts = ["asl", "questions", plan_dir]
    if state_dir_arg:
        parts.extend(["--state-dir", state_dir_arg])
    return shlex.join(parts)


def _reply_command_text(plan_dir: str, state_dir_arg: str | None) -> str:
    # ``--resume`` rehydrates harness/model/effort from the persisted
    # run_config so a paused non-default-harness run never silently resumes on
    # the ``codex`` default.
    parts = ["asl", "run", plan_dir, "--resume"]
    if state_dir_arg:
        parts.extend(["--state-dir", state_dir_arg])
    parts.extend(["--reply", "TEXT"])
    return shlex.join(parts)


def _build_interactive_help(
    args: argparse.Namespace,
    plan_dir: str,
) -> dict[str, Any] | None:
    """Return a payload describing how an operator can resume a paused run.

    Returns ``None`` when paused-run Q&A context cannot be loaded (no paused
    state, missing artifacts, or session advanced underneath the pause), so
    the caller can keep its existing payload shape unchanged.
    """
    state_dir_arg = getattr(args, "state_dir", None)
    try:
        context = qna.load_qna_context(plan_dir, state_dir=state_dir_arg)
    except qna.QnAError, plans_errors.PlanError, OSError:
        return None
    # An unresumable conversation is the one pause with no question in it, so
    # it gets the recovery route in place of a reply command rather than
    # alongside one: an operator handed both would reasonably try the reply.
    recovery_required = qna.requires_conversation_recovery(context)
    return {
        "questions_command": _questions_command_text(plan_dir, state_dir_arg),
        "reply_command": (
            None if recovery_required else _reply_command_text(plan_dir, state_dir_arg)
        ),
        "recovery_required": recovery_required,
        "recovery_command": (
            qna.recovery_command_text(context) if recovery_required else None
        ),
        "session_id": context.session_id,
        "next_pending_id": context.next_pending_id,
        "turn_index": context.turn_index,
        "artifact_paths": qna.artifact_paths_payload(context),
        "rendered_prompt": qna.render_resolver_prompt(context),
    }


def _attach_interactive_help(
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach ``interactive_help`` to a paused payload when Q&A context loads."""
    if not payload.get("paused"):
        return payload
    plan_dir = payload.get("plan_dir") or getattr(args, "plan_dir", None)
    if not plan_dir:
        return payload
    help_payload = _build_interactive_help(args, str(plan_dir))
    if help_payload is not None:
        payload["interactive_help"] = help_payload
    return payload

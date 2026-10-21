"""Pre-lock refusals of the run admission sequence that need more than one line.

``run_entry.run_from_args`` is the ordered gate; two of its steps are long
enough to live here. :func:`resolve_reply` settles the ``--reply`` /
``--reply-stdin`` pair, and :func:`repository_intent_refusal` builds the
``repository-registry-required`` payload. Each emits its own refusal payload
through ``run_outcome`` and tells the caller whether to stop; the caller owns
the exit code and the order. Before the lock there is no ``state.json``, so a
refusal is an emitted payload and nothing durable.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_snapshot,
    preflight_auto_repair,
    repository_registration,
    run_inputs,
    run_outcome,
    run_prompts,
    verification_models,
)


def resolve_reply(args: argparse.Namespace, cwd: Path) -> tuple[str | None, str, bool]:
    """Return ``(reply_text, reply_origin, refused)`` for this run.

    ``refused`` is true when a refusal payload was already emitted and the run
    must stop with exit code 1.
    """
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
            return None, reply_origin, True
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
            return None, reply_origin, True
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
        return None, reply_origin, True
    return reply_text, reply_origin, False


def emit_repository_intent_refusal(
    args: argparse.Namespace,
    initial_snapshot: plans_snapshot.PlanSnapshot,
    verification_context: verification_models.VerificationContext,
    intent_report: Any,
) -> None:
    """Emit the ``repository-registry-required`` refusal payload."""
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
                    preflight_auto_repair.AutoRepair(
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

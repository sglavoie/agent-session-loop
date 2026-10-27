"""Argument validation and plan-context resolution for mailbox commands."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from . import (
    cli_common,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_constants,
    mailbox_cursors,
    mailbox_validation,
)
from .cli_parser_support import NON_NEGATIVE_INTEGER, flag_refusal


def body_from_args(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    inline_source: str = "--text",
) -> str | None:
    inline = getattr(args, "text", None)
    from_stdin = bool(getattr(args, "text_stdin", False))
    if inline is None and not from_stdin:
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox_constants.REJECT_MISSING_TEXT,
            error=(f"exactly one of {inline_source} and --text-stdin is required"),
        )
        return None
    if inline is not None and from_stdin:
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox_cli_constants.ERROR_INVALID_TEXT_SOURCE,
            error=(f"exactly one of {inline_source} and --text-stdin is required"),
        )
        return None
    if from_stdin:
        try:
            body: str | None = cli_common.read_stdin_text()
        except (OSError, TypeError, UnicodeError, ValueError) as error:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason=mailbox_cli_constants.ERROR_STDIN_READ,
                error=f"cannot read message text from stdin: {error}",
            )
            return None
    else:
        body = inline
    if not isinstance(body, str) or body.strip() == "":
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox_constants.REJECT_MISSING_TEXT,
            error="message text must not be blank",
        )
        return None
    return body


def optional_note_from_args(
    args: argparse.Namespace,
) -> tuple[bool, str | None]:
    """Return an optional resolution note while preserving an empty note."""
    inline = getattr(args, "note", None)
    from_stdin = bool(getattr(args, "note_stdin", False))
    if inline is not None and from_stdin:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=mailbox_cli_constants.ERROR_INVALID_NOTE_SOURCE,
            error="at most one of --note and --note-stdin may be supplied",
        )
        return False, None
    if not from_stdin:
        if inline is not None and not isinstance(inline, str):
            mailbox_cli_output.print_input_error(
                args,
                command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
                text_label="inbox resolve",
                reason=mailbox_constants.REJECT_INVALID_NOTE,
                error="resolution note must be text",
            )
            return False, None
        return True, inline
    try:
        note = cli_common.read_stdin_text()
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        cli_common.print_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=mailbox_cli_constants.ERROR_STDIN_READ,
            error=f"cannot read resolution note from stdin: {error}",
        )
        return False, None
    return True, note


def validated_timeout(
    args: argparse.Namespace,
    *,
    value: object,
    option: str,
    command_name: str,
    text_label: str,
) -> float | None:
    # Anything that is not already a number or a numeric string is out of
    # range by definition, and the shared out-of-range message below is the
    # right report for it -- NaN is how it gets there.
    try:
        timeout = float(value) if isinstance(value, int | float | str) else math.nan
    except ValueError:
        timeout = math.nan
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or timeout > mailbox_cli_constants.MAX_WAIT_TIMEOUT_SECONDS
    ):
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error=flag_refusal(
                option,
                "must be between 0 and "
                f"{int(mailbox_cli_constants.MAX_WAIT_TIMEOUT_SECONDS)} seconds",
                value,
            ),
        )
        return None
    return timeout


def validated_since(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> int | None:
    value = getattr(args, "since", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        error = flag_refusal("--since", NON_NEGATIVE_INTEGER, value)
    elif value > mailbox_constants.MAILBOX_MAX_INTEGER:
        # No mailbox sequence gets this high, and the reader refuses to scan
        # past one that could not be stored.
        error = flag_refusal(
            "--since",
            f"must be at most {mailbox_constants.MAILBOX_MAX_INTEGER}",
            value,
        )
    else:
        return value
    mailbox_cli_output.print_input_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason="invalid-since",
        error=error,
    )
    return None


def validated_include(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> str | None:
    value = getattr(args, "include", mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS)
    if value not in mailbox_cli_constants.OUTBOX_INCLUDE_MODES:
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-include",
            error="--include must be one of questions, notices, or all",
        )
        return None
    return value


def validated_supervision_id(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> tuple[str | None, bool]:
    """Return the launch correlation filter and whether it validated."""
    value = getattr(args, "supervision_id", None)
    if value is None:
        return None, True
    if not mailbox_validation.is_supervision_id(value):
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-supervision-id",
            error="--supervision-id must be a UUID",
        )
        return None, False
    return value, True


def resolve(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> cli_common.PlanCommandContext | None:
    return cli_common.resolve_plan_context(
        args,
        command_name=command_name,
        text_label=text_label,
    )


def plan_mailbox_path(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
) -> Path:
    return mailbox_cursors.mailbox_path_for_plan(
        context.snapshot.plan_dir, args.state_dir
    )

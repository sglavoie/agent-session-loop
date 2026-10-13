"""Reply and resolution argv sanitizing for persisted turn results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import (
    mailbox_vocabulary,
)


def reply_argv_placeholder(reply_length: int) -> str:
    return f"<asl reply prompt redacted: {reply_length} chars>"


def sanitize_reply_argv(
    argv: list[str] | tuple[str, ...],
    *,
    full_prompt: str,
    reply_length: int,
) -> list[str]:
    placeholder = reply_argv_placeholder(reply_length)
    return [placeholder if token == full_prompt else token for token in argv]


def resolution_argv_placeholder(
    action: mailbox_vocabulary.ResolutionAction | str,
    note_length: int | None,
) -> str:
    note_label = "omitted" if note_length is None else f"{note_length} chars"
    return f"<asl resolution prompt redacted: action={action}, note={note_label}>"


def sanitize_resolution_argv(
    argv: list[str] | tuple[str, ...],
    *,
    full_prompt: str,
    action: mailbox_vocabulary.ResolutionAction | str,
    note_length: int | None,
) -> list[str]:
    placeholder = resolution_argv_placeholder(action, note_length)
    return [placeholder if token == full_prompt else token for token in argv]


def persisted_result_argv(
    argv: list[str] | tuple[str, ...],
    *,
    live_turn: Mapping[str, Any],
    unredacted_prompt: str | None,
) -> list[str] | tuple[str, ...]:
    """Redact reply and resolution prompts before persisting result argv."""
    if unredacted_prompt is None:
        return argv

    persisted_argv: list[str] = [
        "<asl prompt redacted>" if token == unredacted_prompt else token
        for token in argv
    ]
    prompt_kind = live_turn.get("prompt_kind")
    reply_source = live_turn.get("reply_source")
    reply_length = (
        reply_source.get("reply_length") if isinstance(reply_source, dict) else None
    )
    if prompt_kind == "reply" and type(reply_length) is int:
        return sanitize_reply_argv(
            argv,
            full_prompt=unredacted_prompt,
            reply_length=reply_length,
        )

    resolution_source = live_turn.get("resolution_source")
    action = (
        resolution_source.get("action") if isinstance(resolution_source, dict) else None
    )
    note_length = (
        resolution_source.get("note_length")
        if isinstance(resolution_source, dict)
        else None
    )
    if (
        prompt_kind == "resolution"
        and action in mailbox_vocabulary.RESOLUTION_ACTIONS
        and (note_length is None or type(note_length) is int)
    ):
        return sanitize_resolution_argv(
            argv,
            full_prompt=unredacted_prompt,
            action=action,
            note_length=note_length,
        )
    return persisted_argv

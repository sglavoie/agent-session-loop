# ruff: noqa: F403, F405, I001
"""Bidirectional stream-JSON transport for the Claude CLI."""

from .claude_stream_support import *
from .claude_stream_control import _ClaudeStreamControl
from .claude_stream_guidance import _ClaudeStreamGuidance
from .claude_stream_io import _ClaudeStreamIO
from .claude_stream_state import _ClaudeStreamState


class ClaudeStreamSession(
    _ClaudeStreamGuidance, _ClaudeStreamControl, _ClaudeStreamIO, _ClaudeStreamState
):
    """One bidirectional stream-JSON conversation, scoped to one harness turn."""


__all__ = [
    "CONTROL_REQUEST",
    "CONTROL_RESPONSE",
    "GUIDANCE_CONTEXT_HEADING",
    "HOOK_EVENT",
    "INTERRUPT_TIMEOUT_SECONDS",
    "SUBTYPE_INTERRUPT",
    "TRANSPORT_STREAM_JSON",
    "ClaudeGuidanceSpool",
    "ClaudeStreamSession",
    "control_request",
    "hook_script_path",
    "new_session_id",
    "normalized_echo_body",
    "remove_overlay",
    "user_message",
    "write_settings_overlay",
]

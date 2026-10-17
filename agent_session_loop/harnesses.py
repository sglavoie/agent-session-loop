"""Harness registry and public adapter interface.

Shared contracts and execution live in the harness_types, harness_adapter,
harness_execution, and lifecycle modules. Each provider implements its own
adapter (harness_codex, harness_claude, harness_copilot), imported lazily by the
registry below. Existing callers can continue importing the shared types here.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from . import (
    guidance_delivery,
    harness_adapter,
    harness_conversation,
    harness_process,
    harness_transport,
    harness_types,
)
from .harness_adapter import HarnessAdapter
from .harness_conversation import (
    SubmissionCallback,
    TurnConversation,
    TurnSubmission,
    _SubmissionOnce,
)
from .harness_lifecycle import TurnLifecycle
from .harness_process import (
    AbortSignal,
    _join_readers,
    _kill_process_group,
    _snapshot_descendants,
    _sweep_process_table,
    _sweep_turn_descendants,
    _wait_for_turn_exit,
)
from .harness_transport import (
    HarnessTransport,
    _OutputProcessor,
    _read_last_message,
    _request_native_interrupt,
)
from .harness_types import (
    _ABANDONED_READER_JOIN_SECONDS,
    _ABORT_POLL_INTERVAL_SECONDS,
    _CONVERSATION_ABANDON_RETURNCODE,
    ACCESS_PROFILE_NAMES,
    CANCEL_STAGE_EXECUTION,
    CANCEL_STAGE_HANDSHAKE,
    CANCEL_STAGE_SHUTDOWN,
    CANCEL_STAGE_UNSTARTED,
    CANCEL_STAGES,
    HARNESS_NAMES,
    TRANSPORT_ACP,
    TRANSPORT_APP_SERVER,
    TRANSPORT_EXEC,
    TRANSPORT_STREAM_JSON,
    WORKER_ACCESS,
    AccessProfile,
    AccessProfileName,
    CancelStage,
    ConversationCallback,
    ConversationCaptureError,
    HarnessError,
    HarnessInvocation,
    HarnessName,
    HarnessResult,
    StreamCallback,
    StreamName,
    TurnCancelled,
    audit_read_only_access,
    prompt_provably_unsubmitted,
)


def _adapter_classes() -> tuple[type[harness_adapter.HarnessAdapter], ...]:
    """The shipped adapter classes, in :data:`HARNESS_NAMES` order.

    Imported here rather than at module top because each adapter module
    subclasses :class:`HarnessAdapter` and so imports this one: deferring the
    reverse edge to call time keeps the import graph one-way, so any of the
    four modules can be imported first.
    """
    from . import harness_claude, harness_codex, harness_copilot

    return (
        harness_codex.CodexAdapter,
        harness_claude.ClaudeAdapter,
        harness_copilot.CopilotAdapter,
    )


def get_adapter(name: str) -> harness_adapter.HarnessAdapter:
    """Return the adapter for a supported harness name."""
    for adapter in _adapter_classes():
        if adapter.name == name:
            return adapter()
    raise harness_types.HarnessError(f"unsupported harness: {name}")


def delivers_guidance_next_turn(name: str) -> bool:
    """Whether ``name``'s guidance arrives with the next turn's prompt.

    Asked by the delivery spine rather than answered there, so the fact lives
    with the adapter that knows its harness's protocol.
    """
    try:
        return get_adapter(name).delivers_guidance_next_turn
    except harness_types.HarnessError:
        return False


def run_harness(
    name: str,
    *,
    cwd: Path,
    plan_dir: Path | None,
    prompt: str,
    unsafe_bypass: bool = False,
    last_message_path: Path | None = None,
    timeout: int | None = None,
    stream_callback: harness_types.StreamCallback | None = None,
    model: str | None = None,
    effort: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_final_result: Callable[[], None] | None = None,
    on_observed_model: Callable[[str], None] | None = None,
    access: harness_types.AccessProfile = harness_types.WORKER_ACCESS,
    abort: harness_process.AbortSignal | None = None,
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
    env: Mapping[str, str] | None = None,
    on_conversation: harness_types.ConversationCallback | None = None,
    on_prompt_submitted: harness_conversation.SubmissionCallback | None = None,
    resume_conversation_id: str | None = None,
    on_transport: Callable[[harness_transport.HarnessTransport | None], None]
    | None = None,
) -> harness_types.HarnessResult:
    """Run one harness turn through the selected adapter.

    ``resume_conversation_id`` is the request half of continuity: the adapter
    builds its invocation to continue exactly that native conversation, or
    raises when it cannot. ``on_conversation`` is the capture half, called with
    the identifier the turn actually runs on before any work is submitted. They
    are independent -- a first turn captures without resuming -- and neither
    ever falls back to a different conversation.

    ``on_prompt_submitted`` is called at most once, from the point where this
    turn's prompt actually went out: the spawn for a turn that carries it on
    argv, the accepted ``turn/start`` or the written ``session/prompt`` for one
    that speaks a protocol. A turn that spawned and then refused -- a restore
    the provider declined, a cancellation caught mid-handshake -- never calls
    it, which is what lets a caller tell a delivered prompt from a started
    process.
    """
    return get_adapter(name).run(
        cwd=cwd,
        plan_dir=plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=last_message_path,
        timeout=timeout,
        stream_callback=stream_callback,
        model=model,
        effort=effort,
        on_spawn=on_spawn,
        on_final_result=on_final_result,
        on_observed_model=on_observed_model,
        access=access,
        abort=abort,
        live_delivery=live_delivery,
        env=env,
        on_conversation=on_conversation,
        on_prompt_submitted=on_prompt_submitted,
        resume_conversation_id=resume_conversation_id,
        on_transport=on_transport,
    )


# Single source for the skill-invocation prefix rule: derived from the adapters
# themselves so the mapping cannot drift from what each harness actually parses.
def skill_prefixes() -> dict[harness_types.HarnessName, str]:
    """The skill-invocation prefix each harness parses, keyed by harness name."""
    return {adapter.name: adapter.skill_prefix for adapter in _adapter_classes()}


def continues_conversations(name: str) -> bool:
    """Whether ``name`` can carry one conversation across turns.

    Read from the adapter classes rather than restated, so the capability a
    continuity caller is told about and the capability
    :meth:`HarnessAdapter.run` enforces are the same fact: an adapter cannot be
    advertised as wired without also accepting the calls that make it wired.
    It needs no local harness configuration and answers the same for an
    operator who has never installed the harness.
    """
    for adapter in _adapter_classes():
        if adapter.name == name:
            return bool(
                adapter.captures_conversations and adapter.resumes_conversations
            )
    return False


def detect_invoking_harness(
    env: Mapping[str, str] | None = None,
) -> harness_types.HarnessName | None:
    """Best-effort detection of the harness that launched this process.

    Returns the single harness whose environment markers are present, or
    ``None`` when zero or more than one match (ambiguous). Detection only
    orders human-facing output — it never gates behavior — so unknown or
    conflicting environments fall back to ``None`` and the default ordering.
    """
    source = os.environ if env is None else env
    detected: list[harness_types.HarnessName] = []
    if source.get("CLAUDECODE"):
        detected.append("claude")
    if any(key.startswith("CODEX_") for key in source):
        detected.append("codex")
    if any(
        key.startswith("COPILOT_") or key.startswith("GITHUB_COPILOT_")
        for key in source
    ):
        detected.append("copilot")
    if len(detected) == 1:
        return detected[0]
    return None


def _ordered_harness_names(
    env: Mapping[str, str] | None = None,
) -> list[harness_types.HarnessName]:
    """Harness names with the detected invoking harness (if any) first."""
    detected = detect_invoking_harness(env)
    if detected is None:
        return list(harness_types.HARNESS_NAMES)
    return [
        detected,
        *(name for name in harness_types.HARNESS_NAMES if name != detected),
    ]


def skill_invocations(
    skill: str,
    argument: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[harness_types.HarnessName, str]:
    """Return ready-to-paste ``<prefix><skill>[ <argument>]`` per harness.

    The mapping is ordered with the best-effort invoking harness first (when it
    is unambiguously detected) so callers can surface the most likely command at
    the top; ordering never changes which harnesses appear.
    """
    suffix = f" {argument}" if argument else ""
    prefixes = skill_prefixes()
    return {
        name: f"{prefixes[name]}{skill}{suffix}" for name in _ordered_harness_names(env)
    }


__all__ = [
    "ACCESS_PROFILE_NAMES",
    "AbortSignal",
    "AccessProfile",
    "AccessProfileName",
    "CANCEL_STAGES",
    "CANCEL_STAGE_EXECUTION",
    "CANCEL_STAGE_HANDSHAKE",
    "CANCEL_STAGE_SHUTDOWN",
    "CANCEL_STAGE_UNSTARTED",
    "CancelStage",
    "ConversationCallback",
    "ConversationCaptureError",
    "HARNESS_NAMES",
    "HarnessAdapter",
    "HarnessError",
    "HarnessInvocation",
    "HarnessName",
    "HarnessResult",
    "HarnessTransport",
    "StreamCallback",
    "StreamName",
    "SubmissionCallback",
    "TRANSPORT_ACP",
    "TRANSPORT_APP_SERVER",
    "TRANSPORT_EXEC",
    "TRANSPORT_STREAM_JSON",
    "TurnCancelled",
    "TurnConversation",
    "TurnLifecycle",
    "TurnSubmission",
    "WORKER_ACCESS",
    "_ABANDONED_READER_JOIN_SECONDS",
    "_ABORT_POLL_INTERVAL_SECONDS",
    "_CONVERSATION_ABANDON_RETURNCODE",
    "_OutputProcessor",
    "_SubmissionOnce",
    "_join_readers",
    "_kill_process_group",
    "_read_last_message",
    "_request_native_interrupt",
    "_snapshot_descendants",
    "_sweep_process_table",
    "_sweep_turn_descendants",
    "_wait_for_turn_exit",
    "audit_read_only_access",
    "audit_read_only_unavailable_reason",
    "continues_conversations",
    "delivers_guidance_next_turn",
    "detect_invoking_harness",
    "get_adapter",
    "prompt_provably_unsubmitted",
    "run_harness",
    "skill_invocations",
    "skill_prefixes",
]


def audit_read_only_unavailable_reason(name: str) -> str | None:
    """Say why ``name`` cannot run an audit read-only turn, or ``None``.

    A harness that cannot enforce the profile must be declared unavailable
    rather than run under a weaker one, so this returns a reason instead of a
    fallback.
    """
    try:
        adapter = get_adapter(name)
    except harness_types.HarnessError:
        return "unsupported-harness"
    if not adapter.enforces_audit_read_only:
        return "profile-unenforceable"
    if adapter.resolve_executable() is None:
        return "harness-not-installed"
    return None

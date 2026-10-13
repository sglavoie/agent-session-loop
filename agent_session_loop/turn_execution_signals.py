"""Runner control signals read from a harness result and live output."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import (
    events,
    harnesses,
    turn_execution_constants,
)


def signal_line_candidates(line: str) -> list[str]:
    stripped = line.strip()
    candidates = [stripped]
    for rendered_prefix in ("claude text: ",):
        if stripped.startswith(rendered_prefix):
            candidates.append(stripped[len(rendered_prefix) :].strip())
    return candidates


def extract_prefixed_line(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        for candidate in signal_line_candidates(line):
            if candidate.startswith(prefix):
                return candidate[len(prefix) :].strip()
    return None


def is_noop_runner_pause(message: str) -> bool:
    """Return True only for the exact no-op ``RUNNER_PAUSE: none`` marker.

    Agents are documented never to emit ``RUNNER_PAUSE: none``, but a stray
    placeholder must not fail closed into a real pause. The match is
    case-insensitive on the trimmed detail and matches nothing else, so every
    genuine pause reason keeps its fail-closed behavior.
    """
    return message.strip().casefold() == "none"


def runner_signal_lines(text: str) -> list[tuple[str, str, str]]:
    signals: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        for candidate in signal_line_candidates(line):
            if candidate.startswith(events.RUNNER_STATUS_PREFIX):
                signals.append(
                    (
                        "status",
                        candidate[len(events.RUNNER_STATUS_PREFIX) :].strip(),
                        candidate,
                    )
                )
                break
            if candidate.startswith(events.RUNNER_PAUSE_PREFIX):
                message = candidate[len(events.RUNNER_PAUSE_PREFIX) :].strip()
                if is_noop_runner_pause(message):
                    break
                signals.append(("pause", message, candidate))
                break
    return signals


def signal_source_trusted(source: str) -> bool:
    """Whether a stream may drive runner control state rather than only inform.

    ``stderr`` carries echoed prompts, tool arguments and file diffs, so a
    control marker seen there attests to nothing about what the agent decided.
    Both the post-turn signal read and the live stream watch route their source
    decision through here so the two cannot disagree about which stream is
    control and which is transcript.
    """
    return source in turn_execution_constants.TRUSTED_RUNNER_SIGNAL_SOURCES


def runner_signals(result: harnesses.HarnessResult) -> dict[str, Any]:
    degraded: dict[str, Any] | None = None
    for source, text in (
        ("last_message", result.last_message),
        ("stdout", result.stdout),
        ("stderr", result.stderr),
    ):
        source_signals = runner_signal_lines(text)
        if not source_signals:
            continue
        signal_kind, message, line = source_signals[-1]
        if not signal_source_trusted(source):
            degraded = {
                "source": source,
                "kind": signal_kind,
                "preview": preview_text(line),
            }
            continue
        signals: dict[str, Any] = {}
        if signal_kind == "status":
            signals["runner_status"] = message
            signals["runner_status_source"] = source
            if message.strip().lower() in {"pause", "paused"}:
                signals["runner_pause"] = "Runner status reported paused"
                signals["runner_pause_source"] = source
        else:
            signals["runner_pause"] = message
            signals["runner_pause_source"] = source
        return signals

    # Absence is itself evidence: an explicit record says the trusted streams
    # were read and carried no marker, and a marker refused on an untrusted
    # stream is demoted beside it rather than dropped.
    absent: dict[str, Any] = {
        "runner_status_source": turn_execution_constants.RUNNER_STATUS_SOURCE_ABSENT
    }
    if degraded is not None:
        absent["runner_status_degraded"] = degraded
    return absent


def runner_pause_reason(message: str) -> str:
    """Resolve a structured RUNNER_PAUSE reason, preserving legacy fallback."""
    text = message.strip()
    for separator in (" - ", ": "):
        prefix, found, _detail = text.partition(separator)
        if found and prefix in events.PAUSE_REASONS:
            return prefix
    return "runner-pause"


def preview_text(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def approval_match_is_content(line: str, position: int) -> bool:
    """Whether an approval phrase is text the agent wrote, not a prompt to it.

    Workers are launched unable to ask: codex gets ``-s workspace-write`` and
    claude ``--permission-mode dontAsk``. So a session editing approval-handling
    code streams the phrase far more often than a harness ever asks for one, and
    a bare substring match reads the agent's own diff as an interrupted run. A
    real prompt stands on its own line; a phrase inside a string literal or
    behind a diff marker is content the turn produced.
    """
    if line.lstrip()[:1] in {"+", "-", ">"}:
        return True
    prefix = line[:position]
    return prefix.count('"') % 2 == 1 or prefix.count("'") % 2 == 1


def live_action_signal(
    stream_name: harnesses.StreamName,
    text: str,
) -> dict[str, Any] | None:
    # Only the prefix branch is source-gated: an approval prompt on stderr is a
    # report about the harness, not an assertion the agent made about itself.
    if signal_source_trusted(stream_name):
        pause = extract_prefixed_line(text, events.RUNNER_PAUSE_PREFIX)
        if pause is not None and not is_noop_runner_pause(pause):
            return {
                "reason": "runner-pause",
                "message": pause,
                "source": stream_name,
                "matched": events.RUNNER_PAUSE_PREFIX,
            }

        status = extract_prefixed_line(text, events.RUNNER_STATUS_PREFIX)
        if status is not None and status.strip().lower() in {"pause", "paused"}:
            return {
                "reason": "runner-status-paused",
                "message": status,
                "source": stream_name,
                "matched": events.RUNNER_STATUS_PREFIX,
            }

    for line in text.splitlines():
        lowered = line.lower()
        for pattern in turn_execution_constants.APPROVAL_SIGNAL_PATTERNS:
            position = lowered.find(pattern)
            if position < 0 or approval_match_is_content(line, position):
                continue
            return {
                "reason": "approval-required",
                "message": preview_text(text),
                "source": stream_name,
                "matched": pattern,
            }

    return None


#: The reasons :func:`live_action_signal` can produce. Each names an
#: intervention the agent asked for mid-turn, so each is only current while the
#: run is still stopped on it. A reason the runner itself declared — review
#: findings above all — is not a live signal and is never cleared here. The set
#: lives in :mod:`agent_session_loop.events` because the supervisor audit
#: retires the same reasons on the bundle's status projection.
LIVE_ACTION_REQUIRED_REASONS = events.LIVE_ACTION_REQUIRED_REASONS


def clear_live_action_required(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Drop a live action signal the run has already moved past.

    ``action_required`` on the run root is the current unresolved intervention,
    not the run's history: the turn result keeps the historical record. Starting
    another turn or finishing unpaused both prove the signal was answered, so
    leaving it set would make a terminal, unpaused run report a pending ask.
    Returns the cleared record, or ``None`` when nothing was cleared.
    """

    current = payload.get("action_required")
    if not isinstance(current, Mapping):
        return None
    if current.get("reason") not in LIVE_ACTION_REQUIRED_REASONS:
        return None
    payload["action_required"] = None
    return dict(current)

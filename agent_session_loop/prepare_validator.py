"""Launching validator workers and decoding their JSON output."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import harnesses
from .plans import PlanError
from .prepare_packet import ValidationTaskPacket
from .prepare_parsing import _fail, parse_validator_output

ValidatorRunner = Callable[[ValidationTaskPacket], object]


def _extract_json_object(text: str) -> object:
    """Decode a validator's final message as one JSON object, fail-closed.

    Tolerates a single surrounding markdown code fence (workers are told not
    to emit one, but a fenced JSON object is unambiguous); anything else that
    fails to decode raises :class:`PlanError`.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as error:
        raise _fail(
            f"validator returned invalid JSON: {error.msg} at line {error.lineno}"
        ) from error


VALIDATOR_REPAIR_ATTEMPTS = 2


def _validator_repair_prompt(
    packet: ValidationTaskPacket, rejected_text: str, error: PlanError
) -> str:
    """Ask a validator to fix only the output shape the parser rejected."""
    return "\n".join(
        [
            packet.render(),
            "",
            "## Repair your previous answer",
            (
                "Your previous answer, below, was rejected by the fail-closed "
                f"output parser: {error}"
            ),
            (
                "Do not investigate the codebase again. Return the complete "
                "corrected JSON object only, changing nothing but what the "
                "output schema above requires, and check every other entry "
                "for the same mistake."
            ),
            "",
            "Previous answer:",
            rejected_text.strip(),
        ]
    )


def _launch_validator(
    harness_name: str,
    packet: ValidationTaskPacket,
    *,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
) -> object:
    """Run one validator turn, repairing a rejected output shape in place.

    Validators sometimes return sound findings in a slightly wrong shape (a
    missing ``repo``, a stray key). A rejected answer goes back to the same
    harness with the parser's exact error, at most
    :data:`VALIDATOR_REPAIR_ATTEMPTS` times; the last answer is returned either
    way, so the caller's fail-closed parse still has the final word.
    """
    prompt = packet.render()
    for attempt in range(VALIDATOR_REPAIR_ATTEMPTS + 1):
        text = _run_validator_turn(
            harness_name,
            packet,
            prompt,
            model=model,
            effort=effort,
            timeout=timeout,
        )
        try:
            payload = _extract_json_object(text)
            parse_validator_output(payload)
        except PlanError as error:
            if attempt == VALIDATOR_REPAIR_ATTEMPTS:
                raise
            print(
                f"asl prepare: validator output rejected ({error}); asking "
                f"{harness_name} to repair it "
                f"({attempt + 1}/{VALIDATOR_REPAIR_ATTEMPTS})",
                file=sys.stderr,
                flush=True,
            )
            prompt = _validator_repair_prompt(packet, text, error)
            continue
        return payload
    raise AssertionError("unreachable")


def _run_validator_turn(
    harness_name: str,
    packet: ValidationTaskPacket,
    prompt: str,
    *,
    model: str | None,
    effort: str | None,
    timeout: int | None,
) -> str:
    """Run one read-only validator harness turn and return its last message."""
    read_roots = tuple(
        dict.fromkeys((Path(packet.codebase_root), Path(packet.plan_dir))).keys()
    )
    with tempfile.TemporaryDirectory(prefix="asl-prepare-") as tmp:
        last_message_path = Path(tmp) / "validator-last-message.txt"
        try:
            result = harnesses.run_harness(
                harness_name,
                cwd=Path(packet.codebase_root),
                plan_dir=None,
                prompt=prompt,
                # Preparation validation is research-only. It must never
                # inherit the implementation worker's unsafe-bypass setting.
                unsafe_bypass=False,
                last_message_path=last_message_path,
                timeout=timeout,
                model=model,
                effort=effort,
                access=harnesses.audit_read_only_access(read_roots),
            )
        except harnesses.HarnessError as error:
            raise PlanError(
                f"validator harness {harness_name!r} failed to launch: {error}"
            ) from error
    if result.returncode != 0:
        snippet_lines = (result.stderr or result.stdout).strip().splitlines()
        detail = snippet_lines[-1].strip() if snippet_lines else ""
        message = f"validator harness {harness_name!r} exited {result.returncode}"
        if detail:
            message += f": {detail}"
        raise PlanError(message)
    if not result.last_message.strip():
        raise PlanError(f"validator harness {harness_name!r} returned no output")
    return result.last_message

"""Resume rehydration for ``asl run --resume``.

``asl run --resume`` restores the flags of the plan's last run instead of asking
the operator to retype them. :func:`_load_resume_state` reads the persisted
``state.json``, :func:`_apply_resume_config` overlays its ``run_config`` block
onto the namespace for every dest in :data:`_RESUME_OVERLAY_DESTS`, and
:func:`_explicit_resume_dests` decides which flags the operator passed *this*
time so those win. When no usable config exists, :class:`_ResumeUnavailable`
carries a best-effort ``asl run`` command built by
``run_entry_resume_command``.

Invariants:

* **An explicit flag always beats a persisted one.** The overlay skips any dest
  :func:`_explicit_resume_dests` names, and the deprecated
  ``--max-review-fix-plans`` alias marks its canonical dest explicit too.
* **A dest missing from the persisted block is skipped silently.** The overlay
  leaves such a flag at its parser default rather than failing, which is why
  ``run_payloads._run_config_payload`` must seed every dest in
  :data:`_RESUME_OVERLAY_DESTS` — a contract
  ``tests/test_asl_run_payloads.py`` pins on the writer's side.

This module reads the state file and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import events, run_flags, state
from .run_entry_resume_command import _reconstruct_resume_command

# Flag dests rehydrated from a persisted ``run_config`` by ``asl run --resume``.
# Derived from ``RUN_FLAG_SPEC`` so new ordinary run flags join the overlay
# automatically. Exclusions are flags that are positional, helper-owned, or
# persisted under a different key (``state_dir_override``) / canonical alias.
#
# ``fresh_conversation`` is excluded for a stronger reason than the others, and
# the exclusion is load-bearing rather than tidy: it is a one-use authorization
# to discard an agent's accumulated conversation, granted for the one turn that
# needs it. An overlaid flag is replayed on every later ``--resume``, so leaving
# it in would turn a single deliberate "start over" into a standing licence to
# start over — silently, on runs the operator never re-authorized. Its
# counterpart at the other replay surface is
# ``asl_autopilot._CHILD_RUN_ARG_SKIP_DESTS``; both have to hold for the
# authorization to stay single-use, and ``run_payloads._run_config_payload``
# completes the containment by persisting no key for it at all.
_RESUME_OVERLAY_EXCLUDED_DESTS = frozenset(
    {
        "dry_run",
        "fresh_conversation",
        "json",
        "max_review_fix_plans",
        "plan_dir",
        "reply",
        "reply_stdin",
        "resume",
        "state_dir",
    }
)


def _resume_overlay_dests() -> tuple[str, ...]:
    seen: set[str] = set()
    dests: list[str] = []
    for flag in run_flags.RUN_FLAG_SPEC:
        if (
            flag.positional
            or flag.dest in _RESUME_OVERLAY_EXCLUDED_DESTS
            or flag.dest in seen
        ):
            continue
        seen.add(flag.dest)
        dests.append(flag.dest)
    return tuple(dests)


_RESUME_OVERLAY_DESTS = _resume_overlay_dests()


class _ResumeUnavailable(Exception):
    """Raised when ``--resume`` cannot rehydrate flags from persisted state.

    Carries a best-effort ``asl run`` command reconstructed from whatever fields
    the state file does persist, so the operator can re-run with explicit flags.
    """

    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


def _resume_run_tokens(run_argv: list[str] | None) -> list[str]:
    """Return the ``run`` subcommand's argv tokens, minus the subcommand name."""
    tokens = list(sys.argv[1:] if run_argv is None else run_argv)
    if tokens and tokens[0] == "run":
        tokens = tokens[1:]
    return tokens


def _explicit_resume_dests(run_argv: list[str] | None) -> set[str]:
    """Return the flag dests the operator passed explicitly on this resume.

    Re-parsing with ``mode="resume"`` (``argparse.SUPPRESS`` defaults) leaves
    only operator-supplied flags on the namespace, so ``vars()`` names exactly
    the flags whose values must win over the persisted ``run_config``.
    """
    parser = argparse.ArgumentParser(prog="asl run", add_help=False)
    run_flags.add_run_arguments(parser, mode="resume")
    try:
        namespace, _unknown = parser.parse_known_args(_resume_run_tokens(run_argv))
    except SystemExit:
        return set()
    explicit = set(vars(namespace))
    # The deprecated alias shares intent with the canonical flag; treat passing
    # either as an explicit cap so the persisted value never clobbers it.
    if "max_review_fix_plans" in explicit:
        explicit.add("max_review_fix_cycles")
    return explicit


def _load_resume_state(
    plan_dir: str,
    state_dir_arg: str | None,
) -> dict[str, Any] | None:
    """Best-effort read of a plan's persisted ``state.json`` payload."""
    plan_state = state.plan_state_dir(plan_dir, state_dir_arg)
    state_path = plan_state / events.STATE_FILE_NAME
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError, ValueError:
        # ValueError rather than only OSError: bytes that are not UTF-8 raise
        # UnicodeDecodeError while reading, before any JSON is parsed.
        return None
    try:
        payload = json.loads(raw)
    except ValueError, RecursionError:
        return None
    return payload if isinstance(payload, dict) else None


def _apply_resume_config(
    args: argparse.Namespace,
    run_argv: list[str] | None,
) -> argparse.Namespace:
    """Overlay a persisted ``run_config`` onto ``args`` for ``asl run --resume``.

    Explicitly-passed flags win; every other flag is restored from the
    ``run_config`` the plan's last run persisted. Raises :class:`_ResumeUnavailable`
    when no usable config exists so the caller can emit a structured
    ``resume-unavailable`` error.
    """
    state_dir_arg = getattr(args, "state_dir", None)
    payload = _load_resume_state(args.plan_dir, state_dir_arg)
    if payload is None:
        raise _ResumeUnavailable(
            "no runner state found for this plan; run it once so its "
            "run_config is persisted before resuming",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, {}),
        )
    run_config = payload.get("run_config")
    if not isinstance(run_config, dict):
        raise _ResumeUnavailable(
            "runner state predates run_config persistence; re-run with "
            "explicit flags instead of --resume",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, payload),
        )
    explicit = _explicit_resume_dests(run_argv)
    args._restored_external_write_roots = (
        run_config.get("external_write_root", [])
        if "external_write_root" not in explicit
        else []
    )
    args._resume_workspace_config = {
        "workspace_root": run_config.get("workspace_root"),
        "registry_digest": run_config.get("registry_digest"),
        "repositories": run_config.get("repositories"),
    }
    for dest in _RESUME_OVERLAY_DESTS:
        if dest in explicit or dest not in run_config:
            continue
        setattr(args, dest, run_config[dest])
    return args

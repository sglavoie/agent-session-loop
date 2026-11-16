"""What this machine's own runs say about an installed harness version.

``asl harness-compat`` compares a CLI version with the window this build was
smoke-tested against, and harness CLIs update themselves far more often than
that window moves, so the answer is very often "newer than tested". That
warning is true every day and therefore says nothing new. The state corpus
knows something the pinned window cannot: whether turns have actually
completed on that exact version here. This module reads that, and nothing
else; it never changes a classification or an exit status: it changes what is said
beside an unknown version, and it stops ``asl harness-compat`` from marking as
a warning a version that this machine has already run many turns on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import events, state, state_plan_refs

#: How far back a run still counts as evidence about the current version.
WINDOW_DAYS = 30


def _run_used_version(run: dict[str, Any], harness: str, version: str) -> bool:
    """Whether a run's readiness gate recorded ``harness`` at ``version``."""
    gate = run.get("readiness_gate")
    findings = gate.get("findings") if isinstance(gate, dict) else None
    if not isinstance(findings, list):
        return False
    marker = f"{harness} {version} "
    return any(
        isinstance(finding, dict)
        and finding.get("category") == "env-harness"
        and str(finding.get("detail") or "").startswith(marker)
        for finding in findings
    )


def completed_turns(
    harness: str,
    version: str | None,
    *,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Count turns that completed on ``harness`` at ``version`` in the window.

    Each plan keeps only its latest run in ``state.json``, so the count is a
    floor, never an estimate. ``None`` when there is no version to look for or
    the corpus cannot be read; a readable corpus with no such turn returns a
    zero count, which is itself worth saying.
    """
    if not version:
        return None
    cutoff = (time.time() if now is None else now) - WINDOW_DAYS * 86400
    turns = 0
    runs = 0
    harness_turns = 0
    try:
        state_dirs = list(
            state_plan_refs.iter_plan_state_dirs(state.state_root(state_dir))
        )
    except state.StateError, OSError:
        return None
    for plan_state_dir in state_dirs:
        path = plan_state_dir / events.STATE_FILE_NAME
        try:
            if path.stat().st_mtime < cutoff:
                continue
            run = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError, RecursionError:
            continue
        if not isinstance(run, dict):
            continue
        # Any turn at all on this harness, whatever its version or outcome:
        # what tells a harness in use from one that is merely installed.
        harness_turns += sum(
            1
            for turn in run.get("turns") or []
            if isinstance(turn, dict) and turn.get("harness") == harness
        )
        if not _run_used_version(run, harness, version):
            continue
        completed = sum(
            1
            for turn in run.get("turns") or []
            if isinstance(turn, dict)
            and turn.get("harness") == harness
            and turn.get("returncode") == 0
            and turn.get("status_result") == "complete"
        )
        if completed:
            turns += completed
            runs += 1
    return {
        "completed_turns": turns,
        "runs": runs,
        "window_days": WINDOW_DAYS,
        "proven": turns >= PROVEN_TURNS,
        "harness_turns": harness_turns,
        "in_use": harness_turns > 0,
    }


#: Completed turns on one exact version that count as proof it works here.
#: Ten is far past a lucky run: each is a whole session that started, worked,
#: reported its status and exited cleanly on that CLI.
PROVEN_TURNS = 10


def proven(evidence: dict[str, Any] | None) -> bool:
    """Whether this machine's own runs vouch for the version.

    The pinned window answers "was this build smoke-tested against it"; this
    answers "does it work here", from turns that really completed. It never
    moves the window and never unblocks a version the policy blocks.
    """
    return evidence is not None and evidence["completed_turns"] >= PROVEN_TURNS


def in_use(evidence: dict[str, Any] | None) -> bool:
    """Whether the harness has run here at all inside the window.

    A warning about a CLI that is installed but never launched cannot affect
    a run, and it is what made the report warn about every harness every day.
    Unknown evidence counts as in use, so nothing is quietened on a guess.
    """
    return evidence is None or bool(evidence.get("in_use", True))


def describe(evidence: dict[str, Any] | None) -> str:
    """The clause a warning gains from ``evidence``; empty when there is none."""
    if evidence is None:
        return ""
    turns = evidence["completed_turns"]
    window = evidence["window_days"]
    if not evidence.get("in_use", True):
        return f"; this harness has not run here in the last {window} days"
    if not turns:
        return f"; no turn has completed on it here in the last {window} days"
    return (
        f"; at least {turns} turn(s) across {evidence['runs']} run(s) completed "
        f"on it here in the last {window} days"
    )

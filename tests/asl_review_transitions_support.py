from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    review,
    review_continuation,
    review_repair,
    review_transitions,
)
from agent_session_loop.transitions import IllegalTransitionError

VERDICT_MACHINE = review_transitions.REVIEW_VERDICT_MACHINE
CYCLE_MACHINE = review_transitions.REVIEW_CYCLE_MACHINE


def _delta(
    current: tuple[str, ...] | None = (),
    previous: tuple[str, ...] = (),
    *,
    current_keys: tuple[str, ...] | None = None,
    previous_keys: tuple[str, ...] | None = None,
) -> review_transitions.FindingDelta:
    return review_transitions.diff_review_findings(
        fingerprints=current,
        repeat_keys=current if current_keys is None else current_keys,
        previous_fingerprints=previous,
        previous_repeat_keys=previous if previous_keys is None else previous_keys,
    )


@dataclass(frozen=True)
class _Group:
    """The one attribute the exhaustion check reads off a planned group."""

    repair_group: str

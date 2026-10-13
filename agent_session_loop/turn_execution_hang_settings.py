"""Environment-tunable hang detection thresholds."""

from __future__ import annotations

import math
import os

from . import (
    turn_execution_constants,
)


def _hang_env_seconds(name: str, default: float) -> float:
    """Return a finite positive float override from ``name`` or ``default``.

    Modeled on :func:`agent_mux_delivery.notify_timeout`: anything unparsable,
    non-positive, or non-finite fails safe onto the default so a bad
    override can never disable or destabilize hang detection.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if value <= 0 or not math.isfinite(value):
        return default
    return value


def _hang_trigger_idle_seconds() -> float:
    return _hang_env_seconds(
        turn_execution_constants.HANG_TRIGGER_IDLE_ENV_VAR,
        turn_execution_constants.HANG_TRIGGER_IDLE_SECONDS,
    )


def _hang_result_grace_seconds() -> float:
    return _hang_env_seconds(
        turn_execution_constants.HANG_RESULT_GRACE_ENV_VAR,
        turn_execution_constants.HANG_RESULT_GRACE_SECONDS,
    )


def _hang_confirm_samples() -> int:
    raw = os.environ.get(turn_execution_constants.HANG_CONFIRM_SAMPLES_ENV_VAR)
    if raw is None:
        return turn_execution_constants.HANG_CONFIRM_SAMPLES
    try:
        value = int(raw.strip())
    except ValueError:
        return turn_execution_constants.HANG_CONFIRM_SAMPLES
    return value if value >= 1 else turn_execution_constants.HANG_CONFIRM_SAMPLES

"""Human-facing formatting helpers."""

from __future__ import annotations

import math
from typing import Any


def format_duration_seconds(value: Any) -> str:
    """Format a duration value for text progress output."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"

    if not math.isfinite(seconds):
        return "unknown"
    if round(seconds, 1) < 60.0:
        return f"{seconds:.1f}s"

    total_seconds = int(seconds + 0.5)
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{remaining_minutes:02d}m{remaining_seconds:02d}s"
    return f"{remaining_minutes}m{remaining_seconds:02d}s"

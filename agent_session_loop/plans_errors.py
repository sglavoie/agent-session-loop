"""Error type shared by the plan-directory modules."""

from __future__ import annotations


class PlanError(Exception):
    """Raised when a plan directory cannot be inspected safely."""

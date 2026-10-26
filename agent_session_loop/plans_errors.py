"""Error type shared by the plan-directory modules."""

from __future__ import annotations


class PlanError(Exception):
    """Raised when a plan directory cannot be inspected safely."""


class PlanNotFoundError(PlanError):
    """Raised when the named plan directory does not exist at all.

    A :class:`PlanError` still, so every existing handler keeps working; the
    read commands that resolve ``PLAN_DIR_OR_ID`` report it as
    ``plan-not-found`` rather than calling a missing plan malformed.
    """

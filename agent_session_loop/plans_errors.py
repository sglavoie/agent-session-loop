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


class InputReadError(PlanError):
    """Raised when a file the caller named cannot be read or is not UTF-8.

    A :class:`PlanError` still, so existing handlers keep working; a command
    that distinguishes its own input from a malformed plan reports it as
    ``input-error`` instead of blaming the plan's structure.
    """


#: The ``reason`` every command reports when ``PLAN_DIR`` does not exist.
PLAN_NOT_FOUND_REASON = "plan-not-found"


def plan_error_reason(error: BaseException, default: str) -> str:
    """``plan-not-found`` for a missing plan directory, else ``default``.

    A command keeps its own reason for a plan that exists but cannot be read;
    a directory that is not there at all is reported the same way everywhere.
    """
    if isinstance(error, PlanNotFoundError):
        return PLAN_NOT_FOUND_REASON
    return default

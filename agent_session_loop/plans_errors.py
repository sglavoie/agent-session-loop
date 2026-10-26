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


class ContractError(PlanError):
    """Raised for a malformed completion contract, with where it went wrong.

    ``line`` is the 1-based line of the parsed text the defect sits on, when the
    parser can point at one. ``repository`` marks a check that breaks the plan's
    repository-registry rules rather than the contract's own syntax, so a
    reader can tell an alias problem from a JSON typo.
    """

    def __init__(
        self, message: str, *, line: int | None = None, repository: bool = False
    ) -> None:
        super().__init__(message)
        self.line = line
        self.repository = repository


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

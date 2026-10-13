"""The shared frame the phases of a locked run read and write.

``run_locked`` is a sequence of phases that all work on the same run-scoped
values: the arguments, the plan snapshot, the reply and redirect slots, the
per-turn bookkeeping. Each phase is a function in a ``run_loop_*`` module that
takes the one :class:`RunFrame` and reads or writes those values as attributes,
so a value one phase produces is there for the next. Values only one phase
touches stay ordinary locals of that phase.
"""

from __future__ import annotations

import enum
from typing import Any

RunResult = tuple[int, dict[str, Any]]
"""A run's exit code and its terminal payload."""


class LoopSignal(enum.Enum):
    """How a phase asks the loop pass to continue or end without a result."""

    CONTINUE = "continue"
    BREAK = "break"


class RunFrame:
    """Attribute bag for the values a run carries across its phases.

    Attributes are untyped on purpose: the frame is the union of every value a
    phase hands to a later one, and each is typed where it is built.
    """

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"run frame has no value {name!r} yet")

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value

    def disarm_exception_guard(self) -> None:
        """Retire the crash-path copy of the write guard for this turn.

        The post-turn sequence disarms this *before* it enforces the live
        guard, so the crash handler cannot re-enforce the same guard against
        the same before-image if the live one raises. That ordering is why it
        is a callback rather than a value the boundary hands back.
        """
        self.pending_exception_guard = None

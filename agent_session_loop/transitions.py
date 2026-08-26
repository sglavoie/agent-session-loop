"""Table-driven state machines for the runner's verification and review lanes.

The runner's verification and review/repair behaviour used to be expressed as
ordered branches inside long orchestration functions: legality was whatever the
control flow happened to allow, and an ordering mistake produced a wrong durable
record rather than an error.  This module supplies the small mechanism the
extracted lanes share — a declared transition table, one ``advance`` rule, and a
cursor that refuses an out-of-order step.

Nothing here knows about verification or review.  The vocabularies live beside
the decisions they belong to, in :mod:`agent_session_loop.verification_transitions`
and :mod:`agent_session_loop.review_transitions`, so the table and the policy
that reads it stay in the same file.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass


class IllegalTransitionError(RuntimeError):
    """A machine was asked for a step its table does not declare.

    Raised rather than reported: an illegal transition means the caller's
    ordering is wrong, and continuing would write a durable record describing a
    lifecycle the plan never had.
    """

    def __init__(self, machine: str, state: str, event: str) -> None:
        super().__init__(
            f"{machine}: no transition from state {state!r} on event {event!r}"
        )
        self.machine = machine
        self.state = state
        self.event = event


@dataclass(frozen=True)
class StateMachine:
    """One declared lifecycle: an initial state and its legal transitions.

    ``table`` maps every state — terminal states included, with an empty
    mapping — to the events it accepts.  Declaring terminal states explicitly is
    what lets :meth:`advance` tell "this event is wrong here" apart from "this
    state was never declared", and what lets a test enumerate the whole model.
    """

    name: str
    initial: str
    table: Mapping[str, Mapping[str, str]]

    def __post_init__(self) -> None:
        if self.initial not in self.table:
            raise ValueError(
                f"{self.name}: initial state {self.initial!r} is not declared"
            )
        for state, transitions in self.table.items():
            for event, target in transitions.items():
                if target not in self.table:
                    raise ValueError(
                        f"{self.name}: transition {state!r} --{event}--> "
                        f"{target!r} names an undeclared state"
                    )

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(self.table)

    @property
    def events(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for transitions in self.table.values():
            for event in transitions:
                seen.setdefault(event, None)
        return tuple(seen)

    @property
    def terminal_states(self) -> frozenset[str]:
        return frozenset(state for state, moves in self.table.items() if not moves)

    def is_terminal(self, state: str) -> bool:
        return not self.table[state]

    def legal_transitions(self) -> Iterator[tuple[str, str, str]]:
        """Yield every declared ``(state, event, target)`` triple."""

        for state, transitions in self.table.items():
            for event, target in transitions.items():
                yield state, event, target

    def illegal_transitions(self) -> Iterator[tuple[str, str]]:
        """Yield every ``(state, event)`` pair the table refuses."""

        for state, transitions in self.table.items():
            for event in self.events:
                if event not in transitions:
                    yield state, event

    def advance(self, state: str, event: str) -> str:
        """Return the state ``event`` leads to, or raise for an illegal step."""

        if state not in self.table:
            raise IllegalTransitionError(self.name, state, event)
        try:
            return self.table[state][event]
        except KeyError:
            raise IllegalTransitionError(self.name, state, event) from None

    def start(self) -> MachineCursor:
        return MachineCursor(self)


class MachineCursor:
    """A live position in one :class:`StateMachine`, with its own history.

    The runner drives a cursor beside the effects it performs, so the order the
    lane actually took is checked against the declared table as it happens and
    is available afterwards as evidence rather than reconstructed from the
    branches that ran.
    """

    def __init__(self, machine: StateMachine) -> None:
        self._machine = machine
        self._state = machine.initial
        self._history: list[str] = [machine.initial]

    @property
    def machine(self) -> StateMachine:
        return self._machine

    @property
    def state(self) -> str:
        return self._state

    @property
    def history(self) -> tuple[str, ...]:
        return tuple(self._history)

    @property
    def settled(self) -> bool:
        return self._machine.is_terminal(self._state)

    def advance(self, event: str) -> str:
        self._state = self._machine.advance(self._state, event)
        self._history.append(self._state)
        return self._state

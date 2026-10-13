"""Fail-closed narrowing from a parsed value to a ``Literal`` alias.

Every durable record in this package spells its closed vocabularies as a
``Literal`` alias plus a tuple of the permitted values — ``RubricLane`` and
``RUBRIC_LANES``, ``Severity`` and ``SEVERITIES``, ``HarnessName`` and
``HARNESS_NAMES``. The alias is what the dataclass field declares; the tuple is
what a parser can actually test a JSON value against. Without something joining
the two, a parser either tests membership and hands mypy a plain ``str`` — so
the annotation stops being enforced anywhere the value travels — or skips the
test entirely and lets an unpermitted value reach a durable record.

:func:`as_literal` is that join. It tests membership and *returns the permitted
value it matched*, which mypy narrows to the alias for free: no cast, no inline
suppression, and no second spelling of the permitted set. An unpermitted
value raises rather than falling back to a default, matching the convention
:mod:`agent_session_loop.retention` documents — an unrecognized value is an
absence of knowledge, not a licence to guess, and a durable record written from
a guess is unrecoverable.

The exception type is the caller's, because the raise is part of that module's
parse contract: :mod:`agent_session_loop.assurance_models_primitives` raises
``AssuranceError``, :mod:`agent_session_loop.contracts` raises ``PlanError``,
and code with no domain error of its own gets ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["as_literal", "as_optional_literal"]


def as_literal[ValueT: str](
    value: object,
    allowed: Sequence[ValueT],
    field: str,
    *,
    error: type[Exception] = ValueError,
) -> ValueT:
    """Return ``value`` narrowed to ``allowed``'s element type, or raise.

    ``allowed`` is the module constant that spells the vocabulary — a
    ``tuple[SomeLiteral, ...]`` — so the return type is that ``Literal`` and
    the permitted set is written down exactly once.

    The match is returned from ``allowed`` rather than from ``value``: the two
    are equal strings, and returning the element is what lets mypy narrow
    without a cast. ``field`` names the offending slot in the message, so a
    rejected record is quotable rather than merely rejected.
    """
    if not allowed:
        raise ValueError(f"{field} has no permitted values to match against")
    if isinstance(value, str):
        for permitted in allowed:
            if value == permitted:
                return permitted
    raise error(
        f"{field} must be one of "
        f"{', '.join(repr(permitted) for permitted in allowed)}; got {value!r}"
    )


def as_optional_literal[ValueT: str](
    value: object,
    allowed: Sequence[ValueT],
    field: str,
    *,
    error: type[Exception] = ValueError,
) -> ValueT | None:
    """Narrow ``value`` like :func:`as_literal`, passing ``None`` through.

    The optional form exists because a closed vocabulary and an absent value
    are different things: a flag nobody passed carries no wrong value to
    reject. Only ``None`` passes through — every other unpermitted value still
    raises, so "not supplied" never widens into "not checked".
    """
    if value is None:
        return None
    return as_literal(value, allowed, field, error=error)

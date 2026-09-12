"""Small text and number helpers owned by the evaluation fixture repository.

Every function here is correct as written. A trial seeds one deliberate defect
into exactly one of them, so the repair a live worker is asked to make is
small, self-contained, and covered by a single named case in ``checks/``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert a Celsius reading to Fahrenheit."""
    return celsius * 9 / 5 + 32


def round_half_up(value: float, digits: int = 0) -> float | int:
    """Round *value* half away from zero to *digits* decimal places.

    Decimal is used rather than :func:`round` because the builtin rounds ties
    to even, and because binary floats put 2.675 just below the tie.
    """
    quantum = Decimal(1).scaleb(-digits)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    if digits <= 0:
        return int(rounded)
    return float(rounded)


def slugify(text: str) -> str:
    """Lowercase *text* and join its whitespace-separated words with hyphens."""
    return "-".join(text.lower().split())

"""The named behavioural cases a trial repository is checked against.

Each case states, in executable form, what the function it covers must do.
``focused.py`` runs one of them; ``contract.py`` runs all of them. Keeping the
statements here and the runners thin is what lets a session file point a worker
at a focused check without restating the requirement in prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import toolkit  # noqa: E402


def _equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def temperature() -> None:
    """``celsius_to_fahrenheit`` converts across the whole scale."""
    _equal("0C", toolkit.celsius_to_fahrenheit(0), 32)
    _equal("100C", toolkit.celsius_to_fahrenheit(100), 212)
    _equal("-40C", toolkit.celsius_to_fahrenheit(-40), -40)
    _equal("37C", toolkit.celsius_to_fahrenheit(37), 98.6)


def rounding() -> None:
    """``round_half_up`` rounds ties away from zero, at any precision."""
    _equal("2.5", toolkit.round_half_up(2.5), 3)
    _equal("3.5", toolkit.round_half_up(3.5), 4)
    _equal("-2.5", toolkit.round_half_up(-2.5), -3)
    _equal("1.005 to 2dp", toolkit.round_half_up(1.005, 2), 1.01)
    _equal("2.675 to 2dp", toolkit.round_half_up(2.675, 2), 2.68)
    whole = toolkit.round_half_up(2.5)
    if not isinstance(whole, int):
        raise AssertionError(
            f"rounding to whole numbers must return int, got {type(whole).__name__}"
        )


def slug() -> None:
    """``slugify`` collapses any run of whitespace into a single hyphen."""
    _equal("simple", toolkit.slugify("Hello World"), "hello-world")
    _equal("padded", toolkit.slugify("  Mixed   Case  "), "mixed-case")
    _equal("tabbed", toolkit.slugify("Tab\tSeparated"), "tab-separated")
    _equal("newline", toolkit.slugify("Two\nLines"), "two-lines")


#: Cases a trial session owns. ``contract.py`` runs exactly these.
SESSION_CASES = {
    "temperature": temperature,
    "rounding": rounding,
    "slug": slug,
}

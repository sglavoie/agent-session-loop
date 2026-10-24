"""Serial runner for agent session plan directories."""

from __future__ import annotations

# The guard lives in the package initializer because every entry point imports
# through it: the wrapper, ``python -m agent_session_loop``, the
# ``python -m agent_session_loop.<module>`` recipes the justfile runs, and
# ``install.py``. The package uses 3.14-only grammar, so without it an older
# interpreter fails with a SyntaxError that names a random module rather than
# the version it needs.
import sys

if sys.version_info < (3, 14):  # noqa: UP036 - the guard exists for old Pythons
    sys.stderr.write(
        f"asl requires Python 3.14+; found "
        f"{sys.version_info[0]}.{sys.version_info[1]} at {sys.executable}\n"
    )
    raise SystemExit(2)

__all__ = ["__version__"]

__version__ = "0.2.0"

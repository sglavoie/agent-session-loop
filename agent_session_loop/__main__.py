"""Module entry point for ``python -m agent_session_loop``."""

# The guard runs before the CLI import: the package uses 3.14-only grammar, so
# importing it on an older interpreter fails with a SyntaxError that names a
# random module rather than the version it needs.
import sys

if sys.version_info < (3, 14):  # noqa: UP036 - the guard exists for old Pythons
    sys.stderr.write(
        f"asl requires Python 3.14+; found "
        f"{sys.version_info[0]}.{sys.version_info[1]} at {sys.executable}\n"
    )
    raise SystemExit(2)

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

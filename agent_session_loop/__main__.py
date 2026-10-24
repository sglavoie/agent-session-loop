"""Module entry point for ``python -m agent_session_loop``."""

# The interpreter version guard runs in the package initializer, which Python
# imports before this module.
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

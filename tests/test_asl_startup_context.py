"""Startup-context renderer and installed-context verification regressions."""

from .asl_startup_context_cases_installed import (
    InstalledStartupContextTests,  # noqa: F401
)
from .asl_startup_context_cases_renderer import (
    StartupContextRendererTests,  # noqa: F401
)

if __name__ == "__main__":
    import unittest

    unittest.main()

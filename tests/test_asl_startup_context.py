"""Startup-context renderer and installed-context verification regressions."""

from .asl_startup_context_cases_installed import (
    InstalledStartupContextTests,
)
from .asl_startup_context_cases_renderer import (
    StartupContextRendererTests,
)

ASL_TEST_PORTABLE = True

if __name__ == "__main__":
    import unittest

    unittest.main()

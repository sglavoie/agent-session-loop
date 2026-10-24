"""Workbench launch tests.

Split from tests/test_agent_session_loop.py."""

import unittest

from .asl_workbench_launch_cases_profiles import WorkbenchLaunchArgvTests
from .asl_workbench_launch_cases_spawn import WorkbenchLaunchSpawnTests

ASL_TEST_LANE = "integration"

if __name__ == "__main__":
    unittest.main()

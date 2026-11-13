"""Plan launch tests.

Split from tests/test_agent_session_loop.py."""

import unittest

from .asl_plan_launch_cases_profiles import PlanLaunchArgvTests
from .asl_plan_launch_cases_spawn import PlanLaunchSpawnTests

ASL_TEST_LANE = "integration"

if __name__ == "__main__":
    unittest.main()

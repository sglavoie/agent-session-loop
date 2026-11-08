"""Bridge projection failure backoff, Workbench lifecycle, dispatch, and diagnostics.

Split from tests/test_asl_bridge_server.py."""

import unittest

from . import (
    asl_bridge_server_lifecycle_backoff,
    asl_bridge_server_lifecycle_diagnostics,
    asl_bridge_server_lifecycle_workbench,
)

ASL_TEST_LANE = "integration"


class BridgeProjectionFailureBackoffTests(
    asl_bridge_server_lifecycle_backoff.BridgeProjectionFailureBackoffTests
):
    """Preserve the established test IDs."""


class BridgePipelinedFrameTests(
    asl_bridge_server_lifecycle_backoff.BridgePipelinedFrameTests
):
    """Preserve the established test IDs."""


class BridgeWorkbenchLifecycleTests(
    asl_bridge_server_lifecycle_workbench.BridgeWorkbenchLifecycleTests
):
    """Preserve the established test IDs."""


class BridgeDispatchTests(asl_bridge_server_lifecycle_workbench.BridgeDispatchTests):
    """Preserve the established test IDs."""


class BridgeDiagnosticsTests(
    asl_bridge_server_lifecycle_diagnostics.BridgeDiagnosticsTests
):
    """Preserve the established test IDs."""


class BridgeClosedStdoutTests(
    asl_bridge_server_lifecycle_diagnostics.BridgeClosedStdoutTests
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()

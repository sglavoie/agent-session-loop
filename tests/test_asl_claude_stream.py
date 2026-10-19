"""Test entry point for focused test_asl_claude_stream test groups."""

from __future__ import annotations

import unittest

from .support_asl_claude_stream_1 import (
    GuidanceSpoolTests,
    StreamSessionPromptTests,
)
from .support_asl_claude_stream_2 import (
    StreamSessionInterruptTests,
    StreamSessionSteerTests,
)
from .support_asl_claude_stream_3 import (
    StreamOutputProcessorTests,
    StreamSessionContinuityTests,
)
from .support_asl_claude_stream_4 import (
    ClaudeAdapterTransportTests,
    CopilotSteerRefusalTests,
    GuidanceHookScriptTests,
    NextTurnDeliveryOwnershipTests,
)

for _test_case in (
    GuidanceSpoolTests,
    StreamSessionPromptTests,
    StreamSessionSteerTests,
    StreamSessionInterruptTests,
    StreamSessionContinuityTests,
    StreamOutputProcessorTests,
    GuidanceHookScriptTests,
    ClaudeAdapterTransportTests,
    CopilotSteerRefusalTests,
    NextTurnDeliveryOwnershipTests,
):
    _test_case.__module__ = __name__
del _test_case

if __name__ == "__main__":
    unittest.main()

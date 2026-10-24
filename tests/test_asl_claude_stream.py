"""Test entry point for focused test_asl_claude_stream test groups."""

from __future__ import annotations

import unittest

from .support_asl_claude_stream_adapter import (
    ClaudeAdapterTransportTests,
    CopilotSteerRefusalTests,
    GuidanceHookScriptTests,
    NextTurnDeliveryOwnershipTests,
)
from .support_asl_claude_stream_continuity import (
    StreamOutputProcessorTests,
    StreamSessionContinuityTests,
)
from .support_asl_claude_stream_prompt import (
    GuidanceSpoolTests,
    StreamSessionPromptTests,
)
from .support_asl_claude_stream_steer import (
    StreamSessionInterruptTests,
    StreamSessionSteerTests,
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

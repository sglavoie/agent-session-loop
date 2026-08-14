"""Compatibility shim for the split agent_session_loop test suite.

The former monolithic module now lives in the tests/test_asl_*.py subsystem
modules (shared fixtures in tests/support.py). This shim keeps historical
dotted names such as ``tests.test_agent_session_loop.VerificationExecutorTests``
working. Under broad discovery (``python3 -m unittest discover``) it contributes
no tests so the split modules are not run twice; loading it directly
(``python3 -m unittest tests.test_agent_session_loop``) runs the full suite, and
a self-targeted discovery pattern runs the AgentMux delivery coverage.
"""

from __future__ import annotations

import importlib
import unittest

from tests.support import *  # noqa: F401,F403 - deliberate re-export shim
from tests.test_asl_authoring import (
    PlanAuthoringResplitTests,
    PlanAuthoringTests,
)
from tests.test_asl_baseline import (
    BaselineCacheTests,
    BaselineCliTests,
    BaselineContractTests,
)
from tests.test_asl_cli import (
    CommandAndConfigTests,
    JsonCliTests,
)
from tests.test_asl_compaction import (
    HarnessFailureClassificationTests,
    StateCompactionTests,
)
from tests.test_asl_control import (
    AgentMuxNotificationIntegrationTests,
    AgentMuxSuiteIsolationTests,
    ControlQueueDrainInRunnerTests,
    ControlQueueTests,
    RunControlTests,
)
from tests.test_asl_core import (
    EventPersistenceTests,
    ParserAndStateTests,
)
from tests.test_asl_docs import (
    HarnessDocsRegressionTests,
    InstallVerificationTests,
    PlanWorkflowDocsTests,
    SkillInstructionTests,
    WorkbenchDocsRegressionTests,
)
from tests.test_asl_e2e import (
    EndToEndQATests,
    VerificationRepairLoopTests,
)
from tests.test_asl_harness_integration import (
    FakeHarnessIntegrationTests,
)
from tests.test_asl_pause_ux import (
    ArtifactSafetyAndPauseUxTests,
)
from tests.test_asl_preflight import (
    PreflightCliTests,
    PreflightCommandRunnabilityTests,
    PreflightCoreTests,
    PreflightEnvironmentTests,
    PreflightRepairHintTests,
)
from tests.test_asl_prepare import (
    ExpansionRendererTests,
    PrepareCliTests,
    PrepareEndToEndTests,
    PrepareValidatorSchemaTests,
)
from tests.test_asl_progress import (
    ProgressCliTests,
    ProgressJournalTests,
    ProgressRepairHelperTests,
    RunnerProgressEntryHelperTests,
)
from tests.test_asl_prompts import (
    HarnessInvocationPromptOverrideTests,
    ReplyPromptBuilderTests,
    SessionRunSkillReplyCompatibilityTests,
    TaskPacketBuilderTests,
)
from tests.test_asl_qna import (
    QnAContextLoaderTests,
    QnAQuestionsCliTests,
)
from tests.test_asl_reply_flows import (
    EndToEndReplyResumeFlowTests,
    RunReplyResumeFlowTests,
    RunReplyStdinFlowTests,
)
from tests.test_asl_review import (
    ReviewRepairCycleTests,
    ReviewRepairMaterializationTests,
    StructuredReviewArtifactTests,
)
from tests.test_asl_review_lifecycle import (
    RunReviewHarnessLifecycleTests,
)
from tests.test_asl_run_gates import (
    LongVerificationHeartbeatIntegrationTests,
    RunProgressJournalIntegrationTests,
    RunReadinessGateTests,
    RunVerificationEnforcementTests,
    RunVerificationInterruptTests,
)
from tests.test_asl_tail import (
    TailFollowerTests,
)
from tests.test_asl_verification import (
    ArtifactStorageTests,
    VerificationExecutorTests,
    VerifyCliTests,
)
from tests.test_asl_workbench_archive import (
    WorkbenchArchivePlanTests,
    WorkbenchArchiveRetentionTests,
    WorkbenchArchiveStorageTests,
    WorkbenchDiscoveryTests,
    WorkbenchPurgeArchiveTests,
    WorkbenchRegistryTests,
    WorkbenchRestoreArchiveTests,
)
from tests.test_asl_workbench_launch import (
    WorkbenchLaunchTests,
)
from tests.test_asl_workbench_recovery import (
    WorkbenchRecoveryTests,
)
from tests.test_asl_workbench_server import (
    WorkbenchArtifactReadAPITests,
    WorkbenchQuestionsAPITests,
    WorkbenchServerTests,
)
from tests.test_asl_workbench_static import (
    WorkbenchCliHandlerTests,
    WorkbenchHardeningTests,
    WorkbenchServeCliTests,
    WorkbenchStaticAssetTests,
)
from tests.test_asl_workbench_summary import (
    WorkbenchSummaryTests,
)

_SPLIT_MODULES = (
    "tests.test_asl_core",
    "tests.test_asl_baseline",
    "tests.test_asl_progress",
    "tests.test_asl_verification",
    "tests.test_asl_qna",
    "tests.test_asl_prompts",
    "tests.test_asl_reply_flows",
    "tests.test_asl_pause_ux",
    "tests.test_asl_tail",
    "tests.test_asl_cli",
    "tests.test_asl_control",
    "tests.test_asl_harness_integration",
    "tests.test_asl_preflight",
    "tests.test_asl_docs",
    "tests.test_asl_run_gates",
    "tests.test_asl_review",
    "tests.test_asl_review_lifecycle",
    "tests.test_asl_e2e",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_workbench_launch",
    "tests.test_asl_workbench_server",
    "tests.test_asl_workbench_static",
    "tests.test_asl_compaction",
    "tests.test_asl_authoring",
    "tests.test_asl_prepare",
)


_SELF_DISCOVERY_PATTERN = "test_agent_session_loop.py"


def load_tests(loader, tests, pattern):
    if pattern is not None:
        # Only a pattern naming this module asks for its own tests; the broad
        # pattern must stay empty so the split modules are not run twice.
        if pattern == _SELF_DISCOVERY_PATTERN:
            return loader.loadTestsFromTestCase(AgentMuxNotificationIntegrationTests)
        return unittest.TestSuite()
    suite = unittest.TestSuite()
    for module_name in _SPLIT_MODULES:
        module = importlib.import_module(module_name)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


if __name__ == "__main__":
    unittest.main()

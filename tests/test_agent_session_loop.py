"""Compatibility shim for the split agent_session_loop test suite.

The former monolithic module now lives in the tests/test_asl_*.py subsystem
modules (shared fixtures in tests/support.py). This shim keeps historical
dotted names such as ``tests.test_agent_session_loop.VerificationExecutorTests``
working. Under discovery (``python3 -m unittest discover``) it contributes no
tests so the split modules are not run twice; loading it directly
(``python3 -m unittest tests.test_agent_session_loop``) runs the full suite.
"""

from __future__ import annotations

import importlib
import unittest

from tests.support import *  # noqa: F401,F403
from tests.test_asl_core import (
    EventPersistenceTests,
    ParserAndStateTests,
)
from tests.test_asl_baseline import (
    BaselineContractTests,
    BaselineCacheTests,
    BaselineCliTests,
)
from tests.test_asl_progress import (
    ProgressJournalTests,
    RunnerProgressEntryHelperTests,
    ProgressCliTests,
    ProgressRepairHelperTests,
)
from tests.test_asl_verification import (
    VerificationExecutorTests,
    VerifyCliTests,
    ArtifactStorageTests,
)
from tests.test_asl_qna import (
    QnAContextLoaderTests,
    QnAQuestionsCliTests,
)
from tests.test_asl_prompts import (
    TaskPacketBuilderTests,
    ReplyPromptBuilderTests,
    HarnessInvocationPromptOverrideTests,
    SessionRunSkillReplyCompatibilityTests,
)
from tests.test_asl_reply_flows import (
    RunReplyResumeFlowTests,
    RunReplyStdinFlowTests,
    EndToEndReplyResumeFlowTests,
)
from tests.test_asl_pause_ux import (
    ArtifactSafetyAndPauseUxTests,
)
from tests.test_asl_tail import (
    TailFollowerTests,
)
from tests.test_asl_cli import (
    CommandAndConfigTests,
    JsonCliTests,
)
from tests.test_asl_control import (
    ControlQueueTests,
    ControlQueueDrainInRunnerTests,
    RunControlTests,
    FinishNotificationTests,
)
from tests.test_asl_harness_integration import (
    FakeHarnessIntegrationTests,
)
from tests.test_asl_preflight import (
    PreflightCoreTests,
    PreflightCommandRunnabilityTests,
    PreflightRepairHintTests,
    PreflightEnvironmentTests,
    PreflightCliTests,
)
from tests.test_asl_docs import (
    SkillInstructionTests,
    PlanWorkflowDocsTests,
    InstallVerificationTests,
    HarnessDocsRegressionTests,
    WorkbenchDocsRegressionTests,
)
from tests.test_asl_run_gates import (
    RunReadinessGateTests,
    RunVerificationEnforcementTests,
    RunVerificationInterruptTests,
    LongVerificationHeartbeatIntegrationTests,
    RunProgressJournalIntegrationTests,
)
from tests.test_asl_review import (
    StructuredReviewArtifactTests,
    ReviewRepairMaterializationTests,
    ReviewRepairCycleTests,
)
from tests.test_asl_review_lifecycle import (
    RunReviewHarnessLifecycleTests,
)
from tests.test_asl_e2e import (
    EndToEndQATests,
    VerificationRepairLoopTests,
)
from tests.test_asl_workbench_archive import (
    WorkbenchRegistryTests,
    WorkbenchArchiveRetentionTests,
    WorkbenchArchiveStorageTests,
    WorkbenchArchivePlanTests,
    WorkbenchRestoreArchiveTests,
    WorkbenchPurgeArchiveTests,
    WorkbenchDiscoveryTests,
)
from tests.test_asl_workbench_summary import (
    WorkbenchSummaryTests,
)
from tests.test_asl_workbench_recovery import (
    WorkbenchRecoveryTests,
)
from tests.test_asl_workbench_launch import (
    WorkbenchLaunchTests,
)
from tests.test_asl_workbench_server import (
    WorkbenchServerTests,
    WorkbenchQuestionsAPITests,
    WorkbenchArtifactReadAPITests,
)
from tests.test_asl_workbench_static import (
    WorkbenchStaticAssetTests,
    WorkbenchHardeningTests,
    WorkbenchServeCliTests,
    WorkbenchCliHandlerTests,
)
from tests.test_asl_compaction import (
    StateCompactionTests,
    HarnessFailureClassificationTests,
)
from tests.test_asl_authoring import (
    PlanAuthoringTests,
    PlanAuthoringResplitTests,
)
from tests.test_asl_prepare import (
    PrepareValidatorSchemaTests,
    ExpansionRendererTests,
    PrepareCliTests,
    PrepareEndToEndTests,
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


def load_tests(loader, tests, pattern):
    if pattern is not None:
        return unittest.TestSuite()
    suite = unittest.TestSuite()
    for module_name in _SPLIT_MODULES:
        module = importlib.import_module(module_name)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


if __name__ == "__main__":
    unittest.main()

"""Focused test-suite entry point assembled from smaller case modules."""

from .asl_redirect_admission_cases_outcome import (
    RunnerSideTests,
    SubmissionOutcomeTests,
    UndeliverableChannelTests,
)
from .asl_redirect_admission_cases_target import RefusalTests, TargetInspectionTests

ASL_TEST_PORTABLE = True

__all__ = [
    "TargetInspectionTests",
    "RefusalTests",
    "SubmissionOutcomeTests",
    "UndeliverableChannelTests",
    "RunnerSideTests",
]

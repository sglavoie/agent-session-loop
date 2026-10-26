"""Focused test-suite entry point assembled from smaller case modules."""

from .workbench_history_cases_basics import (
    AttributionTests,
    ClassificationTests,
    NaiveTimestampTests,
    VocabularyTests,
)
from .workbench_history_cases_enrichment import (
    EnrichmentTests,
    NoFalseCompletionTests,
    RetentionTests,
)
from .workbench_history_cases_projections import (
    CheckpointTests,
    ProjectionAgreementTests,
    QuestionTests,
    RestartRuleTests,
)
from .workbench_history_cases_reads import (
    DuplicateTests,
    FileIdentityTests,
    IncrementalReadTests,
)

ASL_TEST_PORTABLE = True

__all__ = [
    "VocabularyTests",
    "ClassificationTests",
    "AttributionTests",
    "NaiveTimestampTests",
    "EnrichmentTests",
    "NoFalseCompletionTests",
    "RetentionTests",
    "IncrementalReadTests",
    "FileIdentityTests",
    "DuplicateTests",
    "QuestionTests",
    "CheckpointTests",
    "RestartRuleTests",
    "ProjectionAgreementTests",
]

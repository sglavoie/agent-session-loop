"""Focused test-suite entry point assembled from smaller case modules."""

from .asl_workbench_archive_cases_discovery import WorkbenchDiscoveryTests
from .asl_workbench_archive_cases_registry import (
    WorkbenchArchiveRetentionTests,
    WorkbenchRegistryTests,
)
from .asl_workbench_archive_cases_restore import (
    WorkbenchPurgeArchiveTests,
    WorkbenchRestoreArchiveTests,
)
from .asl_workbench_archive_cases_storage import (
    WorkbenchArchivePlanTests,
    WorkbenchArchiveStorageTests,
)

ASL_TEST_PORTABLE = True

__all__ = [
    "WorkbenchRegistryTests",
    "WorkbenchArchiveRetentionTests",
    "WorkbenchArchiveStorageTests",
    "WorkbenchArchivePlanTests",
    "WorkbenchRestoreArchiveTests",
    "WorkbenchPurgeArchiveTests",
    "WorkbenchDiscoveryTests",
]

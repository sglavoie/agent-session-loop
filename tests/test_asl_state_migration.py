"""Focused test-suite entry point assembled from smaller case modules."""

from .asl_state_migration_cases_core import MigrateStateTests, OrphanGuardTests
from .asl_state_migration_cases_relocation import (
    ConversationRelocationTests,
    RedirectJournalMigrationTests,
)
from .asl_state_migration_cases_schema import (
    MigrateStateCliTests,
    RelocationSchemaUpgradeTests,
    UpgradeStateSchemaTests,
)

ASL_TEST_PORTABLE = True

__all__ = [
    "OrphanGuardTests",
    "MigrateStateTests",
    "RelocationSchemaUpgradeTests",
    "UpgradeStateSchemaTests",
    "MigrateStateCliTests",
    "ConversationRelocationTests",
    "RedirectJournalMigrationTests",
]

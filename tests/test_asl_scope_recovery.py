# ruff: noqa: F401, I001
"""Compatibility facade for scope-recovery test groups."""

from .test_asl_scope_recovery_external import ExternalRecoveryTests
from .test_asl_scope_recovery_incidents import (
    ExternalChangeCommitTargetTests,
    RecoveryJournalIncidentTests,
)
from .test_asl_scope_recovery_refs import RefRecoveryTests
from .test_asl_scope_recovery_resumption import ExternalRecoveryResumptionTests


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()

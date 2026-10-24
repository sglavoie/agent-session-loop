# ruff: noqa: F401, I001
"""Compatibility facade for assurance-reconciliation test groups."""

from .test_asl_assurance_reconcile_apply import ApplyTests, PreviewTests
from .test_asl_assurance_reconcile_rulings import (
    ConcurrentApplyTests,
    KeepDistinctTests,
)
from .test_asl_assurance_reconcile_schema import (
    ManifestSchemaTests,
    ReconcileWiringTests,
)


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()

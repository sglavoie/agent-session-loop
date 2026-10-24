# ruff: noqa: F401, I001
"""Compatibility facade for assurance-ledger test groups."""

from .test_asl_assurance_ledger_identity import (
    IdentityBindingTests,
    IssueIdTests,
    SchemaVersionTests,
)
from .test_asl_assurance_ledger_index import IndexTests
from .test_asl_assurance_ledger_storage import (
    AppendTests,
    CrashRecoveryTests,
    PathTests,
    RejectionTests,
    SequenceTests,
)


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()

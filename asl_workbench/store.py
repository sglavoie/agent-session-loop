"""Compatibility entry point for Workbench releases predating the store split.

Installed lifecycle updaters require this path in every incoming artifact.
Keep it, and the original public store API, even though current callers use
the focused store modules: changing only the new updater cannot repair the
updater that must accept the package before it can be installed.
"""

from .store_columns import SCHEMA_BOOKKEEPING
from .store_migrations import MIGRATIONS, connect, migrate, schema_version
from .store_types import (
    COMPANION_SCHEMA_VERSION,
    DEFAULT_READ_LIMIT,
    INGEST_REFUSAL_CODES,
    KIND_CATALOG_PAGE,
    KIND_HEARTBEAT,
    KIND_HISTORY_BATCH,
    KIND_SNAPSHOT,
    LEGACY_GENERATION,
    MAX_READ_LIMIT,
    PRUNE_INTERVAL_SECONDS,
    REFUSED_STALE_GENERATION,
    REFUSED_STALE_OBSERVATION,
    SCHEMA_VERSION,
    ConnectorStatus,
    Counts,
    IngestInvalid,
    IngestRefused,
    IngestResult,
    SourceStatus,
    StoreError,
)
from .store_workbench import WorkbenchStore

__all__ = [
    "COMPANION_SCHEMA_VERSION",
    "DEFAULT_READ_LIMIT",
    "INGEST_REFUSAL_CODES",
    "KIND_CATALOG_PAGE",
    "KIND_HEARTBEAT",
    "KIND_HISTORY_BATCH",
    "KIND_SNAPSHOT",
    "LEGACY_GENERATION",
    "MAX_READ_LIMIT",
    "MIGRATIONS",
    "PRUNE_INTERVAL_SECONDS",
    "REFUSED_STALE_GENERATION",
    "REFUSED_STALE_OBSERVATION",
    "SCHEMA_BOOKKEEPING",
    "SCHEMA_VERSION",
    "ConnectorStatus",
    "Counts",
    "IngestInvalid",
    "IngestRefused",
    "IngestResult",
    "SourceStatus",
    "StoreError",
    "WorkbenchStore",
    "connect",
    "migrate",
    "schema_version",
]

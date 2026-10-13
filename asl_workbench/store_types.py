"""Store constants, errors and result types shared across the store modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import freshness as freshness_module

# One integer in ``PRAGMA user_version`` is the whole migration ledger. A table
# of applied migrations would need a migration of its own before it could be
# read.
SCHEMA_VERSION = 4

#: The version an installation migrated from the Companion arrives at. Named so
#: the upgrade test can assert it rather than a bare ``3``.
COMPANION_SCHEMA_VERSION = 3

# How often retention runs on the ingestion path. Uploads arrive every few
# seconds and the delete is indexed, but a delete per upload is still a delete
# per upload for no gain: nothing expires in the gap that would not expire in
# the next one.
PRUNE_INTERVAL_SECONDS = 300.0

# What one read may return. A bound rather than a page cursor, because a
# cockpit shows a working set and not an archive, and an unbounded read is the
# one query that turns a busy corpus into a service outage. The catalog is the
# exception and pages properly -- see `catalog`.
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 500

META_LAST_PRUNED_AT = "last_pruned_at"
META_CREATED_AT = "created_at"
#: The prefix of the Companion's per-connector project blobs. Read once, by
#: migration 4, and never written again.
META_PROJECT_CATALOG_PREFIX = "projects."

KIND_HEARTBEAT = "heartbeat"
KIND_SNAPSHOT = "snapshot"
KIND_HISTORY_BATCH = "history_batch"
KIND_CATALOG_PAGE = "catalog_page"

#: The generation a carried-over Companion label belongs to. Not a real
#: generation, which is what makes the first complete catalog able to retire
#: it: a plan the Mac no longer enumerates is genuinely gone.
LEGACY_GENERATION = ""

# Ingestion refusals. Distinct from any frame vocabulary because these are
# answers about the *store's* state, not about the message: the same bytes that
# are refused now would have been accepted a minute ago.
REFUSED_STALE_GENERATION = "stale-generation"
REFUSED_STALE_OBSERVATION = "stale-observation"
INGEST_REFUSAL_CODES: tuple[str, ...] = (
    REFUSED_STALE_GENERATION,
    REFUSED_STALE_OBSERVATION,
)

_ORDER_NEWER = "newer"


class StoreError(RuntimeError):
    """The store could not be opened or migrated. Nothing was written."""


class IngestRefused(RuntimeError):
    """An upload was refused for what the store already holds, not for its shape."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in INGEST_REFUSAL_CODES:
            raise AssertionError(f"undeclared ingest refusal code: {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class IngestInvalid(ValueError):
    """An upload was refused for its own shape, before anything was written."""


@dataclass(frozen=True)
class IngestResult:
    """What one accepted upload changed."""

    kind: str
    connector_id: str
    duplicate: bool = False
    stored: int = 0
    updated: int = 0
    absent: int = 0
    pruned: int = 0

    def as_document(self) -> dict[str, Any]:
        return {
            "status": "duplicate" if self.duplicate else "accepted",
            "kind": self.kind,
            "stored": self.stored,
            "updated": self.updated,
            "absent": self.absent,
            "pruned": self.pruned,
        }


@dataclass(frozen=True)
class SourceStatus:
    """Whether one source of truth is whole, and why it is not."""

    completeness: str
    reasons: tuple[str, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {"completeness": self.completeness, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ConnectorStatus:
    """One source's connection health, projection health, and completeness.

    Three separate questions with three separate answers. A source that is
    heartbeating perfectly while its projection stopped is the case this shape
    exists to be able to describe.
    """

    connector_id: str
    freshness: freshness_module.Freshness
    compatible: bool
    contract_set_id: str | None
    bridge_alive: bool | None
    degraded_reason: str | None
    source_generation: int | None
    row_count: int
    truncated: bool | None
    warnings: tuple[str, ...]
    runs: SourceStatus
    history: SourceStatus
    catalog: SourceStatus
    last_heartbeat_at: float | None
    last_snapshot_at: float | None
    asl_version: str | None = None
    bridge_protocol: int | None = None
    projection_age_seconds: float | None = None

    @property
    def degraded(self) -> bool:
        return (
            self.freshness.label != freshness_module.FRESHNESS_LIVE
            or self.degraded_reason is not None
            or not self.compatible
            or self.completeness != freshness_module.COMPLETENESS_COMPLETE
        )

    @property
    def completeness(self) -> str:
        """The weakest completeness among this source's three streams."""
        return freshness_module.aggregate_completeness(
            (
                self.runs.completeness,
                self.history.completeness,
                self.catalog.completeness,
            )
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "connector_id": self.connector_id,
            **self.freshness.as_document(),
            "compatible": self.compatible,
            "contract_set_id": self.contract_set_id,
            "bridge_alive": self.bridge_alive,
            "bridge_protocol": self.bridge_protocol,
            "asl_version": self.asl_version,
            "degraded_reason": self.degraded_reason,
            "source_generation": self.source_generation,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "sources": {
                "runs": self.runs.as_document(),
                "history": self.history.as_document(),
                "catalog": self.catalog.as_document(),
            },
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_snapshot_at": self.last_snapshot_at,
        }
        return document


@dataclass(frozen=True)
class Counts:
    """Aggregate numbers, carrying no identity at all.

    This is what a summary-only credential is entitled to: how much there is
    and how healthy it looks, never what any of it is called. Adding a field
    here is adding a number; adding an identifier would make this a different
    kind of answer, and the credential that reads it was not issued for that.
    """

    runs: int = 0
    needs_attention: int = 0
    running: int = 0
    history_entries: int = 0
    connectors: int = 0
    absent_runs: int = 0
    unconfirmed_runs: int = 0
    plans: int = 0
    absent_plans: int = 0
    unconfirmed_plans: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def as_document(self) -> dict[str, int]:
        document = {
            "runs": self.runs,
            "needs_attention": self.needs_attention,
            "running": self.running,
            "absent_runs": self.absent_runs,
            "unconfirmed_runs": self.unconfirmed_runs,
            "history_entries": self.history_entries,
            "connectors": self.connectors,
            "plans": self.plans,
            "absent_plans": self.absent_plans,
            "unconfirmed_plans": self.unconfirmed_plans,
        }
        document.update(self.extra)
        return document

"""What a stored observation may hold, and how whole it claims to be.

The Companion keeps three kinds of observation on disk -- a run summary, an
imported history entry, and (new here) a catalog entry -- and the rule that
makes any of them safe to keep is the same one: *the schema is not the
allowlist*. A table can only hold what its columns hold, but a table grows a
column the moment somebody adds one, and the widened wire shape that motivated
the column is usually the same change. So the boundary lives here, as data, and
:mod:`asl_workbench.store_workbench` projects every record through it on the way in and
on the way out. ``tests/test_workbench_store.py`` then asserts the other
direction: every column in the schema is a name on one of these lists or is
ingestion bookkeeping the source never sent.

Three further things belong here rather than in the store, because all three
are statements about an observation rather than about SQLite:

* **The observation status vocabulary.** A stored row is ``current`` when it
  belongs to the newest observation its source produced, ``unconfirmed`` when
  it does not, and ``absent`` when an observation entitled to say so left it
  out. Those are the only three answers, and the middle one is the one that
  matters: a row nobody has re-observed is not a row that ended.

* **Completeness.** A source that omitted rows says so, and an omission from a
  partial view proves nothing. :func:`completeness_of` maps a stored flag onto
  the vocabulary while keeping ``unknown`` distinct from ``partial`` -- a
  source nothing has been received from is not a source known to have gaps.

* **The narrow policy the persistence layer reads.** The Companion passes its
  whole ``CompanionConfig`` -- forty fields covering TLS, bind addresses,
  credentials and publisher URLs -- into its store, its freshness evaluator and
  its incident engine, none of which can bind a socket. :class:`ObservationPolicy`
  is the subset those three actually read. A transport that needs the rest owns
  the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .operations import OperationError, assert_body_free

# -- Contract compatibility -------------------------------------------------

#: The contract sets a stored observation may have come from. A connector
#: reporting anything else is recorded, because refusing to record it would
#: lose the evidence that it connected at all, but its rows are never treated
#: as a view this application understands.
#:
#: The value matches the Companion's ``protocol.SUPPORTED_CONTRACT_SET_IDS``
#: exactly, and ``tests/test_workbench_store.py`` checks it against the Pi
#: checkout when one is reachable: a divergence here would silently mark every
#: existing connector incompatible on the first deploy.
SUPPORTED_CONTRACT_SET_IDS: tuple[str, ...] = ("asl-amx-1.85ec3041b9f03f47",)

# -- Vocabularies -----------------------------------------------------------

#: Belongs to the newest observation this source produced.
OBSERVATION_CURRENT = "current"
#: Stored, but no longer carried by the newest observation. Not an ending.
OBSERVATION_UNCONFIRMED = "unconfirmed"
#: An observation entitled to prove removal left it out.
OBSERVATION_ABSENT = "absent"
OBSERVATION_STATUSES: tuple[str, ...] = (
    OBSERVATION_CURRENT,
    OBSERVATION_UNCONFIRMED,
    OBSERVATION_ABSENT,
)

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_UNKNOWN = "unknown"
COMPLETENESS_LABELS: tuple[str, ...] = (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_UNKNOWN,
)

#: How long a label may be before it is cut. Matches the Companion's
#: ``MAX_LABEL_CHARS`` so a migrated row renders identically.
MAX_LABEL_CHARS = 120

#: How many warnings one observation may carry into storage.
MAX_WARNINGS = 16

# -- Allowlists -------------------------------------------------------------

#: What a retained run summary row may hold on disk. Byte-for-byte the
#: Companion's ``RUN_SUMMARY_PERSIST_FIELDS``: an existing database migrates
#: forward into these same columns, so a name added or dropped here would
#: change what an already-stored row means.
RUN_SUMMARY_PERSIST_FIELDS: frozenset[str] = frozenset(
    {
        "run_key",
        "project",
        "plan_key",
        "run_id",
        "session_id",
        "harness",
        "primary_state",
        "phase",
        "urgency_group",
        "pause_reason",
        "outcome",
        "question_id",
        "question_asked_at",
        "unanswered_count",
        "run_started_at",
        "run_finished_at",
        "last_activity_at",
        "observed_at",
    }
)

#: What a cockpit may read back about a run. A strict subset of the persisted
#: fields plus the derived labels a browser renders; ``plan_key`` stays behind
#: because it is the Mac's name for a directory on the Mac.
RUN_SUMMARY_EXPORT_FIELDS: frozenset[str] = frozenset(
    {
        "run_key",
        "project",
        "run_id",
        "session_id",
        "harness",
        "primary_state",
        "phase",
        "urgency_group",
        "pause_reason",
        "outcome",
        "question_id",
        "question_asked_at",
        "unanswered_count",
        "run_started_at",
        "run_finished_at",
        "last_activity_at",
        "freshness",
        "observation_status",
        "last_observed_at",
        "plan_label",
        "plan_title",
    }
)

#: What an imported history row may hold on disk. The Companion's
#: ``HISTORY_PERSIST_FIELDS``. :class:`asl_workbench.history.HistoryEntry` also
#: carries ``projects``; it is deliberately absent, because project membership
#: is now catalog storage rather than a copy on every ending.
HISTORY_PERSIST_FIELDS: frozenset[str] = frozenset(
    {
        "entry_id",
        "source_key",
        "plan_key",
        "run_key",
        "project",
        "run_id",
        "harness",
        "model",
        "outcome",
        "pause_reason",
        "recorded_at",
        "run_started_at",
        "run_finished_at",
        "duration_seconds",
        "turn_count",
        "completed_session_count",
        "pending_session_count",
        "completed_session_ids",
        "review_completed",
        "review_outcome",
        "reconstructed",
        "event_schema_version",
    }
)

#: What a cockpit may read back from history. ``source_key``, ``plan_key`` and
#: the event schema version are ingestion bookkeeping: they correlate rows in
#: the store and say nothing a browser renders.
HISTORY_EXPORT_FIELDS: frozenset[str] = frozenset(
    {
        "entry_id",
        "run_key",
        "project",
        "plan_label",
        "plan_title",
        "run_id",
        "harness",
        "model",
        "outcome",
        "pause_reason",
        "run_started_at",
        "run_finished_at",
        "duration_seconds",
        "turn_count",
        "completed_session_count",
        "pending_session_count",
        "completed_session_ids",
        "review_completed",
        "review_outcome",
        "reconstructed",
    }
)

#: What a stored catalog entry may hold. Every name is a key
#: :meth:`asl_workbench.catalog.CatalogEntry.to_payload` emits, minus
#: ``multi_repository``, which is a comparison against ``repository_count``
#: rather than a fact of its own.
#:
#: The list is short of nothing an entry carries because the catalog was built
#: cache-safe in the first place -- ``CACHE_SAFE_FIELDS`` did that work in
#: session 2, and ``search_text`` is the field it produced so that a remote
#: search never needs the plan path.
CATALOG_PERSIST_FIELDS: frozenset[str] = frozenset(
    {
        "plan_key",
        "run_key",
        "title",
        "display_label",
        "primary_state",
        "recommended_action",
        "urgency_rank",
        "urgency_group",
        "activity_label",
        "projects",
        "project_group",
        "counts",
        "sources",
        "exists",
        "pinned",
        "next_pending_session_id",
        "last_activity_at",
        "last_activity_source",
        "completed_at",
        "pause_reason",
        "harness",
        "registry_digest",
        "repository_count",
        "warning_count",
        "search_text",
    }
)

#: What a cockpit may read back about a catalog entry: everything persisted,
#: plus the generation it came from, whether that generation claimed to be
#: whole, and the observation status the reader has to render honestly.
CATALOG_EXPORT_FIELDS: frozenset[str] = CATALOG_PERSIST_FIELDS | frozenset(
    {
        "multi_repository",
        "generation",
        "complete",
        "observation_status",
        "last_observed_at",
    }
)


# -- Projection -------------------------------------------------------------


def persist_run_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one run summary onto the fields a store may keep."""
    return project(record, RUN_SUMMARY_PERSIST_FIELDS, "persisted summary")


def export_run_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one run summary onto the fields a cockpit may read."""
    return project(record, RUN_SUMMARY_EXPORT_FIELDS, "exported summary")


def persist_history_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one imported ending onto the fields a store may keep."""
    return project(record, HISTORY_PERSIST_FIELDS, "persisted history entry")


def export_history_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one ending onto the fields a cockpit may read."""
    return project(record, HISTORY_EXPORT_FIELDS, "exported history entry")


def persist_catalog_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one catalog entry onto the fields a store may keep."""
    return project(record, CATALOG_PERSIST_FIELDS, "persisted catalog entry")


def export_catalog_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one catalog entry onto the fields a cockpit may read."""
    return project(record, CATALOG_EXPORT_FIELDS, "exported catalog entry")


def project(
    record: Mapping[str, Any], allowed: frozenset[str], where: str
) -> dict[str, Any]:
    """Keep only ``allowed`` keys, then refuse anything nested that leaks prose.

    Two checks rather than one, and they catch different mistakes. The
    allowlist drops a top-level forbidden field silently, because a record
    carrying one is the normal case -- the source sent a full summary and the
    store keeps a slice of it. :func:`~asl_workbench.operations.assert_body_free`
    then walks what survived, because a body nested under an *allowed* key is
    not a normal case: it is prose that would have been written.
    """
    projected = {key: value for key, value in record.items() if key in allowed}
    assert_body_free(projected, where=where)
    return projected


# -- Completeness -----------------------------------------------------------


def completeness_of(value: bool | None, *, invert: bool = False) -> str:
    """Map a stored flag onto the completeness vocabulary, keeping ``unknown``.

    ``None`` is not ``False``. A source nothing has been received from is
    unknown, and calling it partial would claim knowledge of a gap nobody
    measured.

    ``invert`` is for the flags stored the other way round: ``truncated`` is
    true when the view is partial, ``complete`` is true when it is whole.
    """
    if value is None:
        return COMPLETENESS_UNKNOWN
    whole = value if invert else not value
    return COMPLETENESS_COMPLETE if whole else COMPLETENESS_PARTIAL


def proves_removal(completeness: str, *, projection_fresh: bool = True) -> bool:
    """Whether an omission from a view this complete is evidence of removal.

    Both halves are required, and for different reasons. A partial view omitted
    rows it never claimed to carry. A stale view may be describing a corpus
    that has since changed, so its omissions describe the past.
    """
    return completeness == COMPLETENESS_COMPLETE and projection_fresh


# -- Policy -----------------------------------------------------------------


class PolicyError(ValueError):
    """An observation policy whose fields contradict each other."""


@dataclass(frozen=True)
class ObservationPolicy:
    """The timing and permission facts persistence and incidents actually read.

    Every field is a number or a flag. There is no host, no port, no path and
    no credential, which is what lets the same policy be constructed on the Pi,
    on the Mac, and in a test with no configuration file at all.
    """

    #: Beyond this, an observation is no longer describing the present.
    observation_stale_seconds: float = 60.0
    #: Beyond this, active work nobody can see any more is an incident.
    lost_observation_alert_seconds: float = 180.0
    #: Beyond this, a question nobody has answered is an incident.
    unanswered_question_alert_seconds: float = 300.0
    #: How long a summary, an ending, and a settled operation are kept.
    summary_retention_days: float = 7.0
    #: When set, reads are served and no mutation is accepted.
    observation_only: bool = False
    #: Whether any mutation method is permitted at all.
    mutations_permitted: bool = True
    #: When unset, incidents are still recorded and never published. A corpus
    #: assembled for a demo or a test has nothing worth waking anyone for.
    publish_incidents: bool = True
    #: Which contract sets this deployment understands.
    supported_contract_set_ids: tuple[str, ...] = SUPPORTED_CONTRACT_SET_IDS

    def __post_init__(self) -> None:
        for name in (
            "observation_stale_seconds",
            "lost_observation_alert_seconds",
            "unanswered_question_alert_seconds",
            "summary_retention_days",
        ):
            if getattr(self, name) <= 0:
                raise PolicyError(f"{name} must be positive")
        if self.lost_observation_alert_seconds < self.observation_stale_seconds:
            # Otherwise every observation that merely went stale would also be
            # a lost-observation incident, and the two words would mean one
            # thing.
            raise PolicyError(
                "lost_observation_alert_seconds must not be below "
                "observation_stale_seconds"
            )
        if not self.supported_contract_set_ids:
            raise PolicyError("at least one supported contract set is required")

    @property
    def summary_retention_seconds(self) -> float:
        """The retention window in seconds, as every caller wants it."""
        return self.summary_retention_days * 86400.0

    @property
    def replies_permitted(self) -> bool:
        """Whether this deployment may carry a mutation at all."""
        return self.mutations_permitted and not self.observation_only


def require_policy(policy: Any) -> ObservationPolicy:
    """Refuse a policy this package cannot read, naming what was passed."""
    if not isinstance(policy, ObservationPolicy):
        raise PolicyError(f"expected an ObservationPolicy, got {type(policy).__name__}")
    return policy


__all__ = [
    "CATALOG_EXPORT_FIELDS",
    "CATALOG_PERSIST_FIELDS",
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_LABELS",
    "COMPLETENESS_PARTIAL",
    "COMPLETENESS_UNKNOWN",
    "HISTORY_EXPORT_FIELDS",
    "HISTORY_PERSIST_FIELDS",
    "MAX_LABEL_CHARS",
    "MAX_WARNINGS",
    "OBSERVATION_ABSENT",
    "OBSERVATION_CURRENT",
    "OBSERVATION_STATUSES",
    "OBSERVATION_UNCONFIRMED",
    "RUN_SUMMARY_EXPORT_FIELDS",
    "RUN_SUMMARY_PERSIST_FIELDS",
    "SUPPORTED_CONTRACT_SET_IDS",
    "ObservationPolicy",
    "OperationError",
    "PolicyError",
    "completeness_of",
    "export_catalog_entry",
    "export_history_entry",
    "export_run_summary",
    "persist_catalog_entry",
    "persist_history_entry",
    "persist_run_summary",
    "project",
    "proves_removal",
    "require_policy",
]

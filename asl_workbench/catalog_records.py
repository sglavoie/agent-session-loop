"""Catalog records and validated projection from local summaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .catalog_labels import (
    _counts,
    _session_id,
    _slug,
    _sources,
    _timestamp,
    _vocabulary_value,
    display_label,
    group_label,
    partition_summary,
    plan_key,
    project_names,
    run_key,
    safe_label,
)
from .catalog_vocabulary import (
    ACTIVITY_LABELS,
    CACHE_SAFE_FIELDS,
    LAST_ACTIVITY_SOURCES,
    PRIMARY_STATES,
    PRIVATE_FIELDS,
    RECOMMENDED_ACTIONS,
    URGENCY_GROUPS,
    CatalogError,
)

# -- Records ----------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One plan as the remote provider may retain it.

    Every field is either a digest, a timestamp, a counter, or a member of a
    vocabulary declared in this module. There is no path, no operator-typed
    string, and no prose, so the whole entry may be persisted and served from a
    cache without asking the Mac.
    """

    plan_key: str
    run_key: str
    display_label: str
    primary_state: str
    recommended_action: str
    urgency_rank: int
    urgency_group: str
    activity_label: str
    projects: tuple[str, ...]
    project_group: str
    counts: dict[str, int]
    sources: tuple[str, ...]
    exists: bool
    pinned: bool
    title: str | None = None
    next_pending_session_id: str | None = None
    last_activity_at: str | None = None
    last_activity_source: str | None = None
    completed_at: str | None = None
    pause_reason: str | None = None
    harness: str | None = None
    registry_digest: str | None = None
    repository_count: int = 0
    warning_count: int = 0

    @property
    def multi_repository(self) -> bool:
        """Whether the plan is bound to more than one registered repository."""
        return self.repository_count > 1

    @property
    def search_text(self) -> str:
        """Search text built from cache-safe fields only.

        The Workbench's own ``searchable_text`` concatenates the plan path, the
        pin label, the pause message, and the last-message preview, which is
        why it is a private field. This is what the Pi searches instead: the
        title, the project names, and the classification.
        """
        parts: list[str] = [
            self.display_label,
            self.primary_state,
            self.recommended_action,
            self.urgency_group,
            self.activity_label,
            *self.projects,
            *self.sources,
        ]
        if self.title:
            parts.append(self.title)
        if self.next_pending_session_id:
            parts.extend(["session", self.next_pending_session_id])
        for optional in (self.pause_reason, self.harness):
            if optional:
                parts.append(optional)
        joined = " ".join(part for part in parts if part)
        return " ".join(joined.lower().split())

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "title": self.title,
            "display_label": self.display_label,
            "primary_state": self.primary_state,
            "recommended_action": self.recommended_action,
            "urgency_rank": self.urgency_rank,
            "urgency_group": self.urgency_group,
            "activity_label": self.activity_label,
            "projects": list(self.projects),
            "project_group": self.project_group,
            "counts": dict(self.counts),
            "sources": list(self.sources),
            "exists": self.exists,
            "pinned": self.pinned,
            "next_pending_session_id": self.next_pending_session_id,
            "last_activity_at": self.last_activity_at,
            "last_activity_source": self.last_activity_source,
            "completed_at": self.completed_at,
            "pause_reason": self.pause_reason,
            "harness": self.harness,
            "registry_digest": self.registry_digest,
            "repository_count": self.repository_count,
            "multi_repository": self.multi_repository,
            "warning_count": self.warning_count,
            "search_text": self.search_text,
        }


@dataclass(frozen=True)
class PlanDetail:
    """The private half of one plan, held for a local or on-demand read.

    A mapping rather than a second field-by-field mirror of the summary: the
    partition in :data:`PRIVATE_FIELDS` is the schema, and restating it here
    would give it two places to drift. What this type adds is the rule --
    detail is fetched, never published -- and the plan key that ties it back to
    an entry the Pi may already hold.
    """

    plan_key: str
    fields: dict[str, object]

    def get(self, name: str) -> object:
        """One private field, or ``None`` when the summary did not carry it."""
        if name in CACHE_SAFE_FIELDS:
            raise CatalogError(f"{name} is a cache-safe field, not a detail field")
        if name not in PRIVATE_FIELDS:
            raise CatalogError(f"{name} is not a classified summary field")
        return self.fields.get(name)

    def to_payload(self) -> dict[str, object]:
        return {"plan_key": self.plan_key, "fields": dict(self.fields)}


@dataclass(frozen=True)
class CatalogRecord:
    """One plan, both halves, paired by plan key."""

    entry: CatalogEntry
    detail: PlanDetail

    @property
    def plan_key(self) -> str:
        return self.entry.plan_key


def record_from_summary(
    payload: Mapping[str, object],
    *,
    title: object = None,
    pinned: bool = False,
) -> CatalogRecord:
    """Build one catalog record from a ``WorkbenchSummary`` payload.

    ``title`` comes from the plan's manifest, which the summary does not carry;
    pass :func:`manifest_title` of the manifest text. ``pinned`` says whether
    the registry holds a pin for the plan -- derivable from ``sources`` for a
    discovered plan, but not for a single-plan read, so the provider states it.
    """
    cache_safe, private = partition_summary(payload)
    plan_dir = private.get("plan_dir")
    if not isinstance(plan_dir, str) or not plan_dir.strip():
        raise CatalogError("summary payload carries no plan_dir")
    key = plan_key(plan_dir)
    primary_state = _vocabulary_value(cache_safe.get("primary_state"), PRIMARY_STATES)
    if primary_state is None:
        raise CatalogError(f"summary payload has no known primary_state: {plan_dir}")
    repository_count = cache_safe.get("repository_count")
    count = (
        repository_count
        if isinstance(repository_count, int) and not isinstance(repository_count, bool)
        else 0
    )
    urgency_rank = cache_safe.get("urgency_rank")
    # A payload arrives from JSON or from a dataclass, so `repositories` is
    # whatever the caller had; anything that is not a list of rows names no
    # project rather than being iterated a character at a time.
    raw_repositories = private.get("repositories")
    repositories: tuple[object, ...] = (
        tuple(raw_repositories)
        if isinstance(raw_repositories, Sequence)
        and not isinstance(raw_repositories, str | bytes)
        else ()
    )
    projects = project_names(
        repositories,
        workspace_root=private.get("workspace_root") or private.get("cwd"),
    )
    resolved_title = safe_label(title)
    warnings = private.get("warnings")
    entry = CatalogEntry(
        plan_key=key,
        run_key=run_key(key, private.get("run_id")),
        display_label=display_label(
            title=resolved_title,
            label=private.get("label"),
            workspace_root=private.get("workspace_root") or private.get("cwd"),
            plan_dir=plan_dir,
            repository_count=count,
        ),
        primary_state=primary_state,
        recommended_action=(
            _vocabulary_value(cache_safe.get("recommended_action"), RECOMMENDED_ACTIONS)
            or "view"
        ),
        urgency_rank=(
            urgency_rank
            if isinstance(urgency_rank, int) and not isinstance(urgency_rank, bool)
            else 0
        ),
        urgency_group=(
            _vocabulary_value(cache_safe.get("urgency_group"), URGENCY_GROUPS)
            or "needs-action"
        ),
        activity_label=(
            _vocabulary_value(cache_safe.get("activity_label"), ACTIVITY_LABELS) or ""
        ),
        projects=projects,
        project_group=group_label(projects),
        counts=_counts(cache_safe.get("counts")),
        sources=_sources(cache_safe.get("sources")),
        exists=bool(cache_safe.get("exists")),
        pinned=pinned or "registry" in _sources(cache_safe.get("sources")),
        title=resolved_title,
        next_pending_session_id=_session_id(cache_safe.get("next_pending_session_id")),
        last_activity_at=_timestamp(cache_safe.get("last_activity_at")),
        last_activity_source=_vocabulary_value(
            cache_safe.get("last_activity_source"), LAST_ACTIVITY_SOURCES
        ),
        completed_at=_timestamp(cache_safe.get("completed_at")),
        pause_reason=_slug(cache_safe.get("pause_reason")),
        harness=safe_label(cache_safe.get("harness")),
        registry_digest=_slug(cache_safe.get("registry_digest")),
        repository_count=count,
        warning_count=(
            len(warnings)
            if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes))
            else 0
        ),
    )
    return CatalogRecord(entry=entry, detail=PlanDetail(plan_key=key, fields=private))

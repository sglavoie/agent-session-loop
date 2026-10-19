"""Catalog grouping, pagination, and generation identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .catalog_records import CatalogEntry, CatalogRecord, PlanDetail
from .catalog_vocabulary import DEFAULT_PAGE_LIMIT, PRIMARY_STATES, CatalogError

# -- The catalog ------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProject:
    """One repository and the plans that work in it."""

    name: str
    plan_keys: tuple[str, ...]
    state_counts: dict[str, int]

    @property
    def plan_count(self) -> int:
        return len(self.plan_keys)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "plan_keys": list(self.plan_keys),
            "plan_count": self.plan_count,
            "state_counts": dict(self.state_counts),
        }


@dataclass(frozen=True)
class CatalogPage:
    """One batch of catalog entries, and where it sits in the generation.

    ``complete`` is the generation's property and ``final`` is the page's: a
    last page of a generation whose source could not read every plan is
    ``final`` and not ``complete``, and a consumer must not reconcile a
    disappearance against it.
    """

    entries: tuple[CatalogEntry, ...]
    offset: int
    limit: int | None
    total: int
    generation: str
    complete: bool
    final: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "entries": [entry.to_payload() for entry in self.entries],
            "offset": self.offset,
            "limit": self.limit,
            "count": len(self.entries),
            "total": self.total,
            "generation": self.generation,
            "complete": self.complete,
            "final": self.final,
        }


@dataclass(frozen=True)
class PlanCatalog:
    """Every plan the source found, in discovery order, with nothing dropped.

    The contrast with the bridge's ``runs.snapshot`` is the point of the type:
    that projection keeps only active and actionable states and stops at
    ``MAX_ROWS`` rows, so a corpus with a few hundred plans loses every healthy
    ``ready`` and ``complete`` one. This holds all of them and pages on
    request.
    """

    records: tuple[CatalogRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    complete: bool = True

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def generation(self) -> str:
        """A digest of every entry in the catalog, in order.

        Wall-clock free, so an unchanged corpus produces one generation
        forever, and a page can name the generation it was cut from without
        the caller having to trust that two pages came from one read.
        """
        material = [record.entry.to_payload() for record in self.records]
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(record.entry for record in self.records)

    def record(self, key: str) -> CatalogRecord:
        for record in self.records:
            if record.plan_key == key:
                return record
        raise CatalogError(f"no plan with key {key!r} in this catalog")

    def detail(self, key: str) -> PlanDetail:
        return self.record(key).detail

    def page(self, *, offset: int = 0, limit: int | None = None) -> CatalogPage:
        """One batch of entries, starting at ``offset``.

        ``limit=None`` returns everything from ``offset`` on, which is what a
        local caller wants; a transport passes its frame budget instead. An
        offset past the end is an empty final page rather than an error, so a
        client that polls a shrinking catalog converges instead of failing.
        """
        if offset < 0:
            raise CatalogError("a catalog page cannot start before the first plan")
        if limit is not None and limit <= 0:
            raise CatalogError("a catalog page limit must be positive")
        selected = (
            self.records[offset:] if limit is None else self.records[offset:][:limit]
        )
        entries = tuple(record.entry for record in selected)
        return CatalogPage(
            entries=entries,
            offset=offset,
            limit=limit,
            total=self.total,
            generation=self.generation,
            complete=self.complete,
            final=offset + len(entries) >= self.total,
        )

    def pages(self, *, limit: int = DEFAULT_PAGE_LIMIT) -> tuple[CatalogPage, ...]:
        """Every page of the catalog at ``limit`` entries each.

        An empty catalog is one empty page rather than none: a generation that
        found nothing still has to be delivered, or the consumer cannot tell it
        apart from a delivery that never happened.
        """
        if limit <= 0:
            raise CatalogError("a catalog page limit must be positive")
        offsets = range(0, self.total, limit) if self.total else (0,)
        return tuple(self.page(offset=offset, limit=limit) for offset in offsets)

    def state_counts(self) -> dict[str, int]:
        """How many plans sit in each primary state, every state present."""
        counts = dict.fromkeys(PRIMARY_STATES, 0)
        for record in self.records:
            counts[record.entry.primary_state] += 1
        return counts

    def projects(self) -> tuple[CatalogProject, ...]:
        """Project membership across the catalog, one row per repository name.

        A plan bound to several repositories is a member of each, which is what
        makes this a membership map rather than a partition: the Companion's
        overview asks "what is happening in this repository", and a
        cross-repository run is happening in all of them.
        """
        keys_by_name: dict[str, list[str]] = {}
        states_by_name: dict[str, dict[str, int]] = {}
        for record in self.records:
            for name in record.entry.projects:
                keys_by_name.setdefault(name, []).append(record.plan_key)
                states = states_by_name.setdefault(
                    name, dict.fromkeys(PRIMARY_STATES, 0)
                )
                states[record.entry.primary_state] += 1
        return tuple(
            CatalogProject(
                name=name,
                plan_keys=tuple(keys_by_name[name]),
                state_counts=states_by_name[name],
            )
            for name in sorted(keys_by_name)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "total": self.total,
            "generation": self.generation,
            "complete": self.complete,
            "warnings": list(self.warnings),
            "entries": [entry.to_payload() for entry in self.entries()],
            "projects": [project.to_payload() for project in self.projects()],
            "state_counts": self.state_counts(),
        }


def build_catalog(
    records: Iterable[CatalogRecord],
    *,
    warnings: Iterable[str] = (),
    complete: bool = True,
) -> PlanCatalog:
    """Assemble a catalog, refusing two records for the same plan.

    A duplicate key would make :meth:`PlanCatalog.record` answer arbitrarily
    and would double-count a plan in every project row, so it is a build-time
    refusal rather than a warning nobody reads.
    """
    collected = tuple(records)
    seen: set[str] = set()
    for record in collected:
        if record.plan_key in seen:
            raise CatalogError(f"duplicate plan key in catalog: {record.plan_key}")
        seen.add(record.plan_key)
    return PlanCatalog(
        records=collected,
        warnings=tuple(warnings),
        complete=complete,
    )

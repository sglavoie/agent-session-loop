"""The Mac provider behind the portable plan catalog.

``asl_workbench.catalog`` defines what a catalog is and may not import this
package; this module is the half that touches the filesystem. It runs the
Workbench's existing discovery and classification -- no second implementation
of either -- and hands the resulting payloads to the portable package, which
decides what may be cached and what stays here.

Two properties of this scan are contracts rather than incidental behaviour:

* **It changes nothing.** Every summary is built with ``reclaim=False`` and no
  archive retention runs, so reading the catalog leaves plan locks, archive
  entries, and state files exactly as they were. The Workbench's own read paths
  did neither of those things until this change; ``observational-reads`` in
  ``asl_workbench.changes`` records why they now do.

* **It is uncapped.** Every discovered plan becomes a record, in discovery
  order, whatever its state. The bridge's ``runs.snapshot`` projection keeps
  only active and actionable rows and stops at ``bridge.MAX_ROWS``, which is
  correct for a frame an operator intervenes from and wrong for a catalog:
  applied here it would silently drop every healthy ``ready`` and ``complete``
  plan past the cap. A transport that needs bounded payloads pages the result
  instead.

A plan the scan cannot project -- deleted between discovery and summary, or
holding files that stopped parsing -- becomes a warning and clears the
catalog's ``complete`` flag. That flag is the difference between "this plan is
gone" and "this scan could not see it", and only the second is recoverable by
trying again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from asl_workbench import catalog as portable_catalog
from asl_workbench.catalog import CatalogRecord, PlanCatalog

from . import plans, workbench

#: Bound on the manifest bytes read to find a plan's title, matching the
#: Companion's ``projects.MAX_TITLE_BYTES``. The title is the document's first
#: ``# H1``; a manifest that has not produced one in 64 KiB does not have one.
MAX_TITLE_BYTES = 64 * 1024


def read_plan_title(plan_dir: Path) -> str | None:
    """The plan's title from its ``manifest.md``, or ``None``.

    Deliberately not ``plans.inspect_plan``'s title: that one falls back to the
    plan directory's name, which is a path component and would put a filesystem
    layout into a field the Pi may cache. A plan with no ``# H1`` has no title
    here, and :func:`asl_workbench.catalog.display_label` picks the next safest
    name instead.

    Returns ``None`` rather than raising for an unreadable or absent manifest.
    A missing title is not a reason to drop a plan from the catalog, and the
    plan's real trouble -- if it has any -- is already classified as ``invalid``
    by the summary builder.
    """
    manifest_path = plan_dir / "manifest.md"
    try:
        with manifest_path.open("rb") as stream:
            content = stream.read(MAX_TITLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return portable_catalog.manifest_title(content)


def build_summary(
    discovered: workbench.DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
) -> workbench.WorkbenchSummary:
    """Classify one discovered plan without repairing it.

    ``reclaim=False`` is the whole reason this wrapper exists: the summary
    builder's default reclaims a stale plan lock as a side effect of being
    asked to describe it, which is a repair a catalog read must never perform.
    Every catalog path in this module goes through here, so there is one place
    to check that no read reclaims.
    """
    return workbench.build_workbench_summary(
        discovered,
        state_dir=state_dir,
        reclaim=False,
    )


def record_for_summary(
    summary: workbench.WorkbenchSummary,
    *,
    pinned: bool = False,
) -> CatalogRecord:
    """Split one classified plan into its cache-safe and private halves.

    The summary carries no title -- ``WorkbenchSummary`` has never had one --
    so the manifest is read here and passed to the portable package, which is
    what lets a catalog entry name a plan without naming its directory.
    """
    return portable_catalog.record_from_summary(
        summary.to_payload(),
        title=read_plan_title(Path(summary.plan_dir)),
        pinned=pinned,
    )


@dataclass(frozen=True)
class CatalogScan:
    """One non-reclaiming pass over every visible plan.

    Both halves of the result come from the same pass. :attr:`catalog` is the
    portable, uncapped enumeration a remote transport consumes; :attr:`summaries`
    are the Mac-side ``WorkbenchSummary`` objects it was built from, in the same
    order, for the local HTTP API that serves full detail to a caller already
    running on this machine. Keeping them together is what stops the local queue
    from running a second discovery-and-classify pass beside the catalog's.
    """

    catalog: PlanCatalog
    summaries: tuple[workbench.WorkbenchSummary, ...]
    discovery: workbench.DiscoveryResult
    #: Plans discovery found and this scan could not classify. Held apart from
    #: ``discovery.warnings`` because the two mean different things to a
    #: caller: a discovery warning is about a source, and one of these is about
    #: a plan that is missing from an otherwise-authoritative enumeration. The
    #: catalog carries both, since a remote consumer receives only the catalog.
    projection_warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.catalog.complete


def scan(
    *,
    state_dir: str | Path | None = None,
    registry: workbench.WorkbenchRegistry | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> CatalogScan:
    """Discover and classify every visible plan, changing nothing.

    The keyword arguments are ``discover_plans``' own, passed straight through,
    so the catalog sees exactly the plans the queue sees: state directories,
    registry pins with their labels and launch profiles, configured and default
    plan roots, and registered multi-repository workspaces.

    Discovery's own warnings (an unreadable root, a pin whose directory is
    gone) travel on the catalog but do not clear ``complete``: discovery
    reported them and still returned a full account of what it found. A
    per-plan projection failure does clear it, because that plan is one the
    scan knows exists and could not describe, and a consumer reconciling
    against an incomplete generation must not read its absence as a deletion.
    """
    discovery = workbench.discover_plans(
        state_root=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    records: list[CatalogRecord] = []
    summaries: list[workbench.WorkbenchSummary] = []
    failures: list[str] = []
    for discovered in discovery.plans:
        try:
            summary = build_summary(discovered, state_dir=state_dir)
            record = record_for_summary(
                summary,
                pinned="registry" in discovered.sources,
            )
        except (
            OSError,
            ValueError,
            plans.PlanError,
            workbench.WorkbenchError,
            portable_catalog.CatalogError,
        ) as error:
            failures.append(
                f"catalog: could not project {discovered.plan_dir}: {error}"
            )
            continue
        records.append(record)
        summaries.append(summary)
    return CatalogScan(
        catalog=portable_catalog.build_catalog(
            records,
            warnings=(*discovery.warnings, *failures),
            complete=not failures,
        ),
        summaries=tuple(summaries),
        discovery=discovery,
        projection_warnings=tuple(failures),
    )


def scan_catalog(
    *,
    state_dir: str | Path | None = None,
    registry: workbench.WorkbenchRegistry | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> PlanCatalog:
    """The catalog alone, for a caller with no use for the Mac-side detail."""
    return scan(
        state_dir=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    ).catalog

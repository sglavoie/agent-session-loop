"""Resolving a remote ``plan_key`` to a plan directory on this Mac.

The gate between a remote request and a local filesystem, split out of
:mod:`agent_session_loop.workbench_remote_reads` so the responder module holds
only the read dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from asl_workbench import catalog as portable_catalog
from asl_workbench import details
from asl_workbench.details import DetailError

from . import workbench_catalog

__all__ = ["PLAN_INDEX_TTL_SECONDS", "PlanIndex"]

#: How long a resolved ``plan_key`` table is trusted before the corpus is
#: walked again. A miss refreshes immediately whatever this says, so the window
#: only decides how long a *stale hit* can last -- a plan archived a minute ago
#: answers from its old directory and reports ``not-found`` when the read lands,
#: which is the same answer a cockpit would have got a moment later anyway.
PLAN_INDEX_TTL_SECONDS = 30.0


class PlanIndex:
    """``plan_key`` to plan directory, from this Mac's own discovery.

    The gate between a remote request and a local filesystem. A key that no
    discovery pass produced resolves to nothing, so the set of directories the
    wire can reach is exactly the set this Mac already publishes in its catalog
    -- not a superset of it, and never a path the far end composed.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        ttl_seconds: float = PLAN_INDEX_TTL_SECONDS,
    ) -> None:
        self.state_dir = state_dir
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self._ttl = float(ttl_seconds)
        self._plans: dict[str, Path] = {}
        self._scanned_at: float | None = None

    @property
    def size(self) -> int:
        """How many plans the last pass indexed."""
        return len(self._plans)

    def refresh(self, now: float) -> int:
        """Walk the corpus and rebuild the table. Returns how many plans."""
        scan = workbench_catalog.scan(
            state_dir=self.state_dir,
            plan_roots=self.plan_roots,
            include_default_dirs=self.include_default_dirs,
        )
        rebuilt: dict[str, Path] = {}
        for summary in scan.summaries:
            plan_dir = str(summary.plan_dir)
            try:
                rebuilt[portable_catalog.plan_key(plan_dir)] = Path(plan_dir)
            except portable_catalog.CatalogError:
                continue
        self._plans = rebuilt
        self._scanned_at = now
        return len(rebuilt)

    def resolve(self, plan_key: object, *, now: float) -> Path:
        """The directory one key names, or a refusal saying it names none.

        A miss always costs a fresh pass before it is believed: a plan created
        since the last walk is a plan an operator is most likely to be asking
        about, and answering ``not-found`` from a stale table would be answering
        about the corpus as it was rather than as it is.
        """
        if not isinstance(plan_key, str) or not plan_key:
            raise DetailError(
                details.REASON_INVALID_REQUEST, "a detail read needs a plan_key"
            )
        if self._stale(now):
            self.refresh(now)
        found = self._plans.get(plan_key)
        if found is None:
            self.refresh(now)
            found = self._plans.get(plan_key)
        if found is None:
            raise DetailError(
                details.REASON_NOT_FOUND, "no plan on this connector has that key"
            )
        return found

    def items(self, *, now: float) -> list[tuple[str, Path]]:
        """Every indexed plan as ``(plan_key, directory)``, refreshed if stale.

        For the caller that has to visit the whole corpus rather than resolve
        one key -- settling what a restart left open is the case -- and it walks
        the same table a read resolves against, so it can never reach a
        directory the wire could not have named.
        """
        if self._stale(now):
            self.refresh(now)
        return sorted(self._plans.items())

    def _stale(self, now: float) -> bool:
        return self._scanned_at is None or now - self._scanned_at >= self._ttl

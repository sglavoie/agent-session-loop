"""Retention pruning for the store."""

from __future__ import annotations

from . import incidents
from .operations import (
    OPERATION_TERMINAL_STATUSES,
)
from .store_base import StoreBase
from .store_types import (
    META_LAST_PRUNED_AT,
    PRUNE_INTERVAL_SECONDS,
)


class StoreRetention(StoreBase):
    """Expires rows past the retention horizon."""

    # -- retention -------------------------------------------------------
    def _maybe_prune(self, *, now: float) -> int:
        last = self.meta(META_LAST_PRUNED_AT)
        if (
            isinstance(last, (int, float))
            and now - float(last) < PRUNE_INTERVAL_SECONDS
        ):
            return 0
        return self._prune(now=now)

    def _prune(self, *, now: float) -> int:
        """Delete everything that has aged out of the retention window.

        Runs and history share one cutoff, because both answer the same
        operator question -- what happened in the last seven days -- and a run
        summary that outlived its history row would be a row with no ending.

        Catalog entries are not a time series and do not age out while they are
        present: a plan that has existed untouched for a year is still a plan.
        Only a retired entry expires, measured from when it went absent.
        """
        cutoff = now - self._policy.summary_retention_seconds
        runs = self._connection.execute(
            "DELETE FROM run_summary WHERE observed_at < ?", (cutoff,)
        ).rowcount
        entries = self._connection.execute(
            "DELETE FROM history_entry WHERE recorded_at < ?", (cutoff,)
        ).rowcount
        plans = self._connection.execute(
            "DELETE FROM catalog_entry WHERE present = 0 AND absent_since < ?",
            (cutoff,),
        ).rowcount
        # Only settled operations age out. A journal row that is still open is
        # the record of a mutation whose outcome nobody established, and
        # deleting it would both erase that admission and silently release the
        # one-outstanding-mutation hold that keeps a second attempt from
        # running against an unknown first one.
        marks = ", ".join("?" * len(OPERATION_TERMINAL_STATUSES))
        operations = self._connection.execute(
            f"DELETE FROM operation WHERE requested_at < ? AND status IN ({marks})",
            (cutoff, *sorted(OPERATION_TERMINAL_STATUSES)),
        ).rowcount
        self.set_meta(META_LAST_PRUNED_AT, now)
        return (
            max(0, runs)
            + max(0, entries)
            + max(0, plans)
            + max(0, operations)
            + incidents.prune(self._connection, policy=self._policy, now=now)
        )

    def prune(self, *, now: float) -> int:
        """Run the retention pass regardless of when the last one ran."""
        with self.transaction():
            return self._prune(now=now)

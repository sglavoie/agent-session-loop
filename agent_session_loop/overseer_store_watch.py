"""The ledger's watcher leases and observation cursors.

:class:`WatchMixin` is the part of the ledger two concurrent watchers contend
over: one lease per plan, taken and renewed inside the same ``BEGIN IMMEDIATE``
that reads it, and a monotonic cursor per stream.
"""

from __future__ import annotations

from .overseer_store_core import LedgerCore
from .overseer_store_models import (
    LedgerError,
    ObservationCursor,
    WatchClaim,
    WatchLease,
)
from .overseer_store_rows import WATCH_LEASE_COLUMNS, watch_lease_from_row


class WatchMixin(LedgerCore):
    """Watcher ownership and observation cursors."""

    # -- watcher ownership -------------------------------------------------

    def claim_watch(
        self,
        *,
        plan_dir: str,
        watcher_id: str,
        run_id: str,
        host: str,
        pid: int,
        now: float,
        lease_seconds: float,
    ) -> WatchClaim:
        """Take or renew this watcher's claim on one plan, atomically.

        Four ways a claim is granted, and exactly one way it is refused. It is
        granted when nobody has ever claimed the plan, when the last holder
        released it, when the claimant already holds it (a renewal, which is
        also how a restart under the same identity resumes), and when an
        unreleased lease has run out of time. It is refused while another
        watcher's lease is still inside its term.

        That last case is the whole point, and the reason expiry is the only
        way a lease changes hands unwillingly: a watcher is a process somewhere,
        possibly on another host, and the observable evidence about it is the
        same whether it is busy, quiet, or gone. Probing it and reading a failed
        probe as death is the mistake
        :func:`agent_session_loop.supervision_liveness.probe` exists to refuse
        to make -- so this waits out a term the holder stopped renewing instead
        of deciding the holder is dead. A watcher that crashed costs one lease
        term of delay; a watcher that is alive and slow keeps its plans.

        The read and the write are one ``BEGIN IMMEDIATE`` transaction, so two
        watchers reaching the same plan at the same moment serialize rather than
        both concluding the row was free.
        """
        if not plan_dir or not watcher_id:
            raise LedgerError("a watch claim must name both a plan and a watcher")
        moment = self._now()
        expires_at = now + lease_seconds
        with self.transaction() as connection:
            existing = self.watch_lease(plan_dir)
            held_by_another = (
                existing is not None
                and existing.held
                and existing.watcher_id != watcher_id
            )
            if existing is not None and held_by_another and not existing.expired(now):
                return WatchClaim(
                    plan_dir=plan_dir,
                    watcher_id=watcher_id,
                    run_id=run_id,
                    granted=False,
                    took_over=False,
                    previous_watcher_id=existing.watcher_id,
                    previous_run_id=existing.run_id,
                    expires_at=existing.expires_at,
                    holder=existing,
                )
            took_over = held_by_another
            claimed_at = (
                existing.claimed_at
                if existing is not None and existing.watcher_id == watcher_id
                else moment
            )
            connection.execute(
                """
                INSERT INTO watch_lease (
                    plan_dir, watcher_id, run_id, host, pid, expires_at,
                    claimed_at, renewed_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    watcher_id = excluded.watcher_id,
                    run_id = excluded.run_id,
                    host = excluded.host,
                    pid = excluded.pid,
                    expires_at = excluded.expires_at,
                    claimed_at = excluded.claimed_at,
                    renewed_at = excluded.renewed_at,
                    released_at = NULL
                """,
                (
                    plan_dir,
                    watcher_id,
                    run_id,
                    host,
                    pid,
                    expires_at,
                    claimed_at,
                    moment,
                ),
            )
            return WatchClaim(
                plan_dir=plan_dir,
                watcher_id=watcher_id,
                run_id=run_id,
                granted=True,
                took_over=took_over,
                previous_watcher_id=(
                    existing.watcher_id if existing is not None else ""
                ),
                previous_run_id=existing.run_id if existing is not None else "",
                expires_at=expires_at,
                holder=None,
            )

    def release_watch(self, *, plan_dir: str, watcher_id: str) -> bool:
        """Give up this watcher's claim, keeping what the lease remembers.

        The row is emptied of its owner rather than deleted, because the run it
        names is the fact a later watcher needs to tell a continuation from a
        run that has been replaced since anybody looked. Releasing a lease
        somebody else holds does nothing and reports ``False``: a watcher can
        only put down what it picked up.
        """
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE watch_lease
                SET watcher_id = '', expires_at = 0.0, released_at = ?
                WHERE plan_dir = ? AND watcher_id = ?
                """,
                (moment, plan_dir, watcher_id),
            )
            return cursor.rowcount == 1

    def watch_lease(self, plan_dir: str) -> WatchLease | None:
        """The lease on one plan, held or released, if the row exists."""
        row = self._connection.execute(
            f"SELECT {WATCH_LEASE_COLUMNS} FROM watch_lease WHERE plan_dir = ?",
            (plan_dir,),
        ).fetchone()
        return None if row is None else watch_lease_from_row(row)

    def watch_leases(self, *, watcher_id: str | None = None) -> tuple[WatchLease, ...]:
        """Every lease, or every lease one watcher holds, in plan order."""
        where = " WHERE watcher_id = ?" if watcher_id is not None else ""
        parameters = (watcher_id,) if watcher_id is not None else ()
        rows = self._connection.execute(
            f"SELECT {WATCH_LEASE_COLUMNS} FROM watch_lease{where} ORDER BY plan_dir",
            parameters,
        ).fetchall()
        return tuple(watch_lease_from_row(row) for row in rows)

    # -- observation cursors ----------------------------------------------

    def advance_cursor(
        self,
        *,
        plan_dir: str,
        stream: str,
        position: int,
        marker: str = "",
    ) -> ObservationCursor:
        """Record how far one stream of one plan has been read.

        Monotonic: a lower position is ignored rather than written. A cursor
        that can move backwards turns a stale observer into a replay of work the
        overseer already did, and the ledger is the only place that ordering is
        enforced.
        """
        moment = self._now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO observation_cursor (
                    plan_dir, stream, position, marker, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (plan_dir, stream) DO UPDATE SET
                    position = MAX(observation_cursor.position, excluded.position),
                    marker = CASE
                        WHEN excluded.position >= observation_cursor.position
                        THEN excluded.marker ELSE observation_cursor.marker END,
                    observed_at = CASE
                        WHEN excluded.position >= observation_cursor.position
                        THEN excluded.observed_at ELSE observation_cursor.observed_at
                        END
                """,
                (plan_dir, stream, position, marker, moment),
            )
        recorded = self.cursor(plan_dir=plan_dir, stream=stream)
        if recorded is None:
            raise LedgerError(
                f"cursor for {plan_dir}/{stream} vanished immediately after it "
                "was written"
            )
        return recorded

    def cursor(self, *, plan_dir: str, stream: str) -> ObservationCursor | None:
        """The recorded position for one stream of one plan."""
        row = self._connection.execute(
            """
            SELECT plan_dir, stream, position, marker, observed_at
            FROM observation_cursor WHERE plan_dir = ? AND stream = ?
            """,
            (plan_dir, stream),
        ).fetchone()
        if row is None:
            return None
        return ObservationCursor(
            plan_dir=str(row["plan_dir"]),
            stream=str(row["stream"]),
            position=int(row["position"]),
            marker=str(row["marker"]),
            observed_at=str(row["observed_at"]),
        )

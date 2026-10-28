"""The operation journal for the store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from .operations import (
    OPERATION_OUTSTANDING_STATUSES,
    OPERATION_UNCONFIRMED,
    REASON_UNKNOWN_OPERATION,
    Evidence,
    OperationError,
    export_operation,
    next_status,
    persist_operation,
    reconcile,
)
from .store_base import StoreBase
from .store_columns import (
    _OPERATION_COLUMNS,
)
from .store_types import (
    DEFAULT_READ_LIMIT,
)
from .store_values import (
    _clamp_limit,
    _operation_record,
)


class OperationJournal(StoreBase):
    """Records and advances remote operations."""

    # -- operation journal -----------------------------------------------
    def record_operation(
        self, record: Mapping[str, Any], *, connector_id: str, received_at: float
    ) -> dict[str, Any]:
        """Write one operation, or return the one this id already names.

        A repeated operation id is the same operation and never a second one.
        That is the whole of the idempotence guarantee an operator's browser
        relies on: a retried submission, a double-clicked button, and a reload
        that resends the form all converge on one row rather than on three
        mutations of one run.
        """
        with self.transaction() as connection:
            return self._insert_operation(
                connection, record, connector_id=connector_id, received_at=received_at
            )

    def admit_operation(
        self,
        operation_id: str,
        plan_key: str,
        decide: Callable[[list[dict[str, Any]]], Mapping[str, Any] | None],
        *,
        connector_id: str,
        received_at: float,
        plan_idle: Callable[[], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Look up, decide, and journal one submission in one transaction.

        ``decide`` receives the row this operation id already names (when
        present) and every outstanding operation on the plan, and returns the
        record to write, or ``None`` to write nothing; it refuses by raising,
        which rolls the transaction back. Reading and writing under one
        ``BEGIN IMMEDIATE`` is what keeps "one outstanding mutation per plan"
        true when two submissions arrive together: the second reads the
        first's row rather than a snapshot taken before it was written.

        ``plan_idle`` checks whether the newest observation shows nothing
        running on the plan. It runs inside this transaction: an observation
        arriving after an earlier idle check must not supersede an uncertain
        operation and admit a second mutation against a newly active plan.
        """
        with self.transaction() as connection:
            if plan_idle is not None and plan_idle():
                self._supersede_uncertain(
                    connection, plan_key, operation_id, now=received_at
                )
            # Every outstanding row, not just the oldest: admission refuses
            # only an outstanding row in the submission's own scope, so an
            # older row in another scope must not hide the one that blocks.
            own = self.operation(operation_id)
            recorded = ([] if own is None else [own]) + self.outstanding_operations(
                plan_key
            )
            decided = decide(recorded)
            if decided is None:
                return None
            return self._insert_operation(
                connection, decided, connector_id=connector_id, received_at=received_at
            )

    def _supersede_uncertain(
        self,
        connection: sqlite3.Connection,
        plan_key: str,
        resubmitted: str,
        *,
        now: float,
    ) -> None:
        """Supersede every ``unconfirmed`` row on an idle plan but a resend's own.

        A resend of the same id is the same submission, and letting it replace
        its own uncertain record would hand it a fresh attempt at a mutation
        that may already have run.
        """
        rows = connection.execute(
            "SELECT * FROM operation WHERE plan_key = ? AND status = ? "
            "AND operation_id != ?",
            (plan_key, OPERATION_UNCONFIRMED, resubmitted),
        ).fetchall()
        for row in rows:
            verdict = reconcile(
                _operation_record(row),
                Evidence(run_active=False),
                now=now,
                superseding=True,
            )
            if verdict is None:
                continue
            connection.execute(
                "UPDATE operation SET status = ?, detail_code = ?, "
                "settled_at = ?, updated_at = ? WHERE operation_id = ?",
                (verdict.status, verdict.detail_code, now, now, row["operation_id"]),
            )

    def _insert_operation(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        connector_id: str,
        received_at: float,
    ) -> dict[str, Any]:
        """Insert one row inside the caller's transaction, or return the existing."""
        projected = persist_operation(record)
        existing = self._operation_row(str(projected["operation_id"]))
        if existing is not None:
            return _operation_record(existing)
        values = {column: projected.get(column) for column in _OPERATION_COLUMNS}
        values["connector_id"] = connector_id
        values["received_at"] = received_at
        values["updated_at"] = received_at
        connection.execute(
            "INSERT INTO operation ({columns}) VALUES ({marks})".format(
                columns=", ".join(_OPERATION_COLUMNS),
                marks=", ".join("?" * len(_OPERATION_COLUMNS)),
            ),
            [values[column] for column in _OPERATION_COLUMNS],
        )
        return _operation_record(self._operation_row(str(projected["operation_id"])))

    def advance_operation(
        self,
        operation_id: str,
        status: str,
        *,
        now: float,
        detail_code: str | None = None,
        dispatched_at: float | None = None,
        settled_at: float | None = None,
    ) -> dict[str, Any]:
        """Move one operation to ``status``, refusing a forbidden transition.

        The legality check and the write share one transaction. Checking
        outside it would let two callers each read ``pending`` and each decide
        they were the one dispatching it.
        """
        with self.transaction() as connection:
            row = self._operation_row(operation_id)
            if row is None:
                raise OperationError(
                    REASON_UNKNOWN_OPERATION, f"unknown operation: {operation_id!r}"
                )
            current = str(row["status"])
            if current == status:
                # Re-reporting the state an operation is already in is a
                # retried delivery of one fact, not an illegal move.
                return _operation_record(row)
            next_status(current, status)
            connection.execute(
                "UPDATE operation SET status = ?, "
                "detail_code = COALESCE(?, detail_code), "
                "dispatched_at = COALESCE(?, dispatched_at), "
                "settled_at = COALESCE(?, settled_at), updated_at = ? "
                "WHERE operation_id = ?",
                (status, detail_code, dispatched_at, settled_at, now, operation_id),
            )
            return _operation_record(self._operation_row(operation_id))

    def settle_operation_if(
        self,
        operation_id: str,
        expected: str,
        status: str,
        *,
        now: float,
        detail_code: str | None = None,
        dispatched_at: float | None = None,
        settled_at: float | None = None,
    ) -> bool:
        """Move one operation to ``status`` only while it is still ``expected``.

        What a sweep uses: its verdict was drawn from a row it read earlier,
        and a report that moved the row since is newer evidence than the
        sweep's, so a row that is no longer ``expected`` is left alone.
        """
        with self.transaction() as connection:
            row = self._operation_row(operation_id)
            if row is None or str(row["status"]) != expected:
                return False
            next_status(expected, status)
            connection.execute(
                "UPDATE operation SET status = ?, "
                "detail_code = COALESCE(?, detail_code), "
                "dispatched_at = COALESCE(?, dispatched_at), "
                "settled_at = COALESCE(?, settled_at), updated_at = ? "
                "WHERE operation_id = ?",
                (status, detail_code, dispatched_at, settled_at, now, operation_id),
            )
            return True

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        """One journal row, or ``None``."""
        with self.reading():
            row = self._operation_row(operation_id)
        return None if row is None else _operation_record(row)

    def outstanding_operation(self, plan_key: str) -> dict[str, Any] | None:
        """The operation that blocks a second mutation on this plan, if any."""
        marks = ", ".join("?" * len(OPERATION_OUTSTANDING_STATUSES))
        with self.reading() as connection:
            row = connection.execute(
                f"SELECT * FROM operation WHERE plan_key = ? AND status IN ({marks}) "
                "ORDER BY requested_at ASC LIMIT 1",
                (plan_key, *sorted(OPERATION_OUTSTANDING_STATUSES)),
            ).fetchone()
        return None if row is None else _operation_record(row)

    def outstanding_operations(self, plan_key: str) -> list[dict[str, Any]]:
        """Every operation still outstanding on this plan, oldest request first."""
        marks = ", ".join("?" * len(OPERATION_OUTSTANDING_STATUSES))
        with self.reading() as connection:
            rows = connection.execute(
                f"SELECT * FROM operation WHERE plan_key = ? AND status IN ({marks}) "
                "ORDER BY requested_at ASC",
                (plan_key, *sorted(OPERATION_OUTSTANDING_STATUSES)),
            ).fetchall()
        return [_operation_record(row) for row in rows]

    def operations_in_status(
        self, status: str, *, connector_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Every operation in one state, oldest request first."""
        query = "SELECT * FROM operation WHERE status = ?"
        parameters: list[Any] = [status]
        if connector_id is not None:
            query += " AND connector_id = ?"
            parameters.append(connector_id)
        query += " ORDER BY requested_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(0, int(limit)))
        with self.reading() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_operation_record(row) for row in rows]

    def exported_operations(
        self, *, now: float, limit: int = DEFAULT_READ_LIMIT
    ) -> list[dict[str, Any]]:
        """What a cockpit may read back about recent operations."""
        cutoff = now - self._policy.summary_retention_seconds
        bound = _clamp_limit(limit)
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM operation WHERE requested_at >= ? "
                "ORDER BY requested_at DESC LIMIT ?",
                (cutoff, bound),
            ).fetchall()
        return [export_operation(_operation_record(row)) for row in rows]

    def command_target(self, run_key: str) -> dict[str, Any] | None:
        """Identifiers for a run in the newest snapshot, or ``None``.

        ``plan_key`` is in here and in nothing a cockpit reads: the browser
        names a run, and the service resolves the plan that run belongs to, so
        the one-outstanding-mutation rule is enforced against an identifier the
        client never chose.
        """
        wanted = (
            "connector_id",
            "run_key",
            "plan_key",
            "run_id",
            "question_id",
            "primary_state",
            "observed_at",
        )
        with self.reading() as connection:
            row = connection.execute(
                f"SELECT {', '.join('r.' + name for name in wanted)} "
                "FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                "WHERE r.run_key = ? AND r.present = 1 "
                "AND r.observed_at = c.snapshot_observed_at "
                "ORDER BY r.observed_at DESC LIMIT 1",
                (run_key,),
            ).fetchone()
        if row is None:
            return None
        return {name: row[name] for name in wanted if row[name] is not None}

    def _operation_row(self, operation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM operation WHERE operation_id = ?", (operation_id,)
        ).fetchone()

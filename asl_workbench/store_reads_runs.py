"""Connector status, run and history reads for the store."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import freshness as freshness_module
from .incidents_models import ACTIVE_STATES
from .observation import (
    OBSERVATION_ABSENT,
    OBSERVATION_CURRENT,
    OBSERVATION_UNCONFIRMED,
    completeness_of,
    export_history_entry,
    export_run_summary,
)
from .store_base import StoreBase
from .store_columns import (
    _HISTORY_BOOLEANS,
)
from .store_types import (
    DEFAULT_READ_LIMIT,
    ConnectorStatus,
    SourceStatus,
)
from .store_values import (
    _as_bool,
    _as_int,
    _clamp_limit,
    _decoded_list,
    _warning_list,
)


class RunReads(StoreBase):
    """Read side for connector health, runs and history."""

    # -- reads -----------------------------------------------------------
    def connector_statuses(self, *, now: float) -> list[ConnectorStatus]:
        """Evaluate every source's health, each part separately."""
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM connector ORDER BY connector_id"
            ).fetchall()
        return [self._connector_status(row, now=now) for row in rows]

    def _connector_status(self, row: sqlite3.Row, *, now: float) -> ConnectorStatus:
        contract_set_id = row["snapshot_contract_set_id"] or row["contract_set_id"]
        compatible = contract_set_id in self._policy.supported_contract_set_ids
        truncated = _as_bool(row["snapshot_truncated"])
        # ``snapshot_observed_at`` is stamped when the corpus walk starts, so it
        # already is the moment the source saw; ``projection_age_seconds`` is
        # the walk's own duration, and subtracting it would count it twice.
        evaluated = freshness_module.evaluate(
            now=now,
            policy=self._policy,
            last_heartbeat_at=row["heartbeat_observed_at"],
            last_snapshot_at=row["snapshot_observed_at"],
            truncated=truncated,
            compatible=compatible,
        )
        run_reasons: list[str] = []
        if truncated:
            run_reasons.append("the newest snapshot was truncated")
        failed = _as_int(row["snapshot_failed_projections"]) or 0
        if failed:
            run_reasons.append(f"ASL could not read {failed} plan(s) during its walk")
        if row["snapshot_observed_at"] is None:
            run_reasons.append("no snapshot has been received")

        history_complete = _as_bool(row["history_complete"])
        history_reasons: list[str] = []
        if history_complete is False:
            history_reasons.append(
                "the last history import could not read every source"
            )
        if row["history_generated_at"] is None:
            history_reasons.append("no history has been imported")
        if _as_bool(row["history_more"]):
            history_reasons.append("more history batches were still to come")

        catalog_complete = _as_bool(row["catalog_complete"])
        catalog_final = _as_bool(row["catalog_final"])
        catalog_reasons: list[str] = []
        if catalog_complete is False:
            catalog_reasons.append(
                "the last catalog generation could not see every plan"
            )
        if row["catalog_generation"] is None:
            catalog_reasons.append("no catalog has been received")
        elif not catalog_final:
            catalog_reasons.append("the newest catalog generation is still arriving")
        if catalog_complete and not catalog_final:
            # Complete but unfinished is not complete yet, and reporting it as
            # complete is what would let a half-delivered catalog retire work.
            catalog_complete = None

        return ConnectorStatus(
            connector_id=str(row["connector_id"]),
            freshness=evaluated,
            compatible=compatible,
            contract_set_id=contract_set_id,
            bridge_alive=_as_bool(row["bridge_alive"]),
            degraded_reason=row["degraded_reason"],
            source_generation=_as_int(row["snapshot_generation"]),
            row_count=_as_int(row["snapshot_row_count"]) or 0,
            truncated=truncated,
            warnings=_warning_list(row["snapshot_warnings"]),
            runs=SourceStatus(
                completeness=completeness_of(truncated), reasons=tuple(run_reasons)
            ),
            history=SourceStatus(
                completeness=completeness_of(history_complete, invert=True),
                reasons=tuple(history_reasons),
            ),
            catalog=SourceStatus(
                completeness=completeness_of(catalog_complete, invert=True),
                reasons=tuple(catalog_reasons),
            ),
            last_heartbeat_at=row["heartbeat_observed_at"],
            last_snapshot_at=row["snapshot_observed_at"],
            asl_version=row["asl_version"],
            bridge_protocol=_as_int(row["bridge_protocol"]),
            projection_age_seconds=row["snapshot_projection_age_seconds"],
        )

    def plan_idle(self, connector_id: str, plan_key: str, *, now: float) -> bool:
        """Whether the newest observation demonstrably shows nothing running.

        Demonstrably: the projection has to be fresh and whole, because a
        stale or truncated one cannot say a run is *not* there.
        """
        status = next(
            (
                found
                for found in self.connector_statuses(now=now)
                if found.connector_id == connector_id
            ),
            None,
        )
        if status is None or not status.freshness.projection_fresh or status.truncated:
            return False
        marks = ", ".join("?" * len(ACTIVE_STATES))
        with self.reading() as connection:
            row = connection.execute(
                "SELECT 1 FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                "WHERE r.connector_id = ? AND r.plan_key = ? AND r.present = 1 "
                "AND r.observed_at = c.snapshot_observed_at "
                f"AND r.primary_state IN ({marks}) LIMIT 1",
                (connector_id, plan_key, *sorted(ACTIVE_STATES)),
            ).fetchone()
        return row is None

    def runs(
        self,
        *,
        now: float,
        project: str | None = None,
        plan_key: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
        include_absent: bool = False,
        search: str | None = None,
        harness: str | None = None,
    ) -> list[dict[str, Any]]:
        """The cockpit's rows, each stamped with its source's freshness.

        Only rows the newest observation still carries are returned by default.
        A row the projection dropped is not a row that finished, so it leaves
        the live view and its ending -- if it has one -- arrives through
        history.

        ``plan_key`` narrows to one plan's runs, and has to be applied here
        rather than by the caller: the plan key is a storage column and not an
        exported field, so a row that has left this method no longer says which
        plan it belongs to.
        """
        cutoff = now - self._policy.summary_retention_seconds
        clauses = ["observed_at >= ?"]
        parameters: list[Any] = [cutoff]
        if not include_absent:
            clauses.append("present = 1")
        if plan_key is not None:
            clauses.append("r.plan_key = ?")
            parameters.append(plan_key)
        with self.reading() as connection:
            labels = self._plan_labels()
            if search and search.strip():
                clauses.append(
                    self._title_clause(search, parameters, labels, prefix="r.")
                )
            if harness:
                clauses.append("harness = ?")
                parameters.append(harness)
            if project is not None:
                clauses.append(
                    self._project_clause(project, parameters, labels, prefix="r.")
                )
            parameters.append(_clamp_limit(limit))
            statement = (
                "SELECT r.*, CASE "
                f"WHEN r.present = 0 THEN '{OBSERVATION_ABSENT}' "
                "WHEN r.observed_at = c.snapshot_observed_at "
                f"THEN '{OBSERVATION_CURRENT}' "
                f"ELSE '{OBSERVATION_UNCONFIRMED}' END AS observation_status "
                "FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY CASE WHEN r.observed_at = c.snapshot_observed_at "
                "AND primary_state IN ('running', 'verifying') THEN 0 "
                "WHEN r.observed_at = c.snapshot_observed_at THEN 1 ELSE 2 END, "
                "COALESCE(last_activity_at, run_started_at, 0) DESC, "
                "run_key ASC LIMIT ?"
            )
            rows = connection.execute(statement, parameters).fetchall()
        freshness_labels = {
            status.connector_id: status.freshness.label
            for status in self.connector_statuses(now=now)
        }
        exported: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["freshness"] = freshness_labels.get(
                str(row["connector_id"]), freshness_module.FRESHNESS_LOST
            )
            record["last_observed_at"] = row["observed_at"]
            self._label_record(record, labels)
            exported.append(export_run_summary(record))
        return exported

    def history(
        self,
        *,
        now: float,
        project: str | None = None,
        outcome: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
        search: str | None = None,
        harness: str | None = None,
    ) -> list[dict[str, Any]]:
        """Terminal runs inside the retention window, newest first."""
        cutoff = now - self._policy.summary_retention_seconds
        clauses = ["recorded_at >= ?"]
        parameters: list[Any] = [cutoff]
        with self.reading() as connection:
            labels = self._plan_labels()
            if search and search.strip():
                clauses.append(self._title_clause(search, parameters, labels))
            if harness:
                clauses.append("harness = ?")
                parameters.append(harness)
            if project is not None:
                clauses.append(self._project_clause(project, parameters, labels))
            if outcome is not None:
                clauses.append("outcome = ?")
                parameters.append(outcome)
            parameters.append(_clamp_limit(limit))
            rows = connection.execute(
                f"SELECT * FROM history_entry WHERE {' AND '.join(clauses)} "
                "ORDER BY recorded_at DESC, entry_id ASC LIMIT ?",
                parameters,
            ).fetchall()
        exported: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            for column in _HISTORY_BOOLEANS:
                record[column] = _as_bool(record[column])
            record["completed_session_ids"] = _decoded_list(
                record["completed_session_ids"]
            )
            self._label_record(record, labels)
            exported.append(export_history_entry(record))
        return exported

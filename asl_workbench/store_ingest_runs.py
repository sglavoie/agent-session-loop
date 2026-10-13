"""Heartbeat and snapshot ingestion for the store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import incidents
from .observation import (
    persist_run_summary,
)
from .store_columns import (
    _RUN_COLUMNS,
)
from .store_retention import StoreRetention
from .store_types import (
    _ORDER_NEWER,
    KIND_HEARTBEAT,
    KIND_SNAPSHOT,
    IngestInvalid,
    IngestRefused,
    IngestResult,
)
from .store_values import (
    _as_int,
    _upsert_statement,
)


class RunIngest(StoreRetention):
    """Ordered, atomic ingestion of heartbeats and run snapshots."""

    # -- ingestion -------------------------------------------------------
    def ingest_heartbeat(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Record transport liveness. Says nothing about the projection."""
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = _as_int(payload.get("source_generation"))
        with self.transaction() as connection:
            row = self._connector_row(connector_id)
            if row is not None and row["heartbeat_observed_at"] is not None:
                if observed_at == float(row["heartbeat_observed_at"]):
                    # The same instant from the same source is a retry of one
                    # heartbeat, not a second one.
                    return IngestResult(
                        kind=KIND_HEARTBEAT, connector_id=connector_id, duplicate=True
                    )
                order = self._order(
                    stored_observed_at=row["heartbeat_observed_at"],
                    stored_generation=row["heartbeat_generation"],
                    observed_at=observed_at,
                    generation=generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer heartbeat is already recorded for this connector",
                    )
            self._ensure_connector(connector_id, now=received_at)
            connection.execute(
                """
                UPDATE connector SET
                    heartbeat_observed_at = ?,
                    heartbeat_received_at = ?,
                    heartbeat_generation = ?,
                    bridge_alive = ?,
                    bridge_protocol = ?,
                    contract_set_id = ?,
                    asl_version = ?,
                    degraded_reason = ?,
                    heartbeat_projection_age_seconds = ?
                WHERE connector_id = ?
                """,
                (
                    observed_at,
                    received_at,
                    generation,
                    1 if payload.get("bridge_alive") else 0,
                    _as_int(payload.get("bridge_protocol")),
                    payload.get("contract_set_id"),
                    payload.get("asl_version"),
                    payload.get("degraded_reason"),
                    payload.get("projection_age_seconds"),
                    connector_id,
                ),
            )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_HEARTBEAT, connector_id=connector_id, pruned=pruned
        )

    def ingest_snapshot(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Replace the projection for one source, wholly or not at all."""
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = _as_int(payload.get("source_generation"))
        digest = str(payload["digest"])
        truncated = bool(payload["truncated"])
        runs = list(payload["runs"])

        with self.transaction() as connection:
            row = self._connector_row(connector_id)
            if row is not None and row["snapshot_observed_at"] is not None:
                if (
                    observed_at == float(row["snapshot_observed_at"])
                    and digest == row["snapshot_digest"]
                ):
                    # Byte-identical redelivery. The source retried an upload
                    # whose response it never saw, and a retry must not disturb
                    # what the first delivery already established.
                    return IngestResult(
                        kind=KIND_SNAPSHOT, connector_id=connector_id, duplicate=True
                    )
                order = self._order(
                    stored_observed_at=row["snapshot_observed_at"],
                    stored_generation=row["snapshot_generation"],
                    observed_at=observed_at,
                    generation=generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer snapshot is already recorded for this connector",
                    )
            self._ensure_connector(connector_id, now=received_at)

            previously_present = {
                str(existing["run_key"])
                for existing in connection.execute(
                    "SELECT run_key FROM run_summary "
                    "WHERE connector_id = ? AND present = 1",
                    (connector_id,),
                )
            }
            if not truncated:
                # A whole observation is the only kind that can say a run is
                # gone. Every row is cleared first and then re-marked by the
                # upserts below, so absence is decided by this snapshot alone.
                connection.execute(
                    "UPDATE run_summary SET present = 0, "
                    "absent_since = COALESCE(absent_since, ?) "
                    "WHERE connector_id = ? AND present = 1",
                    (received_at, connector_id),
                )

            seen: set[str] = set()
            for index, run in enumerate(runs):
                run_key = str(run["run_key"])
                if run_key in seen:
                    # Refused mid-write on purpose: the transaction is what
                    # makes this safe, and a snapshot that counts one run twice
                    # would otherwise leave a row set no observation produced.
                    raise IngestInvalid(f"snapshot.runs[{index}] repeats run {run_key}")
                seen.add(run_key)
                self._write_run(
                    connection,
                    run,
                    connector_id=connector_id,
                    observed_at=observed_at,
                    received_at=received_at,
                    generation=generation,
                )

            retired = self._retire_superseded_runs(
                connection,
                connector_id,
                runs,
                seen=seen,
                previously_present=previously_present,
                received_at=received_at,
            )

            connection.execute(
                """
                UPDATE connector SET
                    snapshot_observed_at = ?,
                    snapshot_received_at = ?,
                    snapshot_generation = ?,
                    snapshot_digest = ?,
                    snapshot_source_digest = ?,
                    snapshot_contract_set_id = ?,
                    snapshot_row_count = ?,
                    snapshot_truncated = ?,
                    snapshot_failed_projections = ?,
                    snapshot_projection_age_seconds = ?,
                    snapshot_staleness_bound_seconds = ?,
                    snapshot_warnings = ?
                WHERE connector_id = ?
                """,
                (
                    observed_at,
                    received_at,
                    generation,
                    digest,
                    payload.get("source_digest"),
                    payload.get("contract_set_id"),
                    int(payload["row_count"]),
                    1 if truncated else 0,
                    _as_int(payload.get("failed_projections")),
                    payload.get("projection_age_seconds"),
                    payload.get("staleness_bound_seconds"),
                    json.dumps(list(payload.get("warnings") or [])),
                    connector_id,
                ),
            )
            incidents.observe_snapshot(
                connection, policy=self._policy, envelope=envelope, now=received_at
            )
            pruned = self._maybe_prune(now=received_at)
        absent = len(retired) if truncated else len(previously_present - seen)
        return IngestResult(
            kind=KIND_SNAPSHOT,
            connector_id=connector_id,
            stored=len(seen),
            absent=absent,
            pruned=pruned,
        )

    def _retire_superseded_runs(
        self,
        connection: sqlite3.Connection,
        connector_id: str,
        runs: Sequence[Mapping[str, Any]],
        *,
        seen: set[str],
        previously_present: set[str],
        received_at: float,
    ) -> set[str]:
        """Retire runs a newer run for the same plan positively replaced.

        Unlike omission, a newer run for the same plan is positive replacement
        evidence, and it holds even when the observation that carried it was
        capped: the new run's existence is a fact the snapshot asserted rather
        than a gap it left.
        """
        retired: set[str] = set()
        for run in runs:
            if not run.get("run_id"):
                continue
            existing = connection.execute(
                "SELECT run_key, run_id, run_started_at, observed_at FROM run_summary "
                "WHERE connector_id = ? AND plan_key = ?",
                (connector_id, run["plan_key"]),
            ).fetchall()
            for old in existing:
                if str(old["run_key"]) in seen or old["run_id"] == run["run_id"]:
                    continue
                started = run.get("run_started_at")
                # An unidentified predecessor is always superseded; an
                # identified one only by a demonstrably later start. A run
                # whose start is older, or unknown, is not evidence at all.
                previous_at = (
                    old["run_started_at"]
                    if old["run_started_at"] is not None
                    else old["observed_at"]
                )
                supersedes = old["run_id"] is None or (
                    started is not None and started > previous_at
                )
                if not supersedes:
                    continue
                if str(old["run_key"]) in previously_present:
                    retired.add(str(old["run_key"]))
                connection.execute(
                    "UPDATE run_summary SET present = 0, "
                    "absent_since = COALESCE(absent_since, ?) "
                    "WHERE connector_id = ? AND run_key = ?",
                    (received_at, connector_id, old["run_key"]),
                )
                incidents.retire_run(
                    connection, connector_id, str(old["run_key"]), received_at
                )
        return retired

    def _write_run(
        self,
        connection: sqlite3.Connection,
        run: Mapping[str, Any],
        *,
        connector_id: str,
        observed_at: float,
        received_at: float,
        generation: int | None,
    ) -> None:
        """Upsert one run row, through the persistence allowlist.

        The allowlist is applied to the wire row rather than the row being
        trusted: the allowlist, not this schema, is what decides that a widened
        snapshot shape cannot widen what is kept.
        """
        record = persist_run_summary({**run, "observed_at": observed_at})
        values = {column: record.get(column) for column in _RUN_COLUMNS}
        values.update(
            {
                "connector_id": connector_id,
                "run_key": record["run_key"],
                "observed_at": observed_at,
                "received_at": received_at,
                "source_generation": generation,
                "present": 1,
                "absent_since": None,
            }
        )
        connection.execute(
            _upsert_statement("run_summary", _RUN_COLUMNS, ("connector_id", "run_key")),
            values,
        )

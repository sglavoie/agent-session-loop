"""History and catalog ingestion for the store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import incidents
from .observation import (
    persist_catalog_entry,
    persist_history_entry,
)
from .store_columns import (
    _CATALOG_BOOLEANS,
    _CATALOG_COLUMNS,
    _CATALOG_IDENTITY,
    _CATALOG_JSON,
    _HISTORY_BOOLEANS,
    _HISTORY_COLUMNS,
)
from .store_retention import StoreRetention
from .store_types import (
    _ORDER_NEWER,
    KIND_CATALOG_PAGE,
    KIND_HISTORY_BATCH,
    IngestInvalid,
    IngestRefused,
    IngestResult,
    StoreError,
)
from .store_values import (
    _as_int,
    _upsert_statement,
)


class HistoryCatalogIngest(StoreRetention):
    """Ordered, atomic ingestion of history batches and catalog pages."""

    def ingest_history(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Upsert one history batch. Redelivery changes nothing.

        History has no generation ordering, because a batch is not a view of
        the corpus: entries are keyed by the run they describe and a run's
        ending does not change. Only the batch *metadata* is ordered, so a late
        batch cannot overwrite a newer import's completeness claim.
        """
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        entries = list(payload["entries"])
        generated_at = float(payload["generated_at"])

        with self.transaction() as connection:
            self._ensure_connector(connector_id, now=received_at)
            row = self._connector_row(connector_id)
            existing = {
                str(found["entry_id"])
                for found in connection.execute(
                    "SELECT entry_id FROM history_entry WHERE connector_id = ?",
                    (connector_id,),
                )
            }
            stored = 0
            updated = 0
            for entry in entries:
                record = persist_history_entry(entry)
                if str(record["entry_id"]) in existing:
                    updated += 1
                else:
                    stored += 1
                self._write_history_entry(
                    connection,
                    record,
                    connector_id=connector_id,
                    received_at=received_at,
                )
                incidents.observe_history(
                    connection,
                    policy=self._policy,
                    connector_id=connector_id,
                    entry=record,
                    now=received_at,
                )
            recorded_generated_at = row["history_generated_at"] if row else None
            if recorded_generated_at is None or generated_at >= float(
                recorded_generated_at
            ):
                connection.execute(
                    """
                    UPDATE connector SET
                        history_batch_id = ?,
                        history_generated_at = ?,
                        history_received_at = ?,
                        history_window_start = ?,
                        history_window_end = ?,
                        history_complete = ?,
                        history_more = ?
                    WHERE connector_id = ?
                    """,
                    (
                        payload.get("batch_id"),
                        generated_at,
                        received_at,
                        payload.get("window_start"),
                        payload.get("window_end"),
                        1 if payload.get("complete") else 0,
                        1 if payload.get("more") else 0,
                        connector_id,
                    ),
                )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_HISTORY_BATCH,
            connector_id=connector_id,
            stored=stored,
            updated=updated,
            duplicate=stored == 0 and updated > 0,
            pruned=pruned,
        )

    def _write_history_entry(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        connector_id: str,
        received_at: float,
    ) -> None:
        values = {column: record.get(column) for column in _HISTORY_COLUMNS}
        values["connector_id"] = connector_id
        values["received_at"] = received_at
        ids = record.get("completed_session_ids")
        values["completed_session_ids"] = None if ids is None else json.dumps(list(ids))
        for column in _HISTORY_BOOLEANS:
            if values[column] is not None:
                values[column] = 1 if values[column] else 0
        connection.execute(
            _upsert_statement(
                "history_entry", _HISTORY_COLUMNS, ("connector_id", "entry_id")
            ),
            values,
        )

    def ingest_catalog(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Apply one page of one catalog generation.

        Three facts travel on every page and all three are load-bearing. The
        ``generation`` says which enumeration this page belongs to, so pages
        from two overlapping scans never mix. ``complete`` says whether that
        enumeration could see everything, so a generation built while a plan
        was mid-write never retires anything. ``final`` says whether the
        generation has finished arriving, so a complete generation does not
        retire the plans its later pages were going to carry.

        The ordering rule is the snapshot's: observation time decides, and the
        source generation counter only refuses what time cannot.
        """
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = str(payload["generation"])
        source_generation = _as_int(payload.get("source_generation"))
        complete = bool(payload.get("complete", False))
        final = bool(payload.get("final", False))
        offset = max(0, int(payload.get("offset", 0)))
        total = _as_int(payload.get("total"))
        entries = list(payload["entries"])
        self._refuse_future(observed_at, received_at=received_at)

        with self.transaction() as connection:
            self._ensure_connector(connector_id, now=received_at)
            row = self._connector_row(connector_id)
            if row is None:  # pragma: no cover - inserted immediately above
                raise StoreError("the connector vanished inside its own transaction")
            delivered = _as_int(row["catalog_delivered"]) or 0
            if row["catalog_generation"] == generation:
                duplicate = offset + len(entries) <= delivered
                delivered = max(delivered, offset + len(entries))
                observed_at = max(
                    observed_at, float(row["catalog_observed_at"] or observed_at)
                )
                already_final = bool(row["catalog_final"])
            else:
                order = self._order(
                    stored_observed_at=row["catalog_observed_at"],
                    stored_generation=row["catalog_source_generation"],
                    observed_at=observed_at,
                    generation=source_generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer catalog generation is already recorded for "
                        "this connector",
                    )
                duplicate = False
                delivered = offset + len(entries)
                already_final = False

            known = {
                str(found["plan_key"])
                for found in connection.execute(
                    "SELECT plan_key FROM catalog_entry WHERE connector_id = ?",
                    (connector_id,),
                )
            }
            stored = 0
            updated = 0
            seen: set[str] = set()
            for index, entry in enumerate(entries):
                plan_key = str(entry["plan_key"])
                if plan_key in seen:
                    raise IngestInvalid(
                        f"catalog.entries[{index}] repeats plan {plan_key}"
                    )
                seen.add(plan_key)
                if plan_key in known:
                    updated += 1
                else:
                    stored += 1
                self._write_catalog_entry(
                    connection,
                    entry,
                    connector_id=connector_id,
                    generation=generation,
                    observed_at=observed_at,
                    received_at=received_at,
                )

            absent = 0
            if complete and final:
                # Only here. A partial generation never proves a plan is gone,
                # and a complete generation that is still arriving has not yet
                # said what it carries.
                absent = max(
                    0,
                    connection.execute(
                        "UPDATE catalog_entry SET present = 0, "
                        "absent_since = COALESCE(absent_since, ?) "
                        "WHERE connector_id = ? AND present = 1 AND generation != ?",
                        (received_at, connector_id, generation),
                    ).rowcount,
                )

            connection.execute(
                """
                UPDATE connector SET
                    catalog_generation = ?,
                    catalog_observed_at = ?,
                    catalog_received_at = ?,
                    catalog_source_generation = ?,
                    catalog_total = ?,
                    catalog_delivered = ?,
                    catalog_complete = ?,
                    catalog_final = ?
                WHERE connector_id = ?
                """,
                (
                    generation,
                    observed_at,
                    received_at,
                    source_generation,
                    total,
                    delivered,
                    1 if complete else 0,
                    1 if (final or already_final) else 0,
                    connector_id,
                ),
            )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_CATALOG_PAGE,
            connector_id=connector_id,
            duplicate=duplicate,
            stored=stored,
            updated=updated,
            absent=absent,
            pruned=pruned,
        )

    def _write_catalog_entry(
        self,
        connection: sqlite3.Connection,
        entry: Mapping[str, Any],
        *,
        connector_id: str,
        generation: str,
        observed_at: float,
        received_at: float,
    ) -> None:
        record = persist_catalog_entry(entry)
        values: dict[str, Any] = {
            column: record.get(column) for column in _CATALOG_COLUMNS
        }
        for column in _CATALOG_JSON:
            payload = record.get(column)
            values[column] = json.dumps(
                list(payload)
                if isinstance(payload, (list, tuple))
                else dict(payload)
                if isinstance(payload, Mapping)
                else []
            )
        for column in _CATALOG_BOOLEANS:
            if values[column] is not None:
                values[column] = 1 if values[column] else 0
        values.update(
            {
                "connector_id": connector_id,
                "plan_key": record["plan_key"],
                "display_label": record.get("display_label") or record["plan_key"],
                "project_group": record.get("project_group") or "",
                "generation": generation,
                "observed_at": observed_at,
                "received_at": received_at,
                "present": 1,
                "absent_since": None,
            }
        )
        connection.execute(
            _upsert_statement("catalog_entry", _CATALOG_COLUMNS, _CATALOG_IDENTITY),
            values,
        )

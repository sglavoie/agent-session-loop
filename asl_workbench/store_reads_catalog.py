"""Catalog, counts and schema reads for the store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from . import incidents
from .observation import (
    OBSERVATION_ABSENT,
    OBSERVATION_CURRENT,
    OBSERVATION_UNCONFIRMED,
    export_catalog_entry,
)
from .store_base import StoreBase
from .store_columns import (
    _CATALOG_BOOLEANS,
)
from .store_types import (
    DEFAULT_READ_LIMIT,
    LEGACY_GENERATION,
    Counts,
)
from .store_values import (
    _as_bool,
    _clamp_limit,
    _decoded_list,
    _decoded_mapping,
)


class CatalogReads(StoreBase):
    """Read side for the plan catalog, aggregate counts and schema checks."""

    @staticmethod
    def _catalog_clauses(
        *,
        connector_id: str | None,
        project: str | None,
        search: str | None,
        harness: str | None,
        states: Sequence[str] | None,
        include_absent: bool,
    ) -> tuple[list[str], list[Any]]:
        """The ``WHERE`` half of a catalog read, built once for three callers.

        Every narrowing the cockpit can ask for is expressed here, which is
        what makes "filter before the read limit" a property of the statement
        rather than a rule three methods have to remember. The page, the
        matched total and the state histogram all start from this, so a page
        and the number above it cannot describe different sets.
        """
        clauses = ["e.generation != ?"]
        parameters: list[Any] = [LEGACY_GENERATION]
        if connector_id is not None:
            clauses.append("e.connector_id = ?")
            parameters.append(connector_id)
        if not include_absent:
            clauses.append("e.present = 1")
        if search and search.strip():
            clauses.append("e.search_text LIKE ?")
            parameters.append(f"%{search.strip().casefold()}%")
        if project is not None:
            # ``projects`` is a JSON array, so the needle is the encoded name
            # rather than the bare one: matching ``"scripts"`` with quotes is
            # what stops a search for ``script`` from claiming membership.
            clauses.append("e.projects LIKE ?")
            parameters.append(f"%{json.dumps(project)}%")
        if harness:
            clauses.append("e.harness = ?")
            parameters.append(harness)
        if states is not None:
            # An empty set is not "no filter": it is a chip that covers nothing
            # once completed plans are hidden, and it must match nothing.
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"e.primary_state IN ({placeholders})" if states else "0")
            parameters.extend(states)
        return clauses, parameters

    def catalog_revision(self) -> str:
        """Stable identity for the published catalog across paged reads."""
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT connector_id, catalog_generation, catalog_complete "
                "FROM connector ORDER BY connector_id"
            ).fetchall()
        material = [tuple(row) for row in rows]
        return hashlib.sha256(
            json.dumps(material, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

    def catalog(
        self,
        *,
        now: float,
        connector_id: str | None = None,
        project: str | None = None,
        search: str | None = None,
        harness: str | None = None,
        states: Sequence[str] | None = None,
        include_absent: bool = False,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> list[dict[str, Any]]:
        """Stored catalog entries, newest generation first, one page at a time.

        Carried-over Companion labels are never returned here. They belong to
        :data:`LEGACY_GENERATION`, which is not a generation anybody observed:
        they can label a retained run and they cannot claim a plan exists.
        """
        clauses, parameters = self._catalog_clauses(
            connector_id=connector_id,
            project=project,
            search=search,
            harness=harness,
            states=states,
            include_absent=include_absent,
        )
        parameters.extend([_clamp_limit(limit), max(0, int(offset))])
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT e.*, c.catalog_generation AS current_generation, "
                "c.catalog_complete AS current_complete "
                "FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY e.urgency_rank ASC, e.plan_key ASC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        exported: list[dict[str, Any]] = []
        for row in rows:
            exported.append(export_catalog_entry(self._catalog_record(row)))
        return exported

    def catalog_matched(
        self,
        *,
        connector_id: str | None = None,
        project: str | None = None,
        search: str | None = None,
        harness: str | None = None,
        states: Sequence[str] | None = None,
        include_absent: bool = False,
    ) -> int:
        """How many entries the same narrowing matches, page or no page.

        The number an operator is shown next to a page. Counted by the database
        over the whole filtered set rather than by the caller over the rows it
        received, which is the difference between "12 of 340" and "12".
        """
        clauses, parameters = self._catalog_clauses(
            connector_id=connector_id,
            project=project,
            search=search,
            harness=harness,
            states=states,
            include_absent=include_absent,
        )
        with self.reading() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()
        return int(row["total"])

    def catalog_states(
        self,
        *,
        connector_id: str | None = None,
        project: str | None = None,
        search: str | None = None,
        harness: str | None = None,
        include_absent: bool = False,
    ) -> dict[str, int]:
        """A primary-state histogram of everything the narrowing matches.

        Deliberately takes no ``states``: this is what the filter chips count
        from, and a chip narrowed by the chip already pressed would report its
        own page back to itself. Project, title and harness do apply, because
        those are narrowings the chips sit inside.
        """
        clauses, parameters = self._catalog_clauses(
            connector_id=connector_id,
            project=project,
            search=search,
            harness=harness,
            states=None,
            include_absent=include_absent,
        )
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT e.primary_state AS primary_state, COUNT(*) AS total "
                "FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)} GROUP BY e.primary_state",
                parameters,
            ).fetchall()
        return {str(row["primary_state"]): int(row["total"]) for row in rows}

    def catalog_projects(self, *, include_absent: bool = False) -> list[str]:
        """Every project the catalog names, sorted, each named once.

        Read from the catalog rather than from :meth:`projects`, which walks
        retained runs and endings: the filter an operator picks from has to
        offer the projects the plan list can actually be narrowed to.
        """
        clauses, parameters = self._catalog_clauses(
            connector_id=None,
            project=None,
            search=None,
            harness=None,
            states=None,
            include_absent=include_absent,
        )
        names: set[str] = set()
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT e.projects AS projects FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchall()
        for row in rows:
            for name in _decoded_list(row["projects"]) or []:
                text = str(name).strip()
                if text:
                    names.add(text)
        return sorted(names)

    def catalog_harnesses(self, *, include_absent: bool = False) -> list[str]:
        """Every harness the catalog names, sorted."""
        clauses, parameters = self._catalog_clauses(
            connector_id=None,
            project=None,
            search=None,
            harness=None,
            states=None,
            include_absent=include_absent,
        )
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT DISTINCT e.harness AS harness FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY e.harness",
                parameters,
            ).fetchall()
        return [str(row["harness"]) for row in rows if row["harness"]]

    def incident_diagnostics(
        self, *, limit: int = incidents.DEFAULT_DIAGNOSTICS_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent incidents and their delivery states, newest first.

        A read, and only a read. :class:`~asl_workbench.incidents.IncidentEngine`
        also exposes these, but constructing one is a recovery step that rewrites
        whatever a dead process left publishing -- which is exactly right when a
        deployment starts and exactly wrong inside a GET.
        """
        with self.reading() as connection:
            return incidents.diagnostics(connection, limit=limit)

    @staticmethod
    def _catalog_record(row: sqlite3.Row) -> dict[str, Any]:
        record: dict[str, Any] = dict(row)
        for column in _CATALOG_BOOLEANS:
            record[column] = _as_bool(record[column])
        record["projects"] = _decoded_list(record["projects"]) or []
        record["sources"] = _decoded_list(record["sources"]) or []
        record["counts"] = _decoded_mapping(record["counts"])
        record["multi_repository"] = (record["repository_count"] or 0) > 1
        record["complete"] = _as_bool(record["current_complete"])
        record["last_observed_at"] = record["observed_at"]
        if not record["present"]:
            record["observation_status"] = OBSERVATION_ABSENT
        elif record["generation"] == record["current_generation"]:
            record["observation_status"] = OBSERVATION_CURRENT
        else:
            record["observation_status"] = OBSERVATION_UNCONFIRMED
        return record

    def harnesses(self, *, now: float) -> list[str]:
        """Harness choices cover retained work, independent of active filters."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT DISTINCT harness FROM run_summary "
                "WHERE observed_at >= ? AND present = 1 "
                "UNION SELECT DISTINCT harness FROM history_entry "
                "WHERE recorded_at >= ? ORDER BY harness",
                (cutoff, cutoff),
            ).fetchall()
        return [str(row["harness"]) for row in rows if row["harness"]]

    def projects(self, *, now: float) -> list[str]:
        """Every project name a cockpit could filter by, sorted."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT DISTINCT connector_id, plan_key, project FROM run_summary "
                "WHERE observed_at >= ? AND present = 1 "
                "UNION SELECT DISTINCT connector_id, plan_key, project "
                "FROM history_entry WHERE recorded_at >= ? "
                "ORDER BY project ASC",
                (cutoff, cutoff),
            ).fetchall()
            labels = self._plan_labels()
        names: set[str] = set()
        for row in rows:
            names.update(self._project_names(dict(row), labels))
        return sorted(names)

    def counts(self, *, now: float) -> Counts:
        """Aggregate numbers with no identity attached."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            runs = connection.execute(
                "SELECT r.urgency_group, r.present, "
                "r.observed_at = c.snapshot_observed_at AS current, "
                "COUNT(*) AS total FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                "WHERE r.observed_at >= ? "
                "GROUP BY r.urgency_group, r.present, current",
                (cutoff,),
            ).fetchall()
            entries = connection.execute(
                "SELECT COUNT(*) AS total FROM history_entry WHERE recorded_at >= ?",
                (cutoff,),
            ).fetchone()
            connectors = connection.execute(
                "SELECT COUNT(*) AS total FROM connector"
            ).fetchone()
            plans = connection.execute(
                "SELECT e.present, e.generation = c.catalog_generation AS current, "
                "COUNT(*) AS total FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                "WHERE e.generation != ? GROUP BY e.present, current",
                (LEGACY_GENERATION,),
            ).fetchall()
        present = needs_attention = running = absent = unconfirmed = 0
        for row in runs:
            total = int(row["total"])
            if not row["present"]:
                absent += total
                continue
            present += total
            if not row["current"]:
                unconfirmed += total
                continue
            if row["urgency_group"] == "needs-action":
                needs_attention += total
            elif row["urgency_group"] == "in-progress":
                running += total
        plan_total = absent_plans = unconfirmed_plans = 0
        for row in plans:
            total = int(row["total"])
            if not row["present"]:
                absent_plans += total
                continue
            plan_total += total
            if not row["current"]:
                unconfirmed_plans += total
        return Counts(
            runs=present,
            needs_attention=needs_attention,
            running=running,
            absent_runs=absent,
            unconfirmed_runs=unconfirmed,
            history_entries=int(entries["total"]),
            connectors=int(connectors["total"]),
            plans=plan_total,
            absent_plans=absent_plans,
            unconfirmed_plans=unconfirmed_plans,
        )

    def columns(self, table: str) -> tuple[str, ...]:
        """The column names of one table, for tests that check the schema."""
        with self.reading() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def healthy(self) -> bool:
        """Whether the database still answers. The liveness endpoint's question."""
        try:
            with self.reading() as connection:
                connection.execute("SELECT 1 FROM service_meta LIMIT 1").fetchall()
        except sqlite3.Error:
            return False
        return True

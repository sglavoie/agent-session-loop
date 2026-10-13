"""Connection ownership, transactions, metadata and labelling for the store."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from .catalog import UNKNOWN_PROJECT
from .observation import (
    MAX_LABEL_CHARS,
    ObservationPolicy,
    require_policy,
)
from .store_migrations import (
    connect,
    migrate,
)
from .store_types import (
    _ORDER_NEWER,
    META_CREATED_AT,
    REFUSED_STALE_GENERATION,
    REFUSED_STALE_OBSERVATION,
)
from .store_values import (
    _decoded_list,
    _legacy_project,
    _membership_clause,
)


class StoreBase:
    """The connection, lock and helpers every store mixin builds on."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        policy: ObservationPolicy,
        owns_connection: bool = False,
        created_at: float = 0.0,
    ) -> None:
        self._connection = connection
        self._policy = require_policy(policy)
        self._owns = owns_connection
        self._lock = threading.RLock()
        self._connection.row_factory = sqlite3.Row
        # A connection handed in by a test may still be in Python's implicit
        # transaction mode; the transaction helper below owns every BEGIN.
        self._connection.isolation_level = None
        self.schema_version = migrate(self._connection, created_at=created_at)

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        policy: ObservationPolicy,
        timeout: float = 5.0,
        created_at: float = 0.0,
    ) -> Self:
        """Open, migrate, and own one database file."""
        connection = connect(path, timeout=timeout)
        try:
            return cls(
                connection,
                policy=policy,
                owns_connection=True,
                created_at=created_at,
            )
        except BaseException:
            connection.close()
            raise

    def close(self) -> None:
        if self._owns:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def policy(self) -> ObservationPolicy:
        return self._policy

    # -- transactions ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One all-or-nothing unit of work, with the write lock taken up front.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: the writes
        below read what is already stored before deciding what to write, and a
        deferred transaction would take the write lock only at the first write,
        after the decision was made against a snapshot another writer could
        have replaced.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection, held against concurrent writers, for reads only."""
        with self._lock:
            yield self._connection

    def meta(self, key: str, default: Any = None) -> Any:
        """One decoded service-metadata value."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM service_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_meta(self, key: str, value: Any) -> None:
        """Write one service-metadata value, inside whatever transaction is open."""
        with self._lock:
            self._connection.execute(
                "INSERT INTO service_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    @property
    def created_at(self) -> float:
        value = self.meta(META_CREATED_AT, 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    # -- labelling -------------------------------------------------------
    def _plan_labels(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Every plan's label, title and project membership, from the catalog.

        Read from ``catalog_entry`` rather than from the run rows: the catalog
        is the enumeration that knows what a plan is called, and a run summary
        only ever carried a guess.
        """
        labels: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._connection.execute(
            "SELECT connector_id, plan_key, display_label, title, projects, "
            "project_group FROM catalog_entry"
        ):
            labels[(str(row["connector_id"]), str(row["plan_key"]))] = {
                "display_label": row["display_label"],
                "title": row["title"],
                # The catalog's "Unknown project" is its own fallback, not a
                # name: it must not overwrite a run row that knows better.
                "projects": [
                    str(name)
                    for name in _decoded_list(row["projects"]) or []
                    if str(name) != UNKNOWN_PROJECT
                ],
                "project_group": row["project_group"],
            }
        return labels

    @staticmethod
    def _label_record(
        record: dict[str, Any], labels: Mapping[tuple[str, str], dict[str, Any]]
    ) -> None:
        metadata = labels.get((str(record["connector_id"]), str(record["plan_key"])))
        if metadata and metadata["projects"]:
            record["project"] = " + ".join(metadata["projects"])[:MAX_LABEL_CHARS]
        else:
            record["project"] = _legacy_project(str(record["project"]))
        if metadata:
            record["plan_label"] = metadata["display_label"]
            if metadata["title"]:
                record["plan_title"] = metadata["title"]

    @staticmethod
    def _project_names(
        record: Mapping[str, Any], labels: Mapping[tuple[str, str], dict[str, Any]]
    ) -> list[str]:
        metadata = labels.get((str(record["connector_id"]), str(record["plan_key"])))
        if metadata and metadata["projects"]:
            return list(metadata["projects"])
        return [_legacy_project(str(record["project"]))]

    def _project_clause(
        self,
        project: str,
        parameters: list[Any],
        labels: Mapping[tuple[str, str], dict[str, Any]],
        *,
        prefix: str = "",
    ) -> str:
        memberships: dict[str, set[str]] = {}
        for row in self._connection.execute(
            "SELECT DISTINCT connector_id, plan_key, project FROM run_summary "
            "UNION SELECT DISTINCT connector_id, plan_key, project FROM history_entry"
        ):
            if project in self._project_names(dict(row), labels):
                memberships.setdefault(str(row["connector_id"]), set()).add(
                    str(row["plan_key"])
                )
        return _membership_clause(memberships, parameters, prefix=prefix)

    @staticmethod
    def _title_clause(
        search: str,
        parameters: list[Any],
        labels: Mapping[tuple[str, str], dict[str, Any]],
        *,
        prefix: str = "",
    ) -> str:
        """Match manifest titles before applying the read limit."""
        needle = search.strip().casefold()
        memberships: dict[str, set[str]] = {}
        for (connector_id, plan_key), metadata in labels.items():
            title = metadata["title"] or ""
            if needle in str(title).casefold() and title:
                memberships.setdefault(connector_id, set()).add(plan_key)
        return _membership_clause(memberships, parameters, prefix=prefix)

    # -- ordering --------------------------------------------------------
    @staticmethod
    def _order(
        *,
        stored_observed_at: Any,
        stored_generation: Any,
        observed_at: float,
        generation: int | None,
    ) -> str:
        """Decide whether one upload supersedes what is already stored.

        Time decides, and the generation only refuses what time cannot. A
        restart resets the generation counter while the connector id stays the
        same, so a lower generation carrying a *newer* observation is a
        restarted source rather than a replay -- refusing it would leave a
        restarted Mac permanently unable to report.
        """
        if stored_observed_at is None:
            return _ORDER_NEWER
        if observed_at > float(stored_observed_at):
            return _ORDER_NEWER
        if (
            generation is not None
            and stored_generation is not None
            and generation < int(stored_generation)
        ):
            return REFUSED_STALE_GENERATION
        return REFUSED_STALE_OBSERVATION

    def _connector_row(self, connector_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM connector WHERE connector_id = ?", (connector_id,)
        ).fetchone()

    def _ensure_connector(self, connector_id: str, *, now: float) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO connector (connector_id, first_seen_at) "
            "VALUES (?, ?)",
            (connector_id, now),
        )

# ruff: noqa: F403, F405
"""Bounded incident evaluation and persist-before-publish delivery."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .incidents_models import *
from .incidents_models import _DETAIL_FOR_STATE
from .incidents_persistence import epoch, record, resolve
from .incidents_reads import diagnostics, prune


class IncidentEngine:
    """One publisher per deployment. Ticks are bounded and clock-driven.

    Construction is itself a recovery step: anything left ``publishing`` by a
    previous process becomes ``unconfirmed``, because a process that died
    mid-send cannot say whether the send happened. No path replays it.
    """

    def __init__(
        self,
        *,
        store: IncidentStore,
        clock: Callable[[], float],
        publisher: Publisher | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.publisher = publisher
        self._tick_lock = threading.Lock()
        with store.transaction() as connection:
            epoch(connection, clock())
            connection.execute(
                "UPDATE incident SET delivery_state = ?, detail_code = ? "
                "WHERE delivery_state = ?",
                (
                    DELIVERY_UNCONFIRMED,
                    DETAIL_RESTART_DURING_PUBLISH,
                    DELIVERY_PUBLISHING,
                ),
            )

    # -- evaluation ------------------------------------------------------
    def _answered(self, connection: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
        """Whether a reply to this question was admitted into the journal."""
        found = connection.execute(
            "SELECT 1 FROM operation WHERE connector_id = ? AND run_key = ? "
            "AND question_id = ? AND method = 'question.reply' "
            "AND status IN ('accepted', 'consumed') LIMIT 1",
            (row["connector_id"], row["run_key"], row["question_id"]),
        ).fetchone()
        return found is not None

    def _evaluate(self, connection: sqlite3.Connection, now: float) -> None:
        policy = self.store.policy
        watches = connection.execute(
            "SELECT * FROM incident_watch WHERE active = 1 OR question_id IS NOT NULL"
        ).fetchall()
        for found in watches:
            watch: dict[str, Any] = dict(found)
            if watch["question_id"] and self._answered(connection, watch):
                resolve(
                    connection,
                    str(watch["connector_id"]),
                    str(watch["run_key"]),
                    now,
                    kind=INCIDENT_UNANSWERED_QUESTION,
                )
                connection.execute(
                    "UPDATE incident_watch SET question_id = NULL, "
                    "question_since = NULL WHERE connector_id = ? AND run_key = ?",
                    (watch["connector_id"], watch["run_key"]),
                )
                watch["question_id"] = None
            if watch["active"] and lost_observation_incident(
                now=now,
                policy=policy,
                active_work_last_seen_at=watch["last_seen_at"],
            ):
                record(
                    connection,
                    kind=INCIDENT_LOST_OBSERVATION,
                    connector_id=str(watch["connector_id"]),
                    run_key=str(watch["run_key"]),
                    plan_key=str(watch["plan_key"]),
                    project=str(watch["project"]),
                    now=now,
                    discriminator=watch["last_seen_at"],
                )
            if watch["question_id"] and unanswered_question_incident(
                now=now, policy=policy, asked_at=watch["question_since"]
            ):
                record(
                    connection,
                    kind=INCIDENT_UNANSWERED_QUESTION,
                    connector_id=str(watch["connector_id"]),
                    run_key=str(watch["run_key"]),
                    plan_key=str(watch["plan_key"]),
                    project=str(watch["project"]),
                    now=now,
                    discriminator=watch["question_id"],
                    question_id=str(watch["question_id"]),
                )
        prune(connection, policy=policy, now=now)

    def _eligible(
        self, connection: sqlite3.Connection, row: Mapping[str, Any], now: float
    ) -> bool:
        """Whether this incident is still worth sending.

        Only an unanswered question can stop being worth sending without being
        resolved: it is the one kind whose premise -- somebody is waiting and
        we can still see them waiting -- can quietly expire. A lost observation
        and a terminal failure stay true.
        """
        if row["kind"] != INCIDENT_UNANSWERED_QUESTION:
            return True
        if self._answered(connection, row):
            return False
        stale = self.store.policy.observation_stale_seconds
        watch = connection.execute(
            "SELECT * FROM incident_watch WHERE connector_id = ? AND run_key = ?",
            (row["connector_id"], row["run_key"]),
        ).fetchone()
        if watch is None or watch["question_id"] != row["question_id"]:
            return False
        if now - float(watch["last_seen_at"]) > stale:
            return False
        connector = connection.execute(
            "SELECT * FROM connector WHERE connector_id = ?",
            (row["connector_id"],),
        ).fetchone()
        if connector is None or not connector["bridge_alive"]:
            return False
        if (
            connector["contract_set_id"]
            not in self.store.policy.supported_contract_set_ids
        ):
            return False
        heartbeat_at = connector["heartbeat_observed_at"]
        if heartbeat_at is None or now - float(heartbeat_at) > stale:
            return False
        projection_age = float(connector["heartbeat_projection_age_seconds"] or 0.0)
        return projection_age + max(0.0, now - float(heartbeat_at)) <= stale

    # -- delivery --------------------------------------------------------
    def tick(self) -> None:
        """Evaluate evidence, then carry at most one notification.

        One per tick, deliberately. The alternative is a loop holding a claim
        over a batch, and a batch is what turns a single transport hang into a
        queue of incidents nobody can tell the state of.
        """
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            claimed = self._claim()
            if claimed is None:
                return
            result = self._publish(claimed)
            self._settle(claimed, result)
        finally:
            self._tick_lock.release()

    def _claim(self) -> dict[str, Any] | None:
        now = self.clock()
        with self.store.transaction() as connection:
            self._evaluate(connection, now)
            if self.publisher is None or not self.store.policy.publish_incidents:
                return None
            candidates = connection.execute(
                "SELECT * FROM incident WHERE resolved_at IS NULL "
                "AND delivery_state IN (?, ?) AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, incident_id LIMIT ?",
                (
                    DELIVERY_PENDING,
                    DELIVERY_RETRY,
                    now,
                    MAX_DELIVERY_CANDIDATES,
                ),
            ).fetchall()
            claimed: dict[str, Any] | None = None
            for candidate in candidates:
                if self._eligible(connection, candidate, now):
                    claimed = dict(candidate)
                    break
                # A stale question must not starve newer eligible incidents
                # outside this bounded batch of candidates.
                connection.execute(
                    "UPDATE incident SET next_attempt_at = ? WHERE incident_id = ?",
                    (now + STARVATION_DEFERRAL_SECONDS, candidate["incident_id"]),
                )
            if claimed is None:
                return None
            connection.execute(
                "UPDATE incident SET delivery_state = ?, attempts = attempts + 1, "
                "dispatched_at = ?, detail_code = NULL WHERE incident_id = ?",
                (DELIVERY_PUBLISHING, now, claimed["incident_id"]),
            )
            return claimed

    def _publish(self, claimed: Mapping[str, Any]) -> PublishResult:
        """Send outside the transaction and the lock, and never raise."""
        incident_id = str(claimed["incident_id"])

        def before_send() -> bool:
            with self.store.reading() as connection:
                current = connection.execute(
                    "SELECT * FROM incident WHERE incident_id = ?", (incident_id,)
                ).fetchone()
                return bool(
                    current is not None
                    and current["resolved_at"] is None
                    and current["delivery_state"] == DELIVERY_PUBLISHING
                    and self._eligible(connection, current, self.clock())
                )

        publisher = self.publisher
        if publisher is None:  # pragma: no cover - claimed only when set
            return PublishResult(DELIVERY_UNCONFIRMED)
        try:
            result = publisher.publish(
                kind=str(claimed["kind"]),
                project=str(claimed["project"]),
                plan_key=str(claimed["plan_key"]),
                run_key=str(claimed["run_key"]),
                question_id=(
                    str(claimed["question_id"]) if claimed.get("question_id") else None
                ),
                before_send=before_send,
            )
        except Exception:  # noqa: BLE001 - an unknown failure sent nothing knowable
            # A transport that raised may still have delivered. Only a
            # publisher that says so establishes that it did not.
            return PublishResult(DELIVERY_UNCONFIRMED)
        if not isinstance(result, PublishResult):
            return PublishResult(DELIVERY_UNCONFIRMED)
        return result

    def _settle(self, claimed: Mapping[str, Any], result: PublishResult) -> None:
        with self.store.transaction() as connection:
            now = self.clock()
            connection.execute(
                "UPDATE incident SET delivery_state = CASE "
                "WHEN resolved_at IS NOT NULL AND ? = ? THEN ? ELSE ? END, "
                "receipt_id = ?, published_at = ?, next_attempt_at = ?, "
                "detail_code = ? WHERE incident_id = ?",
                (
                    result.state,
                    DELIVERY_RETRY,
                    DELIVERY_CANCELLED,
                    result.state,
                    result.receipt_id,
                    now if result.state == DELIVERY_PUBLISHED else None,
                    now + backoff_seconds(int(claimed["attempts"])),
                    _DETAIL_FOR_STATE[result.state],
                    claimed["incident_id"],
                ),
            )

    # -- reads -----------------------------------------------------------
    def diagnostics(
        self, *, limit: int = DEFAULT_DIAGNOSTICS_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent incidents and their delivery states, newest first."""
        with self.store.reading() as connection:
            return diagnostics(connection, limit=limit)

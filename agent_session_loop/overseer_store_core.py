"""The ledger's connection, transaction, policy epoch, enrollment, and gating.

:class:`LedgerCore` is the base the other ledger mixins build on: it owns the
SQLite connection, the single all-or-nothing write path, and the two tables
every other boundary reads first -- the configuration in force and the last
answer AgentMux gave about each plan -- plus the grant checks every gated read
and write goes through.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, Self

from . import clock, overseer_policy_client
from .overseer_store_models import (
    EnrollmentRecord,
    LedgerError,
    LedgerRefused,
    PolicyEpoch,
    PolicyTransition,
    Withholding,
)
from .overseer_store_rows import optional_text
from .overseer_store_schema import user_version


class LedgerCore:
    """Connection, transaction, policy epoch, enrollment, and grant gating."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        path: Path,
        now: Callable[[], str] = clock.utc_iso_now,
    ) -> None:
        self._connection = connection
        self._now = now
        self.path = path

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> Literal[False]:
        self.close()
        return False

    def close(self) -> None:
        """Checkpoint the write-ahead log and close the database.

        The checkpoint matters for what comes after: WAL keeps recent commits in
        a sidecar file, and folding them back means the ledger a backup or an
        operator copies is the whole ledger rather than most of it.
        """
        with contextlib.suppress(sqlite3.Error):
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()

    @property
    def schema_version(self) -> int:
        """The schema version this database is actually at."""
        return user_version(self._connection)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block as one all-or-nothing write.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: the write
        lock is taken up front, so two overseers reaching the ledger at once
        queue on the busy timeout instead of discovering the conflict at commit
        time, after one of them has already decided what it is going to write.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # -- policy epoch ------------------------------------------------------

    def current_epoch(self) -> PolicyEpoch:
        """The configuration the ledger believes is in force."""
        row = self._connection.execute(
            "SELECT epoch, config_fingerprint, observed_at "
            "FROM policy_epoch WHERE id = 1"
        ).fetchone()
        if row is None:
            return PolicyEpoch(epoch=0, config_fingerprint="", observed_at="")
        return PolicyEpoch(
            epoch=int(row["epoch"]),
            config_fingerprint=str(row["config_fingerprint"]),
            observed_at=str(row["observed_at"]),
        )

    def note_policy(self, *, config_fingerprint: str) -> PolicyTransition:
        """Record which configuration is in force, sealing evidence if it moved.

        The fingerprint comes from a policy answer, which is the only place it
        can honestly come from: it names the declarations AgentMux decided
        against, and ASL has no independent way to compute it.

        A change seals every unsealed evidence row in one transaction with the
        epoch bump. Doing it in two steps would leave a window in which the
        epoch has moved and the evidence has not, and a read landing inside that
        window would be handed content gathered under a policy that no longer
        applies.
        """
        if not config_fingerprint:
            raise LedgerError("a policy observation must name a configuration")
        moment = self._now()
        with self.transaction() as connection:
            current = self.current_epoch()
            if current.config_fingerprint == config_fingerprint:
                return PolicyTransition(
                    epoch=current.epoch,
                    config_fingerprint=config_fingerprint,
                    previous_fingerprint=config_fingerprint,
                    changed=False,
                    sealed_evidence=0,
                )
            epoch = current.epoch + 1
            sealed = 0
            if current.observed:
                cursor = connection.execute(
                    "UPDATE evidence SET sealed = 1 WHERE sealed = 0"
                )
                sealed = cursor.rowcount if cursor.rowcount > 0 else 0
            connection.execute(
                """
                INSERT INTO policy_epoch (id, epoch, config_fingerprint, observed_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    epoch = excluded.epoch,
                    config_fingerprint = excluded.config_fingerprint,
                    observed_at = excluded.observed_at
                """,
                (epoch, config_fingerprint, moment),
            )
            return PolicyTransition(
                epoch=epoch,
                config_fingerprint=config_fingerprint,
                previous_fingerprint=current.config_fingerprint,
                changed=current.observed,
                sealed_evidence=sealed,
            )

    # -- enrollment --------------------------------------------------------

    def record_enrollment(
        self, decision: overseer_policy_client.Decision
    ) -> EnrollmentRecord:
        """Record what AgentMux last said about this plan's enrollment.

        Three outcomes, and only one of them moves the enrollment bit upward. An
        available allowance enrolls; an available refusal revokes; an
        *unavailable* answer changes neither, and records the failure instead.
        That last case is the whole point: an overseer that could not reach
        AgentMux has learned nothing about enrollment, and a ledger that let a
        transport fault clear a revocation -- or preserve an enrollment as
        though it had been reconfirmed -- would be laundering an outage into an
        answer.
        """
        moment = self._now()
        failure = decision.failure
        with self.transaction() as connection:
            existing = self.enrollment(decision.plan_dir)
            if decision.available:
                enrolled = decision.allowed
                revoked = not decision.allowed
                fingerprint = decision.config_fingerprint
                digest = decision.request_digest
                epoch = self.current_epoch().epoch
            elif existing is not None:
                enrolled = existing.enrolled
                revoked = existing.revoked
                fingerprint = existing.config_fingerprint
                digest = ""
                epoch = existing.epoch
            else:
                enrolled = False
                revoked = False
                fingerprint = ""
                digest = ""
                epoch = 0
            first_seen = existing.first_seen_at if existing is not None else moment
            connection.execute(
                """
                INSERT INTO enrollment (
                    plan_dir, enrolled, revoked, epoch, config_fingerprint,
                    access, request_digest, last_failure, first_seen_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    enrolled = excluded.enrolled,
                    revoked = excluded.revoked,
                    epoch = excluded.epoch,
                    config_fingerprint = excluded.config_fingerprint,
                    access = excluded.access,
                    request_digest = excluded.request_digest,
                    last_failure = excluded.last_failure,
                    observed_at = excluded.observed_at
                """,
                (
                    decision.plan_dir,
                    int(enrolled),
                    int(revoked),
                    epoch,
                    fingerprint,
                    decision.access,
                    digest,
                    failure,
                    first_seen,
                    moment,
                ),
            )
        recorded = self.enrollment(decision.plan_dir)
        if recorded is None:
            raise LedgerError(
                f"enrollment for {decision.plan_dir} vanished immediately after "
                "it was written"
            )
        return recorded

    def enrollment(self, plan_dir: str) -> EnrollmentRecord | None:
        """The last enrollment answer for ``plan_dir``, if one was recorded."""
        row = self._connection.execute(
            """
            SELECT plan_dir, enrolled, revoked, epoch, config_fingerprint, access,
                   last_failure, first_seen_at, observed_at
            FROM enrollment WHERE plan_dir = ?
            """,
            (plan_dir,),
        ).fetchone()
        if row is None:
            return None
        return EnrollmentRecord(
            plan_dir=str(row["plan_dir"]),
            enrolled=bool(row["enrolled"]),
            revoked=bool(row["revoked"]),
            epoch=int(row["epoch"]),
            config_fingerprint=str(row["config_fingerprint"]),
            access=str(row["access"]),
            last_failure=optional_text(row["last_failure"]),
            first_seen_at=str(row["first_seen_at"]),
            observed_at=str(row["observed_at"]),
        )

    def revoke(self, *, plan_dir: str, reason: str) -> int:
        """Withdraw a plan and seal everything already gathered from it.

        Used when the overseer learns a plan is excluded outside the ordinary
        decision path -- an operator withdrawal, a run whose repository set no
        longer matches what was authorized. Sealing rather than deleting: the
        shared requirements say revocation stops subsequent access, not that the
        record of what was already read is destroyed. The rows stay, unreadable,
        until a fresh grant releases them.
        """
        moment = self._now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO enrollment (
                    plan_dir, enrolled, revoked, epoch, config_fingerprint,
                    access, request_digest, last_failure, first_seen_at, observed_at
                ) VALUES (?, 0, 1, 0, '', '', '', ?, ?, ?)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    enrolled = 0,
                    revoked = 1,
                    request_digest = '',
                    last_failure = excluded.last_failure,
                    observed_at = excluded.observed_at
                """,
                (plan_dir, reason, moment, moment),
            )
            cursor = connection.execute(
                "UPDATE evidence SET sealed = 1 WHERE plan_dir = ? AND sealed = 0",
                (plan_dir,),
            )
            return cursor.rowcount if cursor.rowcount > 0 else 0

    # -- gating ------------------------------------------------------------

    def _withholding(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
    ) -> Withholding | None:
        """Why stored project evidence for ``plan_dir`` is not readable now.

        Order matters only for the message: every clause refuses, and they are
        checked before any row is touched so a withheld read never reads.
        """
        if (
            grant is None
            or grant.plan_dir != plan_dir
            or grant.access != overseer_policy_client.ACCESS_CONTEXT
            or not grant.request_digest
            or not grant.config_fingerprint
        ):
            return "unauthorized"
        epoch = self.current_epoch()
        if not epoch.observed:
            return "policy_unobserved"
        if grant.config_fingerprint != epoch.config_fingerprint:
            return "policy_superseded"
        record = self.enrollment(plan_dir)
        if record is not None and record.revoked:
            return "enrollment_revoked"
        return None

    def _require_context_grant(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
    ) -> None:
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            raise LedgerRefused(f"evidence for {plan_dir} may not be stored: {refusal}")

    def _require_grant(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        access: str,
    ) -> None:
        """Refuse a write whose grant does not answer exactly this question."""
        if (
            grant is None
            or grant.plan_dir != plan_dir
            or grant.access != access
            or not grant.request_digest
            or not grant.config_fingerprint
        ):
            raise LedgerRefused(
                f"no {access} grant for {plan_dir}; the write was not attempted"
            )
        epoch = self.current_epoch()
        if not epoch.observed:
            raise LedgerRefused(
                f"no configuration is on record, so a {access} grant for "
                f"{plan_dir} cannot be checked against the policy in force"
            )
        if grant.config_fingerprint != epoch.config_fingerprint:
            raise LedgerRefused(
                f"the {access} grant for {plan_dir} was issued under a "
                "configuration that is no longer in force; ask again"
            )
        record = self.enrollment(plan_dir)
        if record is not None and record.revoked:
            raise LedgerRefused(f"{plan_dir} is revoked; the write was not attempted")

    def _evidence_count(self, *, plan_dir: str, sealed: bool | None) -> int:
        clause = "" if sealed is None else " AND sealed = ?"
        parameters: tuple[object, ...] = (
            (plan_dir,) if sealed is None else (plan_dir, int(sealed))
        )
        row = self._connection.execute(
            f"SELECT COUNT(*) AS total FROM evidence WHERE plan_dir = ?{clause}",
            parameters,
        ).fetchone()
        return int(row["total"]) if row is not None else 0

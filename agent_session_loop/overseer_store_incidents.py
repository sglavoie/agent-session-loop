"""The ledger's incidents, the evidence they rest on, and recorded decisions.

:class:`IncidentMixin` is where stored project content lives, and so where the
policy gate matters: evidence is written only under a ``context`` grant, read
only while the policy it was gathered under still applies, and released from
its seal only by a fresh grant.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import overseer_policy_client
from .overseer_store_core import LedgerCore
from .overseer_store_models import (
    INCIDENT_STATUSES,
    DecisionRecord,
    EvidenceInput,
    EvidenceView,
    IncidentRecord,
    IncidentStatus,
    LedgerError,
    Release,
)
from .overseer_store_rows import (
    DECISION_COLUMNS,
    EVIDENCE_COLUMNS,
    INCIDENT_COLUMNS,
    decision_from_row,
    evidence_from_row,
    incident_from_row,
)


class IncidentMixin(LedgerCore):
    """Incidents, evidence, and recorded authorization decisions."""

    # -- incidents and evidence -------------------------------------------

    def open_incident(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        run_id: str,
        kind: str,
        summary: str,
        evidence: Sequence[EvidenceInput] = (),
    ) -> IncidentRecord:
        """Record one operational blocker together with the evidence for it.

        The incident row and every evidence row land in one transaction, so a
        failure partway through leaves no incident at all rather than an
        incident whose evidence is missing the row that would have contradicted
        it.

        A grant is required whenever evidence is attached, because evidence is
        project content read under authorization. An incident with no evidence
        needs none: "this run is paused" is ASL's own run state, which the
        metadata-only discovery boundary already establishes before any
        authorization is asked for.
        """
        moment = self._now()
        with self.transaction() as connection:
            # Inside the write lock: a policy change landing between the check
            # and the insert would stamp evidence read under the old grant with
            # the new epoch, unsealed.
            if evidence:
                self._require_context_grant(grant=grant, plan_dir=plan_dir)
            epoch = self.current_epoch()
            cursor = connection.execute(
                """
                INSERT INTO incident (
                    plan_dir, run_id, kind, status, summary, epoch,
                    config_fingerprint, opened_at, settled_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, NULL)
                """,
                (
                    plan_dir,
                    run_id,
                    kind,
                    summary,
                    epoch.epoch,
                    epoch.config_fingerprint,
                    moment,
                ),
            )
            incident_id = int(cursor.lastrowid or 0)
            for item in evidence:
                connection.execute(
                    """
                    INSERT INTO evidence (
                        incident_id, plan_dir, alias, kind, locator, digest,
                        epoch, config_fingerprint, sealed, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        incident_id,
                        plan_dir,
                        item.alias,
                        item.kind,
                        item.locator,
                        item.digest,
                        epoch.epoch,
                        epoch.config_fingerprint,
                        moment,
                    ),
                )
        recorded = self.incident(incident_id)
        if recorded is None:
            raise LedgerError(
                f"incident {incident_id} vanished immediately after it was written"
            )
        return recorded

    def settle_incident(
        self, *, incident_id: int, status: IncidentStatus
    ) -> IncidentRecord:
        """Move an incident out of ``open``."""
        if status not in INCIDENT_STATUSES or status == "open":
            raise LedgerError(
                f"{status!r} is not a settled incident status; use one of "
                f"{', '.join(s for s in INCIDENT_STATUSES if s != 'open')}"
            )
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE incident SET status = ?, settled_at = ? WHERE incident_id = ?",
                (status, moment, incident_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"no incident {incident_id} to settle")
        recorded = self.incident(incident_id)
        if recorded is None:
            raise LedgerError(f"incident {incident_id} vanished while being settled")
        return recorded

    def incident(self, incident_id: int) -> IncidentRecord | None:
        """One incident by identifier."""
        row = self._connection.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incident WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return None if row is None else incident_from_row(row)

    def incidents(
        self,
        *,
        plan_dir: str | None = None,
        status: IncidentStatus | None = None,
    ) -> tuple[IncidentRecord, ...]:
        """Every incident matching the filters, oldest first."""
        clauses: list[str] = []
        parameters: list[object] = []
        if plan_dir is not None:
            clauses.append("plan_dir = ?")
            parameters.append(plan_dir)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incident{where} ORDER BY incident_id",
            tuple(parameters),
        ).fetchall()
        return tuple(incident_from_row(row) for row in rows)

    def read_evidence(
        self, *, plan_dir: str, grant: overseer_policy_client.Grant | None
    ) -> EvidenceView:
        """Hand back this plan's evidence, if the current policy still permits it.

        Four gates, all of which withhold rather than raise, and all of which
        are checked before a row is read: the grant has to be for this plan and
        this access category, the ledger has to know which policy is in force,
        the grant has to have been issued under that same policy, and the plan
        must not be revoked. Sealed rows are then excluded and counted, so the
        caller is told the view is partial rather than left to infer it.
        """
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            return EvidenceView(
                plan_dir=plan_dir,
                records=(),
                withheld=self._evidence_count(plan_dir=plan_dir, sealed=None),
                refusal=refusal,
            )
        rows = self._connection.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence "
            "WHERE plan_dir = ? AND sealed = 0 ORDER BY evidence_id",
            (plan_dir,),
        ).fetchall()
        return EvidenceView(
            plan_dir=plan_dir,
            records=tuple(evidence_from_row(row) for row in rows),
            withheld=self._evidence_count(plan_dir=plan_dir, sealed=True),
            refusal=None,
        )

    def release_evidence(
        self, *, plan_dir: str, grant: overseer_policy_client.Grant | None
    ) -> Release:
        """Re-authorize sealed evidence against the policy now in force.

        This is the only way a sealed row becomes readable again, and it costs a
        fresh authorization: the grant must be for this plan, for ``context``,
        and carry the fingerprint of the configuration currently in force. A
        plan the operator has since excluded produces no such grant, so its
        evidence stays sealed permanently -- which is the requirement that
        stored evidence not survive an exclusion, expressed as an operation that
        cannot succeed rather than a rule somebody has to remember.
        """
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            return Release(plan_dir=plan_dir, released=0, refusal=refusal)
        epoch = self.current_epoch()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE evidence
                SET sealed = 0, epoch = ?, config_fingerprint = ?
                WHERE plan_dir = ? AND sealed = 1
                """,
                (epoch.epoch, epoch.config_fingerprint, plan_dir),
            )
            released = cursor.rowcount if cursor.rowcount > 0 else 0
        return Release(plan_dir=plan_dir, released=released, refusal=None)

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self, decision: overseer_policy_client.Decision
    ) -> DecisionRecord:
        """Record one authorization answer, allowed or not, available or not.

        Every answer is recorded, including the failures, because "we could not
        ask" is the fact an operator needs when an overseer stops acting and the
        configuration looks fine.
        """
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO decision (
                    plan_dir, access, allowed, available, failure, reason,
                    summary, config_fingerprint, request_digest, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.plan_dir,
                    decision.access,
                    int(decision.allowed),
                    int(decision.available),
                    decision.failure,
                    decision.reason,
                    decision.summary,
                    decision.config_fingerprint,
                    decision.request_digest,
                    moment,
                ),
            )
            decision_id = int(cursor.lastrowid or 0)
        recorded = self.decision(decision_id)
        if recorded is None:
            raise LedgerError(
                f"decision {decision_id} vanished immediately after it was written"
            )
        return recorded

    def decision(self, decision_id: int) -> DecisionRecord | None:
        """One recorded decision by identifier."""
        row = self._connection.execute(
            f"SELECT {DECISION_COLUMNS} FROM decision WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return None if row is None else decision_from_row(row)

    def decisions(self, *, plan_dir: str | None = None) -> tuple[DecisionRecord, ...]:
        """Recorded decisions, oldest first."""
        where = " WHERE plan_dir = ?" if plan_dir is not None else ""
        parameters = (plan_dir,) if plan_dir is not None else ()
        rows = self._connection.execute(
            f"SELECT {DECISION_COLUMNS} FROM decision{where} ORDER BY decision_id",
            parameters,
        ).fetchall()
        return tuple(decision_from_row(row) for row in rows)

"""The ledger's dispatched actions and the receipts that settle them.

:class:`ActionMixin` enforces the attempted-is-not-executed rule: an action is
written ``attempted`` before it is dispatched and reaches ``executed`` only
through :meth:`ActionMixin.confirm_action`, which writes the receipt in the
same transaction as the status change.
"""

from __future__ import annotations

from . import overseer_policy_client
from .overseer_store_core import LedgerCore
from .overseer_store_models import (
    ActionRecord,
    ActionStatus,
    LedgerError,
    LedgerRefused,
    ReceiptRecord,
)
from .overseer_store_rows import ACTION_COLUMNS, action_from_row


class ActionMixin(LedgerCore):
    """Actions, their receipts, and the counts retries are judged by."""

    # -- actions and receipts ---------------------------------------------

    def attempt_action(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        operation: str,
        target: str,
        incident_id: int | None = None,
        progress_token: str = "",
    ) -> ActionRecord:
        """Record a dispatch *before* it happens, and return it as ``attempted``.

        Written first on purpose. A record written after a successful dispatch
        cannot exist for a dispatch that crashed halfway, which is precisely the
        case the ledger is for: the durable trace of an in-flight action has to
        outlive the process that started it.

        The grant must be an ``action`` grant for this plan, issued under the
        configuration currently in force. A grant older than the last policy
        change is refused rather than honored: the recheck the requirements ask
        for before dispatch is meaningless if a decision made under a
        superseded configuration still opens the door.

        ``progress_token`` is stored, never interpreted. The ledger's only
        stake in it is that two attempts made in the same situation carry the
        same value and two made in different ones do not, which is what
        :meth:`recovery_attempts` counts by; deciding what "the same situation"
        means is the dispatcher's job, not this table's.
        """
        moment = self._now()
        with self.transaction() as connection:
            # Inside the write lock, so a policy change cannot land between the
            # grant check and the epoch the attempt is stamped with.
            self._require_grant(
                grant=grant,
                plan_dir=plan_dir,
                access=overseer_policy_client.ACCESS_ACTION,
            )
            if grant is None:
                raise LedgerRefused(f"no action grant for {plan_dir}")
            epoch = self.current_epoch()
            cursor = connection.execute(
                """
                INSERT INTO action (
                    plan_dir, incident_id, operation, target, status, epoch,
                    config_fingerprint, request_digest, progress_token,
                    attempted_at, settled_at
                ) VALUES (?, ?, ?, ?, 'attempted', ?, ?, ?, ?, ?, NULL)
                """,
                (
                    plan_dir,
                    incident_id,
                    operation,
                    target,
                    epoch.epoch,
                    epoch.config_fingerprint,
                    grant.request_digest,
                    progress_token,
                    moment,
                ),
            )
            action_id = int(cursor.lastrowid or 0)
        recorded = self.action(action_id)
        if recorded is None:
            raise LedgerError(
                f"action {action_id} vanished immediately after it was written"
            )
        return recorded

    def confirm_action(
        self,
        *,
        action_id: int,
        outcome: str,
        detail: str = "",
        locator: str = "",
        status: ActionStatus = "executed",
    ) -> ActionRecord:
        """Settle an attempted dispatch with the receipt that proves its fate.

        The receipt and the status change are one transaction, so no state
        exists in which an action reads ``executed`` with nothing backing it.
        Only an ``attempted`` action can be settled: a second confirmation is
        refused rather than silently overwriting the first receipt, because two
        different answers about one dispatch means one of them is wrong and the
        ledger has no way to tell which.
        """
        if status not in ("executed", "failed", "abandoned"):
            raise LedgerError(
                f"{status!r} is not a settled action status; an attempted "
                "dispatch settles as executed, failed, or abandoned"
            )
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE action SET status = ?, settled_at = ?
                WHERE action_id = ? AND status = 'attempted'
                """,
                (status, moment, action_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(
                    f"no attempted action {action_id} to confirm; an action "
                    "settles exactly once"
                )
            connection.execute(
                """
                INSERT INTO receipt (action_id, outcome, detail, locator, confirmed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (action_id, outcome, detail, locator, moment),
            )
        recorded = self.action(action_id)
        if recorded is None:
            raise LedgerError(f"action {action_id} vanished while being confirmed")
        return recorded

    def action(self, action_id: int) -> ActionRecord | None:
        """One action and its receipt, if it has one."""
        row = self._connection.execute(
            f"SELECT {ACTION_COLUMNS} FROM action WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return action_from_row(row, self._receipt(action_id))

    def actions(
        self,
        *,
        plan_dir: str | None = None,
        status: ActionStatus | None = None,
    ) -> tuple[ActionRecord, ...]:
        """Every action matching the filters, oldest first."""
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
            f"SELECT {ACTION_COLUMNS} FROM action{where} ORDER BY action_id",
            tuple(parameters),
        ).fetchall()
        return tuple(
            action_from_row(row, self._receipt(int(row["action_id"]))) for row in rows
        )

    def recovery_attempts(
        self, *, plan_dir: str, progress_token: str
    ) -> tuple[ActionRecord, ...]:
        """Dispatches on one plan that ended badly in one situation.

        Keyed by the situation and *not* by the incident. Keying it by incident
        would make opening a second incident for the same trouble a way to buy
        two more attempts, and opening incidents is something the overseer does
        for itself -- so the evasion would need no bad actor, just a blocker
        observed twice.

        ``failed`` and ``abandoned`` both count. An abandoned attempt is one
        the overseer stopped waiting on, which cost the same recovery a failed
        one did; excluding it would let a caller buy attempts by abandoning
        them. ``attempted`` does not count, because an unsettled dispatch may
        yet turn out to have worked -- the dispatcher refuses to retry over one
        of those anyway, so it is never the row standing between a caller and
        an escalation.

        This is a read, not a policy: how many is too many belongs to
        :data:`agent_session_loop.overseer_dispatch.RECOVERY_LIMIT`.
        """
        rows = self._connection.execute(
            f"""
            SELECT {ACTION_COLUMNS} FROM action
            WHERE plan_dir = ? AND progress_token = ?
              AND status IN ('failed', 'abandoned')
            ORDER BY action_id
            """,
            (plan_dir, progress_token),
        ).fetchall()
        return tuple(
            action_from_row(row, self._receipt(int(row["action_id"]))) for row in rows
        )

    def executed_action(
        self,
        *,
        plan_dir: str,
        operation: str,
        target: str,
        progress_token: str,
    ) -> ActionRecord | None:
        """The dispatch that already did this exact thing in this situation.

        The situation is part of the identity on purpose. Without it, a second
        resumption of a run that paused again would read as a duplicate of the
        first, and the overseer would refuse to do the one thing the second
        pause is asking for; with it, "again, in the same circumstances,
        against the same target" is the only shape that counts as a repeat.

        The incident is *not* part of it, for the reason
        :meth:`recovery_attempts` gives: a repeat filed under a new incident is
        still a repeat.
        """
        row = self._connection.execute(
            f"""
            SELECT {ACTION_COLUMNS} FROM action
            WHERE plan_dir = ? AND operation = ? AND target = ?
              AND progress_token = ? AND status = 'executed'
            ORDER BY action_id LIMIT 1
            """,
            (plan_dir, operation, target, progress_token),
        ).fetchone()
        if row is None:
            return None
        return action_from_row(row, self._receipt(int(row["action_id"])))

    def in_doubt_actions(
        self, *, plan_dir: str | None = None
    ) -> tuple[ActionRecord, ...]:
        """Dispatches that were attempted and never confirmed either way.

        What a restart finds. The shared requirements say an already-dispatched
        action is not automatically undone, so these are reported for a human or
        a fresh observation to settle -- never rolled forward into ``executed``
        and never rolled back.
        """
        return self.actions(plan_dir=plan_dir, status="attempted")

    def _receipt(self, action_id: int) -> ReceiptRecord | None:
        row = self._connection.execute(
            """
            SELECT receipt_id, action_id, outcome, detail, locator, confirmed_at
            FROM receipt WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return ReceiptRecord(
            receipt_id=int(row["receipt_id"]),
            action_id=int(row["action_id"]),
            outcome=str(row["outcome"]),
            detail=str(row["detail"]),
            locator=str(row["locator"]),
            confirmed_at=str(row["confirmed_at"]),
        )

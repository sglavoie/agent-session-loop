"""The ledger's multi-repository checkpoint groups.

:class:`CheckpointGroupMixin` records what a checkpoint spanning several
repositories has already committed, so a restart finishes the remainder
instead of repeating or forgetting it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from . import overseer_policy_client
from .overseer_store_core import LedgerCore
from .overseer_store_models import (
    CheckpointGroupRecord,
    LedgerError,
    LedgerRefused,
)
from .overseer_store_rows import CHECKPOINT_GROUP_COLUMNS, checkpoint_group_from_row


class CheckpointGroupMixin(LedgerCore):
    """Checkpoint groups still owed across a plan's repositories."""

    # -- checkpoint groups -------------------------------------------------

    def open_checkpoint_group(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        group_key: str,
        target: str,
        record: Mapping[str, Any],
        incident_id: int | None = None,
        phase: str = "prepared",
    ) -> CheckpointGroupRecord:
        """Open, or re-open, the multi-repository checkpoint owed under ``group_key``.

        Grant-gated like :meth:`attempt_action`, and for the same reason: opening
        a group is the step that decides several repositories are about to be
        written to, so it is a question AgentMux must have answered under the
        configuration currently in force.

        Re-opening is deliberately *not* a reset. A retry after a partial failure
        computes the same key, and this method keeps the row's ``opened_at``
        while replacing the obligation with the one the caller just established
        --- a caller that wanted to forget the commits already made would have to
        omit them from ``record`` itself, which
        :func:`agent_session_loop.overseer_checkpoint_groups.prepare_group` will
        not do.
        """
        self._require_grant(
            grant=grant,
            plan_dir=plan_dir,
            access=overseer_policy_client.ACCESS_ACTION,
        )
        if grant is None:
            raise LedgerRefused(f"no action grant for {plan_dir}")
        if not group_key:
            raise LedgerError("a checkpoint group must be keyed")
        moment = self._now()
        epoch = self.current_epoch()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO checkpoint_group (
                    group_key, plan_dir, incident_id, target, status, phase,
                    epoch, config_fingerprint, request_digest, record,
                    opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_key) DO UPDATE SET
                    status = excluded.status,
                    phase = excluded.phase,
                    epoch = excluded.epoch,
                    config_fingerprint = excluded.config_fingerprint,
                    request_digest = excluded.request_digest,
                    record = excluded.record,
                    updated_at = excluded.updated_at
                """,
                (
                    group_key,
                    plan_dir,
                    incident_id,
                    target,
                    str(record.get("status", "pending")),
                    phase,
                    epoch.epoch,
                    epoch.config_fingerprint,
                    grant.request_digest,
                    json.dumps(dict(record), sort_keys=True),
                    moment,
                    moment,
                ),
            )
        stored = self.checkpoint_group(group_key)
        if stored is None:
            raise LedgerError(
                f"checkpoint group {group_key} vanished immediately after it was opened"
            )
        return stored

    def save_checkpoint_group(
        self,
        *,
        group_key: str,
        record: Mapping[str, Any],
        phase: str,
    ) -> CheckpointGroupRecord:
        """Rewrite an open group at a transaction boundary.

        Not grant-gated, and the asymmetry with :meth:`open_checkpoint_group` is
        the same one :meth:`confirm_action` has with :meth:`attempt_action`: what
        a caller needs permission for is *making* the commits, not recording that
        they were made. Refusing this write when authorization moved mid-group
        would delete the only durable evidence of the repositories that already
        succeeded, which is precisely what a revocation between commits must not
        cost.
        """
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE checkpoint_group
                SET status = ?, phase = ?, record = ?, updated_at = ?
                WHERE group_key = ?
                """,
                (
                    str(record.get("status", "pending")),
                    phase,
                    json.dumps(dict(record), sort_keys=True),
                    moment,
                    group_key,
                ),
            )
            if cursor.rowcount != 1:
                raise LedgerError(
                    f"no open checkpoint group {group_key} to save; a group is "
                    "opened before it is coordinated"
                )
        stored = self.checkpoint_group(group_key)
        if stored is None:
            raise LedgerError(
                f"checkpoint group {group_key} vanished while being saved"
            )
        return stored

    def checkpoint_group(self, group_key: str) -> CheckpointGroupRecord | None:
        """The group owed under one derived key, if the ledger holds one."""
        row = self._connection.execute(
            f"SELECT {CHECKPOINT_GROUP_COLUMNS} FROM checkpoint_group "
            "WHERE group_key = ?",
            (group_key,),
        ).fetchone()
        if row is None:
            return None
        return checkpoint_group_from_row(row)

    def checkpoint_groups(
        self,
        *,
        plan_dir: str | None = None,
        status: str | None = None,
    ) -> tuple[CheckpointGroupRecord, ...]:
        """Every stored group, narrowed to one plan or one status."""
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
            f"SELECT {CHECKPOINT_GROUP_COLUMNS} FROM checkpoint_group{where} "
            "ORDER BY opened_at, group_key",
            tuple(parameters),
        ).fetchall()
        return tuple(checkpoint_group_from_row(row) for row in rows)

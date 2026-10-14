"""Focused tests for the overseer's durable ledger.

Five properties, and each one is proved against a real SQLite file on disk
rather than against an in-memory double, because four of the five are about
what survives the process.

*Restart.* Everything written is read back by a second :func:`open_ledger` over
the same path -- and an attempted dispatch is still merely attempted, which is
the one fact a restart most wants to round up.

*Migration.* A fresh database lands on the current version, a second open
replays nothing, an appended migration runs exactly once over existing rows, a
migration that fails leaves the version and the schema where they were, and a
database from a newer build is refused rather than opened.

*Partial write.* A fault injected midway through a multi-row write must leave
none of it behind. The seam is a proxy connection that fails a chosen statement,
in the spirit of ``tests.test_asl_commit_groups.DurableLedgerTests``.

*Unavailable policy.* A decision that could not be obtained never moves an
enrollment bit and never yields evidence, and the stored form of a *successful*
decision is asserted to have no method that could turn it back into permission.

*Revocation.* After a withdrawal, stored evidence for that plan is withheld and
stays withheld -- through a re-read, through a release attempt, and through a
restart.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_policy_client,
    overseer_store_actions,
    overseer_store_core,
    overseer_store_groups,
    overseer_store_incidents,
    overseer_store_ledger,
    overseer_store_models,
    overseer_store_rows,
    overseer_store_schema,
    overseer_store_watch,
    state,
    state_plan_refs,
)
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
OTHER_PLAN = "/state/agent-session-loop/plan-xyz"
FINGERPRINT = "fp-first"
NEXT_FINGERPRINT = "fp-second"


def _decision(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    allowed: bool = True,
    available: bool = True,
    fingerprint: str = FINGERPRINT,
    digest: str = "rd-1",
    failure: Any = None,
) -> overseer_policy_client.Decision:
    """A decision in the shape the policy client hands over."""
    return overseer_policy_client.Decision(
        plan_dir=plan_dir,
        access=access,
        repositories=(
            overseer_policy_client.Repository(
                alias="scripts", path="/home/dev/scripts"
            ),
        ),
        allowed=allowed and available,
        available=available,
        reason="opted_in" if allowed else "excluded",
        failure=failure,
        summary="1 repository",
        config_fingerprint=fingerprint if available else "",
        request_digest=digest if available else "",
        contract_version=1 if available else None,
        exit_status=0 if allowed and available else 1,
        outcomes=(),
    )


def _grant(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    fingerprint: str = FINGERPRINT,
    digest: str = "rd-1",
) -> overseer_policy_client.Grant:
    granted = _decision(
        plan_dir=plan_dir, access=access, fingerprint=fingerprint, digest=digest
    ).grant()
    assert granted is not None
    return granted


def _evidence(count: int = 1) -> tuple[overseer_store_models.EvidenceInput, ...]:
    return tuple(
        overseer_store_models.EvidenceInput(
            alias="scripts",
            kind="transcript",
            locator=f"scripts:sessions/{index}.jsonl",
            digest=f"sha256-{index}",
        )
        for index in range(count)
    )


def _columns(ledger: overseer_store_ledger.Ledger, table: str) -> set[str]:
    """One table's column names, read from the file by a separate connection."""
    raw = sqlite3.connect(str(ledger.path))
    try:
        return {str(row[1]) for row in raw.execute(f"PRAGMA table_info({table})")}
    finally:
        raw.close()


class _FlakyConnection:
    """A connection proxy that fails one chosen statement.

    Only ``execute`` and ``close`` are used by :class:`~overseer_store_ledger.Ledger`,
    so a proxy is enough, and it is a proxy rather than a patched method because
    :class:`sqlite3.Connection` is a C type that will not take one.
    """

    def __init__(self, connection: sqlite3.Connection, *, fail_on: str) -> None:
        self._connection = connection
        self._fail_on = fail_on
        self.failed = False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if not self.failed and self._fail_on in " ".join(sql.split()):
            self.failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        self._connection.close()


class _LedgerCase(unittest.TestCase):
    """A temporary state root with a ledger open on it."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-store-"))
        self.addCleanup(self._close)
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    def reopen(self) -> overseer_store_ledger.Ledger:
        """Close and open again over the same path: the restart under test."""
        self.ledger.close()
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        return self.ledger

    def enrolled(self, *, plan_dir: str = PLAN) -> overseer_policy_client.Grant:
        """Put a plan into the state every gated operation needs."""
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.ledger.record_enrollment(_decision(plan_dir=plan_dir))
        return _grant(plan_dir=plan_dir)


class LedgerLocationTests(_LedgerCase):
    """Where the database lives, and what must not notice it."""

    def test_module_does_not_import_the_runner(self) -> None:
        for module in (
            overseer_store_actions,
            overseer_store_core,
            overseer_store_groups,
            overseer_store_incidents,
            overseer_store_ledger,
            overseer_store_models,
            overseer_store_rows,
            overseer_store_schema,
            overseer_store_watch,
        ):
            assert_no_runner_import(self, module)

    def test_the_ledger_is_a_file_beside_the_plan_state_directories(self) -> None:
        """A directory here would be enumerated as a plan by the fleet
        inventory and reported as a run nobody can read."""
        self.assertEqual(
            overseer_store_ledger.ledger_path(self.root),
            state.state_root(self.root) / overseer_store_models.LEDGER_FILE_NAME,
        )
        self.assertTrue(self.ledger.path.is_file())
        self.assertEqual(
            list(state_plan_refs.iter_plan_state_dirs(state.state_root(self.root))), []
        )


class MigrationTests(_LedgerCase):
    """The schema moves forward, one step at a time, or not at all."""

    def test_a_fresh_database_lands_on_the_current_version(self) -> None:
        self.assertEqual(
            self.ledger.schema_version, overseer_store_schema.SCHEMA_VERSION
        )
        self.assertEqual(
            overseer_store_schema.SCHEMA_VERSION, len(overseer_store_schema.MIGRATIONS)
        )

    def test_reopening_replays_nothing_and_keeps_every_row(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="question_pending",
            summary="waiting on a reply",
            evidence=_evidence(2),
        )
        reopened = self.reopen()
        self.assertEqual(reopened.schema_version, overseer_store_schema.SCHEMA_VERSION)
        self.assertEqual(len(reopened.incidents(plan_dir=PLAN)), 1)
        view = reopened.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(len(view.records), 2)

    def test_an_appended_migration_runs_once_over_existing_rows(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="run_paused",
            summary="paused",
        )
        self.ledger.close()

        def probe(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE incident ADD COLUMN probe TEXT")

        upgraded = overseer_store_schema.SCHEMA_VERSION + 1
        with (
            mock.patch.object(
                overseer_store_schema,
                "MIGRATIONS",
                overseer_store_schema.MIGRATIONS + (probe,),
            ),
            mock.patch.object(overseer_store_schema, "SCHEMA_VERSION", upgraded),
        ):
            self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
            self.assertEqual(self.ledger.schema_version, upgraded)
            self.assertEqual(len(self.ledger.incidents(plan_dir=PLAN)), 1)
            self.ledger.close()
            # A second open at the same version must not replay the step, which
            # would fail on the duplicate column.
            self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
            self.assertEqual(self.ledger.schema_version, upgraded)

    def test_a_failed_migration_leaves_the_version_and_schema_untouched(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="run_paused",
            summary="paused",
        )
        self.ledger.close()

        def half_applied(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE incident ADD COLUMN probe TEXT")
            raise sqlite3.OperationalError("migration exploded")

        with (
            mock.patch.object(
                overseer_store_schema,
                "MIGRATIONS",
                overseer_store_schema.MIGRATIONS + (half_applied,),
            ),
            mock.patch.object(
                overseer_store_schema,
                "SCHEMA_VERSION",
                overseer_store_schema.SCHEMA_VERSION + 1,
            ),
            self.assertRaises(overseer_store_models.LedgerError),
        ):
            overseer_store_ledger.open_ledger(state_dir=self.root)

        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.assertEqual(
            self.ledger.schema_version, overseer_store_schema.SCHEMA_VERSION
        )
        self.assertEqual(len(self.ledger.incidents(plan_dir=PLAN)), 1)
        self.assertNotIn("probe", _columns(self.ledger, "incident"))

    def test_a_database_from_a_newer_build_is_refused(self) -> None:
        """Opening it anyway would mean writing this version's rows into a
        shape only the newer build knows the meaning of."""
        path = self.ledger.path
        self.ledger.close()
        raw = sqlite3.connect(str(path), isolation_level=None)
        raw.execute(f"PRAGMA user_version = {overseer_store_schema.SCHEMA_VERSION + 3}")
        raw.close()
        with self.assertRaises(overseer_store_models.LedgerError) as caught:
            overseer_store_ledger.open_ledger(state_dir=self.root)
        self.assertIn("newer", str(caught.exception))


class RestartTests(_LedgerCase):
    """What the second process finds."""

    def test_an_attempted_dispatch_is_still_attempted_after_a_restart(self) -> None:
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.assertEqual(action.status, "attempted")
        self.assertTrue(action.in_doubt)
        self.assertFalse(action.confirmed)
        self.assertIsNone(action.receipt)

        reopened = self.reopen()
        in_doubt = reopened.in_doubt_actions()
        self.assertEqual([item.action_id for item in in_doubt], [action.action_id])
        self.assertTrue(in_doubt[0].in_doubt)
        self.assertFalse(in_doubt[0].confirmed)
        self.assertEqual(reopened.actions(status="executed"), ())
        self.assertEqual(in_doubt[0].request_digest, "rd-act")

    def test_a_confirmed_dispatch_carries_its_receipt_across_a_restart(self) -> None:
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.ledger.confirm_action(
            action_id=action.action_id, outcome="delivered", detail="consumed"
        )
        reopened = self.reopen()
        settled = reopened.action(action.action_id)
        assert settled is not None
        self.assertEqual(settled.status, "executed")
        self.assertTrue(settled.confirmed)
        assert settled.receipt is not None
        self.assertEqual(settled.receipt.outcome, "delivered")
        self.assertEqual(reopened.in_doubt_actions(), ())

    def test_an_action_settles_exactly_once(self) -> None:
        """Two answers about one dispatch means one of them is wrong, and the
        ledger has no way to tell which."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.ledger.confirm_action(action_id=action.action_id, outcome="delivered")
        with self.assertRaises(overseer_store_models.LedgerError):
            self.ledger.confirm_action(action_id=action.action_id, outcome="failed")
        settled = self.ledger.action(action.action_id)
        assert settled is not None
        assert settled.receipt is not None
        self.assertEqual(settled.receipt.outcome, "delivered")

    def test_cursors_and_enrollment_survive_and_never_move_backwards(self) -> None:
        self.enrolled()
        self.ledger.advance_cursor(
            plan_dir=PLAN, stream="events", position=120, marker="e-120"
        )
        self.ledger.advance_cursor(
            plan_dir=PLAN, stream="events", position=40, marker="e-40"
        )
        reopened = self.reopen()
        cursor = reopened.cursor(plan_dir=PLAN, stream="events")
        assert cursor is not None
        self.assertEqual(cursor.position, 120)
        self.assertEqual(cursor.marker, "e-120")
        record = reopened.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.enrolled)
        self.assertFalse(record.revoked)


class PartialWriteTests(_LedgerCase):
    """A write that fails partway leaves nothing behind."""

    def _make_flaky(self, *, fail_on: str) -> _FlakyConnection:
        real = self.ledger._connection
        flaky = _FlakyConnection(real, fail_on=fail_on)
        self.ledger._connection = flaky
        self.addCleanup(setattr, self.ledger, "_connection", real)
        return flaky

    def test_an_incident_whose_evidence_fails_is_not_written_at_all(self) -> None:
        grant = self.enrolled()
        flaky = self._make_flaky(fail_on="INSERT INTO evidence")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.open_incident(
                grant=grant,
                plan_dir=PLAN,
                run_id="run-1",
                kind="verify_failed",
                summary="two checks failed",
                evidence=_evidence(2),
            )
        self.assertTrue(flaky.failed)
        self.ledger._connection = flaky._connection

        self.assertEqual(self.ledger.incidents(plan_dir=PLAN), ())
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 0)

        # The rollback also has to leave the connection usable, or the next
        # write would fail on an transaction nobody closed.
        recorded = self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="two checks failed",
            evidence=_evidence(2),
        )
        self.assertEqual(recorded.status, "open")
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

    def test_a_receipt_that_fails_leaves_the_action_attempted(self) -> None:
        """The status change and the receipt are one transaction, so there is
        no state in which an action reads executed with nothing backing it."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        flaky = self._make_flaky(fail_on="INSERT INTO receipt")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.confirm_action(action_id=action.action_id, outcome="delivered")
        self.ledger._connection = flaky._connection

        still = self.ledger.action(action.action_id)
        assert still is not None
        self.assertEqual(still.status, "attempted")
        self.assertTrue(still.in_doubt)
        self.assertIsNone(still.receipt)
        self.assertEqual(
            [item.action_id for item in self.reopen().in_doubt_actions()],
            [action.action_id],
        )

    def test_a_failed_policy_bump_leaves_the_epoch_and_the_seals_together(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        flaky = self._make_flaky(fail_on="INSERT INTO policy_epoch")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.ledger._connection = flaky._connection

        epoch = self.ledger.current_epoch()
        self.assertEqual(epoch.config_fingerprint, FINGERPRINT)
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(len(view.records), 1)
        self.assertEqual(view.withheld, 0)


class UnavailablePolicyTests(_LedgerCase):
    """A decision that could not be obtained confers nothing."""

    def test_a_stored_decision_cannot_become_permission(self) -> None:
        """The substitution the shared requirements forbid is unrepresentable
        rather than merely discouraged: the stored form has no conversion."""
        self.enrolled()
        record = self.ledger.record_decision(_decision())
        self.assertTrue(record.allowed)
        self.assertFalse(hasattr(record, "grant"))
        self.assertFalse(hasattr(record, "authorization"))
        self.assertFalse(hasattr(record, "permits"))
        self.assertNotIsInstance(record, overseer_policy_client.Grant)

    def test_an_unavailable_answer_does_not_move_the_enrollment_bit(self) -> None:
        self.enrolled()
        before = self.ledger.enrollment(PLAN)
        assert before is not None
        self.assertTrue(before.enrolled)

        after = self.ledger.record_enrollment(
            _decision(available=False, failure="client_timeout")
        )
        self.assertTrue(after.enrolled)
        self.assertFalse(after.revoked)
        self.assertEqual(after.last_failure, "client_timeout")
        self.assertEqual(after.config_fingerprint, before.config_fingerprint)
        self.assertEqual(after.first_seen_at, before.first_seen_at)

    def test_an_unavailable_answer_never_enrolls_an_unknown_plan(self) -> None:
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        record = self.ledger.record_enrollment(
            _decision(plan_dir=OTHER_PLAN, available=False, failure="client_missing")
        )
        self.assertFalse(record.enrolled)
        self.assertFalse(record.revoked)
        self.assertEqual(record.last_failure, "client_missing")

    def test_a_failed_decision_is_recorded_as_the_fault_it_was(self) -> None:
        self.enrolled()
        record = self.ledger.record_decision(
            _decision(available=False, failure="client_timeout")
        )
        self.assertFalse(record.allowed)
        self.assertFalse(record.available)
        self.assertEqual(record.failure, "client_timeout")
        self.assertEqual(record.request_digest, "")
        self.assertEqual(record.config_fingerprint, "")

    def test_no_grant_means_no_evidence_and_no_dispatch(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=None)
        self.assertEqual(view.refusal, "unauthorized")
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 1)
        self.assertFalse(view.complete)
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=None, plan_dir=PLAN, operation="reply", target="q"
            )
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.open_incident(
                grant=None,
                plan_dir=PLAN,
                run_id="run-1",
                kind="verify_failed",
                summary="failed",
                evidence=_evidence(1),
            )

    def test_a_grant_for_another_plan_or_category_reads_nothing(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        for label, wrong in {
            "another plan": _grant(plan_dir=OTHER_PLAN),
            "another category": _grant(access=overseer_policy_client.ACCESS_ACTION),
        }.items():
            with self.subTest(label):
                view = self.ledger.read_evidence(plan_dir=PLAN, grant=wrong)
                self.assertEqual(view.refusal, "unauthorized")
                self.assertEqual(view.records, ())

    def test_evidence_is_unreadable_before_any_policy_is_observed(self) -> None:
        """Not knowing which configuration applies is never read as any
        configuration applying."""
        fresh = Path(tempfile.mkdtemp(prefix="asl-overseer-store-"))
        ledger = overseer_store_ledger.open_ledger(state_dir=fresh)
        self.addCleanup(ledger.close)
        self.assertFalse(ledger.current_epoch().observed)
        view = ledger.read_evidence(plan_dir=PLAN, grant=_grant())
        self.assertEqual(view.refusal, "policy_unobserved")
        with self.assertRaises(overseer_store_models.LedgerRefused):
            ledger.attempt_action(
                grant=_grant(access=overseer_policy_client.ACCESS_ACTION),
                plan_dir=PLAN,
                operation="reply",
                target="q",
            )


class PolicyChangeTests(_LedgerCase):
    """A configuration edit seals what was gathered under the old one."""

    def test_a_changed_fingerprint_seals_evidence_until_re_authorized(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(2),
        )
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

        transition = self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.assertTrue(transition.changed)
        self.assertEqual(transition.sealed_evidence, 2)
        self.assertEqual(transition.previous_fingerprint, FINGERPRINT)

        stale = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(stale.refusal, "policy_superseded")
        self.assertEqual(stale.records, ())
        self.assertEqual(stale.withheld, 2)

        fresh = _grant(fingerprint=NEXT_FINGERPRINT, digest="rd-2")
        held = self.ledger.read_evidence(plan_dir=PLAN, grant=fresh)
        self.assertIsNone(held.refusal)
        self.assertEqual(held.records, ())
        self.assertEqual(held.withheld, 2)
        self.assertFalse(held.complete)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=fresh)
        self.assertEqual(release.released, 2)
        released = self.ledger.read_evidence(plan_dir=PLAN, grant=fresh)
        self.assertEqual(len(released.records), 2)
        self.assertTrue(released.complete)

    def test_observing_the_same_fingerprint_seals_nothing(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        transition = self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.assertFalse(transition.changed)
        self.assertEqual(transition.sealed_evidence, 0)
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 1
        )

    def test_a_superseded_grant_cannot_dispatch(self) -> None:
        """The recheck before dispatch is meaningless if a decision made under
        a configuration that has since changed still opens the door."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=grant, plan_dir=PLAN, operation="reply", target="q"
            )
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())


class RevocationTests(_LedgerCase):
    """A withdrawn plan yields nothing, and keeps yielding nothing."""

    def _stocked(self) -> overseer_policy_client.Grant:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(2),
        )
        return grant

    def test_an_explicit_revocation_withholds_evidence_through_a_restart(self) -> None:
        grant = self._stocked()
        sealed = self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        self.assertEqual(sealed, 2)

        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.refusal, "enrollment_revoked")
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 2)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(release.refusal, "enrollment_revoked")
        self.assertEqual(release.released, 0)

        reopened = self.reopen()
        after = reopened.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(after.refusal, "enrollment_revoked")
        self.assertEqual(after.records, ())
        record = reopened.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.revoked)
        self.assertFalse(record.enrolled)

    def test_an_answered_refusal_revokes_and_withholds(self) -> None:
        """AgentMux saying no is the ordinary revocation path, and it has to
        reach stored evidence rather than only the next read of the source."""
        grant = self._stocked()
        self.ledger.record_enrollment(_decision(allowed=False, digest="rd-no"))
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.refusal, "enrollment_revoked")
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=_grant(access=overseer_policy_client.ACCESS_ACTION),
                plan_dir=PLAN,
                operation="reply",
                target="q",
            )

    def test_revocation_does_not_reach_another_plan(self) -> None:
        first = self._stocked()
        second = self.enrolled(plan_dir=OTHER_PLAN)
        self.ledger.open_incident(
            grant=second,
            plan_dir=OTHER_PLAN,
            run_id="run-2",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")

        self.assertEqual(
            self.ledger.read_evidence(plan_dir=PLAN, grant=first).refusal,
            "enrollment_revoked",
        )
        other = self.ledger.read_evidence(plan_dir=OTHER_PLAN, grant=second)
        self.assertIsNone(other.refusal)
        self.assertEqual(len(other.records), 1)

    def test_re_enrolment_restores_the_plan_but_not_its_sealed_evidence(self) -> None:
        """Coming back is one live allowance; reading what was gathered before
        the withdrawal is a second, explicit one."""
        grant = self._stocked()
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        self.ledger.record_enrollment(_decision(digest="rd-again"))

        record = self.ledger.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.enrolled)
        self.assertFalse(record.revoked)

        held = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertIsNone(held.refusal)
        self.assertEqual(held.records, ())
        self.assertEqual(held.withheld, 2)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(release.released, 2)
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

    def test_a_revoked_plan_keeps_its_operational_record(self) -> None:
        """Incidents and receipts say what the overseer did, not what the
        project contains, so revocation seals the evidence and not the log."""
        self._stocked()
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        incidents = self.ledger.incidents(plan_dir=PLAN)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].status, "open")
        settled = self.ledger.settle_incident(
            incident_id=incidents[0].incident_id, status="escalated"
        )
        self.assertEqual(settled.status, "escalated")
        self.assertIsNotNone(settled.settled_at)


if __name__ == "__main__":
    unittest.main()

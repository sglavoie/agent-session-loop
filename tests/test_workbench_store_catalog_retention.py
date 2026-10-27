"""Catalog uploads, the operation journal and retention."""

from __future__ import annotations

from typing import Any

from asl_workbench import (
    freshness,
    incidents,
    store_types,
)
from asl_workbench.operations import (
    METHOD_LAUNCH,
    METHOD_STOP_AFTER_CURRENT,
    OperationError,
    OperationTarget,
    admit,
)
from asl_workbench.service_mutations import _rehydrate
from tests.support_workbench_store import (
    CONNECTOR_ID,
    NOW,
    RETENTION,
    StoreCase,
    catalog_entry,
    history_entry,
    run_row,
)

ASL_TEST_PORTABLE = True


class CatalogTests(StoreCase):
    """Catalog storage is separate from run history, and pages honestly."""

    def test_a_catalog_page_is_stored_apart_from_run_history(self) -> None:
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        self.assertEqual(result.stored, 2)
        self.assertEqual(self.store.runs(now=NOW), [])
        plans = self.plans_by_key()
        self.assertEqual(set(plans), {"plan-a", "plan-b"})
        self.assertEqual(plans["plan-a"]["projects"], ["pi-monitoring"])
        self.assertEqual(plans["plan-a"]["counts"], {"sessions": 3})
        self.assertIs(plans["plan-a"]["exists"], True)
        self.assertIs(plans["plan-a"]["multi_repository"], False)
        self.assertEqual(plans["plan-a"]["observation_status"], "current")
        self.assertEqual(plans["plan-a"]["generation"], "gen-1")

    def test_a_complete_final_generation_retires_what_it_omits(self) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
        )
        self.assertEqual(result.absent, 1)
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})
        absent = self.plans_by_key(include_absent=True)["plan-b"]
        self.assertEqual(absent["observation_status"], "absent")

    def test_a_partial_generation_never_retires_unseen_work(self) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            complete=False,
        )
        self.assertEqual(result.absent, 0)
        plans = self.plans_by_key()
        self.assertEqual(set(plans), {"plan-a", "plan-b"})
        self.assertEqual(plans["plan-b"]["observation_status"], "unconfirmed")
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_PARTIAL)

    def test_a_complete_generation_still_arriving_retires_nothing_yet(self) -> None:
        """Retiring at page one would empty a catalog too large for one page."""
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        first = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            final=False,
            total=2,
        )
        self.assertEqual(first.absent, 0)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-b"})
        # Complete but unfinished is not complete yet, and must not be
        # reported as though absence already meant something.
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_UNKNOWN)
        second = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-c")],
            offset=1,
            total=2,
        )
        self.assertEqual(second.absent, 1)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-c"})
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_COMPLETE)

    def test_a_page_that_lost_a_plan_keeps_its_generation_partial(self) -> None:
        """A later whole, final page must not retire what an earlier page lost.

        The connector sends a page whose plan it could not publish as neither
        complete nor final; if the next page's own flags decided, that plan,
        absent from the generation only because it was lost, would be retired.
        """
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[
                catalog_entry("plan-a"),
                catalog_entry("plan-b"),
                catalog_entry("plan-c"),
            ],
        )
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            complete=False,
            final=False,
            total=3,
        )
        last = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-c")],
            offset=2,
            total=3,
        )
        self.assertEqual(last.absent, 0)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-b", "plan-c"})
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_PARTIAL)
        # A new generation starts whole again and may retire what it omits.
        fresh = self.send_catalog(
            observed_at=NOW - 5,
            generation="gen-3",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-c")],
        )
        self.assertEqual(fresh.absent, 1)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-c"})

    def test_a_replayed_page_of_the_same_generation_is_a_duplicate(self) -> None:
        entries = [catalog_entry("plan-a")]
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=entries,
            final=False,
            total=2,
        )
        replay = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=entries,
            final=False,
            total=2,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.stored, 0)
        self.assertEqual(len(self.store.catalog(now=NOW)), 1)

    def test_an_older_catalog_generation_is_refused(self) -> None:
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
        )
        with self.assertRaises(store_types.IngestRefused) as refusal:
            self.send_catalog(
                observed_at=NOW - 20,
                generation="gen-1",
                entries=[catalog_entry("plan-b")],
            )
        self.assertEqual(refusal.exception.code, store_types.REFUSED_STALE_OBSERVATION)
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})

    def test_a_page_that_repeats_a_plan_is_refused_without_partial_writes(
        self,
    ) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a")],
        )
        with self.assertRaises(store_types.IngestInvalid):
            self.send_catalog(
                observed_at=NOW - 10,
                generation="gen-2",
                entries=[catalog_entry("plan-b"), catalog_entry("plan-b")],
            )
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})

    def test_the_catalog_pages_filters_and_counts(self) -> None:
        entries = [
            catalog_entry("plan-a", urgency_rank=1, projects=["scripts"]),
            catalog_entry("plan-b", urgency_rank=2),
            catalog_entry("plan-c", urgency_rank=3),
        ]
        self.send_catalog(observed_at=NOW - 10, generation="gen-1", entries=entries)
        page = self.store.catalog(now=NOW, limit=2)
        self.assertEqual([row["plan_key"] for row in page], ["plan-a", "plan-b"])
        rest = self.store.catalog(now=NOW, limit=2, offset=2)
        self.assertEqual([row["plan_key"] for row in rest], ["plan-c"])
        self.assertEqual(
            [row["plan_key"] for row in self.store.catalog(now=NOW, project="scripts")],
            ["plan-a"],
        )
        self.assertEqual(
            [
                row["plan_key"]
                for row in self.store.catalog(now=NOW, search="title for plan-b")
            ],
            ["plan-b"],
        )
        counts = self.store.counts(now=NOW)
        self.assertEqual(counts.plans, 3)
        self.assertEqual(counts.absent_plans, 0)

    def test_project_and_search_filters_are_literal_rather_than_patterns(
        self,
    ) -> None:
        """``_`` and ``%`` are text here, and project membership is exact."""
        entries = [
            catalog_entry("plan-a", projects=["my_app"], search_text="ship my_app"),
            catalog_entry("plan-b", projects=["my-app"], search_text="ship my-app"),
            catalog_entry("plan-c", projects=["My_App"], search_text="ship 100%"),
            catalog_entry("plan-d", projects=["x"], search_text="ship 1000"),
        ]
        self.send_catalog(observed_at=NOW - 10, generation="gen-1", entries=entries)

        def keys(**filters: str) -> list[str]:
            return sorted(
                row["plan_key"] for row in self.store.catalog(now=NOW, **filters)
            )

        self.assertEqual(keys(project="my_app"), ["plan-a"])
        self.assertEqual(keys(project="My_App"), ["plan-c"])
        self.assertEqual(keys(search="my_app"), ["plan-a"])
        self.assertEqual(keys(search="100%"), ["plan-c"])
        self.assertEqual(keys(search="\\"), [])

    def test_catalog_labels_rather_than_run_rows_name_a_plan(self) -> None:
        """The catalog knows what a plan is called; a run summary only guessed."""
        self.send_snapshot(
            observed_at=NOW - 20,
            runs=[
                run_row(
                    "alpha",
                    plan_key="plan-a",
                    project="plan-1a2b3c4d5e6f-0011223344ff",
                )
            ],
        )
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=[
                catalog_entry(
                    "plan-a",
                    display_label="Alpha plan",
                    title="Ship the alpha",
                    projects=["scripts", "pi-monitoring"],
                )
            ],
        )
        row = self.store.runs(now=NOW)[0]
        self.assertEqual(row["project"], "scripts + pi-monitoring")
        self.assertEqual(row["plan_label"], "Alpha plan")
        self.assertEqual(row["plan_title"], "Ship the alpha")
        self.assertEqual(
            [r["run_key"] for r in self.store.runs(now=NOW, project="scripts")],
            ["alpha"],
        )
        self.assertEqual(
            [r["run_key"] for r in self.store.runs(now=NOW, search="ship the")],
            ["alpha"],
        )
        self.assertEqual(self.store.runs(now=NOW, project="nobody"), [])

    def test_a_catalog_that_only_guessed_unknown_keeps_the_run_rows_project(
        self,
    ) -> None:
        """The catalog's fallback name must not overwrite a name a row knows."""
        self.send_snapshot(
            observed_at=NOW - 20,
            runs=[run_row("alpha", plan_key="plan-a", project="raspberry-pi")],
        )
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=[
                catalog_entry(
                    "plan-a",
                    display_label="Alpha plan",
                    title="Ship the alpha",
                    projects=["Unknown project"],
                    project_group="Unknown project",
                )
            ],
        )
        row = self.store.runs(now=NOW)[0]
        self.assertEqual(row["project"], "raspberry-pi")
        self.assertEqual(row["plan_label"], "Alpha plan")
        self.assertEqual(row["plan_title"], "Ship the alpha")

    def test_an_unlabelled_plan_falls_back_without_leaking_a_directory_name(
        self,
    ) -> None:
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[run_row("alpha", project="plan-1a2b3c4d5e6f-0011223344ff")],
        )
        self.assertEqual(self.store.runs(now=NOW)[0]["project"], "Unknown project")


# --------------------------------------------------------------------------
# Operation journal
# --------------------------------------------------------------------------


def operation_record(operation_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "command_id": f"cmd-{operation_id}",
        "method": "question.reply",
        "status": "pending",
        "plan_key": "plan-alpha",
        "run_key": "alpha",
        "run_id": "20260619T140000Z-alpha",
        "question_id": "q-1",
        "requested_at": NOW - 60,
        "deliver_by": NOW + 60,
    }
    record.update(overrides)
    return record


class OperationJournalTests(StoreCase):
    """One operation id is one mutation, whatever the browser does."""

    def test_a_repeated_operation_id_is_the_same_operation(self) -> None:
        first = self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        second = self.store.record_operation(
            operation_record("op-1", command_id="cmd-other"),
            connector_id=CONNECTOR_ID,
            received_at=NOW + 1,
        )
        self.assertEqual(first, second)
        self.assertEqual(second["command_id"], "cmd-op-1")
        self.assertEqual(len(self.store.operations_in_status("pending")), 1)

    def test_a_forbidden_transition_is_refused_and_a_repeat_is_not(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        self.store.advance_operation(
            "op-1", "dispatched", now=NOW + 1, dispatched_at=NOW + 1
        )
        repeated = self.store.advance_operation("op-1", "dispatched", now=NOW + 2)
        self.assertEqual(repeated["dispatched_at"], NOW + 1)
        with self.assertRaises(OperationError) as refusal:
            self.store.advance_operation("op-1", "pending", now=NOW + 3)
        self.assertEqual(refusal.exception.reason, "invalid-operation-transition")

    def test_an_unknown_operation_cannot_be_advanced(self) -> None:
        with self.assertRaises(OperationError) as refusal:
            self.store.advance_operation("nope", "dispatched", now=NOW)
        self.assertEqual(refusal.exception.reason, "unknown-operation")

    def test_one_outstanding_mutation_per_plan_is_visible_to_the_caller(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        self.assertEqual(
            self.store.outstanding_operation("plan-alpha")["operation_id"], "op-1"
        )
        self.store.advance_operation(
            "op-1", "dispatched", now=NOW + 1, dispatched_at=NOW + 1
        )
        self.store.advance_operation("op-1", "accepted", now=NOW + 2)
        # Accepted is still outstanding: ASL has the reply and has not yet
        # said what it did with it.
        self.assertIsNotNone(self.store.outstanding_operation("plan-alpha"))
        self.store.advance_operation(
            "op-1", "consumed", now=NOW + 3, settled_at=NOW + 3
        )
        self.assertIsNone(self.store.outstanding_operation("plan-alpha"))

    def test_admission_sees_an_outstanding_row_behind_an_older_one(self) -> None:
        """An older row in another scope must not hide the one that blocks.

        A pending stop is the plan's oldest outstanding row, and admission
        refuses only a row in the submission's own scope; handed that row
        alone, it would admit a second launch while the first is pending.
        """

        def submit(operation_id: str, method: str, at: float) -> None:
            def decide(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
                admission = admit(
                    [_rehydrate(row) for row in rows],
                    operation_id=operation_id,
                    method=method,
                    target=OperationTarget(plan_key="plan-alpha"),
                    now=at,
                    deliver_by=at + 60,
                    command_id=f"cmd-{operation_id}",
                    run_key="alpha",
                )
                if admission.refused:
                    raise admission.error()
                return None if admission.replayed else admission.record

            self.store.admit_operation(
                operation_id,
                "plan-alpha",
                decide,
                connector_id=CONNECTOR_ID,
                received_at=at,
            )

        submit("stop", METHOD_STOP_AFTER_CURRENT, NOW)
        submit("launch-1", METHOD_LAUNCH, NOW + 1)
        with self.assertRaises(OperationError) as refusal:
            submit("launch-2", METHOD_LAUNCH, NOW + 2)
        self.assertEqual(refusal.exception.reason, "operation-outstanding")
        self.assertIsNone(self.store.operation("launch-2"))
        # A resend of the admitted launch is still the same operation.
        submit("launch-1", METHOD_LAUNCH, NOW + 3)
        self.assertEqual(len(self.store.operations_in_status("pending")), 2)

    def test_the_journal_never_stores_the_reply_it_carried(self) -> None:
        with self.assertRaises(OperationError) as refusal:
            self.store.record_operation(
                operation_record("op-1", detail_code={"reply_text": "hello"}),
                connector_id=CONNECTOR_ID,
                received_at=NOW,
            )
        self.assertEqual(refusal.exception.reason, "operation-body-leak")
        self.assertIsNone(self.store.operation("op-1"))

    def test_an_exported_operation_is_narrower_than_the_stored_one(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        exported = self.store.exported_operations(now=NOW)
        self.assertEqual(len(exported), 1)
        self.assertNotIn("plan_key", exported[0])
        self.assertEqual(exported[0]["operation_id"], "op-1")

    def test_a_command_target_resolves_the_plan_the_client_never_chose(self) -> None:
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        target = self.store.command_target("alpha")
        self.assertEqual(target["plan_key"], "plan-alpha")
        self.assertEqual(target["connector_id"], CONNECTOR_ID)
        self.assertIsNone(self.store.command_target("nobody"))


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


class RetentionTests(StoreCase):
    """One cutoff for the time series, and never an open uncertainty."""

    def test_runs_and_history_share_one_cutoff(self) -> None:
        self.send_snapshot(observed_at=NOW - RETENTION - 10, runs=[run_row("old")])
        self.send_history(
            generated_at=NOW - RETENTION - 10,
            entries=[history_entry("old", recorded_at=NOW - RETENTION - 10)],
        )
        self.assertEqual(self.store.prune(now=NOW), 2)
        self.assertEqual(self.store.runs(now=NOW, include_absent=True), [])
        self.assertEqual(self.store.history(now=NOW), [])

    def test_only_settled_operations_age_out(self) -> None:
        """An open row is the record of a mutation whose outcome is unknown.

        Deleting it would erase that admission and release the hold that keeps
        a second attempt from running against an unfinished first one.
        """
        self.store.record_operation(
            operation_record("open", requested_at=NOW - RETENTION - 10),
            connector_id=CONNECTOR_ID,
            received_at=NOW - RETENTION - 10,
        )
        self.store.record_operation(
            operation_record(
                "settled", requested_at=NOW - RETENTION - 10, plan_key="plan-beta"
            ),
            connector_id=CONNECTOR_ID,
            received_at=NOW - RETENTION - 10,
        )
        self.store.advance_operation("settled", "cancelled", now=NOW - RETENTION - 5)
        self.store.prune(now=NOW)
        self.assertIsNotNone(self.store.operation("open"))
        self.assertIsNone(self.store.operation("settled"))
        self.assertEqual(
            self.store.outstanding_operation("plan-alpha")["operation_id"], "open"
        )

    def test_a_present_plan_never_expires_and_a_retired_one_does(self) -> None:
        """The catalog is not a time series: an old untouched plan is still a plan."""
        self.send_catalog(
            observed_at=NOW - RETENTION - 100,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        self.send_catalog(
            observed_at=NOW - RETENTION - 50,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            received_at=NOW - RETENTION - 50,
        )
        self.store.prune(now=NOW)
        self.assertEqual(set(self.plans_by_key(include_absent=True)), {"plan-a"})

    def test_retention_runs_at_most_once_an_interval_on_the_upload_path(self) -> None:
        self.send_snapshot(observed_at=NOW - RETENTION - 10, runs=[run_row("old")])
        first = self.send_snapshot(observed_at=NOW, runs=[run_row("fresh")])
        self.assertGreaterEqual(first.pruned, 1)
        self.send_heartbeat(observed_at=NOW + 1)
        self.assertEqual(self.store.meta(store_types.META_LAST_PRUNED_AT), NOW)

    def test_an_incident_still_publishing_is_never_deleted(self) -> None:
        with self.store.transaction() as connection:
            incidents.record(
                connection,
                kind=incidents.INCIDENT_TERMINAL_FAILURE,
                connector_id=CONNECTOR_ID,
                run_key="alpha",
                plan_key="plan-alpha",
                project="pi-monitoring",
                now=NOW - RETENTION - 100,
            )
            connection.execute(
                "UPDATE incident SET delivery_state = ?, resolved_at = ?",
                (incidents.DELIVERY_PUBLISHING, NOW - RETENTION - 50),
            )
        self.store.prune(now=NOW)
        with self.store.reading() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS n FROM incident")
            self.assertEqual(remaining.fetchone()["n"], 1)


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------

"""Payload mapping, the ASL provider half, identity, and token files.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge_protocol,
    intervention,
    workbench_connector,
)
from asl_workbench import catalog, history, observation, operations
from asl_workbench import connector_payloads as portable_payloads
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench.transport import (
    CATALOG_ENTRY_SHAPE,
    MAX_RUNS_PER_SNAPSHOT,
    MAX_WARNING_CHARS,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_SNAPSHOT,
    TransportError,
    observation_envelope,
)
from tests.workbench_connector_support import (
    CONNECTOR_ID,
    NOW,
    TARGET_ID,
    FakeChannel,
    StubSource,
    build_page,
    cycle_with,
    drain,
    history_batch,
    history_entry,
    make_connector,
    summary_payload,
    wire_row,
)

# -- Payload mapping --------------------------------------------------------


class PayloadTests(unittest.TestCase):
    """Every frame the connector builds is one its own receiver accepts."""

    def assert_publishable(self, payload: dict[str, Any]) -> None:
        observation_envelope(
            payload=payload,
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=NOW,
        )

    def test_the_local_catalog_entry_shape_is_not_the_wire_shape(self) -> None:
        # The reason `catalog_entry_payload` exists. A title-less plan is legal
        # locally and refused on the wire, so the raw payload cannot be sent.
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        self.assertIsNone(entry.title)
        with self.assertRaises(TransportError):
            self.assert_publishable(
                {
                    "view": VIEW_CATALOG_PAGE,
                    "observed_at": NOW,
                    "generation": "g",
                    "complete": False,
                    "final": False,
                    "offset": 0,
                    "entry_count": 1,
                    "entries": [dict(entry.to_payload())],
                }
            )
        self.assertEqual("", portable_payloads.catalog_entry_payload(entry)["title"])

    def test_every_field_the_wire_requires_is_supplied_by_the_mapper(self) -> None:
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        published = portable_payloads.catalog_entry_payload(entry)
        self.assertEqual(
            set(),
            CATALOG_ENTRY_SHAPE.required - set(published),
            "the catalog mapper dropped a field the transport requires",
        )
        declared = CATALOG_ENTRY_SHAPE.required | CATALOG_ENTRY_SHAPE.optional
        self.assertEqual(
            set(),
            set(published) - declared,
            "the catalog mapper carried a field the transport does not declare",
        )

    def test_an_unknown_pause_reason_folds_onto_other_rather_than_dropping_the_plan(
        self,
    ) -> None:
        self.assertEqual("blocked", portable_payloads.pause_reason_word("blocked"))
        self.assertEqual(
            history.PAUSE_REASON_OTHER,
            portable_payloads.pause_reason_word("brand-new-reason"),
        )
        self.assertIsNone(portable_payloads.pause_reason_word(None))
        self.assertIsNone(portable_payloads.pause_reason_word(""))
        self.assertIsNone(portable_payloads.pause_reason_word(7))

    def test_a_session_identifier_that_is_not_a_dotted_number_is_omitted(self) -> None:
        self.assertEqual("1", portable_payloads.session_id_word("1"))
        self.assertEqual("2.3.1", portable_payloads.session_id_word(" 2.3.1 "))
        self.assertIsNone(portable_payloads.session_id_word("review-fix"))
        self.assertIsNone(portable_payloads.session_id_word("0.1"))
        self.assertIsNone(portable_payloads.session_id_word(None))

    def test_a_long_project_list_is_truncated_rather_than_costing_the_plan(
        self,
    ) -> None:
        names = [f"repo-{index}" for index in range(catalog.MAX_PROJECTS_PER_PLAN)]
        entry = catalog.record_from_summary(
            summary_payload(
                "/tmp/plan",
                repositories=[
                    {"alias": name, "path": f"/tmp/{name}"} for name in names
                ],
                repository_count=len(names),
            )
        ).entry
        kept = observation.MAX_WARNINGS
        self.assertGreater(len(entry.projects), kept)
        published = portable_payloads.catalog_entry_payload(entry)
        self.assertEqual(kept, len(published["projects"]))
        self.assertEqual(list(entry.projects[:kept]), published["projects"])

    def test_a_catalog_page_is_published_in_the_shape_the_store_ingests(self) -> None:
        page = build_page(2)
        published = portable_payloads.catalog_page_payload(page, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(page.generation, published["generation"])
        self.assertEqual(2, published["entry_count"])
        self.assertEqual(2, published["total"])
        self.assertTrue(published["complete"])
        self.assertTrue(published["final"])
        # Not `CatalogPage.to_payload`: those two names are local only.
        self.assertNotIn("count", published)
        self.assertNotIn("limit", published)

    def test_an_entry_the_wire_refuses_clears_both_complete_and_final(self) -> None:
        # Clearing `complete` alone would be refused on arrival, and a page
        # that lost a plan must not retire the plan it lost.
        page = build_page(2)
        broken = dict(portable_payloads.catalog_entry_payload(page.entries[0]))
        broken["urgency_rank"] = "not a number"
        with mock.patch.object(
            portable_payloads,
            "catalog_entry_payload",
            side_effect=[
                broken,
                portable_payloads.catalog_entry_payload(page.entries[1]),
            ],
        ):
            published = portable_payloads.catalog_page_payload(page, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(1, published["entry_count"])
        self.assertFalse(published["complete"])
        self.assertFalse(published["final"])
        self.assertTrue(
            any("could not be published" in text for text in published["warnings"])
        )

    def test_a_history_entry_with_no_run_id_is_dropped_and_the_batch_says_so(
        self,
    ) -> None:
        batch = history_batch((history_entry("a"), history_entry("b", run_id=None)))
        published = portable_payloads.history_batch_payload(batch)
        self.assert_publishable(published)
        self.assertEqual(1, published["entry_count"])
        self.assertFalse(published["complete"])
        self.assertTrue(
            any("could not be published" in text for text in published["warnings"])
        )

    def test_a_history_batch_never_carries_the_questions_it_was_built_beside(
        self,
    ) -> None:
        # A question is prose. It reaches the Pi through a correlated detail
        # read or not at all.
        question = history.QuestionEntry(
            entry_id="q-1",
            source_key="source-a",
            plan_key="0123456789abcdef",
            run_key="fedcba9876543210",
            question_key="qk-1",
            outcome=history.QUESTION_OUTCOME_ANSWERED,
            recorded_at=NOW - 30.0,
        )
        batch = history_batch((history_entry(),), questions=(question,))
        self.assertIn("questions", batch.to_payload())
        published = portable_payloads.history_batch_payload(batch)
        self.assert_publishable(published)
        self.assertNotIn("questions", published)
        self.assertNotIn("question_count", published)

    def test_a_complete_batch_with_nothing_dropped_stays_complete(self) -> None:
        published = portable_payloads.history_batch_payload(
            history_batch((history_entry(),))
        )
        self.assertTrue(published["complete"])
        self.assertEqual(1, published["entry_count"])

    def test_a_snapshot_digest_ignores_the_moment_it_was_taken(self) -> None:
        # Otherwise every poll of an idle corpus would look like a change.
        rows = (wire_row(),)
        first = portable_payloads.snapshot_payload(
            rows, observed_at=NOW, source_digest="gen"
        )
        second = portable_payloads.snapshot_payload(
            rows, observed_at=NOW + 600.0, source_digest="gen"
        )
        self.assertEqual(1, first["row_count"])
        self.assertEqual(first["digest"], second["digest"])
        self.assertNotEqual(first["observed_at"], second["observed_at"])

    def test_a_changed_row_changes_the_digest(self) -> None:
        first = portable_payloads.snapshot_payload(
            (wire_row(primary_state="running"),), observed_at=NOW, source_digest="gen"
        )
        second = portable_payloads.snapshot_payload(
            (wire_row(primary_state="verifying"),), observed_at=NOW, source_digest="gen"
        )
        self.assertNotEqual(first["digest"], second["digest"])

    def test_rows_beyond_the_frame_limit_are_cut_and_the_frame_admits_it(self) -> None:
        rows = tuple(
            wire_row(run_id=f"run-{index}", run_key=f"{index:016x}")
            for index in range(MAX_RUNS_PER_SNAPSHOT + 3)
        )
        published = portable_payloads.snapshot_payload(rows, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(MAX_RUNS_PER_SNAPSHOT, published["row_count"])
        self.assertTrue(published["truncated"])
        self.assertTrue(any("row limit" in text for text in published["warnings"]))

    def test_a_row_the_wire_refuses_is_dropped_and_counted(self) -> None:
        published = portable_payloads.snapshot_payload(
            (wire_row(), wire_row(run_key=None)), observed_at=NOW
        )
        self.assert_publishable(published)
        self.assertEqual(1, published["row_count"])
        self.assertEqual(1, published["failed_projections"])

    def test_warnings_are_bounded_in_count_and_in_length(self) -> None:
        published = portable_payloads.snapshot_payload(
            (),
            observed_at=NOW,
            warnings=["w" * (MAX_WARNING_CHARS + 50)] * (observation.MAX_WARNINGS + 5),
        )
        self.assert_publishable(published)
        self.assertEqual(observation.MAX_WARNINGS, len(published["warnings"]))
        self.assertEqual(MAX_WARNING_CHARS, len(published["warnings"][0]))

    def test_a_heartbeat_always_names_the_contract_set_it_speaks(self) -> None:
        published = portable_payloads.heartbeat_payload(
            observed_at=NOW, bridge_alive=True
        )
        self.assert_publishable(published)
        self.assertEqual(
            observation.SUPPORTED_CONTRACT_SET_IDS[0], published["contract_set_id"]
        )

    def test_the_hello_declares_observation_only_and_reads_but_never_mutation(
        self,
    ) -> None:
        hello = portable_payloads.connector_hello(
            connector_id=CONNECTOR_ID, target_id=TARGET_ID, asl_version="1.2.3"
        )
        self.assertTrue(hello["observation_only"])
        self.assertEqual(
            list(portable_support.CONNECTOR_CAPABILITIES), hello["capabilities"]
        )
        self.assertIn("detail", hello["capabilities"])
        # Reading on request is not mutating on request. The connector answers a
        # correlated read and still performs nothing, so ``observation_only``
        # stays true and ``mutate`` stays absent.
        self.assertNotIn("mutate", hello["capabilities"])

    def test_a_connector_without_a_responder_does_not_offer_detail(self) -> None:
        connector = portable_runtime.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=StubSource(),
            channel=FakeChannel(),
        )
        self.assertEqual(
            portable_support.OBSERVATION_CAPABILITIES, connector.capabilities
        )
        self.assertNotIn("detail", connector.hello()["capabilities"])

    def test_a_hello_the_service_would_refuse_is_refused_before_anything_is_sent(
        self,
    ) -> None:
        with self.assertRaises(TransportError):
            portable_payloads.connector_hello(
                connector_id="not a valid id", target_id=TARGET_ID
            )

    def test_the_scan_warnings_ride_only_the_page_that_closes_the_generation(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(
            StubSource([cycle_with(pages=3, warnings=("one plan is unreadable",))]),
            channel,
        )
        drain(connector, steps=8)
        pages = channel.payloads(VIEW_CATALOG_PAGE)
        self.assertEqual(3, len(pages))
        carrying = [page for page in pages if page.get("warnings")]
        self.assertEqual(1, len(carrying))
        self.assertTrue(carrying[0]["final"])

    def test_a_frame_this_connector_cannot_publish_does_not_wedge_the_loop(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(StubSource([cycle_with()]), channel)
        with mock.patch.object(
            portable_runtime, "heartbeat_payload", return_value={"view": VIEW_HEARTBEAT}
        ):
            self.assertIsNone(connector.step(NOW))
            self.assertIn("unpublishable", str(connector.degraded_reason))
            # The refused frame comes off the queue along with the cycle. A
            # refusal is a property of the payload and not of the moment, so a
            # frame left queued would be re-selected and re-refused on every
            # step, and nothing behind it would ever be delivered.
            self.assertEqual(VIEW_SNAPSHOT, connector.step(NOW + 1.0))
        self.assertEqual(VIEW_HEARTBEAT, connector.step(NOW + 15.0))


# -- The ASL provider half --------------------------------------------------


class RemoteActionTests(unittest.TestCase):
    """The published action vocabulary is the operations vocabulary."""

    def test_every_published_action_is_a_real_operation_method(self) -> None:
        for action, method in workbench_connector.REMOTE_ACTIONS.items():
            self.assertIn(action, intervention.ACTIONS, f"{action} is not an action")
            self.assertIn(method, operations.OPERATION_METHODS, f"{method} is not real")

    def test_the_two_actions_with_no_remote_form_are_not_published(self) -> None:
        # `question.resolve` has no operation method, and `workbench.ensure`
        # opens a browser on this machine.
        self.assertNotIn(
            intervention.ACTION_RESOLVE, workbench_connector.REMOTE_ACTIONS
        )
        self.assertNotIn(
            intervention.ACTION_WORKBENCH, workbench_connector.REMOTE_ACTIONS
        )

    def test_every_other_action_is_published(self) -> None:
        unpublished = set(intervention.ACTIONS) - set(
            workbench_connector.REMOTE_ACTIONS
        )
        self.assertEqual(
            {intervention.ACTION_RESOLVE, intervention.ACTION_WORKBENCH},
            unpublished,
            "an action gained or lost a remote form without this test moving",
        )

    def test_an_unknown_action_is_dropped_rather_than_published_verbatim(self) -> None:
        row = {
            "actions": ["run.abort", "something.new", 7, intervention.ACTION_RESOLVE]
        }
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        published = workbench_connector.project_run(
            dict(row, run_id="run-1", last_activity_at=NOW), entry=entry
        )
        assert published is not None
        self.assertEqual([operations.METHOD_ABORT], published["actions"])

    def test_the_active_state_list_is_the_bridges_own(self) -> None:
        self.assertEqual(
            bridge_protocol.ACTIVE_PRIMARY_STATES,
            workbench_connector.ACTIVE_PRIMARY_STATES,
        )


class RunProjectionTests(unittest.TestCase):
    """One bridge row becomes one wire row, or none at all."""

    def setUp(self) -> None:
        self.entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry

    def project(self, **row: Any) -> dict[str, Any] | None:
        return workbench_connector.project_run(row, entry=self.entry)

    def test_the_identity_fields_come_from_the_catalog_record(self) -> None:
        published = self.project(run_id="run-1", last_activity_at=NOW)
        assert published is not None
        self.assertEqual(self.entry.plan_key, published["plan_key"])
        self.assertEqual(self.entry.run_key, published["run_key"])
        self.assertEqual(self.entry.project_group, published["project"])

    def test_a_row_with_no_run_id_is_refused(self) -> None:
        self.assertIsNone(self.project(last_activity_at=NOW))
        self.assertIsNone(self.project(run_id="", last_activity_at=NOW))
        self.assertIsNone(self.project(run_id=7, last_activity_at=NOW))

    def test_a_row_with_no_usable_time_is_refused_rather_than_stamped(self) -> None:
        self.assertIsNone(self.project(run_id="run-1"))
        published = self.project(run_id="run-1", run_started_at="2026-09-01T10:00:00Z")
        assert published is not None
        self.assertIsNotNone(published["last_activity_at"])

    def test_the_question_keeps_its_identity_and_loses_its_words(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            question={
                "question_id": "q-1",
                "asked_at": "2026-09-01T10:00:00+00:00",
                "unanswered_count": 2,
                "text": "Should we drop the v1 column?",
                "session_id": "3",
                "context_kind": "waiting",
            },
        )
        assert published is not None
        self.assertEqual("q-1", published["question_id"])
        self.assertEqual(2, published["unanswered_count"])
        self.assertNotIn("question", published)
        self.assertNotIn(
            "Should we drop the v1 column?", json.dumps(published, sort_keys=True)
        )

    def test_the_rows_paths_and_operator_label_never_travel(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            plan_dir="/Users/someone/plans/alpha",
            workspace_root="/Users/someone/code",
            label="Nightly refactor",
        )
        assert published is not None
        rendered = json.dumps(published, sort_keys=True)
        for leaked in ("/Users/someone", "Nightly refactor"):
            self.assertNotIn(leaked, rendered)

    def test_a_projected_row_is_one_the_wire_accepts(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            primary_state="running",
            phase="running",
            urgency_group="in-progress",
            actions=["run.abort"],
            harness="codex",
            session_id="2",
            pause_reason="blocked",
        )
        assert published is not None
        observation_envelope(
            payload=portable_payloads.snapshot_payload((published,), observed_at=NOW),
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=NOW,
        )


class ConnectorIdentityTests(unittest.TestCase):
    """The receiver's primary key for this Mac is stable and spellable."""

    def test_a_hostname_becomes_a_transport_identifier(self) -> None:
        self.assertEqual(
            "macbook-pro", workbench_connector.default_connector_id("macbook pro")
        )
        self.assertEqual(
            "studio.local", workbench_connector.default_connector_id("studio.local")
        )

    def test_a_name_that_cannot_be_spelled_at_all_becomes_mac(self) -> None:
        for raw in ("", "   ", "!!!", "---"):
            self.assertEqual("mac", workbench_connector.default_connector_id(raw))

    def test_the_derived_identifier_is_one_the_hello_accepts(self) -> None:
        for raw in ("séb's mac", "a" * 400, "studio.local", "!!!"):
            derived = workbench_connector.default_connector_id(raw)
            portable_payloads.connector_hello(connector_id=derived, target_id=TARGET_ID)

    def test_the_identifier_is_stable_across_calls(self) -> None:
        self.assertEqual(
            workbench_connector.default_connector_id(),
            workbench_connector.default_connector_id(),
        )


class TokenFileTests(unittest.TestCase):
    """A token file only this user may read, or no connector at all."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "connector.json"

    def write(self, document: object, *, mode: int = 0o600) -> Path:
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.path.chmod(mode)
        return self.path

    def test_a_private_file_yields_its_token(self) -> None:
        self.write({"connector_token": "abc123"})
        self.assertEqual("abc123", workbench_connector.read_connector_token(self.path))

    def test_a_file_readable_beyond_its_owner_is_refused(self) -> None:
        self.write({"connector_token": "abc123"}, mode=0o644)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)

    def test_a_symlink_is_refused_rather_than_followed(self) -> None:
        real = self.write({"connector_token": "abc123"})
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(link)

    def test_a_missing_malformed_or_tokenless_file_is_refused(self) -> None:
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.root / "absent.json")
        self.path.write_text("{not json", encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)
        for document in ([], {}, {"connector_token": ""}, {"connector_token": 7}):
            self.write(document)
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.read_connector_token(self.path)

    def test_a_token_that_is_not_printable_ascii_is_refused(self) -> None:
        self.write({"connector_token": "abcé"})
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)

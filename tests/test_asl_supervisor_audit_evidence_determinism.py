"""Evidence bundle determinism, size shedding, and mailbox evidence."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    assurance_models_primitives,
    events,
    mailbox_constants,
    mailbox_vocabulary,
    supervisor_audit_evidence_collector,
    supervisor_audit_evidence_constants,
    supervisor_audit_evidence_failures,
    supervisor_audit_evidence_input,
    supervisor_audit_evidence_models,
    supervisor_audit_evidence_shedding,
)
from tests import support_supervisor_audit_evidence as evidence_support
from tests.support_supervisor_audit import RUN_ID


class EvidenceBundleTests(evidence_support.EvidenceBundleFixture):
    """The audit input bundle is bounded, deterministic, and control-plane only."""

    def test_equivalent_histories_serialize_to_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "history"
            root.mkdir()
            one = self._collect(*self._history(root))
            shutil.rmtree(root)
            root.mkdir()
            two = self._collect(*self._history(root))

            self.assertEqual(one.to_bytes(), two.to_bytes())
            self.assertEqual(one.sha256, two.sha256)

    def test_relocating_a_history_preserves_its_control_plane_evidence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            one = self._collect(*self._history(Path(first))).payload
            two = self._collect(*self._history(Path(second))).payload

            for section in (
                "turns",
                "events",
                "mailbox",
                "scopes",
                "review",
                "status",
                "registry",
            ):
                self.assertEqual(one[section], two[section], section)

    def test_collection_is_stable_across_repeated_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            self.assertEqual(
                self._collect(*history).to_bytes(), self._collect(*history).to_bytes()
            )

    def test_huge_logs_stay_under_the_serialized_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(
                Path(tmp), stdout_bytes=4 * 1024 * 1024, event_count=20000
            )

            bundle = self._collect(*history)
            payload = bundle.payload

            self.assertLessEqual(
                len(bundle.to_bytes()),
                supervisor_audit_evidence_constants.AUDIT_INPUT_MAX_BYTES,
            )
            self.assertTrue(
                any(
                    entry["section"] == "events"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_SIZE_CAP
                    for entry in payload["omissions"]
                )
            )
            terminal = [
                record
                for record in payload["events"]["records"]
                if record["name"] == events.RUN_FINISHED_EVENT
            ]
            self.assertEqual(1, len(terminal))

    def test_shedding_keeps_the_terminal_record_before_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp), event_count=20000)

            records = self._collect(*history).payload["events"]["records"]
            names = {record["name"] for record in records}

            self.assertIn(events.RUN_FINISHED_EVENT, names)
            self.assertIn("run_started", names)

    def test_shedding_gives_up_failing_chatter_before_the_pause(self) -> None:
        """The pause and the rejection outlast heartbeats that look like failures.

        A tier is shed oldest first, so hundreds of failure-shaped heartbeats
        sharing the protected tier with a late pause shed the pause first and
        leave containment unjudgeable (ASI-9).
        """

        def record(sequence: int, name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "sequence": sequence,
                "name": name,
                "created_at": None,
                "version": 2,
                "priority": supervisor_audit_evidence_failures._event_priority(
                    name, payload
                ),
                "payload": payload,
            }

        chatter = [
            record(index, events.TURN_HEARTBEAT_EVENT, {"hang_suspected": True})
            for index in range(1, 200)
        ] + [
            record(200 + index, events.AGENT_MUX_DELIVERY_EVENT, {"ok": False})
            for index in range(200)
        ]
        closing = [
            record(500, events.TURN_REJECTED_EVENT, {"run_id": RUN_ID}),
            record(501, events.RUN_PAUSED_EVENT, {"reason": "x" * 64}),
        ]
        payload: dict[str, Any] = {
            "events": {"records": chatter + closing},
            "mailbox": {"records": []},
            "turns": [],
            "omissions": [],
        }
        collector = supervisor_audit_evidence_collector._Collector(
            supervisor_audit_evidence_models.EvidenceRoots(roots=())
        )
        budget = (
            len(
                assurance_models_primitives.canonical_json_bytes(
                    {**payload, "events": {"records": closing}}
                )
            )
            + 2048
        )

        supervisor_audit_evidence_shedding._shed(payload, collector, max_bytes=budget)

        names = [item["name"] for item in payload["events"]["records"]]
        self.assertIn(events.RUN_PAUSED_EVENT, names)
        self.assertIn(events.TURN_REJECTED_EVENT, names)
        self.assertEqual(
            0,
            supervisor_audit_evidence_failures._event_priority(
                events.TURN_REJECTED_EVENT, {}
            ),
        )

    def test_tightening_a_bundle_sends_less_of_the_same_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp), event_count=20000))
            size = len(bundle.to_bytes())
            digest = bundle.sha256

            tightened = supervisor_audit_evidence_input.tighten_audit_input(
                bundle, max_bytes=size // 2
            )

            self.assertLess(len(tightened.to_bytes()), size)
            # The canonical bundle does not move: it stays the artifact on disk,
            # the input the deterministic verdicts were read from, and the source
            # of the locators an answer is validated against.
            self.assertEqual(digest, bundle.sha256)
            self.assertEqual(
                [ref.locator for ref in bundle.evidence],
                [ref.locator for ref in tightened.evidence],
            )

    def test_tightening_extends_the_omissions_it_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp), event_count=20000))
            before = {
                (entry["section"], entry["reason"]): entry for entry in bundle.omissions
            }

            tightened = supervisor_audit_evidence_input.tighten_audit_input(
                bundle, max_bytes=len(bundle.to_bytes()) // 2
            )
            after = {
                (entry["section"], entry["reason"]): entry
                for entry in tightened.omissions
            }

            # Nothing the first pass gave up is forgotten by the second: every
            # bucket survives, its count only grows, and it still carries a
            # digest identifying exactly one set of withheld payloads.
            self.assertTrue(before)
            self.assertLessEqual(set(before), set(after))
            for key, entry in before.items():
                self.assertGreaterEqual(after[key]["count"], entry["count"], key)
                self.assertEqual(64, len(after[key]["sha256"]), key)
            events_cap = ("events", supervisor_audit_evidence_constants.REASON_SIZE_CAP)
            self.assertGreater(after[events_cap]["count"], before[events_cap]["count"])
            # And the second pass gives up in the same order the first would
            # have: previews are trimmed before whole turns are dropped, which
            # only holds because the previews inside the payload were recovered
            # as shrinkable rather than left invisible to it.
            previews_cap = (
                "previews",
                supervisor_audit_evidence_constants.REASON_SIZE_CAP,
            )
            self.assertNotIn(previews_cap, before)
            self.assertIn(previews_cap, after)

    def test_application_source_and_diffs_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn("transfer_funds", text)
            self.assertIn(
                supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT, text
            )

    def test_credentials_never_reach_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn(self.SECRET_ARGV, text)
            self.assertNotIn(self.SECRET_ENV, text)
            self.assertNotIn(self.SECRET_PASSWORD, text)
            self.assertNotIn("user:pw@", text)
            self.assertIn("github.com/owner/repo", text)

    def test_missing_artifacts_are_explicit_rather_than_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            (run_dir / "turn-0001-01" / "last-message.txt").unlink()

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload
            last_message = payload["turns"][0]["last_message"]

            self.assertFalse(last_message["available"])
            self.assertEqual(
                supervisor_audit_evidence_constants.REASON_MISSING,
                last_message["reason"],
            )
            self.assertTrue(
                any(
                    entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_MISSING
                    and entry["count"] >= 1
                    and len(entry["sha256"]) == 64
                    for entry in payload["omissions"]
                )
            )

    def test_a_damaged_event_log_is_counted_and_digested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            with (state_dir / events.EVENTS_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertEqual(
                1,
                payload["events"]["summary"][
                    supervisor_audit_evidence_constants.UNPARSABLE_LINE_COUNT_KEY
                ],
            )
            self.assertTrue(
                any(
                    entry["section"] == "events"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_MALFORMED
                    for entry in payload["omissions"]
                )
            )

    def test_the_operator_exchange_reaches_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload
            section = payload["mailbox"]

            self.assertTrue(section["summary"]["available"])
            self.assertEqual(
                [
                    (self.QUESTION_ID, mailbox_vocabulary.KIND_QUESTION),
                    (self.WAIT_ID, mailbox_vocabulary.KIND_WAIT_START),
                    (self.REPLY_ID, mailbox_vocabulary.KIND_REPLY),
                    (self.RECEIPT_ID, mailbox_vocabulary.KIND_RECEIPT),
                    (self.WAIT_END_ID, mailbox_vocabulary.KIND_WAIT_END),
                    (self.NOTICE_ID, mailbox_vocabulary.KIND_NOTICE),
                ],
                [(record["id"], record["kind"]) for record in section["records"]],
            )
            self.assertEqual(
                [1, 2, 4, 6, 7, 8],
                [record["sequence"] for record in section["records"]],
            )
            self.assertEqual("01", section["records"][0]["session_id"])
            self.assertEqual(1, section["records"][0]["turn_index"])

    def test_the_reply_prose_is_withheld_while_its_digest_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn(self.REPLY_TEXT, text)
            self.assertNotIn(self.QUESTION_TEXT, text)
            self.assertIn(
                hashlib.sha256(self.REPLY_TEXT.encode("utf-8")).hexdigest()[:16], text
            )

    def test_another_runs_exchange_is_counted_rather_than_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertEqual(2, bundle.payload["mailbox"]["summary"]["unscoped_count"])
            self.assertNotIn(self.FOREIGN_QUESTION_ID, text)
            self.assertNotIn(self.FOREIGN_REPLY_ID, text)

    def test_a_missing_mailbox_is_explicit_rather_than_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            (state_dir / mailbox_constants.MAILBOX_FILE_NAME).unlink()

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload
            summary = payload["mailbox"]["summary"]

            self.assertEqual([], payload["mailbox"]["records"])
            self.assertFalse(summary["available"])
            self.assertEqual(
                supervisor_audit_evidence_constants.REASON_MISSING, summary["reason"]
            )
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_MISSING
                    for entry in payload["omissions"]
                )
            )

    def test_a_damaged_mailbox_line_is_counted_and_digested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            with (state_dir / mailbox_constants.MAILBOX_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertEqual(
                1,
                payload["mailbox"]["summary"][
                    supervisor_audit_evidence_constants.UNPARSABLE_LINE_COUNT_KEY
                ],
            )
            self.assertEqual(6, len(payload["mailbox"]["records"]))
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_MALFORMED
                    and len(entry["sha256"]) == 64
                    for entry in payload["omissions"]
                )
            )

    def test_the_size_cap_sheds_lifecycle_records_and_keeps_the_ruling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp), event_count=20000)

            payload = self._collect(*history, max_bytes=4 * 1024).payload
            kinds = [record["kind"] for record in payload["mailbox"]["records"]]

            self.assertEqual(
                [
                    mailbox_vocabulary.KIND_QUESTION,
                    mailbox_vocabulary.KIND_REPLY,
                    mailbox_vocabulary.KIND_NOTICE,
                ],
                kinds,
            )
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_SIZE_CAP
                    for entry in payload["omissions"]
                )
            )


if __name__ == "__main__":
    unittest.main()

# ruff: noqa: F403, F405
"""Assurance ledger storage and recovery tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_assurance_ledger_test_support import *


class PathTests(LedgerTestCase):
    def test_ledger_lock_and_index_are_state_root_siblings(self) -> None:
        # The state root is canonicalized, which on macOS turns a /var/folders
        # temporary directory into its /private/var/folders spelling.
        root = state.state_root(self.state_dir)
        self.assertEqual(root / "assurance-inbox.jsonl", self.ledger_path)
        self.assertEqual(
            root / "assurance-inbox.lock", ledger.ledger_lock_path(self.state_dir)
        )
        self.assertEqual(root / "assurance-index.json", self.index_path)

    def test_reading_an_absent_ledger_creates_nothing(self) -> None:
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual((), view.rejected)
        self.assertEqual(0, view.last_seq)
        self.assertFalse(self.state_dir.exists())


class AppendTests(LedgerTestCase):
    def test_first_append_writes_one_newline_terminated_line(self) -> None:
        record = ledger.append_issue_observation(
            _observation(), state_dir=self.state_dir
        )
        self.assertEqual(1, record.seq)
        self.assertEqual("ASI-1", record.issue_id)
        data = self.ledger_path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(1, len(data.splitlines()))

    def test_envelope_carries_version_kind_timestamp_and_payload(self) -> None:
        ledger.append_issue_observation(
            _observation(), state_dir=self.state_dir, recorded_at=TIMESTAMP
        )
        envelope = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(("version", "kind", "recorded_at", "issue_id", "payload")),
            sorted(envelope),
        )
        self.assertEqual(ledger.LEDGER_SCHEMA_VERSION, envelope["version"])
        self.assertEqual("issue_observed", envelope["kind"])
        self.assertEqual(TIMESTAMP, envelope["recorded_at"])
        self.assertEqual("ASI-1", envelope["issue_id"])
        self.assertEqual(_observation().to_dict(), envelope["payload"])

    def test_records_round_trip_through_the_file(self) -> None:
        observation = _observation()
        ledger.append_issue_observation(observation, state_dir=self.state_dir)
        ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        ledger.append_record(
            ledger.KIND_ISSUE_DISPOSITION,
            assurance_models_inbox.Disposition(
                issue_id="ASI-1", kind="resolve", recorded_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
        )
        view = self.read()
        self.assertEqual(
            ["issue_observed", "audit_pending", "issue_disposition"],
            [record.kind for record in view.records],
        )
        self.assertEqual(observation.to_dict(), view.records[0].payload.to_dict())
        self.assertEqual("ASI-1", view.records[0].issue_id)
        self.assertIsNone(view.records[1].issue_id)

    def test_every_record_kind_is_storable(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        entries = [
            (ledger.KIND_AUDIT_PENDING, _summary()),
            (
                ledger.KIND_AUDIT_COMPLETED,
                _summary(state="all-clear", run_id=OTHER_RUN_ID),
            ),
            (
                ledger.KIND_AUDIT_SKIPPED,
                _summary(state="skipped", eligible=False, reason="opted-out"),
            ),
            (
                ledger.KIND_AUDIT_UNAVAILABLE,
                _summary(state="unavailable", reason="no-independent-role"),
            ),
            (
                ledger.KIND_ISSUE_DISPOSITION,
                assurance_models_inbox.Disposition(
                    issue_id="ASI-1", kind="accept-risk", recorded_at=TIMESTAMP
                ),
            ),
            (
                ledger.KIND_ISSUE_MERGED,
                assurance_models_inbox.IssueMerge(
                    source_issue_id="ASI-1",
                    target_issue_id="ASI-2",
                    recorded_at=TIMESTAMP,
                ),
            ),
            (
                ledger.KIND_ISSUE_KEPT_DISTINCT,
                assurance_models_inbox.IssueKeptDistinct(
                    issue_ids=("ASI-1", "ASI-2"),
                    identity_digests=(DIGEST, OTHER_DIGEST),
                    recorded_at=TIMESTAMP,
                ),
            ),
            (
                ledger.KIND_HUMAN_CLOSEOUT,
                assurance_models_inbox.Closeout(
                    run_id=RUN_ID, outcome="accepted", recorded_at=TIMESTAMP
                ),
            ),
            (
                ledger.KIND_IMPROVEMENT_PLAN_DRAFTED,
                assurance_models_inbox.ImprovementPlanDraft(
                    issue_id="ASI-1",
                    harness="codex",
                    output_path="/tmp/improve-asi-1.md",
                    output_sha256=DIGEST,
                    drafted_at=TIMESTAMP,
                ),
            ),
        ]
        for kind, payload in entries:
            ledger.append_record(kind, payload, state_dir=self.state_dir)
        view = self.read()
        self.assertEqual((), view.rejected)
        self.assertEqual(set(ledger.LEDGER_KINDS), {r.kind for r in view.records})

    def test_issue_observed_cannot_be_appended_through_the_generic_writer(self) -> None:
        with self.assertRaises(LedgerError) as caught:
            ledger.append_record(
                ledger.KIND_ISSUE_OBSERVED, _observation(), state_dir=self.state_dir
            )
        self.assertEqual(ledger.ERROR_INVALID_KIND, caught.exception.reason)
        self.assertIn("append_issue_observation", str(caught.exception))
        self.assertFalse(self.ledger_path.exists())

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(LedgerError) as caught:
            ledger.append_record("audit_finished", _summary(), state_dir=self.state_dir)
        self.assertEqual(ledger.ERROR_INVALID_KIND, caught.exception.reason)

    def test_payload_type_must_match_the_kind(self) -> None:
        with self.assertRaises(LedgerError) as caught:
            ledger.append_record(
                ledger.KIND_HUMAN_CLOSEOUT, _summary(), state_dir=self.state_dir
            )
        self.assertEqual(ledger.ERROR_INVALID_PAYLOAD, caught.exception.reason)

    def test_audit_kind_must_agree_with_its_summary_state(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError) as caught:
            ledger.append_record(
                ledger.KIND_AUDIT_COMPLETED, _summary(), state_dir=self.state_dir
            )
        self.assertIn("requires assurance state", str(caught.exception))
        self.assertFalse(self.ledger_path.exists())

    def test_a_record_larger_than_the_cap_is_refused(self) -> None:
        artifacts = tuple(
            assurance_models_findings.EvidenceRef(
                kind="artifact",
                locator=f"turns/{index}/{'p' * 400}.json",
                detail="d" * assurance_models_vocab.MAX_TEXT_CHARS,
            )
            for index in range(assurance_models_vocab.MAX_EVIDENCE_ENTRIES)
        )
        with self.assertRaises(LedgerError) as caught:
            ledger.append_record(
                ledger.KIND_AUDIT_PENDING,
                _summary(artifacts=artifacts),
                state_dir=self.state_dir,
            )
        self.assertEqual(ledger.ERROR_RECORD_TOO_LARGE, caught.exception.reason)
        self.assertFalse(self.ledger_path.exists())

    def test_the_written_line_is_flushed_through_the_durability_barrier(self) -> None:
        synced: list[int] = []
        real = ledger.durability.fullsync

        def spy(fd: int) -> None:
            synced.append(os.fstat(fd).st_size)
            real(fd)

        with mock.patch.object(ledger.durability, "fullsync", spy):
            ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        self.assertEqual([self.ledger_path.stat().st_size], synced)

    def test_the_ledger_is_created_private_to_its_owner(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        self.assertEqual(0o600, self.ledger_path.stat().st_mode & 0o777)

    def test_append_never_rewrites_an_earlier_line(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        first_line = self.ledger_path.read_bytes()
        ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        self.assertTrue(self.ledger_path.read_bytes().startswith(first_line))


class SequenceTests(LedgerTestCase):
    def test_sequences_are_physical_line_positions(self) -> None:
        for index in range(3):
            ledger.append_issue_observation(
                _observation(finding=_finding(invariant_key=f"retry.key-{index}")),
                state_dir=self.state_dir,
            )
        view = self.read()
        self.assertEqual([1, 2, 3], [record.seq for record in view.records])
        self.assertEqual(3, view.last_seq)

    def test_a_rejected_line_keeps_its_sequence_and_the_next_append_follows(
        self,
    ) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json}\n")
        record = ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        self.assertEqual(3, record.seq)
        view = self.read()
        self.assertEqual([1, 3], [item.seq for item in view.records])
        self.assertEqual([2], [item.seq for item in view.rejected])
        self.assertEqual(3, view.last_seq)


class RejectionTests(LedgerTestCase):
    def test_each_malformed_shape_has_its_own_reason(self) -> None:
        valid = json.dumps(
            {
                "version": 1,
                "kind": "audit_pending",
                "recorded_at": TIMESTAMP,
                "issue_id": None,
                "payload": _summary().to_dict(),
            }
        )
        self.write_ledger_text(
            "\n".join(
                [
                    "   ",
                    "{oops",
                    "[1, 2, 3]",
                    json.dumps({"version": 99, "kind": "audit_pending"}),
                    json.dumps({"version": 1, "kind": "audit_finished"}),
                    json.dumps(
                        {
                            "version": 1,
                            "kind": "audit_pending",
                            "recorded_at": "not-a-time",
                            "payload": _summary().to_dict(),
                        }
                    ),
                    valid,
                ]
            )
            + "\n"
        )
        view = self.read()
        self.assertEqual(
            [
                ledger.REJECT_EMPTY_LINE,
                ledger.REJECT_INVALID_JSON,
                ledger.REJECT_NOT_OBJECT,
                ledger.REJECT_UNSUPPORTED_VERSION,
                ledger.REJECT_UNKNOWN_KIND,
                ledger.REJECT_INVALID_RECORD,
            ],
            [item.reason for item in view.rejected],
        )
        self.assertEqual([1, 2, 3, 4, 5, 6], [item.seq for item in view.rejected])
        self.assertEqual([7], [record.seq for record in view.records])

    def test_an_unexpected_envelope_key_is_rejected_not_ignored(self) -> None:
        self.write_ledger_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "audit_pending",
                    "recorded_at": TIMESTAMP,
                    "payload": _summary().to_dict(),
                    "sequence": 1,
                }
            )
            + "\n"
        )
        view = self.read()
        self.assertEqual((), view.records)
        self.assertIn("'sequence'", view.rejected[0].detail or "")

    def test_only_an_issue_observation_may_carry_an_issue_id(self) -> None:
        self.write_ledger_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "audit_pending",
                    "recorded_at": TIMESTAMP,
                    "issue_id": "ASI-7",
                    "payload": _summary().to_dict(),
                }
            )
            + "\n"
        )
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual(ledger.REJECT_INVALID_RECORD, view.rejected[0].reason)

    def test_a_mistyped_version_or_kind_is_rejected_not_raised(self) -> None:
        self.write_ledger_text(
            "\n".join(
                [
                    json.dumps({"version": [1], "kind": "audit_pending"}),
                    json.dumps({"version": 1.0, "kind": "audit_pending"}),
                    json.dumps({"version": 1, "kind": ["audit_pending"]}),
                    json.dumps({"version": 1, "kind": {}}),
                ]
            )
            + "\n"
        )
        self.assertEqual(
            [
                ledger.REJECT_UNSUPPORTED_VERSION,
                ledger.REJECT_UNSUPPORTED_VERSION,
                ledger.REJECT_UNKNOWN_KIND,
                ledger.REJECT_UNKNOWN_KIND,
            ],
            [item.reason for item in self.read().rejected],
        )

    def test_an_issue_id_too_long_to_number_is_rejected_not_raised(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        text = self.ledger_path.read_text(encoding="utf-8")
        self.write_ledger_text(text.replace('"ASI-1"', f'"ASI-{"9" * 5000}"'))
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual(
            [ledger.REJECT_INVALID_RECORD], [item.reason for item in view.rejected]
        )

    def test_an_over_deep_line_is_rejected_not_raised(self) -> None:
        self.write_ledger_text("[" * 1_000_000 + "]" * 1_000_000 + "\n")
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual(
            [ledger.REJECT_INVALID_JSON], [item.reason for item in view.rejected]
        )

    def test_rejected_raw_text_is_bounded(self) -> None:
        self.write_ledger_text("{" + "x" * (ledger.MAX_REJECTED_RAW_CHARS * 2) + "\n")
        rejected = self.read().rejected[0]
        self.assertEqual(ledger.MAX_REJECTED_RAW_CHARS + 1, len(rejected.raw))

    def test_rejected_lines_are_never_removed_by_a_later_append(self) -> None:
        self.write_ledger_text("{oops\n")
        ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        self.assertTrue(
            self.ledger_path.read_text(encoding="utf-8").startswith("{oops\n")
        )
        self.assertEqual(1, len(self.read().rejected))


class CrashRecoveryTests(LedgerTestCase):
    def _truncate_final_append(self) -> bytes:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        complete = self.ledger_path.read_bytes()
        partial = complete + b'{"version":1,"kind":"issue_obser'
        self.ledger_path.write_bytes(partial)
        return partial

    def test_a_partial_final_line_is_readable_history_not_a_read_failure(self) -> None:
        self._truncate_final_append()
        view = self.read()
        self.assertEqual(1, len(view.records))
        self.assertEqual(
            [(2, ledger.REJECT_INVALID_JSON)],
            [(item.seq, item.reason) for item in view.rejected],
        )

    def test_appending_after_a_partial_line_preserves_every_prior_byte(self) -> None:
        partial = self._truncate_final_append()
        record = ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        data = self.ledger_path.read_bytes()
        self.assertTrue(data.startswith(partial))
        self.assertEqual(partial + b"\n", data[: len(partial) + 1])
        self.assertEqual(3, record.seq)
        self.assertEqual(3, len(data.splitlines()))

    def test_the_repaired_line_stays_rejected_and_the_new_record_reads_back(
        self,
    ) -> None:
        self._truncate_final_append()
        ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        view = self.read()
        self.assertEqual([1, 3], [record.seq for record in view.records])
        self.assertEqual([2], [item.seq for item in view.rejected])

    def test_a_ledger_that_is_only_a_partial_line_still_accepts_appends(self) -> None:
        self.write_ledger_text('{"version":1,"kind":"audit_pen')
        record = ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        self.assertEqual(2, record.seq)
        view = self.read()
        self.assertEqual(1, len(view.records))
        self.assertEqual(1, len(view.rejected))

# ruff: noqa: F403, F405
"""Assurance ledger index-cache tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_assurance_ledger_test_support import *


class IndexTests(LedgerTestCase):
    def test_an_append_refreshes_the_cache_to_match_the_ledger(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertTrue(index.matches(self.ledger_path.read_bytes()))
        self.assertEqual(1, index.record_count)
        self.assertEqual(2, index.next_issue_number)
        self.assertEqual({_finding().fingerprint: "ASI-1"}, dict(index.issue_ids))

    def test_a_missing_cache_is_rebuilt_identically(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        before = ledger.read_index(state_dir=self.state_dir)
        self.index_path.unlink()
        after = ledger.read_index(state_dir=self.state_dir)
        self.assertTrue(self.index_path.exists())
        self.assertEqual(before.to_payload()["ledger_sha256"], after.ledger_sha256)
        self.assertEqual(dict(before.issue_ids), dict(after.issue_ids))
        self.assertEqual(before.next_issue_number, after.next_issue_number)

    def test_a_malformed_cache_is_rebuilt(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        self.index_path.write_text("this is not json", encoding="utf-8")
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertEqual(1, index.record_count)
        self.assertTrue(index.matches(self.ledger_path.read_bytes()))

    def test_an_over_deep_cache_is_rebuilt(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        self.index_path.write_text("[" * 1_000_000 + "]" * 1_000_000, encoding="utf-8")
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertEqual(1, index.record_count)

    def test_a_cache_from_another_version_is_rebuilt(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        payload["version"] = ledger.LEDGER_INDEX_VERSION + 1
        self.index_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(
            ledger.LEDGER_INDEX_VERSION,
            ledger.read_index(state_dir=self.state_dir).version,
        )

    def test_a_stale_cache_is_rebuilt_from_the_ledger(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        stale = json.loads(self.index_path.read_text(encoding="utf-8"))
        with self.ledger_path.open("ab") as handle:
            handle.write(
                json.dumps(
                    {
                        "version": 1,
                        "kind": "audit_pending",
                        "recorded_at": TIMESTAMP,
                        "issue_id": None,
                        "payload": _summary().to_dict(),
                    }
                ).encode("utf-8")
                + b"\n"
            )
        self.index_path.write_text(json.dumps(stale), encoding="utf-8")
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertEqual(2, index.record_count)
        self.assertEqual(2, index.last_seq)

    def test_losing_the_cache_does_not_change_the_ledger_truth(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        before = self.read()
        self.index_path.unlink()
        after = self.read()
        self.assertEqual(
            [(r.seq, r.kind, r.issue_id) for r in before.records],
            [(r.seq, r.kind, r.issue_id) for r in after.records],
        )
        self.assertEqual(before.next_issue_number, after.next_issue_number)

    def test_a_cache_that_cannot_be_written_never_fails_the_append(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        self.index_path.unlink()
        self.index_path.mkdir()
        (self.index_path / "occupied").write_text("blocked", encoding="utf-8")
        record = ledger.append_record(
            ledger.KIND_AUDIT_PENDING, _summary(), state_dir=self.state_dir
        )
        self.assertEqual(2, record.seq)
        self.assertEqual(2, len(self.read().records))
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertEqual(2, index.record_count)

    def test_a_cache_pointing_at_a_different_ledger_is_not_trusted(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        payload["issue_ids"] = {"deadbeef": "ASI-99"}
        payload["next_issue_number"] = 100
        payload["ledger_sha256"] = "f" * 64
        self.index_path.write_text(json.dumps(payload), encoding="utf-8")
        index = ledger.read_index(state_dir=self.state_dir)
        self.assertEqual(2, index.next_issue_number)
        self.assertEqual({_finding().fingerprint: "ASI-1"}, dict(index.issue_ids))

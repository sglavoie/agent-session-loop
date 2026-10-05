"""Corpus replay harness: bundle discovery, matrix capture, and drift diffs."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import audit_replay, supervisor_audit_checks

INVARIANT_KEYS = tuple(
    spec.invariant_key
    for spec in supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS
    if spec.replayable
)


def _matrix(
    rows: dict[str, dict[str, str]],
    keys: tuple[str, ...] = ("a", "b"),
    errors: dict[str, str] | None = None,
):
    return audit_replay.CorpusReplay(
        invariant_keys=keys, matrix=rows, errors=errors or {}
    )


class BundleDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name)

    def _write_bundle(self, plan: str, run_id: str, payload: object = None) -> Path:
        run_dir = self.state_root / plan / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        bundle = run_dir / "supervisor-audit-input.json"
        bundle.write_text(json.dumps(payload if payload is not None else {}))
        return bundle

    def test_every_recorded_bundle_is_found_in_run_order(self) -> None:
        self._write_bundle("plan-b", "20260812T000000000000Z-2")
        self._write_bundle("plan-a", "20260812T000000000000Z-1")
        self._write_bundle("plan-a", "20260813T000000000000Z-3")

        found = audit_replay.bundle_paths(self.state_root)

        self.assertEqual(
            [path.parent.name for path in found],
            [
                "20260812T000000000000Z-1",
                "20260812T000000000000Z-2",
                "20260813T000000000000Z-3",
            ],
        )

    def test_sibling_audit_artifacts_are_not_mistaken_for_bundles(self) -> None:
        bundle = self._write_bundle("plan-a", "20260812T000000000000Z-1")
        (bundle.parent / "supervisor-audit.json").write_text("{}")
        (self.state_root / "plan-a" / "state.json").write_text("{}")

        self.assertEqual(audit_replay.bundle_paths(self.state_root), [bundle])

    def test_replay_reports_one_status_per_invariant_key(self) -> None:
        bundle = self._write_bundle("plan-a", "20260812T000000000000Z-1")

        row = audit_replay.replay_bundle(bundle)

        self.assertEqual(tuple(row), INVARIANT_KEYS)
        self.assertLessEqual(set(row.values()), {"pass", "fail", "unavailable"})

    def test_corpus_matrix_is_keyed_by_run_id(self) -> None:
        self._write_bundle("plan-a", "20260812T000000000000Z-1")
        self._write_bundle("plan-b", "20260812T000000000000Z-2")

        replay = audit_replay.replay_corpus(self.state_root)

        self.assertEqual(
            sorted(replay.matrix),
            ["20260812T000000000000Z-1", "20260812T000000000000Z-2"],
        )
        self.assertEqual(replay.invariant_keys, INVARIANT_KEYS)
        self.assertEqual(replay.errors, {})

    def test_unreadable_bundle_is_recorded_without_abandoning_the_sweep(self) -> None:
        self._write_bundle("plan-a", "20260812T000000000000Z-1")
        truncated = self._write_bundle("plan-b", "20260812T000000000000Z-2")
        truncated.write_text('{"plan": ')

        replay = audit_replay.replay_corpus(self.state_root)

        self.assertEqual(list(replay.matrix), ["20260812T000000000000Z-1"])
        self.assertIn("20260812T000000000000Z-2", replay.errors)
        self.assertIn("JSONDecodeError", replay.errors["20260812T000000000000Z-2"])

    def test_replay_bundle_raises_a_named_error_for_unreadable_json(self) -> None:
        missing = self.state_root / "plan-a" / "runs" / "r1" / "input.json"

        with self.assertRaises(audit_replay.BundleUnreadable):
            audit_replay.replay_bundle(missing)

    def test_a_check_that_reads_outside_the_bundle_is_left_out_of_the_matrix(
        self,
    ) -> None:
        unreproducible = tuple(
            spec.invariant_key
            for spec in supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS
            if not spec.replayable
        )
        self.assertIn("identity.repository-intent", unreproducible)
        bundle = self._write_bundle("plan-a", "20260812T000000000000Z-1")

        row = audit_replay.replay_bundle(bundle)
        replay = audit_replay.replay_corpus(self.state_root)

        for key in unreproducible:
            self.assertNotIn(key, row)
            self.assertNotIn(key, replay.invariant_keys)

    def test_the_live_audit_still_runs_the_checks_replay_leaves_out(self) -> None:
        results = supervisor_audit_checks.run_deterministic_checks({})

        self.assertIn(
            "identity.repository-intent",
            {result.invariant_key for result in results},
        )


class MatrixStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_matrix_round_trips_through_its_stored_document(self) -> None:
        replay = audit_replay.CorpusReplay(
            invariant_keys=("a", "b"),
            matrix={"run-1": {"a": "pass", "b": "fail"}},
            errors={"run-2": "OSError: gone"},
        )
        path = self.root / "matrix.json"
        path.write_text(json.dumps(replay.payload()), encoding="utf-8")

        self.assertEqual(audit_replay.load_matrix(path), replay)

    def test_a_foreign_schema_version_is_refused(self) -> None:
        path = self.root / "matrix.json"
        payload = {
            "schema_version": audit_replay.MATRIX_SCHEMA_VERSION + 1,
            "invariant_keys": [],
            "matrix": {},
            "errors": {},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ValueError):
            audit_replay.load_matrix(path)


class MatrixDiffTests(unittest.TestCase):
    def test_identical_matrices_report_no_change(self) -> None:
        rows = {"run-1": {"a": "pass", "b": "fail"}}

        diff = audit_replay.diff_matrices(_matrix(rows), _matrix(dict(rows)))

        self.assertEqual(diff.changes, ())
        self.assertFalse(diff.has_changes)

    def test_each_moved_cell_is_named_and_counted_per_invariant_key(self) -> None:
        before = _matrix(
            {
                "run-1": {"a": "fail", "b": "pass"},
                "run-2": {"a": "fail", "b": "pass"},
            }
        )
        after = _matrix(
            {
                "run-1": {"a": "pass", "b": "pass"},
                "run-2": {"a": "pass", "b": "fail"},
            }
        )

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(
            diff.changes,
            (
                audit_replay.StatusChange("run-1", "a", "fail", "pass"),
                audit_replay.StatusChange("run-2", "a", "fail", "pass"),
                audit_replay.StatusChange("run-2", "b", "pass", "fail"),
            ),
        )
        self.assertTrue(diff.has_changes)
        self.assertEqual(
            diff.transitions(),
            {"a": {"fail->pass": 2}, "b": {"pass->fail": 1}},
        )

    def test_runs_recorded_after_the_baseline_are_listed_but_are_not_drift(
        self,
    ) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}})
        after = _matrix(
            {
                "run-1": {"a": "pass", "b": "pass"},
                "run-2": {"a": "fail", "b": "pass"},
            }
        )

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(diff.added_runs, ("run-2",))
        self.assertEqual(diff.changes, ())
        self.assertFalse(diff.has_changes)
        self.assertFalse(diff.incomplete)

    def test_pruned_runs_are_listed_but_are_not_drift(self) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}})
        after = _matrix({})

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(diff.removed_runs, ("run-1",))
        self.assertEqual(diff.unreadable_runs, ())
        self.assertFalse(diff.has_changes)
        self.assertFalse(diff.incomplete)

    def test_an_unreadable_baseline_run_is_lost_evidence_not_a_pruned_run(self) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}})
        after = _matrix({}, errors={"run-1": "JSONDecodeError: truncated"})

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(diff.unreadable_runs, ("run-1",))
        self.assertEqual(diff.removed_runs, ())
        self.assertFalse(diff.has_changes)
        self.assertTrue(diff.incomplete)

    def test_an_unreadable_run_the_baseline_never_judged_is_not_incomplete(
        self,
    ) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}})
        after = _matrix(
            {"run-1": {"a": "pass", "b": "pass"}},
            errors={"run-2": "OSError: gone"},
        )

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(diff.unreadable_runs, ())
        self.assertEqual(diff.errors, {"run-2": "OSError: gone"})
        self.assertFalse(diff.incomplete)

    def test_the_diff_payload_reports_lost_evidence_apart_from_drift(self) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}})
        after = _matrix({}, errors={"run-1": "JSONDecodeError: truncated"})

        payload = audit_replay.diff_matrices(before, after).payload()

        self.assertFalse(payload["has_changes"])
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["unreadable_runs"], ["run-1"])
        self.assertEqual(payload["errors"], {"run-1": "JSONDecodeError: truncated"})
        self.assertEqual(payload["removed_runs"], [])

    def test_a_new_or_dropped_invariant_key_is_drift(self) -> None:
        before = _matrix({"run-1": {"a": "pass", "b": "pass"}}, keys=("a", "b"))
        after = _matrix({"run-1": {"a": "pass", "c": "pass"}}, keys=("a", "c"))

        diff = audit_replay.diff_matrices(before, after)

        self.assertEqual(diff.added_keys, ("c",))
        self.assertEqual(diff.removed_keys, ("b",))
        self.assertEqual(diff.changes, ())
        self.assertTrue(diff.has_changes)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_root = self.root / "state"
        self.run_dir = self.state_root / "plan-a" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "supervisor-audit-input.json").write_text("{}")

    def _run(self, *argv: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = audit_replay.main(["--state-root", str(self.state_root), *argv])
        return code, stdout.getvalue()

    def test_capture_writes_a_loadable_matrix(self) -> None:
        output = self.root / "baseline.json"

        code, printed = self._run("capture", "--output", str(output))

        self.assertEqual(code, 0)
        self.assertIn("captured 1 runs", printed)
        self.assertEqual(list(audit_replay.load_matrix(output).matrix), ["run-1"])

    def test_capture_prints_the_matrix_when_no_output_is_given(self) -> None:
        code, printed = self._run("capture")

        self.assertEqual(code, 0)
        self.assertEqual(list(json.loads(printed)["matrix"]), ["run-1"])

    def test_diff_against_an_unchanged_corpus_succeeds(self) -> None:
        output = self.root / "baseline.json"
        self._run("capture", "--output", str(output))

        code, printed = self._run("diff", str(output))

        self.assertEqual(code, 0)
        self.assertIn("0 cell(s) changed", printed)

    def test_diff_reports_and_fails_on_a_moved_status(self) -> None:
        output = self.root / "baseline.json"
        self._run("capture", "--output", str(output))
        stored = json.loads(output.read_text(encoding="utf-8"))
        key = stored["invariant_keys"][0]
        stored["matrix"]["run-1"][key] = "fail"
        output.write_text(json.dumps(stored), encoding="utf-8")

        code, printed = self._run("diff", str(output))

        self.assertEqual(code, 1)
        self.assertIn(f"run-1  {key}  fail -> ", printed)
        self.assertIn("1 cell(s) changed", printed)

    def test_diff_json_output_carries_the_verdict(self) -> None:
        output = self.root / "baseline.json"
        self._run("capture", "--output", str(output))

        code, printed = self._run("--json", "diff", str(output))

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed)["has_changes"], False)

    def test_diff_fails_when_a_baseline_bundle_no_longer_reads(self) -> None:
        output = self.root / "baseline.json"
        self._run("capture", "--output", str(output))
        (self.run_dir / "supervisor-audit-input.json").write_text('{"plan": ')

        code, printed = self._run("diff", str(output))

        self.assertEqual(code, 1)
        self.assertIn("unreadable baseline run: run-1", printed)
        self.assertIn("comparison incomplete", printed)

    def test_diff_json_output_distinguishes_lost_evidence_from_no_drift(self) -> None:
        output = self.root / "baseline.json"
        self._run("capture", "--output", str(output))
        (self.run_dir / "supervisor-audit-input.json").write_text('{"plan": ')

        code, printed = self._run("--json", "diff", str(output))
        payload = json.loads(printed)

        self.assertEqual(code, 1)
        self.assertFalse(payload["has_changes"])
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["unreadable_runs"], ["run-1"])
        self.assertIn("JSONDecodeError", payload["errors"]["run-1"])


class WorktreeFidelityTests(unittest.TestCase):
    """The harness must report this tree, not whatever ``asl`` resolves to."""

    def test_the_harness_cannot_shell_out(self) -> None:
        source = Path(audit_replay.__file__).read_text(encoding="utf-8")

        for spawner in ("subprocess", "os.system", "os.exec", "os.popen"):
            self.assertNotIn(spawner, source, msg=f"harness may spawn via {spawner}")

    def test_checks_are_taken_from_the_imported_package(self) -> None:
        self.assertIs(
            audit_replay.supervisor_audit_checks.run_deterministic_checks,
            supervisor_audit_checks.run_deterministic_checks,
        )


if __name__ == "__main__":
    unittest.main()

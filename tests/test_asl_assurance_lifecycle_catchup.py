"""Pending audit catch-up of runs that ended without an audit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    assurance_catchup,
    assurance_ledger,
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_vocab,
    assurance_projection,
    events,
    plans_files,
    state,
    state_abandonment,
    state_assurance_pending,
)
from tests.support import (
    read_jsonl,
)
from tests.support_asl_assurance_lifecycle import (
    AFTER_FIX_TIMESTAMP,
    BEFORE_FIX_TIMESTAMP,
    FIX_TIMESTAMP,
    FIXED_CLOCK,
    RUN_ID,
    SEED_RUN_ID,
    SEED_TIMESTAMP,
    _all_clear_reply,
    _RunHistory,
    _ScriptedAuditor,
)


class PendingAuditCatchupTests(_RunHistory, unittest.TestCase):
    """A run whose process died still gets audited, later and out of band."""

    #: What a run that died still owing its audit records for itself.
    OWED = {"state": "pending", "eligible": True}

    def _abandon(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        assurance: dict[str, object] | None = OWED,
        run_config: dict[str, object] | None = None,
    ) -> None:
        """Give the recorded run the abandoned ending its own process never wrote.

        Passing ``assurance=None`` writes the state an ASL that had no auditing
        would have left behind.
        """
        workspace, plan_dir, state_dir, _ = history
        state_path = state_dir / events.STATE_FILE_NAME
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload.update(
            {
                "run_id": RUN_ID,
                "plan_dir": str(plans_files.canonicalize_path(plan_dir)),
                # Old enough that the no-lock grace window has elapsed, which
                # is what proves the run is gone rather than starting up.
                "run_started_at": "2026-08-10T10:11:12+00:00",
                "run_finished_at": None,
                "run_config": {
                    "cwd": str(workspace),
                    "harness": "codex",
                    "audit_harness": "claude",
                    "audit_timeout": 30,
                    **(run_config or {}),
                },
            }
        )
        if assurance is not None:
            payload["assurance"] = assurance
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                state_dir, lock_info=None, trigger="test"
            )
        )

    def _process(
        self,
        history: tuple[Path, Path, Path, Path],
        auditor: _ScriptedAuditor | None = None,
        **kwargs: object,
    ) -> assurance_catchup.CatchupReport:
        state_dir = history[2]
        return assurance_catchup.process_pending(
            state_dir=state_dir.parent,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=auditor or self._auditor(history),
            **kwargs,
        )

    def _auditor(self, history: tuple[Path, Path, Path, Path]) -> _ScriptedAuditor:
        bundle = self._bundle(history, terminal_pending=True)
        return _ScriptedAuditor(_all_clear_reply(bundle.evidence[0].locator))

    def test_an_abandoned_run_is_audited_after_the_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)

            report = self._process(history)

            self.assertEqual(1, report.candidate_count)
            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_AUDITED, result.outcome)
            self.assertEqual("all-clear", result.state, result.reason)
            view = assurance_ledger.read_ledger(state_dir=root)
            self.assertEqual(
                [assurance_ledger.KIND_AUDIT_COMPLETED],
                [record.kind for record in view.records],
            )

    def test_the_abandoned_outcome_survives_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_path = history[2] / events.STATE_FILE_NAME
            self._abandon(history)
            before = json.loads(state_path.read_text(encoding="utf-8"))

            self._process(history)

            after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_abandonment.ABANDONED_REASON, after["reason"])
            self.assertEqual(before, after)
            records = read_jsonl(history[2] / events.EVENTS_FILE_NAME)
            self.assertEqual(
                events.RUN_OUTCOME_ABANDONED, records[-1]["payload"]["outcome"]
            )

    def test_processing_twice_publishes_one_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._process(history)

            repeat = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_ALREADY_AUDITED,
                repeat.results[0].outcome,
            )
            view = assurance_ledger.read_ledger(state_dir=root)
            self.assertEqual(1, len(view.records))

    def test_a_run_another_pass_holds_is_reported_rather_than_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir = history[3]
            self._abandon(history)

            with assurance_catchup._claim(run_dir) as held:
                self.assertTrue(held)
                report = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_CLAIMED, report.results[0].outcome
            )
            self.assertEqual((), report.audited)

    def test_a_run_that_predates_auditing_is_reported_never_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history, assurance=None)

            report = self._process(history)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_SKIPPED, result.outcome)
            self.assertEqual(
                state_assurance_pending.ASSURANCE_SKIP_PREDATES, result.reason
            )
            self.assertEqual(
                0, len(assurance_ledger.read_ledger(state_dir=root).records)
            )

    def test_naming_the_run_overrides_its_recorded_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._abandon(history, assurance=None)

            report = self._process(history, run_id=RUN_ID)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def _publish_unavailable(self, history: tuple[Path, Path, Path, Path]) -> None:
        self._abandon(history)
        first = self._process(history, _ScriptedAuditor("not json", "still not"))
        self.assertEqual("unavailable", first.results[0].state)

    def test_naming_a_run_whose_only_audit_was_unavailable_audits_it_again(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._publish_unavailable(history)
            auditor = self._auditor(history)

            report = self._process(history, auditor, run_id=RUN_ID)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_AUDITED, result.outcome)
            self.assertEqual("all-clear", result.state, result.reason)
            self.assertEqual(1, len(auditor.prompts))
            self.assertTrue(result.archived_artifacts)
            view = assurance_projection.read_projection(state_dir=root)
            run = view.run(RUN_ID)
            assert run is not None
            self.assertEqual(
                ["unavailable", "all-clear"],
                [summary.state for summary in run.summaries],
            )

    def test_a_bulk_pass_leaves_an_unavailable_run_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._publish_unavailable(history)
            auditor = self._auditor(history)

            report = self._process(history, auditor)

            self.assertEqual(
                assurance_catchup.OUTCOME_ALREADY_AUDITED, report.results[0].outcome
            )
            self.assertEqual([], auditor.prompts)

    def test_naming_a_run_with_a_verdict_does_not_audit_it_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._abandon(history)
            self._process(history)
            auditor = self._auditor(history)

            report = self._process(history, auditor, run_id=RUN_ID)

            self.assertEqual(
                assurance_catchup.OUTCOME_ALREADY_AUDITED, report.results[0].outcome
            )
            self.assertEqual([], auditor.prompts)

    def _dispose(self, root: Path, kind: str, recorded_at: str) -> None:
        """Put one disposed issue in the ledger, decided at ``recorded_at``.

        Published under a run of its own so that seeding a generation boundary
        never doubles as the audit the run under test still owes.
        """
        assurance_projection.publish_audit(
            assurance_projection.AuditPublication(
                run_id=SEED_RUN_ID,
                state="findings",
                eligible=True,
                findings=(
                    assurance_models_findings.Finding(
                        category="lifecycle",
                        component="agent_session_loop/runner.py",
                        invariant_key="retry.budget-respected",
                        lane="lifecycle-retry-resume",
                        severity="high",
                        summary="A retry ran after the budget was exhausted.",
                        recommendation="Clamp the retry counter before relaunching.",
                        evidence=("events.jsonl",),
                    ),
                ),
                artifacts=(
                    assurance_models_findings.EvidenceRef(
                        kind="artifact",
                        locator="supervisor-audit-input.json",
                        digest="a" * 64,
                    ),
                ),
                completed_at=SEED_TIMESTAMP,
            ),
            state_dir=root,
            recorded_at=SEED_TIMESTAMP,
        )
        assurance_ledger.append_record(
            assurance_ledger.KIND_ISSUE_DISPOSITION,
            assurance_models_inbox.Disposition(
                issue_id="ASI-1", kind=kind, recorded_at=recorded_at
            ),
            state_dir=root,
        )

    def _date_record(
        self,
        history: tuple[Path, Path, Path, Path],
        when: str,
        *,
        started: str | None = None,
    ) -> None:
        """Date the pending record, which is otherwise stamped as the run dies.

        ``started`` defaults to ``when``: the run started and was found dead
        in the same generation.
        """
        path = assurance_catchup.record_path(history[3])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["recorded_at"] = when
        payload["run_started_at"] = started or when
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_a_run_older_than_the_newest_fix_is_left_to_the_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._date_record(history, BEFORE_FIX_TIMESTAMP)
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_SKIPPED, result.outcome)
            self.assertEqual(
                assurance_catchup.REASON_SUPERSEDED_GENERATION, result.reason
            )
            inbox = assurance_projection.read_projection(state_dir=root)
            self.assertIsNone(inbox.run(RUN_ID))

    def test_a_run_found_dead_after_the_fix_is_dated_by_its_start(self) -> None:
        """Detection can come long after the fix; the run's start dates it."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._date_record(
                history, AFTER_FIX_TIMESTAMP, started=BEFORE_FIX_TIMESTAMP
            )
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.REASON_SUPERSEDED_GENERATION,
                report.results[0].reason,
            )

    def test_a_record_without_a_start_is_dated_by_its_detection(self) -> None:
        """A record written before the start was carried keeps its old dating."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            path = assurance_catchup.record_path(history[3])
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("run_started_at")
            payload["recorded_at"] = BEFORE_FIX_TIMESTAMP
            path.write_text(json.dumps(payload), encoding="utf-8")
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.REASON_SUPERSEDED_GENERATION,
                report.results[0].reason,
            )

    def test_naming_a_superseded_run_audits_it_anyway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._date_record(history, BEFORE_FIX_TIMESTAMP)
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history, run_id=RUN_ID)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def test_a_run_newer_than_the_fix_is_still_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._date_record(history, AFTER_FIX_TIMESTAMP)
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def test_a_dismissal_does_not_start_a_new_generation(self) -> None:
        """Dismissing a defect leaves the code exactly as the audits found it."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            self._date_record(history, BEFORE_FIX_TIMESTAMP)
            self._dispose(root, "dismiss", FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def test_a_ledger_that_resolved_nothing_leaves_every_run_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._abandon(history)
            self._date_record(history, BEFORE_FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def test_an_undated_record_is_not_dropped_as_old(self) -> None:
        """A run that cannot be placed in a generation stays in the current one."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._abandon(history)
            path = assurance_catchup.record_path(history[3])
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("recorded_at")
            payload.pop("run_started_at")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self._dispose(root, "resolve", FIX_TIMESTAMP)

            report = self._process(history)

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )

    def test_a_run_with_no_recorded_debt_is_refused_rather_than_invented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            with self.assertRaises(assurance_catchup.CatchupError) as raised:
                self._process(history, run_id=RUN_ID)

            self.assertEqual(
                assurance_catchup.REASON_UNKNOWN_RUN, raised.exception.reason
            )

    def test_a_later_run_supersedes_the_evidence_and_stops_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_path = history[2] / events.STATE_FILE_NAME
            self._abandon(history)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["run_id"] = "20260811T000000000000Z-9999"
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            report = self._process(history)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_UNUSABLE, result.outcome)
            self.assertEqual(
                assurance_catchup.REASON_EVIDENCE_SUPERSEDED, result.reason
            )

    def test_the_limit_bounds_the_audits_a_pass_launches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._abandon(history)

            report = self._process(history, limit=1)

            self.assertEqual(1, len(report.audited))
            self.assertFalse(report.limit_reached)
            with self.assertRaises(assurance_catchup.CatchupError):
                self._process(history, limit=0)

    def test_a_run_that_died_mid_audit_is_re_audited_beside_its_leftovers(self) -> None:
        attempt_dir_name = f"{assurance_models_vocab.ATTEMPT_DIR_PREFIX}1"
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            run_dir = history[3]
            self._abandon(history)
            # What a runner killed after its own audit began leaves behind. The
            # canonical writers create exclusively, so without archiving these
            # the re-audit refuses and reports a no-op as a completed audit.
            (run_dir / assurance_models_vocab.AUDIT_INPUT_FILENAME).write_text(
                "{}", encoding="utf-8"
            )
            (run_dir / attempt_dir_name).mkdir()

            report = self._process(history)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_AUDITED, result.outcome)
            self.assertEqual("all-clear", result.state, result.reason)
            self.assertEqual(
                [assurance_models_vocab.AUDIT_INPUT_FILENAME, attempt_dir_name],
                list(result.archived_artifacts),
            )
            archive = run_dir / f"{assurance_catchup.ABANDONED_AUDIT_ARCHIVE_PREFIX}1"
            self.assertEqual(
                "{}",
                (archive / assurance_models_vocab.AUDIT_INPUT_FILENAME).read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((archive / attempt_dir_name).is_dir())
            self.assertTrue(
                (run_dir / assurance_models_vocab.AUDIT_REPORT_FILENAME).is_file()
            )
            view = assurance_ledger.read_ledger(state_dir=root)
            self.assertEqual(
                [assurance_ledger.KIND_AUDIT_COMPLETED],
                [record.kind for record in view.records],
            )

    def test_an_override_replaces_the_dead_run_s_own_audit_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._abandon(history, run_config={"audit_harness": "codex"})
            auditor = self._auditor(history)

            report = self._process(
                history,
                auditor,
                overrides=assurance_catchup.AuditOverrides(harness="claude"),
            )

            self.assertEqual(
                assurance_catchup.OUTCOME_AUDITED, report.results[0].outcome
            )
            self.assertEqual("claude", auditor.harnesses[0])


class PendingRecordReadTests(unittest.TestCase):
    """A damaged pending-audit record reads as absent rather than crashing."""

    def _record(self, root: Path, run_id: str, body: bytes) -> Path:
        plan_dir = root / "plan"
        plan_dir.mkdir(exist_ok=True)
        run_dir = state.plan_state_dir(plan_dir, root / "state") / "runs" / run_id
        run_dir.mkdir(parents=True)
        assurance_catchup.record_path(run_dir).write_bytes(body)
        return run_dir

    def test_a_record_that_is_not_utf8_reads_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._record(
                Path(tmp), "r1", b'{"run_id": "r1", "trigger": "\xff"}'
            )

            self.assertIsNone(assurance_catchup.read_pending_run(run_dir))

    def test_an_over_deep_record_reads_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._record(Path(tmp), "r1", b"[" * 1_000_000 + b"]" * 1_000_000)

            self.assertIsNone(assurance_catchup.read_pending_run(run_dir))

    def test_an_over_deep_plan_state_no_longer_describes_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._record(Path(tmp), "r1", b'{"run_id": "r1"}')
            pending = assurance_catchup.read_pending_run(run_dir)
            assert pending is not None
            (pending.state_dir / events.STATE_FILE_NAME).write_bytes(
                b"[" * 1_000_000 + b"]" * 1_000_000
            )

            self.assertFalse(assurance_catchup._state_still_describes(pending))

    def test_a_non_string_stamp_is_undated_and_still_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._record(Path(tmp), "r1", b'{"run_id": "r1", "recorded_at": 5}')
            self._record(
                Path(tmp),
                "r2",
                b'{"run_id": "r2", "recorded_at": "2026-08-10T10:11:12+00:00"}',
            )

            pending = assurance_catchup.iter_pending_runs(state_dir=Path(tmp) / "state")

            self.assertEqual(["r1", "r2"], [item.run_id for item in pending])
            self.assertIsNone(pending[0].recorded_at)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

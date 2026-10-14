"""Terminal audit artifacts, publication and degraded paths."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_ledger,
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_vocab,
    assurance_projection,
    events,
    state,
    supervisor_audit,
    supervisor_audit_agent,
    supervisor_audit_evidence_input,
    supervisor_audit_vocabulary,
)
from agent_session_loop.assurance_models_summary import AuditRole
from tests.support_asl_assurance_lifecycle import (
    FIXED_CLOCK,
    MUTATION_INVARIANT_KEY,
    RUN_ID,
    _all_clear_reply,
    _RunHistory,
    _ScriptedAuditor,
)


class TerminalAuditTests(_RunHistory, unittest.TestCase):
    """``audit_terminal_run`` produces artifacts, publishes, and never raises."""

    def _plan(self, *, enabled: bool = True, role: bool = True):
        config_values = supervisor_audit.AuditConfig(
            enabled=enabled, harness="claude" if role else None, timeout_seconds=30
        )
        if not enabled:
            return supervisor_audit.AuditPlan(
                config=config_values,
                role=None,
                state="skipped",
                reason=supervisor_audit.REASON_OPTED_OUT,
            )
        if not role:
            return supervisor_audit.AuditPlan(
                config=config_values,
                role=None,
                state="unavailable",
                reason=supervisor_audit.REASON_NO_INDEPENDENT_ROLE,
            )
        return supervisor_audit.AuditPlan(
            config=config_values,
            role=AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
            ),
            state="pending",
            reason=None,
        )

    def _audit(
        self,
        history: tuple[Path, Path, Path, Path],
        auditor: _ScriptedAuditor | None = None,
        *,
        plan: object = None,
    ):
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit.audit_terminal_run(
            self._plan() if plan is None else plan,
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            repositories=("app",),
            ledger_state_dir=state_dir.parent,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=auditor or _ScriptedAuditor("not json"),
        )

    def _auditor(self, history: tuple[Path, Path, Path, Path]) -> _ScriptedAuditor:
        bundle = self._bundle(history, terminal_pending=True)
        return _ScriptedAuditor(_all_clear_reply(bundle.evidence[0].locator))

    def _mutating_auditor(
        self, history: tuple[Path, Path, Path, Path]
    ) -> _ScriptedAuditor:
        """An auditor that writes into the workspace it may only read."""
        workspace = history[0]
        reply = _all_clear_reply(
            self._bundle(history, terminal_pending=True).evidence[0].locator
        )

        class _MutatingAuditor(_ScriptedAuditor):
            def __call__(self, name: str, **kwargs: object):
                (workspace / "app" / "sneaked.txt").write_text(
                    "written by the auditor\n", encoding="utf-8"
                )
                return super().__call__(name, **kwargs)

        return _MutatingAuditor(reply)

    def test_an_all_clear_audit_writes_every_canonical_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir = history[3]

            summary = self._audit(history, self._auditor(history))

            self.assertEqual("all-clear", summary.state, summary.reason)
            for name in assurance_models_vocab.CANONICAL_AUDIT_ARTIFACTS:
                self.assertTrue((run_dir / name).exists(), name)
            self.assertEqual(
                {
                    f"run/{name}"
                    for name in assurance_models_vocab.CANONICAL_AUDIT_ARTIFACTS
                },
                {reference.locator for reference in summary.artifacts},
            )

    def test_an_all_clear_audit_is_published_to_the_shared_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent

            summary = self._audit(history, self._auditor(history))

            view = assurance_ledger.read_ledger(state_dir=root)
            kinds = [record.kind for record in view.records]
            self.assertEqual([assurance_ledger.KIND_AUDIT_COMPLETED], kinds)
            self.assertEqual((), summary.issue_ids)
            self.assertEqual(FIXED_CLOCK.isoformat(), summary.started_at)

    def test_a_partial_audit_is_published_as_a_completed_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            bundle = self._bundle(history, terminal_pending=True)
            auditor = _ScriptedAuditor(
                _all_clear_reply(
                    bundle.evidence[0].locator,
                    unjudged=("resources-stalls", "unknown-anomalies"),
                )
            )

            summary = self._audit(history, auditor)

            view = assurance_ledger.read_ledger(state_dir=root)
            self.assertEqual("partial", summary.state, summary.reason)
            self.assertEqual(
                supervisor_audit_agent.REASON_LANE_EVIDENCE_ABSENT, summary.reason
            )
            self.assertEqual(
                [assurance_ledger.KIND_AUDIT_COMPLETED],
                [record.kind for record in view.records],
            )
            self.assertEqual((), summary.issue_ids)
            self.assertEqual((), assurance_projection.project(view).open_issues)

    def test_republishing_the_same_audit_leaves_one_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            self._audit(history, self._auditor(history))

            # The artifacts already exist, so the second pass degrades to
            # pending rather than writing a second verdict.
            repeat = self._audit(history, self._auditor(history))

            view = assurance_ledger.read_ledger(state_dir=root)
            self.assertEqual(1, len(view.records))
            self.assertEqual("pending", repeat.state)

    def test_a_run_with_no_worker_turn_is_still_audited(self) -> None:
        """No turn is not the same as nothing to audit: the run itself is."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp), turns=0)
            root = history[2].parent
            bundle = self._bundle(history, terminal_pending=True)
            auditor = _ScriptedAuditor(
                _all_clear_reply(
                    bundle.evidence[0].locator, unjudged=("mutation-commit-guards",)
                )
            )

            summary = self._audit(history, auditor)

            # The write guards are genuinely unjudgeable without a turn, so the
            # audit ran, reported the lane it could not judge, and published
            # what it did judge rather than skipping.
            self.assertEqual("partial", summary.state)
            self.assertEqual(
                supervisor_audit_agent.REASON_LANE_EVIDENCE_ABSENT, summary.reason
            )
            self.assertTrue(summary.artifacts)
            self.assertEqual(
                [assurance_ledger.KIND_AUDIT_COMPLETED],
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_an_opted_out_plan_neither_audits_nor_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor("never reached")

            summary = self._audit(history, auditor, plan=self._plan(enabled=False))

            self.assertEqual("skipped", summary.state)
            self.assertEqual(supervisor_audit.REASON_OPTED_OUT, summary.reason)
            self.assertEqual([], auditor.prompts)
            self.assertFalse(assurance_ledger.ledger_path(history[2].parent).exists())

    def test_a_run_without_an_independent_role_publishes_what_it_proved(self) -> None:
        """A missing auditor costs the semantic half, not the whole audit."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir, root = history[3], history[2].parent
            auditor = _ScriptedAuditor("never reached")

            summary = self._audit(history, auditor, plan=self._plan(role=False))

            self.assertEqual("unavailable", summary.state)
            self.assertEqual(
                supervisor_audit.REASON_NO_INDEPENDENT_ROLE, summary.reason
            )
            self.assertEqual([], auditor.prompts)
            for name in assurance_models_vocab.CANONICAL_AUDIT_ARTIFACTS:
                self.assertTrue((run_dir / name).exists(), name)
            self.assertEqual(
                [
                    assurance_ledger.KIND_AUDIT_UNAVAILABLE,
                    assurance_ledger.KIND_ISSUE_OBSERVED,
                    assurance_ledger.KIND_ISSUE_IDENTITY_BOUND,
                ],
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_an_agent_that_never_answers_still_publishes_the_checks(self) -> None:
        """The deterministic half is evidence whether or not the agent replied."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir, root = history[2], history[2].parent
            # The implementation harness reviewed its own work, which is a
            # finding no agent is needed to reach.
            payload = self._state(history[0])
            payload["review_harness"] = "codex"
            (state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )

            summary = self._audit(history, _ScriptedAuditor("not json", "still not"))

            self.assertEqual("unavailable", summary.state)
            view = assurance_ledger.read_ledger(state_dir=root)
            findings = [
                record
                for record in view.records
                if record.kind == assurance_ledger.KIND_ISSUE_OBSERVED
            ]
            self.assertTrue(summary.issue_ids)
            self.assertEqual(len(summary.issue_ids), len(findings))
            report = json.loads(
                (history[3] / assurance_models_vocab.AUDIT_REPORT_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(
                "review.independent-verification",
                {finding["invariant_key"] for finding in report["findings"]},
            )

    def test_repeated_unavailability_opens_one_system_issue(self) -> None:
        """The same reason class is one standing issue, not one per run."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, plan_dir, state_dir, _ = history
            root = state_dir.parent
            second = state_dir / "runs" / "20260810T101112123456Z-4343"

            first = self._audit(history, plan=self._plan(role=False))
            repeat = supervisor_audit.audit_terminal_run(
                self._plan(role=False),
                run_id="20260810T101112123456Z-4343",
                run_dir=second,
                state_dir=state_dir,
                plan_dir=plan_dir,
                workspace_root=workspace,
                repositories=("app",),
                ledger_state_dir=root,
                durable=False,
                now=lambda: FIXED_CLOCK,
            )

            self.assertEqual("unavailable", first.state)
            self.assertEqual("unavailable", repeat.state)
            self.assertEqual(first.issue_ids, repeat.issue_ids)
            self.assertEqual(0, repeat.new_issue_count)

    def test_a_collection_failure_still_names_something_to_open(self) -> None:
        """An unavailable audit that points at nothing is an unactionable issue."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir, root = history[3], history[2].parent

            def refuse(**kwargs: object):
                raise OSError("evidence unreadable")

            with mock.patch.object(
                supervisor_audit_evidence_input, "collect_audit_input", refuse
            ):
                summary = self._audit(history)

            self.assertEqual("unavailable", summary.state)
            self.assertEqual(
                supervisor_audit.REASON_EVIDENCE_UNAVAILABLE, summary.reason
            )
            self.assertEqual(
                [f"run/{supervisor_audit.UNAVAILABLE_ARTIFACT_NAME}"],
                [reference.locator for reference in summary.artifacts],
            )
            self.assertTrue(
                (run_dir / supervisor_audit.UNAVAILABLE_ARTIFACT_NAME).exists()
            )
            self.assertEqual(
                [
                    assurance_ledger.KIND_AUDIT_UNAVAILABLE,
                    assurance_ledger.KIND_ISSUE_OBSERVED,
                    assurance_ledger.KIND_ISSUE_IDENTITY_BOUND,
                ],
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_a_harness_that_cannot_enforce_the_profile_never_runs(self) -> None:
        """A weaker profile is not a fallback: the audit goes unavailable instead."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            auditor = _ScriptedAuditor("never reached")

            with mock.patch.object(
                supervisor_audit.harnesses,
                "audit_read_only_unavailable_reason",
                lambda name: "profile-unenforceable",
            ):
                summary = self._audit(history, auditor)

            self.assertEqual("unavailable", summary.state)
            self.assertEqual([], auditor.prompts)
            self.assertIn(
                assurance_ledger.KIND_ISSUE_OBSERVED,
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_an_auditor_that_touched_the_workspace_is_unavailable(self) -> None:
        """Activity invalidates the verdict without establishing who wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent

            summary = self._audit(history, self._mutating_auditor(history))

            self.assertEqual("unavailable", summary.state)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_CHANGED, summary.reason)
            self.assertIn(
                assurance_ledger.KIND_ISSUE_OBSERVED,
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_a_recorded_commit_does_not_hide_live_workspace_changes(self) -> None:
        """A pin the auditor cannot reach must never stand in for the live tree."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, _, run_dir = history
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace / "app",
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            commit_dir = run_dir / "commit-1"
            commit_dir.mkdir()
            (commit_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(
                    {
                        "commit_index": 1,
                        "repository_path": "app",
                        "head_after": head,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                supervisor_audit.repository_evidence,
                "add_detached_worktree",
                side_effect=AssertionError("audits must not create private pins"),
            ):
                summary = self._audit(history, self._mutating_auditor(history))

            self.assertEqual("unavailable", summary.state, summary.reason)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_CHANGED, summary.reason)
            self.assertTrue((workspace / "app" / "sneaked.txt").exists())

    def test_unattributed_activity_publishes_availability_not_a_critical_breach(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent

            summary = self._audit(history, self._mutating_auditor(history))

            projection = assurance_projection.read_projection(state_dir=root)
            self.assertEqual("unavailable", summary.state)
            self.assertEqual(
                ["workspace-changed"], [a.outcome for a in summary.attempts]
            )
            self.assertFalse(
                any(
                    i.finding.invariant_key == MUTATION_INVARIANT_KEY
                    for i in projection.issues
                )
            )
            self.assertTrue(summary.issue_ids)
            self.assertEqual(summary.issue_ids, projection.run(RUN_ID).issue_ids)
            for issue in projection.issues:
                self.assertNotEqual("critical", issue.finding.severity)
                self.assertIn("cannot identify who wrote", issue.finding.recommendation)
            outcome = json.loads(
                (history[3] / "supervisor-audit-attempt-1" / "outcome.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                ["sneaked.txt"],
                outcome["workspace_change"]["changed"]["app"]["untracked_added"],
            )
            self.assertEqual(1, len(summary.attempts))
            self.assertEqual(
                len(summary.issue_ids),
                summary.new_issue_count + summary.recurring_issue_count,
            )

    def test_snapshot_activity_does_not_reopen_a_historical_mutation_issue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            prior = assurance_ledger.append_issue_observation(
                assurance_models_inbox.IssueObservation(
                    run_id="20260809T101112123456Z-1111",
                    finding=assurance_models_findings.Finding(
                        category="audit-integrity",
                        component="supervisor-audit",
                        invariant_key=MUTATION_INVARIANT_KEY,
                        lane="mutation-commit-guards",
                        severity="critical",
                        summary="An earlier auditor changed repository evidence.",
                        recommendation="Confirm the read-only profile is enforced.",
                        evidence=(supervisor_audit_vocabulary.MUTATION_ARTIFACT_NAME,),
                    ),
                    observed_at="2026-08-09T10:11:12+00:00",
                    audit_artifact=supervisor_audit_vocabulary.MUTATION_ARTIFACT_NAME,
                ),
                state_dir=root,
            )
            assurance_ledger.append_record(
                assurance_ledger.KIND_ISSUE_DISPOSITION,
                assurance_models_inbox.Disposition(
                    issue_id=prior.issue_id,
                    kind="resolve",
                    recorded_at="2026-08-09T10:12:12+00:00",
                ),
                state_dir=root,
            )

            summary = self._audit(history, self._mutating_auditor(history))

            self.assertNotIn(prior.issue_id, summary.issue_ids)
            issue = assurance_projection.read_projection(state_dir=root).issue(
                prior.issue_id
            )
            self.assertEqual("resolved", issue.state)
            self.assertEqual(1, issue.occurrence_count)
            self.assertEqual(0, summary.recurring_issue_count)

    def test_a_failed_publication_leaves_no_report_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir = history[3]

            def refuse(*args: object, **kwargs: object):
                raise assurance_ledger.LedgerError("ledger-unwritable")

            with mock.patch.object(
                supervisor_audit.assurance_projection, "publish_audit", refuse
            ):
                summary = self._audit(history, self._auditor(history))

            self.assertEqual("pending", summary.state)
            # The reports never reached their canonical names, so the summary
            # names the bundle alone: an operator following a locator here would
            # otherwise be sent to a report this run decided not to publish.
            self.assertEqual(
                {f"run/{assurance_models_vocab.AUDIT_INPUT_FILENAME}"},
                {reference.locator for reference in summary.artifacts},
            )
            self.assertFalse(
                (run_dir / assurance_models_vocab.AUDIT_REPORT_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / assurance_models_vocab.AUDIT_MARKDOWN_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / supervisor_audit_agent.AUDIT_STAGING_DIR_NAME).exists()
            )

    def test_a_failed_append_takes_back_the_promoted_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir, run_dir = history[2], history[3]

            canonical = run_dir / assurance_models_vocab.AUDIT_REPORT_FILENAME
            promoted: list[bool] = []

            def refuse(*args: object, **kwargs: object):
                promoted.append(canonical.is_file())
                raise OSError("the ledger could not be extended")

            with mock.patch.object(assurance_ledger, "_append_locked", refuse):
                summary = self._audit(history, self._auditor(history))

            self.assertEqual("pending", summary.state)
            self.assertEqual([True], promoted)
            # Promotion had already run when the append failed, so the canonical
            # names existed for the length of the transaction and are given back.
            self.assertFalse(canonical.exists())
            self.assertFalse(
                (run_dir / assurance_models_vocab.AUDIT_MARKDOWN_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / supervisor_audit_agent.AUDIT_STAGING_DIR_NAME).exists()
            )
            projected = assurance_projection.read_projection(state_dir=state_dir)
            self.assertEqual([], list(projected.issues))
            self.assertEqual([], list(projected.runs))

    def test_an_unwritable_run_directory_degrades_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, plan_dir, state_dir, _ = history
            blocked = state_dir / "runs" / "not-a-directory"
            blocked.write_text("occupied\n", encoding="utf-8")

            summary = supervisor_audit.audit_terminal_run(
                self._plan(),
                run_id=RUN_ID,
                run_dir=blocked,
                state_dir=state_dir,
                plan_dir=plan_dir,
                workspace_root=workspace,
                repositories=("app",),
                ledger_state_dir=state_dir.parent,
                durable=False,
            )

            self.assertEqual("pending", summary.state)
            self.assertEqual((), summary.artifacts)

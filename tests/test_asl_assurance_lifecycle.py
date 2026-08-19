"""Terminalization: audit and publish one ending run, then finalize it."""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_catchup,
    assurance_ledger,
    assurance_models,
    assurance_projection,
    cli,
    config,
    control_queue,
    events,
    harnesses,
    plans,
    run_control,
    runner,
    state,
    supervisor_audit,
)
from agent_session_loop.assurance_models import RUBRIC_LANES, AuditRole
from tests.support import (
    init_git_repo,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_plan,
)

RUN_ID = "20260810T101112123456Z-4242"

#: Injected so an audit's timestamps are inputs rather than wall-clock reads.
FIXED_CLOCK = datetime(2026, 8, 10, 10, 11, 12, tzinfo=UTC)

#: Identity of the issue the audit's own read-only guard opens.
MUTATION_INVARIANT_KEY = "audit-read-only-mutation"

#: A run other than the one under test, so a seeded ledger issue is never
#: mistaken for the audit that run still owes.
SEED_RUN_ID = "20260810T101112999999Z-4243"
SEED_TIMESTAMP = "2026-08-09T00:00:00+00:00"

#: The moment a defect was declared fixed, and records either side of it.
FIX_TIMESTAMP = "2026-08-10T00:00:00+00:00"
BEFORE_FIX_TIMESTAMP = "2026-08-09T12:00:00+00:00"
AFTER_FIX_TIMESTAMP = "2026-08-11T00:00:00+00:00"


def _summary(state_name: str = "all-clear", **overrides: object):
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "state": state_name,
        "eligible": True,
    }
    values.update(overrides)
    return assurance_models.AssuranceSummary(**values)  # type: ignore[arg-type]


class TerminalOutcomeNamingTests(unittest.TestCase):
    """One ending, one name — and ``all-complete`` names two of them."""

    def test_all_complete_with_turns_is_a_completed_run(self) -> None:
        payload = {"reason": "all-complete", "turns": [{"name": "turn-0001-01"}]}

        self.assertEqual("complete", runner._terminal_outcome(payload))

    def test_all_complete_without_turns_is_a_no_work_run(self) -> None:
        payload = {"reason": "all-complete", "turns": []}

        self.assertEqual("no-work", runner._terminal_outcome(payload))

    def test_a_session_cap_keeps_its_own_reason(self) -> None:
        self.assertEqual(
            "max-sessions", runner._terminal_outcome({"reason": "max-sessions"})
        )

    def test_an_intentional_stop_keeps_its_own_reason(self) -> None:
        self.assertEqual("stopped", runner._terminal_outcome({"reason": "stopped"}))

    def test_a_pause_outranks_the_reason_it_recorded(self) -> None:
        payload = {"paused": True, "reason": "review-harness-required"}

        self.assertEqual("paused", runner._terminal_outcome(payload))

    def test_a_dry_run_is_named_apart_from_every_executed_ending(self) -> None:
        self.assertEqual("dry-run", runner._terminal_outcome({"reason": "dry-run"}))

    def test_an_unnamed_reason_still_resolves_to_an_audited_outcome(self) -> None:
        """A future cap-driven stop is audited by construction, not by listing."""
        outcome = runner._terminal_outcome({"reason": "max-pending-sessions"})

        self.assertEqual("max-pending-sessions", outcome)
        self.assertNotIn(outcome, runner._UNAUDITED_TERMINAL_OUTCOMES)


class _RunHistory:
    """One recorded run on disk, before its terminal record is written."""

    MANIFEST = (
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| app | app | https://example.invalid/app.git |\n"
    )

    def _history(self, tmp: Path, *, turns: int = 1) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state" / "planhash"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(self.MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        for index in range(1, turns + 1):
            turn_dir = run_dir / f"turn-{index:04d}-01"
            turn_dir.mkdir()
            (turn_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(self._result(workspace, turn_dir)), encoding="utf-8"
            )
            (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
            (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
            (turn_dir / "last-message.txt").write_text("Done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(self._state(workspace)), encoding="utf-8"
        )
        # The terminal record is deliberately absent: this is the history an
        # audit reads when it runs ahead of its own run's run_finished.
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            json.dumps(
                {
                    "name": "run_started",
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": "2026-08-10T10:11:12Z",
                    "payload": {"run_id": RUN_ID},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _result(self, workspace: Path, turn_dir: Path) -> dict[str, object]:
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "interrupted": False,
            "session_kind": "implementation",
            "artifact_dir": str(turn_dir),
            "stdout_path": str(turn_dir / "stdout.log"),
            "stderr_path": str(turn_dir / "stderr.log"),
            "last_message_path": str(turn_dir / "last-message.txt"),
            "result_path": str(turn_dir / state.RESULT_FILE_NAME),
            "invocation": {"name": "codex", "cwd": str(workspace / "app")},
            "workspace_write_guard": {
                "passed": True,
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {},
            },
            "verification": {
                "passed": True,
                "checks": [{"name": "tests", "passed": True, "returncode": 0}],
                "skipped_checks": [],
            },
        }

    def _state(self, workspace: Path) -> dict[str, object]:
        return {
            "harness": "codex",
            "model": "gpt-5",
            "ok": True,
            "paused": False,
            "workspace_root": str(workspace),
            "commit_mode": "session",
            "review_completed": True,
            "review_harness": "claude",
            "review_verdict": "approved",
            "repositories": [
                {
                    "alias": "app",
                    "path": "app",
                    "origin": "https://example.invalid/app.git",
                    "resolved_root": str(workspace / "app"),
                }
            ],
            "run_config": {"cwd": str(workspace)},
        }

    def _bundle(
        self, history: tuple[Path, Path, Path, Path], *, terminal_pending: bool
    ) -> supervisor_audit.AuditInput:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            terminal_pending=terminal_pending,
        )

    def _verdict(
        self, bundle: supervisor_audit.AuditInput, check: str
    ) -> assurance_models.DeterministicResult:
        for result in supervisor_audit.run_deterministic_checks(bundle):
            if result.check == check:
                return result
        raise AssertionError(f"no deterministic check named {check}")


class TerminalPendingEvidenceTests(_RunHistory, unittest.TestCase):
    """An audit running ahead of run_finished must not indict its own ordering."""

    def test_a_missing_terminal_record_is_a_defect_after_the_run_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)), terminal_pending=False)

            self.assertEqual("fail", self._verdict(bundle, "terminal-artifacts").status)
            self.assertEqual(
                "fail", self._verdict(bundle, "state-event-agreement").status
            )

    def test_a_terminal_record_still_owed_proves_nothing_either_way(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)), terminal_pending=True)

            terminal = self._verdict(bundle, "terminal-artifacts")
            agreement = self._verdict(bundle, "state-event-agreement")

            # ``pass``, not ``unavailable``: an unavailable lane would block the
            # all-clear every audited run is otherwise entitled to.
            self.assertEqual("pass", terminal.status, terminal.detail)
            self.assertEqual("pass", agreement.status, agreement.detail)

    def test_the_pending_flag_is_recorded_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            pending = self._bundle(history, terminal_pending=True)
            closed = self._bundle(history, terminal_pending=False)

            self.assertTrue(pending.payload["events"]["terminal_pending"])
            self.assertFalse(closed.payload["events"]["terminal_pending"])
            self.assertNotEqual(pending.sha256, closed.sha256)


class _ScriptedAuditor:
    """Stand-in auditor whose replies are consumed in order; the last repeats.

    ``interrupted`` returns what ``HarnessAdapter.run`` itself returns once it
    has absorbed a Ctrl+C: no exception escapes to the caller, only a result.
    """

    def __init__(self, *replies: str, interrupted: bool = False) -> None:
        self.replies = list(replies)
        self.interrupted = interrupted
        self.prompts: list[str] = []
        self.harnesses: list[str] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        self.harnesses.append(name)
        access = kwargs["access"]
        assert isinstance(access, harnesses.AccessProfile)
        argv = harnesses.get_adapter(name).build_argv(
            cwd=Path(str(kwargs["cwd"])),
            plan_dir=None,
            prompt=prompt,
            last_message_path=Path(str(kwargs["last_message_path"])),
            access=access,
        )
        index = min(len(self.prompts), len(self.replies)) - 1
        return harnesses.HarnessResult(
            argv=argv,
            returncode=130 if self.interrupted else 0,
            stdout="",
            stderr="",
            last_message=self.replies[index],
            name=name,
            interrupted=self.interrupted,
        )


class _AuditorAbortedMidTurn(_ScriptedAuditor):
    """An auditor an operator aborts while its turn is in flight.

    ``enqueue`` stands in for the second ``asl control abort``: it appends the
    command the finalization drain reads, so the kill this returns is the one
    the runner's own poll armed rather than one the test staged directly.
    """

    def __init__(self, enqueue: Callable[[], None], *replies: str) -> None:
        super().__init__(*replies)
        self._enqueue = enqueue

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        signal = kwargs["abort"]
        assert isinstance(signal, harnesses.AbortSignal)
        self._enqueue()
        deadline = time.monotonic() + 10.0
        while not signal.armed:
            if time.monotonic() > deadline:
                raise AssertionError("the finalization drain never armed the abort")
            time.sleep(0.01)
        return dataclasses.replace(
            super().__call__(name, **kwargs), returncode=137, aborted=True
        )


def _fast_finalization() -> Callable[..., runner._LiveFinalization]:
    """``_LiveFinalization`` whose control drain ticks at test speed."""
    live = runner._LiveFinalization

    def build(**kwargs: object) -> runner._LiveFinalization:
        kwargs.setdefault("poll_interval", 0.01)
        return live(**kwargs)  # type: ignore[arg-type]

    return build


def _all_clear_reply(locator: str, *, unjudged: tuple[str, ...] = ()) -> str:
    body = json.dumps(
        {
            "schema_version": supervisor_audit.AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": lane,
                    "status": "unavailable" if lane in unjudged else "pass",
                    "summary": f"{lane} judged from the recorded evidence",
                    "evidence": [locator],
                }
                for lane in RUBRIC_LANES
            ],
            "findings": [],
        }
    )
    return f"```json\n{body}\n```\n"


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
            for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
                self.assertTrue((run_dir / name).exists(), name)
            self.assertEqual(
                {f"run/{name}" for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS},
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
                supervisor_audit.REASON_LANE_EVIDENCE_ABSENT, summary.reason
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
                supervisor_audit.REASON_LANE_EVIDENCE_ABSENT, summary.reason
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
            for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
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
                (history[3] / assurance_models.AUDIT_REPORT_FILENAME).read_text(
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

            with mock.patch.object(supervisor_audit, "collect_audit_input", refuse):
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
        """A read-only turn that mutated anything has disproved its own verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent

            summary = self._audit(history, self._mutating_auditor(history))

            self.assertEqual("unavailable", summary.state)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_MUTATED, summary.reason)
            self.assertIn(
                assurance_ledger.KIND_ISSUE_OBSERVED,
                [
                    record.kind
                    for record in assurance_ledger.read_ledger(state_dir=root).records
                ],
            )

    def test_a_run_that_committed_is_audited_away_from_the_live_workspace(self) -> None:
        """Writes the audit could not have caused are not charged to it."""
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

            summary = self._audit(history, self._mutating_auditor(history))

            self.assertEqual("all-clear", summary.state, summary.reason)
            self.assertTrue((workspace / "app" / "sneaked.txt").exists())

    def test_a_detected_mutation_is_named_by_the_summary_of_its_own_run(self) -> None:
        """The run's most severe issue has to be copyable from the run itself."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent

            summary = self._audit(history, self._mutating_auditor(history))

            projection = assurance_projection.read_projection(state_dir=root)
            mutation = next(
                issue
                for issue in projection.issues
                if issue.finding.invariant_key == MUTATION_INVARIANT_KEY
            )
            self.assertEqual("critical", mutation.finding.severity)
            self.assertIn(mutation.issue_id, summary.issue_ids)
            self.assertIn(mutation.issue_id, projection.run(RUN_ID).issue_ids)
            # One sighting, written by the guard: the publication links the
            # issue rather than observing the same mutation a second time.
            self.assertEqual(1, len(mutation.occurrences))
            self.assertEqual(
                len(summary.issue_ids),
                summary.new_issue_count + summary.recurring_issue_count,
            )
            self.assertEqual(0, summary.recurring_issue_count)

    def test_a_mutation_issue_an_earlier_run_opened_counts_as_recurring(self) -> None:
        """Linked issues are counted by who opened them, exactly like minted ones."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            prior = assurance_ledger.append_issue_observation(
                assurance_models.IssueObservation(
                    run_id="20260809T101112123456Z-1111",
                    finding=assurance_models.Finding(
                        category="audit-integrity",
                        component="supervisor-audit",
                        invariant_key=MUTATION_INVARIANT_KEY,
                        lane="mutation-commit-guards",
                        severity="critical",
                        summary="An earlier auditor changed repository evidence.",
                        recommendation="Confirm the read-only profile is enforced.",
                        evidence=(supervisor_audit.MUTATION_ARTIFACT_NAME,),
                    ),
                    observed_at="2026-08-09T10:11:12+00:00",
                    audit_artifact=supervisor_audit.MUTATION_ARTIFACT_NAME,
                ),
                state_dir=root,
            )

            summary = self._audit(history, self._mutating_auditor(history))

            self.assertIn(prior.issue_id, summary.issue_ids)
            self.assertEqual(1, summary.recurring_issue_count)
            self.assertEqual(
                len(summary.issue_ids),
                summary.new_issue_count + summary.recurring_issue_count,
            )

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
                {f"run/{assurance_models.AUDIT_INPUT_FILENAME}"},
                {reference.locator for reference in summary.artifacts},
            )
            self.assertFalse(
                (run_dir / assurance_models.AUDIT_REPORT_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / assurance_models.AUDIT_MARKDOWN_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / supervisor_audit.AUDIT_STAGING_DIR_NAME).exists()
            )

    def test_a_failed_append_takes_back_the_promoted_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir, run_dir = history[2], history[3]

            canonical = run_dir / assurance_models.AUDIT_REPORT_FILENAME
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
                (run_dir / assurance_models.AUDIT_MARKDOWN_FILENAME).exists()
            )
            self.assertFalse(
                (run_dir / supervisor_audit.AUDIT_STAGING_DIR_NAME).exists()
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


class TerminalizationCoordinatorTests(unittest.TestCase):
    """Every ending routes through one coordinator that changes no verdict."""

    def _payload(self, tmp_dir: Path, tokens: list[str]) -> dict[str, object]:
        plan_dir = tmp_dir / "plan"
        write_plan(plan_dir, ("pending",))
        args = cli.build_parser().parse_args(["run", str(plan_dir), *tokens])
        plan_state = state.plan_state_dir(plan_dir, tmp_dir / "state")
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_info = state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans.canonicalize_path(plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        return runner._initial_payload(
            args=args,
            snapshot=plans.inspect_plan(plan_dir),
            state_dir=plan_state,
            run_dir=plan_state / "runs" / RUN_ID,
            event_path=plan_state / events.EVENTS_FILE_NAME,
            state_path=plan_state / events.STATE_FILE_NAME,
            lock_info=lock_info,
            cwd=tmp_dir,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=runner._mailbox_activation_payload(args, plan_dir),
        )

    def _context(self, payload: dict[str, object]) -> runner._TerminalAuditContext:
        state_path = Path(str(payload["state_path"]))
        return runner._TerminalAuditContext(
            plan=supervisor_audit.AuditPlan(
                config=supervisor_audit.AuditConfig(harness="claude"),
                role=AuditRole(
                    harness="claude",
                    implementation_harness="codex",
                    source="explicit",
                    timeout_seconds=30.0,
                ),
                state="pending",
                reason=None,
            ),
            run_dir=state_path.parent / "runs" / RUN_ID,
            state_dir=state_path.parent,
            plan_dir=Path(str(payload["plan_dir"])),
            workspace_root=state_path.parent,
            # Never the default root: reading the operator's real inbox would
            # make these assertions depend on the machine they run on.
            ledger_state_dir=state_path.parent.parent / "ledger",
        )

    def _terminalize(
        self,
        payload: dict[str, object],
        *,
        outcome: str,
        exit_code: int,
        summary: object = None,
    ) -> tuple[int, list[dict[str, object]]]:
        calls: list[dict[str, object]] = []

        def record(plan: object, **kwargs: object):
            calls.append(dict(kwargs))
            return _summary() if summary is None else summary

        with mock.patch.object(runner.supervisor_audit, "audit_terminal_run", record):
            code = runner._terminalize_run(
                outcome=outcome,
                exit_code=exit_code,
                event_path=Path(str(payload["events_path"])),
                payload=payload,
                audit_context=self._context(payload),
            )
        return code, calls

    def test_finalizing_is_reachable_only_through_the_coordinator(self) -> None:
        """Structural, because commit failures and interrupts are hard to stage.

        Every persisted ending — including the ones no fixture here reaches —
        gets audited exactly when finalizing has no other way in.
        """
        tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call.func, ast.Name) and call.func.id == "_finish_run"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
        }

        self.assertEqual({"_terminalize_run"}, callers)

    def test_the_exit_code_survives_the_coordinator_unchanged(self) -> None:
        for exit_code in (0, 1, runner.INTERRUPTED_EXIT):
            with (
                self.subTest(exit_code=exit_code),
                tempfile.TemporaryDirectory() as tmp,
            ):
                payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

                code, calls = self._terminalize(
                    payload, outcome="complete", exit_code=exit_code
                )

                self.assertEqual(exit_code, code)
                self.assertEqual(1, len(calls))

    def test_the_terminal_event_carries_the_bounded_assurance_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary("findings", issue_ids=("ASI-1",), new_issue_count=1),
            )

            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])
            assurance = record["payload"]["assurance"]
            self.assertEqual("findings", assurance["state"])
            self.assertEqual(["ASI-1"], assurance["issue_ids"])
            self.assertNotIn("findings", assurance)

    def test_the_terminal_event_carries_the_additive_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary(
                    "findings",
                    issue_ids=("ASI-1",),
                    new_issue_count=1,
                    artifacts=(
                        assurance_models.EvidenceRef(
                            kind="artifact", locator="run/supervisor-audit.json"
                        ),
                    ),
                ),
            )

            report = read_jsonl(Path(str(payload["events_path"])))[-1]["payload"][
                "assurance_report"
            ]
            self.assertEqual(["ASI-1"], report["issue_ids"])
            self.assertEqual(1, report["new_issue_count"])
            self.assertEqual(0, report["recurring_issue_count"])
            self.assertFalse(report["partial"])
            self.assertEqual(0, report["open_issue_count"])
            self.assertEqual("asl assurance list", report["list_command"])
            self.assertEqual(
                str(Path(str(payload["run_dir"])) / "supervisor-audit.json"),
                report["report_path"],
            )
            self.assertEqual(report, payload["assurance_report"])

    def test_a_partial_verdict_is_reported_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary(
                    "unavailable",
                    reason="timeout",
                    issue_ids=("ASI-1",),
                    new_issue_count=1,
                ),
            )

            self.assertTrue(payload["assurance_report"]["partial"])

    def test_an_unaudited_ending_carries_no_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(payload, outcome="dry-run", exit_code=0)

            self.assertIsNone(payload.get("assurance_report"))
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertIsNone(record["payload"]["assurance_report"])

    def test_the_audit_precedes_the_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            event_path = Path(str(payload["events_path"]))
            seen: list[list[str]] = []

            def record(plan: object, **kwargs: object):
                seen.append(
                    [item["name"] for item in read_jsonl(event_path)]
                    if event_path.exists()
                    else []
                )
                return _summary()

            with mock.patch.object(
                runner.supervisor_audit, "audit_terminal_run", record
            ):
                runner._terminalize_run(
                    outcome="complete",
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    audit_context=self._context(payload),
                )

            self.assertEqual([[events.FINALIZATION_STARTED_EVENT]], seen)
            self.assertEqual(
                [
                    events.FINALIZATION_STARTED_EVENT,
                    events.FINALIZATION_FINISHED_EVENT,
                    events.RUN_FINISHED_EVENT,
                ],
                [item["name"] for item in read_jsonl(event_path)],
            )

    def test_an_unaudited_ending_finalizes_without_launching_an_auditor(self) -> None:
        for outcome in sorted(runner._UNAUDITED_TERMINAL_OUTCOMES):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

                code, calls = self._terminalize(payload, outcome=outcome, exit_code=0)

                self.assertEqual(0, code)
                self.assertEqual([], calls)
                record = read_jsonl(Path(str(payload["events_path"])))[-1]
                self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])

    def test_a_run_without_an_independent_role_is_still_audited(self) -> None:
        """``unavailable`` is a starting point the audit can still improve on."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), [])
            self.assertEqual("unavailable", payload["assurance"]["state"])

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual(1, len(calls))
            self.assertEqual("all-clear", payload["assurance"]["state"])

    def test_an_opted_out_run_launches_no_checks_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--no-supervisor-audit"])

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual([], calls)
            self.assertEqual("skipped", payload["assurance"]["state"])

    def test_a_run_that_already_holds_a_verdict_is_not_audited_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            payload["assurance"] = _summary(
                "findings", issue_ids=("ASI-1",), new_issue_count=1
            ).to_dict()

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual([], calls)
            self.assertEqual("findings", payload["assurance"]["state"])

    def test_an_interrupted_audit_cancels_the_audit_and_nothing_else(self) -> None:
        """The outcome and the exit code were decided before the audit ran."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            def interrupt(plan: object, **kwargs: object):
                raise KeyboardInterrupt

            with mock.patch.object(
                runner.supervisor_audit, "audit_terminal_run", interrupt
            ):
                code = runner._terminalize_run(
                    outcome="paused",
                    exit_code=runner.INTERRUPTED_EXIT,
                    event_path=Path(str(payload["events_path"])),
                    payload=payload,
                    audit_context=self._context(payload),
                )

            self.assertEqual(runner.INTERRUPTED_EXIT, code)
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            # Terminalization still completed: the run has its terminal record.
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])
            self.assertEqual("unavailable", record["payload"]["assurance"]["state"])

    def test_an_interrupted_audit_turn_launches_no_second_attempt(self) -> None:
        """The production path: the harness absorbs the Ctrl+C and returns."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            auditor = _ScriptedAuditor("", interrupted=True)

            with mock.patch.object(harnesses, "run_harness", auditor):
                code = runner._terminalize_run(
                    outcome="complete",
                    exit_code=1,
                    event_path=Path(str(payload["events_path"])),
                    payload=payload,
                    audit_context=self._context(payload),
                )

            self.assertEqual(1, code)
            self.assertEqual(1, len(auditor.prompts))
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            self.assertEqual(
                ["interrupted"],
                [attempt["outcome"] for attempt in assurance["attempts"]],
            )
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])

    def test_the_finalization_phase_is_published_while_the_audit_runs(self) -> None:
        """Otherwise a run holding the lock for an audit looks like a hang."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            event_path = Path(str(payload["events_path"]))
            state_path = Path(str(payload["state_path"]))
            live: list[dict[str, object]] = []

            def record(plan: object, **kwargs: object):
                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                live.append(
                    {
                        "finalization": persisted["active_finalization"],
                        "events": [item["name"] for item in read_jsonl(event_path)],
                    }
                )
                return _summary()

            with mock.patch.object(
                runner.supervisor_audit, "audit_terminal_run", record
            ):
                runner._terminalize_run(
                    outcome="stopped",
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    audit_context=self._context(payload),
                )

            self.assertEqual([events.FINALIZATION_STARTED_EVENT], live[0]["events"])
            published = live[0]["finalization"]
            assert isinstance(published, dict)
            self.assertEqual(RUN_ID, published["run_id"])
            self.assertEqual(
                runner.FINALIZATION_PHASE_ASSURANCE_AUDIT, published["phase"]
            )
            # No cancel signal was supplied, so this phase advertises itself as
            # visible but not cancellable.
            self.assertFalse(published["cancellable"])

            records = read_jsonl(event_path)
            self.assertEqual(
                [
                    events.FINALIZATION_STARTED_EVENT,
                    events.FINALIZATION_FINISHED_EVENT,
                    events.RUN_FINISHED_EVENT,
                ],
                [item["name"] for item in records],
            )
            self.assertFalse(records[1]["payload"]["cancelled"])
            self.assertIsNone(payload["active_finalization"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(persisted["active_finalization"])

    def test_an_abort_while_finalizing_cancels_the_audit_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self._payload(tmp_dir, ["--audit-harness", "claude"])
            payload["reason"] = "stopped"
            event_path = Path(str(payload["events_path"]))
            plan_dir = Path(str(payload["plan_dir"]))
            context = self._context(payload)
            abort = harnesses.AbortSignal()
            persistence = runner._RunPersistence()
            mailbox_poll = runner._MailboxPoll(
                plan_dir=plan_dir,
                state_dir_arg=str(tmp_dir / "state"),
                control_state=run_control.RunControlState(current_harness="codex"),
                seen_control_ids=set(),
                payload=payload,
                event_path=event_path,
                control_payload=dict(payload["control"]),
                persistence=persistence,
                run_control_lock=threading.Lock(),
                enabled=False,
            )
            mailbox_poll.set_abort(abort, finalization=True)

            def enqueue_abort() -> None:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_REQUEST_ABORT_TURN,
                    state_dir=tmp_dir / "state",
                    command_id="abort-during-finalization",
                )

            auditor = _AuditorAbortedMidTurn(enqueue_abort, "never read")
            with (
                mock.patch.object(harnesses, "run_harness", auditor),
                mock.patch.object(runner, "_LiveFinalization", _fast_finalization()),
            ):
                code = runner._terminalize_run(
                    outcome="stopped",
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    audit_context=context,
                    cancel=abort,
                    poll=mailbox_poll,
                )

            self.assertEqual(0, code)
            self.assertEqual(1, len(auditor.prompts))
            actions = [
                item["payload"]["action"]
                for item in read_jsonl(event_path)
                if item["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertIn(events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED, actions)
            # Cancelling the audit cancels only the audit.
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            self.assertEqual([], assurance["attempts"])
            ledger = assurance_ledger.read_ledger(state_dir=context.ledger_state_dir)
            self.assertEqual((), ledger.records)
            self.assertEqual("stopped", payload["reason"])
            self.assertTrue(payload["run_finished_at"])
            self.assertIsNone(payload["active_finalization"])
            finished = [
                item
                for item in read_jsonl(event_path)
                if item["name"] == events.FINALIZATION_FINISHED_EVENT
            ]
            self.assertTrue(finished[0]["payload"]["cancelled"])
            self.assertEqual(
                events.RUN_FINISHED_EVENT, read_jsonl(event_path)[-1]["name"]
            )

    def test_the_ledger_root_is_kept_apart_from_the_plan_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            context = self._context(payload)

            _, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(context.state_dir, calls[0]["state_dir"])
            self.assertEqual(context.ledger_state_dir, calls[0]["ledger_state_dir"])
            self.assertNotEqual(context.state_dir, calls[0]["ledger_state_dir"])


class TerminalBranchRoutingTests(unittest.TestCase):
    """Each successful and intentional ending reaches the coordinator once."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}), encoding="utf-8"
        )

    def _run(
        self,
        statuses: tuple[str, ...],
        *tokens: str,
        mode: str = "success",
        audit_state: str = "all-clear",
        allow_not_loop_ready: bool = True,
    ) -> tuple[int, list[dict[str, object]], dict[str, object]]:
        write_plan(self.plan_dir, statuses)
        argv = [
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--audit-harness",
            "claude",
            "--state-dir",
            str(self.state_dir),
            *(["--allow-not-loop-ready"] if allow_not_loop_ready else []),
            "--json",
            *tokens,
        ]
        calls: list[dict[str, object]] = []

        def record(plan: object, **kwargs: object):
            calls.append(dict(kwargs))
            reason = (
                {"reason": supervisor_audit.REASON_EVIDENCE_UNAVAILABLE}
                if audit_state == "unavailable"
                else {}
            )
            return _summary(audit_state, run_id=str(kwargs["run_id"]), **reason)

        out, err = io.StringIO(), io.StringIO()
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                FAKE_CODEX_MODE=mode,
                FAKE_CODEX_SLEEP="5",
            ),
            mock.patch.object(runner.supervisor_audit, "audit_terminal_run", record),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = runner.run_from_args(
                cli.build_parser().parse_args(argv), run_argv=argv
            )
        return code, calls, json.loads(out.getvalue())

    def _assurance(self) -> dict[str, object]:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        finished = [
            record for record in records if record["name"] == events.RUN_FINISHED_EVENT
        ]
        self.assertEqual(1, len(finished))
        return finished[-1]["payload"]["assurance"]

    def test_a_completed_run_is_audited_once_and_keeps_its_exit_code(self) -> None:
        code, calls, payload = self._run(("pending",))

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_run_with_no_pending_work_is_audited_once(self) -> None:
        code, calls, payload = self._run(("complete",))

        self.assertEqual(0, code, payload)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(0, len(payload["turns"]))
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_session_capped_run_is_audited_once(self) -> None:
        code, calls, payload = self._run(("pending", "pending"), "--max-sessions", "1")

        self.assertEqual(0, code, payload)
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_intentional_stop_is_audited_once(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-1",
        )

        code, calls, payload = self._run(("pending", "pending"))

        self.assertEqual(0, code, payload)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_paused_run_is_audited_once_and_keeps_its_pause_reason(self) -> None:
        code, calls, payload = self._run(("pending",), mode="runner-pause")

        self.assertEqual(1, code, payload)
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_failed_turn_is_audited_once_and_keeps_its_exit_code(self) -> None:
        code, calls, payload = self._run(("pending",), mode="failure")

        self.assertEqual(1, code, payload)
        self.assertEqual("nonzero-exit", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_exhausted_retry_budget_is_audited_once(self) -> None:
        code, calls, payload = self._run(("pending",), mode="no-progress")

        self.assertEqual(1, code, payload)
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_refusal_before_the_loop_is_audited_once(self) -> None:
        """A refusal ends a run as truly as the loop's own exits do."""
        code, calls, payload = self._run(("pending",), allow_not_loop_ready=False)

        self.assertEqual(1, code, payload)
        self.assertEqual("not-loop-ready", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_unavailable_audit_changes_nothing_it_graded(self) -> None:
        """Auditing is advisory: a verdict it could not reach costs the run nothing."""
        code, calls, payload = self._run(
            ("pending",), mode="runner-pause", audit_state="unavailable"
        )

        # Identical to the audited pause above, verdict apart.
        self.assertEqual(1, code, payload)
        self.assertEqual("runner-pause", payload["reason"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertTrue(payload["paused"])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["pause_message"])
        self.assertEqual(1, len(calls))
        self.assertEqual("unavailable", self._assurance()["state"])

    def test_an_opted_out_run_reaches_the_coordinator_without_auditing(self) -> None:
        code, calls, payload = self._run(("pending",), "--no-supervisor-audit")

        self.assertEqual(0, code, payload)
        self.assertEqual([], calls)
        self.assertEqual("skipped", self._assurance()["state"])

    def test_a_dry_run_reaches_the_coordinator_without_auditing(self) -> None:
        code, calls, payload = self._run(("pending",), "--dry-run")

        self.assertEqual(0, code, payload)
        self.assertEqual("dry-run", payload["reason"])
        self.assertEqual([], calls)
        self.assertEqual("skipped", payload["assurance"]["state"])


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
                "plan_dir": str(plans.canonicalize_path(plan_dir)),
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
            state.mark_run_abandoned(state_dir, lock_info=None, trigger="test")
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
            self.assertEqual(state.ABANDONED_REASON, after["reason"])
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
            self.assertEqual(state.ASSURANCE_SKIP_PREDATES, result.reason)
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
                    assurance_models.Finding(
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
                    assurance_models.EvidenceRef(
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
            assurance_models.Disposition(
                issue_id="ASI-1", kind=kind, recorded_at=recorded_at
            ),
            state_dir=root,
        )

    def _date_record(self, history: tuple[Path, Path, Path, Path], when: str) -> None:
        """Date the pending record, which is otherwise stamped as the run dies."""
        path = assurance_catchup.record_path(history[3])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["recorded_at"] = when
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
        attempt_dir_name = f"{assurance_models.ATTEMPT_DIR_PREFIX}1"
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            root = history[2].parent
            run_dir = history[3]
            self._abandon(history)
            # What a runner killed after its own audit began leaves behind. The
            # canonical writers create exclusively, so without archiving these
            # the re-audit refuses and reports a no-op as a completed audit.
            (run_dir / assurance_models.AUDIT_INPUT_FILENAME).write_text(
                "{}", encoding="utf-8"
            )
            (run_dir / attempt_dir_name).mkdir()

            report = self._process(history)

            result = report.results[0]
            self.assertEqual(assurance_catchup.OUTCOME_AUDITED, result.outcome)
            self.assertEqual("all-clear", result.state, result.reason)
            self.assertEqual(
                [assurance_models.AUDIT_INPUT_FILENAME, attempt_dir_name],
                list(result.archived_artifacts),
            )
            archive = run_dir / f"{assurance_catchup.ABANDONED_AUDIT_ARCHIVE_PREFIX}1"
            self.assertEqual(
                "{}",
                (archive / assurance_models.AUDIT_INPUT_FILENAME).read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((archive / attempt_dir_name).is_dir())
            self.assertTrue(
                (run_dir / assurance_models.AUDIT_REPORT_FILENAME).is_file()
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

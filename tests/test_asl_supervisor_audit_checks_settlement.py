"""Deterministic checks for commit settlement and turn results."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    events,
    plans_metadata,
    state,
    supervisor_audit_checks,
    supervisor_audit_evidence,
    supervisor_audit_vocabulary,
)
from agent_session_loop.assurance_models import (
    RUBRIC_LANES,
    DeterministicResult,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support import init_git_repo
from tests.support_supervisor_audit import RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _statuses(self, results: dict[str, DeterministicResult]) -> dict[str, str]:
        return {check: result.status for check, result in results.items()}

    def _settlement_record(self, **overrides: Any) -> dict[str, Any]:
        """One ``paused_work_commit`` record, shaped like the settlement's own."""
        record: dict[str, Any] = {
            "guard": "paused-work-commit",
            "pause_reason": "verify-failed",
            "commit_mode": "session",
            "result": "committed",
            "commit_group": {"group_id": "group-paused-1"},
            "commit": None,
            "error": None,
            "residue": {"repositories": []},
        }
        record.update(overrides)
        return record

    def _settlement_epilogue(
        self,
        *,
        group_id: str = "group-paused-1",
        scope: str = commit_groups.GROUP_SCOPE_SESSION_PAUSED,
        closed: bool = True,
        record: dict[str, Any] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """The records a paused run's settlement writes after its terminal record.

        Shaped like what ``commit_settlement._settle_paused_retained_work``
        emits: the scope lives on the group, and the commit records carry the
        group id and the ``scope: "session"`` every session commit is stamped
        with.
        """
        epilogue: list[tuple[str, dict[str, Any]]] = [
            (
                events.COMMIT_GROUP_STARTED_EVENT,
                {
                    "run_id": RUN_ID,
                    "phase": "started",
                    "group": {
                        "group_id": group_id,
                        "scope": scope,
                        "session_id": 1,
                    },
                },
            ),
            (
                events.COMMIT_STARTED_EVENT,
                {"run_id": RUN_ID, "group_id": group_id, "scope": "session"},
            ),
            (
                events.COMMIT_FINISHED_EVENT,
                {
                    "run_id": RUN_ID,
                    "group_id": group_id,
                    "scope": "session",
                    "success": True,
                },
            ),
        ]
        if closed:
            epilogue.append(
                (
                    events.PAUSED_WORK_COMMITTED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "session_id": 1,
                        **(self._settlement_record() if record is None else record),
                    },
                )
            )
        return epilogue

    def _paused_run(
        self,
        history: tuple[Path, Path, Path, Path],
        epilogue: list[tuple[str, dict[str, Any]]],
        *,
        mirror: object = ...,
        unresolved: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record a paused run whose state agrees with the epilogue it wrote.

        ``run_loop`` mirrors the settlement record into state under
        ``paused_work_commit`` in the same locked section that appends the
        event, so a fixture that writes one without the other describes a run
        that died between the two. Passing ``mirror=None`` is how a test asks
        for exactly that run.
        """
        payload = self._read_state(history[2])
        resolved = self._settlement_record() if mirror is ... else mirror
        if resolved is not None:
            payload[supervisor_audit_evidence.PAUSED_WORK_COMMIT_KEY] = resolved
        if unresolved is not None:
            payload["unresolved_commit_groups"] = unresolved
        self._write_state(history[2], payload)
        self._write_events(
            history[2],
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.RUN_PAUSED_EVENT,
                    {"run_id": RUN_ID, "reason": "verify-failed"},
                ),
                *epilogue,
            ],
        )

    def _shed_start_epilogue(self, *, closed: bool) -> list[tuple[str, dict[str, Any]]]:
        """A settlement whose ``commit_group_started`` record the cap shed.

        The group is still described by its surviving lifecycle records and by
        the ``paused_work_committed`` close, which carry the scope on the
        group's identity the way ``run_commit`` writes it.
        """
        group = {
            "group_id": "group-paused-1",
            "identity": {"scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED},
        }
        epilogue = self._settlement_epilogue(
            closed=closed, record=self._settlement_record(commit_group=group)
        )
        del epilogue[0]
        return epilogue

    def test_the_settlement_vocabulary_is_mirrored_from_its_writer(self) -> None:
        # `supervisor_audit` cannot import `commit_settlement` without dragging
        # the commit ledger and the loop's reporting into every audit, so the
        # key and the outcome set are literals there. This is what keeps the
        # copies from drifting apart silently.
        self.assertEqual(
            commit_settlement.PAUSED_WORK_COMMIT_KEY,
            supervisor_audit_evidence.PAUSED_WORK_COMMIT_KEY,
        )
        source = inspect.getsource(commit_settlement._settle_paused_retained_work)
        for result in supervisor_audit_vocabulary._PAUSED_WORK_RESULTS:
            self.assertIn(f'result = "{result}"', source)

    def test_a_clean_run_passes_every_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle, results = self._audit(self._history(Path(tmp)))

            self.assertEqual(
                {check: "pass" for check in results}, self._statuses(results)
            )
            self.assertEqual(
                {lane: "pass" for lane in RUBRIC_LANES},
                supervisor_audit_checks.lane_rollup(results.values()),
            )
            self.assertEqual(
                (), supervisor_audit_checks.findings_from_results(results.values())
            )
            self.assertTrue(bundle.evidence)

    def test_resource_check_distinguishes_recovered_and_unresolved_hangs(self) -> None:
        recovery = {
            "policy": "verified_leaf_sigterm",
            "attempted_at": "2026-08-20T12:00:00+00:00",
            "signal": "SIGTERM",
            "candidate_pids": [5001],
            "signaled_pids": [5001],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }
        block = {
            "reason": "result_received_process_alive",
            "detected_at": "2026-08-20T12:00:00+00:00",
            "recovery": recovery,
        }
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir = history[2]
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_HANG_SUSPECTED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (
                        events.TURN_HANG_CLEARED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _bundle, recovered_results = self._audit(history)
            recovered = recovered_results["resource-anomalies"]
            self.assertEqual("pass", recovered.status)
            assert recovered.detail is not None
            self.assertIn(
                "1 post-result hang(s) recovered automatically", recovered.detail
            )

            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_HANG_SUSPECTED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )
            _bundle, unresolved_results = self._audit(history)
            unresolved = unresolved_results["resource-anomalies"]
            self.assertEqual("fail", unresolved.status)
            assert unresolved.detail is not None
            self.assertIn("suspected hang(s) were never cleared", unresolved.detail)

    def test_legacy_single_repository_pass_is_described_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = history[0] / "app"
            (history[1] / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (history[1] / "session-01.md").rename(history[1] / "session-1.md")
            (history[1] / "session-1.md").write_text("# Session 1\n", encoding="utf-8")
            (history[1] / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
                json.dumps({"version": 1, "cwd": str(repository)}),
                encoding="utf-8",
            )

            _, results = self._audit(history, workspace_root=repository)

            verdict = results["repository-intent"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("legacy plan stays within", verdict.detail)
            self.assertNotIn("declares a repository registry", verdict.detail)

    def test_a_workspace_relative_registry_path_uses_its_resolved_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])

            self.assertEqual("app", state_payload["repositories"][0]["path"])
            _, results = self._audit(history)

            self.assertEqual("pass", results["root-registry-agreement"].status)

    def test_a_registry_reaching_outside_the_workspace_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            init_git_repo(root / "kiln")
            payload = self._read_state(history[2])
            payload["repositories"].append(
                {
                    "alias": "kiln",
                    "path": "../kiln",
                    "origin": "https://example.invalid/kiln.git",
                    "resolved_root": str(root / "kiln"),
                }
            )
            self._write_state(history[2], payload)

            _, results = self._audit(history)
            finding = supervisor_audit_checks.findings_from_results(results.values())

            self.assertEqual("fail", results["root-registry-agreement"].status)
            self.assertIn("kiln", results["root-registry-agreement"].detail)
            self.assertEqual(
                ["identity.root-registry-agreement"],
                [item.invariant_key for item in finding],
            )
            self.assertEqual("critical", finding[0].severity)
            self.assertEqual(
                "fail",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "identity-authority"
                ],
            )

    def test_state_and_events_that_disagree_are_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.RUN_PAUSED_EVENT,
                        {"run_id": RUN_ID, "reason": "verify-failed"},
                    ),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["state-event-agreement"].status)
            self.assertIn("pause", results["state-event-agreement"].detail)
            self.assertIn(
                "observability.state-event-agreement",
                [
                    item.invariant_key
                    for item in supervisor_audit_checks.findings_from_results(
                        results.values()
                    )
                ],
            )
            # The pause was never classified, so the lifecycle lane reports what
            # it could not see rather than reading the silence as a clean run.
            self.assertEqual("unavailable", results["retry-resume"].status)

    def test_the_settlement_of_a_pause_is_not_work_after_the_terminal_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(history, self._settlement_epilogue())

            _, results = self._audit(history)

            # The settlement runs downstream of the `run_paused` record it
            # settles for by design, so its commits are post-terminal on every
            # run that settles anything.
            self.assertEqual("pass", results["terminal-artifacts"].status)
            self.assertIn(
                "the settlement of the work its pause retained",
                results["terminal-artifacts"].detail,
            )

    def test_a_settlement_that_died_mid_commit_is_still_work_after_the_end(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # No `paused_work_committed` record closes the block, so the run
            # died between opening the group and recording what it committed.
            self._paused_run(history, self._settlement_epilogue(closed=False))

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "2 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_settlement_whose_start_was_shed_is_identified_by_its_close(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(history, self._shed_start_epilogue(closed=True))

            _, results = self._audit(history)

            self.assertEqual("pass", results["terminal-artifacts"].status)
            self.assertIn(
                "the settlement of the work its pause retained",
                results["terminal-artifacts"].detail,
            )

    def test_a_settlement_identified_by_its_group_update_still_needs_a_close(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            epilogue = self._shed_start_epilogue(closed=False)
            epilogue.insert(
                0,
                (
                    events.COMMIT_GROUP_UPDATED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "phase": "updated",
                        "group": {
                            "group_id": "group-paused-1",
                            "identity": {
                                "scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED
                            },
                        },
                    },
                ),
            )
            self._paused_run(history, epilogue)

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "2 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_post_terminal_commit_outside_the_settlement_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # A closed settlement exempts its own group and nothing else: an
            # end-of-run group that kept committing after the pause is the
            # unaccounted work this check exists to catch.
            epilogue = self._settlement_epilogue()
            epilogue.insert(
                3,
                (
                    events.COMMIT_FINISHED_EVENT,
                    {"run_id": RUN_ID, "group_id": "group-run-1", "scope": "run"},
                ),
            )
            self._paused_run(history, epilogue)

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "1 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_session_scoped_group_earns_no_settlement_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The exemption is keyed on the group's own `session-paused` scope.
            # An ordinary session group that opened after the terminal record
            # is a run that kept working, whatever else follows it.
            self._paused_run(
                history,
                self._settlement_epilogue(scope=commit_groups.GROUP_SCOPE_SESSION),
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "2 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

    def test_a_settled_pause_is_graded_rather_than_left_to_the_catch_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(history, self._settlement_epilogue())

            _, results = self._audit(history)

            self.assertEqual("pass", results["paused-work-settlement"].status)
            self.assertIn(
                "1 paused-work settlement(s) recorded an outcome ASL writes",
                results["paused-work-settlement"].detail,
            )
            # The record carries the pause's reason, which is why the catch-all
            # read every successful settlement as an unclassified failure. It
            # is exempt now because the check above owns it.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_run_that_settled_nothing_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, results = self._audit(self._history(Path(tmp)))

            self.assertEqual("pass", results["paused-work-settlement"].status)
            self.assertIn(
                "left no paused work behind",
                results["paused-work-settlement"].detail,
            )

    def test_a_settlement_outcome_asl_never_writes_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="partially-committed")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "'partially-committed', which is no settlement outcome ASL writes",
                results["paused-work-settlement"].detail,
            )

    def test_a_failed_settlement_must_record_why_and_stay_owed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="failed")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            verdict = results["paused-work-settlement"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("failed without recording why", verdict.detail)
            self.assertIn("owing no paused-work commit group", verdict.detail)

    def test_a_failed_settlement_that_accounted_for_itself_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The reason may be written as the caught exception or as the
            # underlying commit's own outcome; `run_reporting` reads the pair.
            record = self._settlement_record(
                result="failed", commit={"status_result": "dirty-after-commit"}
            )
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
                unresolved=[
                    {
                        "group_id": "group-paused-1",
                        "scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                        "session_id": 1,
                        "status": commit_groups.GROUP_STATUS_FAILED,
                    }
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["paused-work-settlement"].status)
            # The loss is still reported, by the check that owns it.
            self.assertEqual("fail", results["commit-group-resolution"].status)

    def test_a_clean_settlement_that_left_residue_contradicts_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(
                result="committed", residue={"repositories": [{"alias": "app"}]}
            )
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "reported a clean commit while naming 1 repository(ies) it left dirty",
                results["paused-work-settlement"].detail,
            )

    def test_a_residue_settlement_must_name_where_the_residue_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            record = self._settlement_record(result="committed-with-residue")
            self._paused_run(
                history,
                self._settlement_epilogue(record=record),
                mirror=record,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "reported residue while naming no repository it was left in",
                results["paused-work-settlement"].detail,
            )

    def test_a_settlement_state_never_mirrored_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The append and the write happen under one lock, so a record with
            # no mirror is a run that died between them -- and every reader but
            # this check reads the mirror.
            self._paused_run(history, self._settlement_epilogue(), mirror=None)

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "was never mirrored into the run's state",
                results["paused-work-settlement"].detail,
            )

    def test_a_mirror_that_disagrees_with_the_record_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_run(
                history,
                self._settlement_epilogue(),
                mirror=self._settlement_record(result="no-changes"),
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["paused-work-settlement"].status)
            self.assertIn(
                "recorded 'committed' while state mirrors 'no-changes'",
                results["paused-work-settlement"].detail,
            )

    def test_a_turn_without_a_result_is_never_a_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[3] / "turn-0001-01" / state.RESULT_FILE_NAME).unlink()

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(state.RESULT_FILE_NAME, results["terminal-artifacts"].detail)
            self.assertEqual("unavailable", results["mutation-commit-guards"].status)
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertNotIn(
                "pass",
                {
                    results["terminal-artifacts"].status,
                    results["mutation-commit-guards"].status,
                    results["scope-escape"].status,
                },
            )


if __name__ == "__main__":
    unittest.main()

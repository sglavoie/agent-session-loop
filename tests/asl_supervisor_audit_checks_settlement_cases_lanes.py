# mypy: ignore-errors
from .asl_supervisor_audit_checks_settlement_support import *


class DeterministicCheckLaneTests(SettlementCheckBase):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def test_the_settlement_vocabulary_is_mirrored_from_its_writer(self) -> None:
        # `supervisor_audit` cannot import `commit_settlement` without dragging
        # the commit ledger and the loop's reporting into every audit, so the
        # key and the outcome set are literals there. This is what keeps the
        # copies from drifting apart silently.
        self.assertEqual(
            commit_settlement.PAUSED_WORK_COMMIT_KEY,
            supervisor_audit_evidence_constants.PAUSED_WORK_COMMIT_KEY,
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

    def test_a_commit_record_naming_its_group_by_a_list_is_not_a_settlement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            epilogue = self._settlement_epilogue()
            epilogue.append(
                (
                    events.COMMIT_STARTED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "group_id": ["group-paused-1"],
                        "scope": "session",
                    },
                )
            )
            self._paused_run(history, epilogue)

            _, results = self._audit(history)

            # A group id that is not a string names no settlement group, so
            # the record is work after the end rather than an unhashable crash.
            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(
                "1 record(s) of work follow the run's terminal record",
                results["terminal-artifacts"].detail,
            )

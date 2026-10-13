"""File and regex checks, gate runs, and observer events of run_verification."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_session_loop import (
    contracts,
    plans,
    verification,
)
from tests.support_asl_verification import VerificationExecutorFixtures


class RunVerificationAndObserverTests(VerificationExecutorFixtures):
    def test_file_exists_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.FileExistsCheck(name="absent", path="nope.txt")

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)
            self.assertIn("nope.txt", result.detail)

    def test_regex_in_file_check_passes_when_pattern_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text(
                "def parse_completion_contract():\n    pass\n",
                encoding="utf-8",
            )
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)

    def test_regex_in_file_check_fails_when_pattern_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text("nothing\n", encoding="utf-8")
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("regex-not-matched", result.failure_reason)
            self.assertIn("module.py", result.detail)

    def test_regex_in_file_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="missing.py",
                pattern=r"anything",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)

    def test_run_verification_aggregates_all_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.FileExistsCheck(name="absent", path="absent.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertEqual("1", result.session_id.value)
            self.assertEqual(3, len(result.checks))
            self.assertFalse(result.passed)
            failure_names = {failure.name for failure in result.failures}
            self.assertEqual({"absent"}, failure_names)

    def test_run_verification_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertTrue(result.passed)
            self.assertEqual((), result.failures)

    def test_passing_gate_skips_conditional_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("ok\n", encoding="utf-8")
            marker = repo_root / "diagnostic-ran"
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="present.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=(
                            "python3",
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                        repo="OAK",
                    ),
                )
            )
            skipped_events: list[verification.CheckSkippedEvent] = []
            observer = verification.VerificationObserver(
                on_check_skipped=skipped_events.append
            )

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["suite"], [check.name for check in result.checks])
            self.assertEqual(
                ["focused"], [check.name for check in result.skipped_checks]
            )
            self.assertFalse(marker.exists())
            self.assertEqual(1, len(skipped_events))
            self.assertEqual(("suite",), skipped_events[0].on_failure_of)
            self.assertEqual("condition-not-met", skipped_events[0].reason)
            self.assertEqual("OAK", skipped_events[0].repo)

    def test_failing_gate_runs_conditional_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            marker = repo_root / "diagnostic-ran"
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="missing.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=(
                            "python3",
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertFalse(result.passed)
            self.assertTrue(marker.exists())
            self.assertEqual(
                ["suite", "focused"], [check.name for check in result.checks]
            )
            self.assertEqual(["suite"], [check.name for check in result.failures])
            self.assertEqual((), result.diagnostic_failures)
            self.assertEqual((), result.skipped_checks)

    def test_diagnostic_failure_is_reported_but_is_not_a_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="missing.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=("python3", "-c", "raise SystemExit(3)"),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertFalse(result.passed)
            self.assertEqual(["suite"], [check.name for check in result.failures])
            self.assertEqual(
                ["focused"], [check.name for check in result.diagnostic_failures]
            )

    def test_run_verification_emits_observer_events_for_passing_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="ok",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
                repo="SVC",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            finished = events[1][1]
            self.assertIsInstance(started, verification.CheckStartedEvent)
            self.assertIsInstance(finished, verification.CheckFinishedEvent)
            self.assertEqual(0, started.check_index)
            self.assertEqual(1, started.check_count)
            self.assertEqual("ok", started.name)
            self.assertEqual("command", started.kind)
            self.assertEqual(check.argv, started.argv)
            self.assertEqual("SVC", started.repo)
            self.assertEqual(str(plans.canonicalize_path(repo_root)), started.cwd)
            self.assertEqual(started.session_id, contract.session_id)
            self.assertEqual(0, finished.check_index)
            self.assertEqual(1, finished.check_count)
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("command", finished.result.kind)
            self.assertEqual(0, finished.result.returncode)
            self.assertEqual("SVC", finished.repo)

    def test_run_verification_emits_observer_events_for_failing_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="boom",
                argv=("python3", "-c", "import sys; sys.exit(3)"),
                timeout_seconds=10.0,
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            finished = events[1][1]
            self.assertEqual("fail", finished.result.status)
            self.assertEqual("command-failed", finished.result.failure_reason)
            self.assertEqual(3, finished.result.returncode)

    def test_run_verification_emits_observer_events_for_command_timeout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="sleeper",
                argv=("python3", "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.1,
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            finished = events[1][1]
            self.assertEqual("fail", finished.result.status)
            self.assertEqual("command-timeout", finished.result.failure_reason)

    def test_run_verification_emits_observer_events_for_file_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            check = contracts.FileExistsCheck(name="present", path="present.txt")
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            self.assertEqual("file_exists", started.kind)
            self.assertIsNone(started.argv)
            self.assertIsNone(started.cwd)
            finished = events[1][1]
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("file_exists", finished.kind)

    def test_run_verification_emits_observer_events_for_regex_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text(
                "def parse_completion_contract():\n    pass\n",
                encoding="utf-8",
            )
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            self.assertEqual("regex_in_file", started.kind)
            self.assertIsNone(started.argv)
            self.assertIsNone(started.cwd)
            finished = events[1][1]
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("regex_in_file", finished.kind)

    def test_run_verification_emits_events_in_declared_check_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.CommandCheck(
                        name="middle",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                    contracts.FileExistsCheck(name="absent", path="nope.txt"),
                )
            )
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(
                [
                    ("started", 0, "present"),
                    ("finished", 0, "present"),
                    ("started", 1, "middle"),
                    ("finished", 1, "middle"),
                    ("started", 2, "absent"),
                    ("finished", 2, "absent"),
                ],
                [(tag, event.check_index, event.name) for tag, event in events],
            )
            for tag, event in events:
                self.assertEqual(3, event.check_count)
                if tag == "finished":
                    expected_status = "pass" if event.name != "absent" else "fail"
                    self.assertEqual(expected_status, event.result.status)

    def test_run_verification_resolves_observer_cwd_for_command_subdir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "nested").mkdir()
            check = contracts.CommandCheck(
                name="pwd",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
                cwd="nested",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            started = events[0][1]
            self.assertEqual(
                str(plans.canonicalize_path(repo_root / "nested")),
                started.cwd,
            )

    def test_finished_observer_events_name_the_role_each_check_played(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="missing.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=("python3", "-c", "raise SystemExit(3)"),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                    ),
                )
            )
            observer, events = self._record_observer()

            verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertEqual(
                [
                    ("suite", verification.CHECK_ROLE_GATE),
                    ("focused", verification.CHECK_ROLE_DIAGNOSTIC),
                ],
                [
                    (event.name, event.role)
                    for tag, event in events
                    if tag == "finished"
                ],
            )

    def test_run_verification_with_partial_observer_only_fires_set_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(contracts.FileExistsCheck(name="present", path="present.txt"),)
            )
            finished_events: list[verification.CheckFinishedEvent] = []
            observer = verification.VerificationObserver(
                on_check_started=None,
                on_check_finished=finished_events.append,
            )

            verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertEqual(1, len(finished_events))
            self.assertEqual("present", finished_events[0].name)

    def test_run_verification_without_observer_matches_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(contracts.FileExistsCheck(name="present", path="present.txt"),)
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertTrue(result.passed)
            self.assertEqual(1, len(result.checks))

    def test_run_verification_propagates_observer_callback_exceptions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(contracts.FileExistsCheck(name="present", path="present.txt"),)
            )

            class _Boom(RuntimeError):
                pass

            def _explode(_event: verification.CheckStartedEvent) -> None:
                raise _Boom("observer error")

            observer = verification.VerificationObserver(on_check_started=_explode)

            with self.assertRaises(_Boom):
                verification.run_verification(
                    contract, repo_root=repo_root, observer=observer
                )

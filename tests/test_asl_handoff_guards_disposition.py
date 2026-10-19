# ruff: noqa: F401, F403, F405, I001
"""Terminating-result disposition and legacy repository tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_handoff_guards_test_support import *


class TerminatingResultDispositionTests(unittest.TestCase):
    """Which branch ends the turn, and in which order it is asked."""

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "argv": ("codex",),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def disposition(
        self,
        *,
        after: plans_models.PlanSnapshot | None = None,
        turn_payload: dict[str, Any] | None = None,
        **result_fields: Any,
    ) -> str | None:
        return handoff_guards._terminating_result_disposition(
            self.result(**result_fields),
            after if after is not None else object(),  # type: ignore[arg-type]
            {} if turn_payload is None else turn_payload,
        )

    def test_a_clean_turn_reaches_the_status_guards(self) -> None:
        self.assertIsNone(self.disposition())

    def test_each_terminating_branch_has_a_name(self) -> None:
        for name, kwargs in (
            ("interrupted", {"interrupted": True}),
            ("timeout", {"timed_out": True}),
            ("aborted", {"aborted": True}),
            ("nonzero-exit", {"returncode": 1}),
        ):
            with self.subTest(name=name):
                self.assertEqual(name, self.disposition(**kwargs))

    def test_a_plan_that_could_not_be_inspected_is_named_malformed(self) -> None:
        self.assertEqual(
            "malformed-plan",
            handoff_guards._terminating_result_disposition(self.result(), None, {}),
        )

    def test_a_worker_pause_is_named_before_a_nonzero_exit(self) -> None:
        # A worker that printed `RUNNER_PAUSE` and then exited nonzero has said
        # why it stopped; reporting the exit code instead loses the reason.
        self.assertEqual(
            "runner-pause",
            self.disposition(
                returncode=1, turn_payload={"runner_pause": {"reason": "blocked"}}
            ),
        )

    def test_an_interrupt_outranks_every_other_branch(self) -> None:
        self.assertEqual(
            "interrupted",
            self.disposition(
                interrupted=True,
                timed_out=True,
                aborted=True,
                returncode=1,
                turn_payload={"runner_pause": {"reason": "blocked"}},
            ),
        )


class LegacyUnobservedRepositoriesTests(unittest.TestCase):
    """Silence about a place you did not look is not evidence of no change."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bound = self.tmp_dir / "bound"
        (self.bound / ".git").mkdir(parents=True)
        self.plan_dir = self.tmp_dir / "plan"
        self.plan_dir.mkdir()

    def make_repo(self, name: str) -> Path:
        repo = self.tmp_dir / name
        (repo / ".git").mkdir(parents=True)
        return repo

    def artifact(self, text: str, name: str = "session-1.md") -> Path:
        path = self.plan_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def unobserved(self, *artifacts: Path, **overrides: Any) -> dict[str, Any]:
        return handoff_guards._legacy_unobserved_repositories(
            cwd=self.bound,
            artifact_paths=artifacts,
            exempt_roots=(self.plan_dir,),
            provenance_roots=(),
            **overrides,
        )

    def test_the_bound_repository_is_not_reported_as_unobserved(self) -> None:
        self.assertEqual(
            {}, self.unobserved(self.artifact(f"Work in `{self.bound}`.\n"))
        )

    def test_a_second_repository_named_by_an_artifact_is_reported(self) -> None:
        other = self.make_repo("other")
        payload = self.unobserved(self.artifact(f"Also patch `{other}/mod.py`.\n"))
        self.assertEqual([str(other)], list(payload))
        self.assertEqual("repository-not-observed", payload[str(other)]["reason"])
        self.assertEqual(str(self.bound), payload[str(other)]["observed_repository"])

    def test_an_unreadable_artifact_names_no_repository_and_does_not_raise(
        self,
    ) -> None:
        # The turn is still judged on every artifact that can be read; a
        # fingerprint missing because a file vanished is caught by the
        # comparison, not by this scan.
        other = self.make_repo("other")
        missing = self.plan_dir / "session-2.md"
        payload = self.unobserved(missing, self.artifact(f"Patch `{other}`.\n"))
        self.assertEqual([str(other)], list(payload))

    def test_a_truncated_list_still_reports_the_count_it_dropped(self) -> None:
        # The guard fails closed on the first entry either way, so once the list
        # is cut the number matters more than the names -- and a truncation that
        # said nothing would read as a shorter list, not a longer one.
        repos = [self.make_repo(f"repo-{index}") for index in range(4)]
        body = "\n".join(f"Patch `{repo}`." for repo in repos)
        payload = self.unobserved(self.artifact(body + "\n"), limit=2)
        self.assertEqual(2 + 1, len(payload))
        self.assertIn("<omitted>", payload)
        self.assertEqual(2, payload["<omitted>"]["omitted_repositories"])

    def test_the_default_limit_is_the_one_the_intent_report_uses(self) -> None:
        # The pre-turn packet gate and this post-turn scan are read side by
        # side; two different truncation points would make them disagree about
        # how many repositories a turn reached.
        signature = inspect.signature(handoff_guards._legacy_unobserved_repositories)
        self.assertEqual(
            repository_intent.INTENT_REPORT_LIMIT,
            signature.parameters["limit"].default,
        )

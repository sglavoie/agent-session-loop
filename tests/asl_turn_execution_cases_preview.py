from .asl_turn_execution_support import *


class PostResultTerminationPreviewTests(unittest.TestCase):
    """A turn killed after its result must not keep a clean-finish preview.

    ASI-152: result.json carried returncode 143 and a post-result-shutdown
    classification while ``last_activity_preview`` still read "claude result:
    success", and stdout.log ended on that same line.
    """

    RESULT_LINE = "claude result: success (495 turns, 10217.4s)"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        plan_dir = tmp / "plan"
        write_plan(plan_dir, ("pending",))
        self.snapshot = plans_snapshot.inspect_plan(plan_dir)
        self.paths = {
            name: tmp / file_name
            for name, file_name in (
                ("artifact_dir", "."),
                ("stdout", "stdout.log"),
                ("stderr", "stderr.log"),
                ("last_message", "last-message.txt"),
                ("result", "result.json"),
            )
        }

    def write(
        self,
        result: harnesses.HarnessResult,
        *,
        preview: str | None = RESULT_LINE,
        shutdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        live_turn: dict[str, Any] = {
            "started_at": "2026-09-16T19:41:15+00:00",
            "finished_at": "2026-09-16T22:31:15+00:00",
            "duration_seconds": 10200.0,
            "idle_seconds_since_previous_turn": None,
            "last_activity_preview": preview,
        }
        if shutdown is not None:
            live_turn["harness_shutdown"] = shutdown
        return turn_execution_artifacts.write_turn_artifacts(
            paths=self.paths,
            result=result,
            before=self.snapshot,
            after=self.snapshot,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            live_turn=live_turn,
        )

    def result(self, returncode: int, **kwargs: Any) -> harnesses.HarnessResult:
        return harnesses.HarnessResult(
            name="claude",
            argv=["claude"],
            returncode=returncode,
            stdout=f"claude text: done\n{self.RESULT_LINE}\n",
            stderr="",
            **kwargs,
        )

    def stdout_log(self) -> str:
        return self.paths["stdout"].read_text(encoding="utf-8")

    def test_post_result_shutdown_rewrites_preview_and_log(self) -> None:
        shutdown = {
            "result": "exited-after-terminal-result",
            "recovery": {
                "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
                "root_pid": 4242,
                "signal": "SIGTERM",
                "signaled_pids": [4242],
            },
        }

        payload = self.write(self.result(143), shutdown=shutdown)

        expected = "harness terminated after result (post-result-shutdown, rc 143)"
        self.assertEqual(expected, payload["last_activity_preview"])
        self.assertEqual(self.RESULT_LINE, payload["last_harness_activity_preview"])
        self.assertEqual(
            "post-result-shutdown", payload["failure_classification"]["marker"]
        )
        self.assertTrue(
            self.stdout_log().endswith(f"{self.RESULT_LINE}\nasl: {expected}\n")
        )
        on_disk = json.loads(self.paths["result"].read_text(encoding="utf-8"))
        self.assertEqual(expected, on_disk["last_activity_preview"])

    def test_abort_after_result_is_named(self) -> None:
        payload = self.write(self.result(137, aborted=True))

        expected = "harness terminated after result (aborted, rc 137)"
        self.assertEqual(expected, payload["last_activity_preview"])
        self.assertIn(f"asl: {expected}\n", self.stdout_log())

    def test_nonzero_exit_after_result_is_named(self) -> None:
        payload = self.write(self.result(1))

        self.assertEqual(
            "harness terminated after result (nonzero-exit, rc 1)",
            payload["last_activity_preview"],
        )

    def test_clean_exit_after_result_is_left_alone(self) -> None:
        payload = self.write(self.result(0))

        self.assertEqual(self.RESULT_LINE, payload["last_activity_preview"])
        self.assertNotIn("last_harness_activity_preview", payload)
        self.assertNotIn("asl:", self.stdout_log())

    def test_nonzero_exit_without_a_result_preview_is_left_alone(self) -> None:
        payload = self.write(self.result(1), preview="claude tool: Bash")

        self.assertEqual("claude tool: Bash", payload["last_activity_preview"])
        self.assertNotIn("asl:", self.stdout_log())


if __name__ == "__main__":
    unittest.main()

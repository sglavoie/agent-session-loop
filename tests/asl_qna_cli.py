# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_qna_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_qna_shared import *


class QnAQuestionsCliTests(unittest.TestCase):
    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
        paused: bool = True,
    ) -> tuple[Path, Path, Path]:
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, statuses)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = tmp_dir / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn = {
            "run_id": "run-1",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id) if paused_session_id.isdigit() else 1,
            "session_number": (
                int(paused_session_id) if paused_session_id.isdigit() else 1
            ),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-1" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": not paused,
            "paused": paused,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-1",
            "run_dir": str(plan_state / "runs" / "run-1"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _run_questions(
        self,
        plan_dir: Path,
        state_dir: Path,
        *,
        json_output: bool = False,
    ) -> tuple[int, str, str]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            state_dir=str(state_dir),
            json=json_output,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = qna.questions_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def test_text_output_renders_resolver_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Should we use 1a or 1b?\n",
            )

            code, out, err = self._run_questions(plan_dir, state_dir)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertIn("# Paused Agent Session - Resolver Prompt", out)
            self.assertIn("## Plan", out)
            self.assertIn("## Harness", out)
            self.assertIn("## Pause", out)
            self.assertIn("## Artifacts", out)
            self.assertIn("## Previous Agent Message", out)
            self.assertIn("Should we use 1a or 1b?", out)
            self.assertIn("## Instructions", out)
            self.assertIn("`asl run --resume --reply`", out)
            self.assertIn(str(last_message_path), out)
            self.assertIn("- Session ID: 1", out)

    def test_json_output_exposes_stable_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Pick 1a, 2b, or 3c?\n",
            )

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual("questions", payload["command"])
            for key in (
                "plan_dir",
                "state_path",
                "run_id",
                "session_id",
                "turn_index",
                "pause_reason",
                "pause_message",
                "artifact_paths",
                "last_message",
                "rendered_prompt",
            ):
                self.assertIn(key, payload)
            self.assertEqual("1", payload["session_id"])
            self.assertEqual("1", payload["next_pending_id"])
            self.assertEqual("run-1", payload["run_id"])
            self.assertEqual(2, payload["turn_index"])
            self.assertEqual("runner-pause", payload["pause_reason"])
            self.assertEqual("agent asked a question", payload["pause_message"])
            self.assertEqual("Pick 1a, 2b, or 3c?\n", payload["last_message"])
            self.assertEqual(
                str(last_message_path),
                payload["artifact_paths"]["last_message_path"],
            )
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )
            self.assertIn("Pick 1a, 2b, or 3c?", payload["rendered_prompt"])

    def test_json_output_recovers_from_expired_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Use the strict or lenient parser?\n",
                paused=False,
            )
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
            )
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["waiting_for_reply"] = {
                "question_id": "question-2",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 2,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            raw["run_config"] = {"state_dir_override": str(state_dir)}
            state_path.write_text(json.dumps(raw), encoding="utf-8")

            code, out, err = self._run_questions(
                plan_dir,
                state_dir,
                json_output=True,
            )

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("question_id", payload)
            self.assertNotIn("reply_command", payload)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                payload["pause_reason"],
            )
            self.assertIn("asl run", payload["resume_command"])
            self.assertIn("--resume", payload["resume_command"])
            self.assertIn("--reply TEXT", payload["resume_command"])
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )

            status_payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["waiting_for_reply_status"],
            )
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["runner_state"]["waiting_for_reply_status"],
            )
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                state_status_text._print_text_status(status_payload)
            self.assertIn("Status: expired", status_output.getvalue())

    def test_json_output_recovers_from_orphaned_future_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Use the strict or lenient parser?\n",
                paused=False,
            )
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
            )
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["waiting_for_reply"] = {
                "question_id": "question-2",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 2,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            raw["run_config"] = {"state_dir_override": str(state_dir)}
            state_path.write_text(json.dumps(raw), encoding="utf-8")

            code, out, err = self._run_questions(
                plan_dir,
                state_dir,
                json_output=True,
            )

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("question_id", payload)
            self.assertNotIn("reply_command", payload)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                payload["pause_reason"],
            )
            self.assertIn("--resume", payload["resume_command"])
            self.assertIn("--reply TEXT", payload["resume_command"])

            status_payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertFalse(status_payload["lock"]["present"])
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["waiting_for_reply_status"],
            )

    def test_text_error_prints_to_stderr_when_no_paused_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            code, out, err = self._run_questions(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertEqual("", out)
            self.assertIn("agent-session-loop questions:", err)

    def test_json_error_payload_uses_stable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual(qna.REASON_NO_PAUSED_RUN, payload["reason"])
            self.assertFalse(payload["paused"])
            self.assertIn("plan_dir", payload)

    def test_json_error_for_reply_context_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = self._seed_paused_plan(
                tmp_dir,
                statuses=("complete", "pending"),
                paused_session_id="1",
            )

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"])
            self.assertEqual("1", payload["details"]["paused_session_id"])
            self.assertEqual("2", payload["details"]["next_pending_id"])

    def test_json_error_for_malformed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            missing_plan = tmp_dir / "does-not-exist"
            state_dir = tmp_dir / "state"

            code, out, err = self._run_questions(
                missing_plan, state_dir, json_output=True
            )

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])

    def test_subprocess_questions_command_wired_into_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = self._seed_paused_plan(tmp_dir)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "questions",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual("1", payload["session_id"])
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite

"""Test asl codex app server adapter."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    guidance_delivery,
    harness_codex,
    harness_process,
    harnesses,
)
from tests import support_asl_codex_app_server as fixtures


class AdapterTransportTests(unittest.TestCase):
    """The adapter against a real app-server process on a real pipe."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.record = self.tmp / "record.json"
        self.launch_log = self.tmp / "launches.log"
        # The real sweep shells out to ``ps``; these tests are about the
        # transport, not the process table.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.env = mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_RECORD": str(self.record),
                "FAKE_APP_SERVER_LAUNCH_LOG": str(self.launch_log),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _adapter(self, mode: str = "success") -> harness_codex.CodexAdapter:
        os.environ["FAKE_APP_SERVER_MODE"] = mode
        executable = self.tmp / "codex-app-server"
        shutil.copyfile(fixtures.FAKE_APP_SERVER_FIXTURE, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return harness_codex.CodexAdapter(executable=str(executable))

    def _run(self, adapter: harness_codex.CodexAdapter, **kwargs: Any) -> Any:
        return adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
            **kwargs,
        )

    def _recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_a_completed_turn_reports_success_and_its_final_message(self) -> None:
        chunks: list[tuple[str, str]] = []

        result = self._run(
            self._adapter(),
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertEqual(2, len(result.argv))
        self.assertEqual("app-server", result.argv[1])
        self.assertEqual("the turn is done\n", result.last_message)
        self.assertEqual("fake-model-1", result.observed_model)
        self.assertIn("working\n", result.stdout)
        self.assertIn("$ just test-fast (exit 0)", result.stdout)
        self.assertIn("turn completed", result.stdout)

    def test_the_prompt_travels_the_protocol_and_never_the_log(self) -> None:
        result = self._run(self._adapter())

        self.assertEqual(fixtures.PROMPT, self._recorded()["prompt"])
        self.assertNotIn(fixtures.PROMPT, result.stdout)
        self.assertNotIn(fixtures.PROMPT, " ".join(result.argv))

    def test_the_access_profile_crosses_the_wire(self) -> None:
        self._run(self._adapter())

        thread_params = self._recorded()["thread_params"]
        self.assertEqual("workspace-write", thread_params["sandbox"])
        self.assertEqual("never", thread_params["approvalPolicy"])
        self.assertEqual(
            [str(self.plan_dir)],
            thread_params["config"]["sandbox_workspace_write"]["writable_roots"],
        )

    def test_an_audit_turn_asks_for_the_read_only_sandbox(self) -> None:
        adapter = self._adapter()

        adapter.run(
            cwd=self.tmp,
            plan_dir=None,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
            access=harnesses.audit_read_only_access((self.tmp,)),
        )

        thread_params = self._recorded()["thread_params"]
        self.assertEqual("read-only", thread_params["sandbox"])
        self.assertNotIn("config", thread_params)

    def test_a_widened_sandbox_stops_the_turn_before_it_runs(self) -> None:
        result = self._run(self._adapter("widen-sandbox"))

        # No turn was ever sent, so the prompt never reached the app-server and
        # the legacy transport -- which states the profile in argv -- ran it.
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertIn("applied sandbox", result.stderr)

    def _resume_invocation(
        self, adapter: harness_codex.CodexAdapter, native_id: str
    ) -> Any:
        return adapter.build_invocation(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            resume_conversation_id=native_id,
        )

    def test_a_resumed_turn_restores_exactly_the_thread_it_was_handed(self) -> None:
        adapter = self._adapter()
        captured: list[str] = []

        result = self._run(
            adapter,
            invocation=self._resume_invocation(adapter, "thread-77"),
            on_conversation=captured.append,
        )

        recorded = self._recorded()
        self.assertIsNone(recorded["thread_params"])
        self.assertEqual("thread-77", recorded["resume_params"]["threadId"])
        self.assertEqual("workspace-write", recorded["resume_params"]["sandbox"])
        self.assertEqual(["thread-77"], captured)
        self.assertEqual("thread-77", result.conversation_id)
        self.assertIsNone(result.continuity_unavailable)
        self.assertEqual(0, result.returncode)

    def test_a_fresh_turn_captures_the_thread_the_app_server_started(self) -> None:
        captured: list[str] = []

        result = self._run(self._adapter(), on_conversation=captured.append)

        self.assertEqual(["thread-1"], captured)
        self.assertEqual("thread-1", result.conversation_id)

    def test_a_resume_the_app_server_refuses_never_reruns_on_codex_exec(self) -> None:
        adapter = self._adapter("resume-unknown")

        result = self._run(
            adapter, invocation=self._resume_invocation(adapter, "thread-77")
        )

        # One launch: the fixture logs every spawn, so a second line here would
        # be `codex exec` answering "continue thread-77" with a fresh session.
        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no such thread", result.stderr)

    def test_a_widened_sandbox_on_a_resume_is_refused_without_a_fallback(self) -> None:
        adapter = self._adapter("widen-sandbox")

        result = self._run(
            adapter, invocation=self._resume_invocation(adapter, "thread-77")
        )

        # The same failure that falls back on a fresh start (above) does not
        # fall back here, because the turn asked to continue something.
        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertIn("applied sandbox", result.stderr)

    def test_a_conversation_that_cannot_be_recorded_abandons_the_turn(self) -> None:
        def refuse(native_id: str) -> None:
            raise RuntimeError(f"no room for {native_id}")

        result = self._run(self._adapter(), on_conversation=refuse)

        self.assertIsNone(self._recorded()["prompt"])
        self.assertIsNone(result.conversation_id)
        self.assertIn("no room for thread-1", str(result.conversation_error))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(result.aborted)
        self.assertIn("turn abandoned", result.stderr)
        assert result.stream_io is not None
        self.assertEqual(len(result.stderr), result.stream_io["stderr_chars"])

    def test_an_abort_before_submission_ends_the_turn_at_the_handshake(self) -> None:
        adapter = self._adapter()
        abort = harnesses.AbortSignal()

        result = self._run(
            adapter,
            abort=abort,
            on_conversation=lambda native_id: abort.arm("the operator stopped it"),
        )

        self.assertIsNone(self._recorded()["prompt"])
        self.assertTrue(result.aborted)
        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, result.cancel_stage)
        # The thread existed and was recorded before the cancellation landed,
        # so the turn that never ran still knows where it would have run.
        self.assertEqual("thread-1", result.conversation_id)

    def test_a_prompt_the_server_read_is_never_reported_as_unsubmitted(self) -> None:
        """Every way a ``turn/start`` answer can be lost, over a real pipe.

        The prompt is in the app-server's own record in all three, which is the
        fact that matters: the words are on the far side of the wire. What the
        client does not have is anything to say about them, and the result has
        to carry that absence rather than the ``False`` a caller would act on by
        handing the same words to the next worker.
        """
        for mode in ("die-before-turn-response", "reject-turn", "unidentified-turn"):
            with self.subTest(mode=mode):
                self.launch_log.unlink(missing_ok=True)

                result = self._run(self._adapter(mode))

                self.assertEqual(fixtures.PROMPT, self._recorded()["prompt"])
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(result.prompt_submitted)
                self.assertFalse(harnesses.prompt_provably_unsubmitted(result))
                # No second launch either: the same silence that must not read
                # as "never sent" must not read as "safe to run again".
                self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
                self.assertEqual(
                    ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
                )

    def test_a_turn_the_server_never_saw_still_proves_its_prompt_stayed_home(
        self,
    ) -> None:
        submitted: list[str] = []
        adapter = self._adapter("resume-unknown")

        refused = self._run(
            adapter,
            invocation=self._resume_invocation(adapter, "thread-77"),
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        # The counterweight to the test above: the refusal is answered before
        # ``turn/start`` is written, so this one *is* proof, and the run that
        # was carrying a correction gets it back.
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual([], submitted)
        self.assertIs(False, refused.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(refused))

    def test_guidance_reaches_the_running_turn_and_stays_out_of_the_log(self) -> None:
        adapter = self._adapter("await-steer")
        turn = fixtures.turn_identity()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn)
        outcomes: list[guidance_delivery.GuidanceOutcome] = []

        def deliver() -> None:
            # The endpoint is opened the instant the process exists, which is
            # before the handshake has a turn to steer, so an early attempt
            # honestly defers with ``no-active-turn``. This retries the way the
            # mailbox poller's next tick would.
            deadline = time.monotonic() + fixtures.WAIT_SECONDS
            while time.monotonic() < deadline:
                if endpoint.opened:
                    outcome = endpoint.submit(fixtures.guidance_request())
                    outcomes.append(outcome)
                    if outcome.delivered:
                        return
                time.sleep(0.05)

        courier = threading.Thread(target=deliver)
        courier.start()
        self.addCleanup(courier.join, fixtures.WAIT_SECONDS)

        result = self._run(adapter, live_delivery=endpoint)
        courier.join(timeout=fixtures.WAIT_SECONDS)

        self.assertEqual([fixtures.GUIDANCE_BODY], self._recorded()["steers"])
        self.assertTrue(outcomes[-1].delivered)
        self.assertTrue(
            all(
                outcome.reason == guidance_delivery.REASON_NO_ACTIVE_TURN
                for outcome in outcomes[:-1]
            ),
            outcomes,
        )
        self.assertEqual(0, result.returncode)
        self.assertNotIn(fixtures.GUIDANCE_BODY, result.stdout)
        self.assertNotIn(fixtures.GUIDANCE_BODY, result.stderr)


if __name__ == "__main__":
    unittest.main()

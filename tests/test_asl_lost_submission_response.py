"""A correction the provider read and never answered for, end to end.

A turn's prompt can reach a worker without the run ever being told it did. The
app-server takes ``turn/start``, and then the answer is lost: the process dies
before writing it, the socket breaks, the reply is a rejection or a shape the
client cannot read. The words are in the provider's hands either way, and the
run has nothing that says so.

The dangerous reading of that silence is "nothing was sent". Taking it would
release the operator's correction back to be handed to a second worker while
the first one is holding it -- the duplicate delivery the whole acknowledgment
protocol exists to prevent, and one the journal can never take back, since a
release cannot follow a delivery.

So this module drives the silence through the real loop, with the result the
real transport produces against a real app-server that reads the prompt and
dies: the carry stays standing, no automatic recovery offers it again, and it
is put in front of an operator instead.

Its counterpart lives in :mod:`tests.test_asl_native_restore_recovery`, where a
provider that refuses ``thread/resume`` *proves* the prompt stayed home and the
correction is released automatically. The pair is the point -- this repair
narrows what counts as proof, and must not have cost the release that proof
earns.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import stat
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harness_codex,
    harness_process,
    harnesses,
    redirect_recovery,
    redirect_store_journal,
    redirect_store_models,
)
from tests.support_asl_codex_app_server import (
    FAKE_APP_SERVER_FIXTURE,
    PROMPT,
    WAIT_SECONDS,
)

from .asl_redirect_continuation_support import FEEDBACK, SESSION_ID, WORKER_PID
from .asl_redirect_run_support import _RedirectedRun

#: The conversation the fixture run opens and its successor asks to continue.
CONVERSATION_ID = "conv-1"


def codex_lost_response_result(tmp: Path) -> harnesses.HarnessResult:
    """One real codex turn whose ``turn/start`` answer never came back.

    Produced by the real adapter against the real app-server fixture rather than
    assembled by hand, for the reason the refused-restore results are: the run
    loop's reading of this turn is a reading of fields some transport has to
    actually set, and a hand-built result would keep the branch passing against
    a shape nothing produces.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    executable = tmp / "codex-app-server"
    shutil.copyfile(FAKE_APP_SERVER_FIXTURE, executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    adapter = harness_codex.CodexAdapter(executable=str(executable))
    plan_dir = tmp / "plan"
    plan_dir.mkdir(exist_ok=True)
    last_message = tmp / "last-message.txt"
    with (
        mock.patch.object(harness_process, "sweep_enabled", return_value=False),
        mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_MODE": "die-before-turn-response",
                "FAKE_APP_SERVER_RECORD": str(tmp / "record.json"),
            },
        ),
    ):
        return adapter.run(
            cwd=tmp,
            plan_dir=plan_dir,
            prompt=PROMPT,
            last_message_path=last_message,
            timeout=WAIT_SECONDS,
            invocation=adapter.build_invocation(
                cwd=tmp,
                plan_dir=plan_dir,
                prompt=PROMPT,
                last_message_path=last_message,
                resume_conversation_id=CONVERSATION_ID,
            ),
        )


class LostSubmissionResponseRunTests(_RedirectedRun):
    """The whole loop: a corrected turn whose submission was never confirmed.

    The fixture's own story supplies the first turn -- a worker corrected
    mid-turn, leaving a successor to resume the conversation it was corrected
    in. Here that successor is the one whose answer is lost, so the run reaches
    the silence the way a real run does: with the correction bound to an attempt
    and a launch already declared in the journal.
    """

    def setUp(self) -> None:
        super().setUp()
        self.prompts: list[str] = []
        self.app_server = self.tmp / "app-server"
        self.lost = codex_lost_response_result(self.app_server)

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            return super()._fake_worker(name, **kwargs)
        self.harness_calls.append(name)
        # A spawn and nothing else. The adapter that produced this result got
        # as far as writing the prompt out and was never able to say so, which
        # is exactly the callback this worker does not make.
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)
        kwargs["abort"].release()
        return dataclasses.replace(self.lost, name=name)

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def _recorded(self) -> dict[str, Any]:
        return json.loads((self.app_server / "record.json").read_text(encoding="utf-8"))

    def test_the_run_is_driven_by_a_prompt_the_server_really_read(self) -> None:
        """What makes the rest of the module about anything."""
        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, self.lost.transport)
        self.assertNotEqual(0, self.lost.returncode)
        # Neither claim: the turn cannot say the prompt landed, and must not be
        # read as saying it stayed home.
        self.assertIsNone(self.lost.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(self.lost))

    def test_the_lost_answer_is_not_a_correction_that_stayed_home(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        request = self._request()
        self.assertIn(FEEDBACK, self.prompts[1])
        self.assertTrue(request.carried)
        self.assertTrue(request.launching)
        # The defect this closes: a release here, and the words the server is
        # holding go to whoever runs next.
        self.assertFalse(request.released)
        self.assertFalse(request.delivered)
        self.assertTrue(request.launch_ambiguous)

    def test_the_silence_is_never_settled_behind_the_run_s_back(self) -> None:
        """No line either way, which is what makes the carry readable later."""
        self.run_loop_once(expect_exit_code=1)

        outcomes = [
            record["outcome"]
            for record in self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        ]
        self.assertIn("launching", outcomes)
        self.assertNotIn("released", outcomes)
        self.assertNotIn("delivered", outcomes)

    def test_the_next_run_puts_the_carry_to_an_operator(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-lost-2",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual((), report.released)
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(("op-1",), report.uncertain_for(SESSION_ID))

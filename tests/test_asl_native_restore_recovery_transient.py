"""Tests for test asl native restore recovery transient."""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    harnesses,
)
from tests import support_asl_native_restore_recovery as fixtures
from tests.asl_redirect_continuation_support import (
    FEEDBACK,
    target,
)
from tests.support_asl_native_restore_recovery import (
    CONFIG_FILE_NAME_ERROR,
    METHOD_NAME_ERROR,
    RUN_CONVERSATION_ID,
    SESSION_FILE_ERROR,
    UNRELATED_NO_SUCH_ERROR,
)

ASL_TEST_LANE = "integration"


class TransientRestoreFailureRunTests(fixtures._NativeRestoreRun):
    """The same whole loop, when the provider was merely having a bad minute.

    Identical to :class:`RefusedRestoreRunTests` in every respect but one: the
    sentence ``thread/resume`` is answered with. That is the whole claim -- an
    outage and a disowned history arrive through the same call, on the same
    turn, against the same confirmed ledger entry, and only the second one may
    cost the operator their history.

    The failing turn still fails; what it must not do is decide anything about
    the conversation. So the recovery here is the one nobody has to authorize:
    the provider comes back, and an ordinary relaunch continues the exact
    conversation the failed turn asked for.
    """

    #: The relaunch after the provider recovered, minting its own id.
    REPAIRED_RUN_ID = "run-restore-3"

    #: What ``thread/resume`` is answered with, and the only thing subclasses
    #: vary: every claim below is about what the loop does with the sentence.
    RESUME_ERROR: dict[str, Any] = {
        "code": -32603,
        "message": "temporary service unavailable; retry later",
    }

    #: Where the real app-server for that answer is installed, so two classes
    #: never share one directory.
    FAILURE_DIR = "app-server"

    #: The provider's words the pause must still show an operator.
    REPORTED_WORDS = "temporary service unavailable"

    def setUp(self) -> None:
        super().setUp()
        self.resumed: list[str | None] = []
        self.prompts: list[str] = []
        self.failed = False
        self.failure = fixtures.codex_resume_result(
            self.tmp / self.FAILURE_DIR,
            conversation=RUN_CONVERSATION_ID,
            mode="resume-rejected",
            resume_error=self.RESUME_ERROR,
        )

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.resumed.append(kwargs.get("resume_conversation_id"))
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            return super()._fake_worker(name, **kwargs)
        if self.failed:
            return self._repaired_turn(name, **kwargs)
        self.failed = True
        self.harness_calls.append(name)
        self._spawned(**kwargs)
        kwargs["abort"].release()
        return dataclasses.replace(self.failure, name=name)

    def _repaired_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The relaunch once the provider is answering again.

        It reports the *same* conversation back, because that is what continuing
        means here: the history was never gone, so nothing was started over.
        """
        self.harness_calls.append(name)
        self._submitted(**kwargs)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation(RUN_CONVERSATION_ID)
        self._receipt_from_prompt(
            str(kwargs.get("prompt") or ""), harness=name, stale=target(2)
        )
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        kwargs["abort"].release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="codex completed\n",
            stderr="",
            conversation_id=RUN_CONVERSATION_ID,
        )

    def _pause(self) -> dict[str, Any]:
        payload = self.run_loop_once(expect_exit_code=1)
        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertEqual(
            [None, RUN_CONVERSATION_ID],
            self.resumed,
        )
        return payload

    def test_the_failed_turn_stops_the_run_without_calling_it_a_refusal(self) -> None:
        payload = self._pause()

        self.assertTrue(payload["paused"])
        # A failed turn is a failed turn. What it is not is the one disposition
        # that spends the conversation, and the pause that asks the operator to
        # authorize a fresh one is the shape of having spent it.
        self.assertNotEqual("conversation-recovery-required", payload["reason"])
        self.assertNotIn(
            conversation_resume.CONTINUATION_RESTORE_REFUSED,
            [
                record.get("disposition")
                for record in self.events_named(events.CONVERSATION_CONTINUATION_EVENT)
            ],
        )

    def test_the_provider_still_says_what_went_wrong(self) -> None:
        self._pause()

        # Preserving the binding is not the same as swallowing the reason. The
        # words the provider used are what an operator decides on.
        self.assertIn(self.REPORTED_WORDS, self.failure.stderr)
        self.assertIsNone(self.failure.conversation_restore_refused)

    def test_the_conversation_is_still_the_one_a_later_launch_would_resume(
        self,
    ) -> None:
        self._pause()

        selection = self._selection()

        self.assertEqual(conversations.SELECTION_RESUMABLE, selection.state)
        assert selection.reference is not None
        self.assertEqual(
            RUN_CONVERSATION_ID,
            selection.reference.native_id,
        )

    def test_an_ordinary_relaunch_continues_the_exact_conversation(self) -> None:
        payload = self._repair_after_pause()

        # The claim in one line: no ``--fresh-conversation``, and the third turn
        # asked for the same history the second one was refused.
        self.assertEqual(
            [
                None,
                RUN_CONVERSATION_ID,
                RUN_CONVERSATION_ID,
            ],
            self.resumed,
        )
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", self._session_status())

    def test_the_correction_the_failed_turn_held_is_still_handed_over(self) -> None:
        self._repair_after_pause()

        # The failing turn never spoke, so the operator's words are still owed --
        # and they are owed to a turn that is continuing, not starting over.
        self.assertEqual(["codex", "codex", "codex"], self.harness_calls)
        self.assertIn(FEEDBACK, self.prompts[2])
        self.assertTrue(self._request().delivered)

    def _repair_after_pause(self) -> dict[str, Any]:
        self._pause()
        return self.run_loop_once(run_id=self.REPAIRED_RUN_ID, prepare_plan=False)


class UnrelatedAbsenceRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a host.

    The failure above announces itself as temporary; this one announces nothing
    of the kind, and an operator repairs it by fixing a name server rather than
    by waiting. It is here because the sentence contains the words "no such",
    which is the entire reason it once retired a conversation the provider was
    still holding -- and because no list of transient words could have stopped
    it, only requiring the absence to be *about a conversation* can.

    Every claim it inherits is the claim that matters: the turn fails, nothing
    is decided about the history, and an ordinary relaunch after the lookup
    starts working again continues the exact conversation.
    """

    REPAIRED_RUN_ID = "run-restore-4"

    RESUME_ERROR = UNRELATED_NO_SUCH_ERROR

    FAILURE_DIR = "app-server-unrelated-absence"

    REPORTED_WORDS = "no such host"


class SessionFileRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a file.

    The nearest miss of all of them. This sentence does not merely contain "no
    such" somewhere -- it names a session and then says "not found", which is
    the exact shape of a disowned history. What it is about is a configuration
    file on the provider's disk, and an operator who puts that file back has a
    provider that continues the conversation it was holding all along.

    So the claim is the one the classifier exists for: a history noun standing in
    front of a piece of infrastructure is not evidence about the history, and
    reading it as evidence costs an operator their accumulated context and makes
    them authorize a fresh conversation to get moving again.
    """

    REPAIRED_RUN_ID = "run-restore-5"

    RESUME_ERROR = SESSION_FILE_ERROR

    FAILURE_DIR = "app-server-session-file"

    REPORTED_WORDS = "session file not found"


class MethodNameRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a method.

    A provider one version behind answers an unrecognized call by naming it, and
    the name is "session.load". The dot is the whole story: it is how a sentence
    ends as well as how a method is spelled, so the sentence was once read as
    "no such session" with the rest discarded -- a conversation retired because
    the operator had not upgraded a binary yet.

    The claims it inherits are the ones that cost real work: the turn fails, the
    ledger keeps the exact conversation, and the relaunch after the upgrade
    continues it and hands over the correction the failed turn was holding.
    """

    REPAIRED_RUN_ID = "run-restore-6"

    RESUME_ERROR = METHOD_NAME_ERROR

    FAILURE_DIR = "app-server-method-name"

    REPORTED_WORDS = "no such session.load method"


class ConfigFileNameRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a filename.

    The nearest miss the punctuation rule has to catch on the other side: the
    noun, then "not found", with a word between them that carries a dot the way
    a minted identifier does. Read as a name, "session config.json not found" is
    a history the provider disowned; read as the compound it is, it is a file an
    operator puts back in a minute with the conversation still sitting there.
    """

    REPAIRED_RUN_ID = "run-restore-7"

    RESUME_ERROR = CONFIG_FILE_NAME_ERROR

    FAILURE_DIR = "app-server-config-file-name"

    REPORTED_WORDS = "session config.json not found"


class VersionedInfrastructureRestoreTests(TransientRestoreFailureRunTests):
    RESUME_ERROR = {"code": -32000, "message": "session config-v2.json not found"}
    REPORTED_WORDS = "session config-v2.json not found"
    FAILURE_DIR = "versioned-config"
    REPAIRED_RUN_ID = "versioned-config-repaired"


class CompoundMethodRestoreTests(TransientRestoreFailureRunTests):
    RESUME_ERROR = {"code": -32000, "message": "no such session::load method"}
    REPORTED_WORDS = "no such session::load method"
    FAILURE_DIR = "compound-method"
    REPAIRED_RUN_ID = "compound-method-repaired"


if __name__ == "__main__":
    unittest.main()

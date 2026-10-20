"""Postflight passes the accumulated session delta to its guards."""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    continuation_guards,
    events,
    handoff_guards,
    harnesses,
    plans_snapshot,
    turn_postflight,
    write_guards,
    write_guards_protected,
)

from .support import write_plan
from .support_asl_continuation_guards import (
    SESSION_ID,
    SESSION_LABEL,
    FakeRunContext,
    registered_guard,
)


class ContinuationPostflightTests(unittest.TestCase):
    """The sequence's own step, with the change-path projection left real.

    Every guard is replaced by a recorder, but
    ``write_guards_protected._protected_path_changed_paths`` is deliberately
    *not*: it is the consumer whose answer the accumulation is supposed to change, so
    stubbing it would assert the plumbing and prove nothing about the verdict.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = self.tmp_dir / "repo"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.protected_calls: list[dict[str, Any]] = []

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "last_message": self.artifact_dir / "last-message.txt",
            "result": self.artifact_dir / "result.json",
        }

    @contextlib.contextmanager
    def patched(self, *, guard: dict[str, Any], write_paused: bool) -> Any:
        def write_guard(**kwargs: Any) -> bool:
            kwargs["turn_payload"]["workspace_write_guard"] = guard
            return write_paused

        def protected(**kwargs: Any) -> bool:
            self.protected_calls.append(kwargs)
            return False

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    write_guards, "_enforce_workspace_write_guard", write_guard
                )
            )
            stack.enter_context(
                mock.patch.object(
                    write_guards_protected, "_enforce_protected_path_guard", protected
                )
            )
            stack.enter_context(
                mock.patch.object(
                    handoff_guards,
                    "_enforce_plan_artifact_guard",
                    lambda **kwargs: False,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    handoff_guards,
                    "_terminating_result_disposition",
                    lambda *args, **kwargs: "timeout",
                )
            )
            yield

    def attempt(
        self,
        *,
        guard: dict[str, Any],
        turn_index: int,
        write_paused: bool = False,
    ) -> dict[str, Any]:
        """One turn through the sequence, then the loop's own ledger record."""
        turn_payload: dict[str, Any] = {"turn_index": turn_index}
        with self.patched(guard=guard, write_paused=write_paused):
            turn_postflight.enforce_post_turn_guards(
                self.ctx,
                before=self.before,
                after=None,
                result=harnesses.HarnessResult(
                    argv=["claude"], returncode=0, stdout="", stderr=""
                ),
                turn_payload=turn_payload,
                workspace=mock.Mock(name="workspace", repositories=("SVC", "OAK")),
                session_scope=mock.Mock(name="scope"),
                workspace_snapshot_before={"/repos/SVC": {"head": "abc"}},
                legacy_snapshot_before=None,
                review_fingerprint_before=None,
                external_digests_before={},
                turn_started_ns=1,
                plan_artifacts_before={},
                plan_artifacts_written_by_runner=(),
                is_review_session=False,
                cwd=self.cwd,
                session_id=SESSION_ID,
                session_label=SESSION_LABEL,
                session_details={"session_id": SESSION_ID},
                review_guard_excludes=(".venv",),
                review_guard_include_ignored=False,
                paths=self.paths(),
                state_dir_arg=None,
                disarm_exception_guard=lambda: None,
            )
        if not write_paused:
            self.ctx.session_attempts.record(
                session_id=SESSION_ID,
                turn_index=turn_index,
                disposition="timeout",
                guard=turn_payload.get("workspace_write_guard"),
            )
        return turn_payload

    def test_the_protected_path_guard_sees_the_whole_session_delta(self) -> None:
        self.attempt(
            guard=registered_guard(changes={"SVC": ("first.txt",)}), turn_index=1
        )
        self.attempt(
            guard=registered_guard(changes={"OAK": ("second.txt",)}), turn_index=2
        )

        self.assertEqual(
            [("SVC:first.txt",), ("OAK:second.txt", "SVC:first.txt")],
            [call["changed_paths"] for call in self.protected_calls],
        )

    def test_the_accumulated_record_is_what_the_result_artifact_carries(self) -> None:
        self.attempt(
            guard=registered_guard(changes={"SVC": ("first.txt",)}), turn_index=1
        )
        turn_payload = self.attempt(
            guard=registered_guard(changes={"OAK": ("second.txt",)}), turn_index=2
        )

        # The commit settlement reads the payload; a resume and every audit read
        # the artifact. They have to be the same record.
        written = json.loads(
            (self.artifact_dir / "result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            turn_payload["workspace_write_guard"], written["workspace_write_guard"]
        )
        self.assertEqual(
            ["OAK", "SVC"], list(turn_payload["workspace_write_guard"]["changed"])
        )

    def test_a_first_attempt_leaves_its_record_exactly_as_the_guard_wrote_it(
        self,
    ) -> None:
        guard = registered_guard(changes={"SVC": ("first.txt",)})
        turn_payload = self.attempt(guard=guard, turn_index=1)

        self.assertEqual(guard, turn_payload["workspace_write_guard"])
        self.assertNotIn(
            continuation_guards.CONTINUATION_EVIDENCE_KEY,
            turn_payload["workspace_write_guard"],
        )
        self.assertFalse((self.artifact_dir / "result.json").exists())

    def test_a_finalization_refusal_keeps_its_own_record(self) -> None:
        self.attempt(
            guard=registered_guard(changes={"SVC": ("first.txt",)}), turn_index=1
        )
        refusal = registered_guard(
            changes={"OAK": ("second.txt",)},
            passed=False,
            violations=({"kind": "read-only-repository", "alias": "OAK"},),
        )
        turn_payload = self.attempt(guard=refusal, turn_index=2, write_paused=True)

        # The record beside a scope violation is a verdict about one turn, and
        # an operator reading it must not find paths from a turn that is no
        # longer in question mixed into it.
        self.assertEqual(refusal, turn_payload["workspace_write_guard"])
        self.assertEqual(
            ["OAK"], list(turn_payload["workspace_write_guard"]["changed"])
        )


if __name__ == "__main__":
    unittest.main()

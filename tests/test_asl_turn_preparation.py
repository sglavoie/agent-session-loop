"""The preparation boundary, exercised without the run loop.

``turn_preparation`` answers the four questions a *selected* session raises
before a harness process exists: what kind of session it is, whether a pending
review still describes the truth, whether a repair child may run at all, and
what the worker actually receives. These tests drive those four through a
stand-in run context defined here rather than through ``runner``, which is the
point of the boundary.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* every ``ctx.pause`` is immediately followed by the return that carries it,
  so the ``break`` the moved branches ended in cannot be lost to a
  fall-through;
* the module owns no loop statement, because the loop is not its to break;
* each step is a no-op for the session kind it does not own, so the loop's
  ``if is_review_session`` / ``if is_repair_session`` guards survive the move
  as the module's own preconditions;
* a reply and a resolution pending at once raises rather than picking one;
* the repository-intent gate reads the rendered prompt and is skipped
  entirely for a run with a registered workspace;
* the runner's own body reaches this module and no longer builds a prompt or
  resumes a repair child itself.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    mailbox_vocabulary,
    plans,
    qna,
    review_guard,
    run_loop_context,
    run_prompts,
    task_packet,
    turn_preparation,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import, patched_env, write_plan

RUN_ID = "run-preparation-1"

#: The six entry points the run loop is allowed to reach here. A seventh would
#: be work the loop kept that this boundary was supposed to take.
ENTRY_POINTS = (
    "turn_preparation.classify_session",
    "turn_preparation.prepare_review_session",
    "turn_preparation.collect_packet_inputs",
    "turn_preparation.prepare_turn_continuation",
    "turn_preparation.resume_repair_session",
    "turn_preparation.prepare_turn_prompt",
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real — the boundary takes it around the preflight record and a
    fake that did not would hide a missing hold rather than expose it — while
    events and state writes are kept in order so a test can assert on what was
    recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class FakeVerificationContext:
    """The verification context, reduced to the one member preparation reads."""

    def __init__(self) -> None:
        self.workspace = None


class FakeRunContext:
    """A :class:`turn_preparation.RunContext` built by hand rather than by a run.

    Only the members the four preparation steps reach are populated, which is
    what the Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans.PlanSnapshotCache()
        self.verification_context = FakeVerificationContext()
        self.final_snapshots: list[plans.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class FakeReviewResume:
    """The repair continuation's projection, reduced to what the gate reads."""

    def __init__(self, *, action: str, eligible: bool, blocker: str | None) -> None:
        self.action = action
        self.eligible = eligible
        self.blocker = blocker

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, "eligible": self.eligible}


class PreparationTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a step needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)

    def snapshot(self, statuses: tuple[str, ...] = ("pending",)) -> plans.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans.inspect_plan(self.plan_dir)

    def args(self, **overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "state_dir": None,
            "max_repair_group_attempts": 2,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def classification(
        self,
        *,
        review: bool = False,
        repair: bool = False,
    ) -> turn_preparation.SessionClassification:
        return turn_preparation.SessionClassification(
            is_review_session=review,
            is_repair_session=repair,
            session_kind=(
                plans.SESSION_KIND_REVIEW
                if review
                else plans.SESSION_KIND_IMPLEMENTATION
            ),
        )

    def qna_context(self) -> qna.QnAContext:
        return qna.QnAContext(
            plan_dir=self.plan_dir,
            state_dir=self.run_dir,
            state_path=self.run_dir / "state.json",
            run_id=RUN_ID,
            run_dir=self.run_dir,
            pause_reason="question",
            pause_message="ask",
            pause_details={},
            harness="codex",
            model=None,
            model_source=None,
            effort=None,
            effort_source=None,
            session_id="1",
            next_pending_id="1",
            turn_index=3,
            artifact_dir=self.run_dir,
            stdout_path=None,
            stderr_path=None,
            last_message_path=self.run_dir / "last-message.txt",
            result_path=None,
            last_message="a question",
        )


class PreparationBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any session it prepares."""

    def module_source(self) -> ast.Module:
        source = Path(turn_preparation.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_turn_preparation_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, turn_preparation)

    def test_every_pause_is_followed_by_the_return_that_carries_it(self) -> None:
        # The branches this replaced ended in `break`. Here the `break` is the
        # returned `paused` flag, so a pause that fell through would refuse a
        # turn and then run it anyway.
        pauses = 0
        for node in ast.walk(self.module_source()):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body):
                if not isinstance(statement, ast.Expr):
                    continue
                call = statement.value
                if not isinstance(call, ast.Call):
                    continue
                if ast.unparse(call.func) != "ctx.pause":
                    continue
                pauses += 1
                self.assertLess(
                    index + 1,
                    len(body),
                    "a pause at the end of a block falls through into the "
                    f"turn it refused (turn_preparation.py:{statement.lineno})",
                )
                self.assertIsInstance(
                    body[index + 1],
                    ast.Return,
                    "a pause must be followed by the return that carries it "
                    f"back to the loop (turn_preparation.py:{statement.lineno})",
                )
        self.assertEqual(
            6,
            pauses,
            "the six refusals that live here are the malformed review scope, "
            "the conversation-recovery gate, the ineligible repair child, its "
            "dirty writable set, the repository-intent gate, and the "
            "plan-store state-root gate",
        )

    def test_the_boundary_carries_no_loop_statements(self) -> None:
        stray = [
            f"line {statement.lineno}"
            for statement in ast.walk(self.module_source())
            if isinstance(statement, ast.Break | ast.Continue)
        ]
        self.assertEqual(
            [],
            stray,
            "the loop's `break` is the returned `paused` flag now; a "
            "surviving one would belong to a loop this module does not own",
        )

    def test_every_result_is_frozen(self) -> None:
        for record in (
            turn_preparation.SessionClassification,
            turn_preparation.ReviewPreparation,
            turn_preparation.PacketInputs,
            turn_preparation.RepairResumption,
            turn_preparation.TurnPrompt,
        ):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)

    def test_the_protocol_names_only_members_the_run_context_has(self) -> None:
        # The Protocol exists so this module never imports the runner; that is
        # only true if the runner's own context object still fits through it.
        declared = {
            name
            for name in vars(turn_preparation.RunContext)
            if not name.startswith("_")
        }
        self.assertTrue(declared)
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        for name in sorted(declared):
            with self.subTest(member=name):
                self.assertTrue(
                    name in fields or hasattr(run_loop_context.RunContext, name),
                    f"turn_preparation.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its work."""

    def loop_source(self) -> str:
        return run_loop_view.assembled_source()

    def test_the_loop_reaches_exactly_the_five_entry_points(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith("turn_preparation.")
        }
        self.assertEqual(set(ENTRY_POINTS), calls)

    def test_the_loop_no_longer_builds_a_prompt_itself(self) -> None:
        moved = {
            "task_packet.build_task_packet",
            "run_prompts.build_reply_prompt",
            "run_prompts.build_resolution_prompt",
            "run_prompts.build_session_prompt",
            "write_guards._pending_repair_child_continuation",
            "review_guard._repair_commit_preflight",
            "run_inputs._task_packet_repository_intent_report",
        }
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            set(),
            moved & calls,
            "prompt construction and repair resumption belong to "
            "turn_preparation now; a surviving call is work that escaped",
        )


class ClassificationTests(PreparationTestCase):
    """What kind of session was selected, decided once from its own file."""

    def test_an_ordinary_session_is_implementation_work(self) -> None:
        snapshot = self.snapshot()

        result = turn_preparation.classify_session(snapshot.sessions[0].path)

        self.assertFalse(result.is_review_session)
        self.assertFalse(result.is_repair_session)
        self.assertEqual(plans.SESSION_KIND_IMPLEMENTATION, result.session_kind)

    def test_a_generated_review_session_is_review_work(self) -> None:
        snapshot = self.snapshot()
        path = snapshot.sessions[0].path
        with mock.patch.object(
            turn_preparation.review_session_contract,
            "is_generated_review_session_file",
            return_value=True,
        ):
            result = turn_preparation.classify_session(path)

        self.assertTrue(result.is_review_session)
        self.assertEqual(plans.SESSION_KIND_REVIEW, result.session_kind)

    def test_a_generated_repair_child_is_still_implementation_work(self) -> None:
        snapshot = self.snapshot()
        path = snapshot.sessions[0].path
        with mock.patch.object(
            turn_preparation.review_repair,
            "is_generated_review_repair_session_file",
            return_value=True,
        ):
            result = turn_preparation.classify_session(path)

        self.assertTrue(result.is_repair_session)
        self.assertEqual(
            plans.SESSION_KIND_IMPLEMENTATION,
            result.session_kind,
            "a review authored the child, but the child does implementation "
            "work and must not be counted as a review turn",
        )


class ReviewPreparationTests(PreparationTestCase):
    """A pending review is re-derived, or refused; it is never assumed."""

    def test_an_implementation_session_is_left_alone(self) -> None:
        snapshot = self.snapshot()

        result = turn_preparation.prepare_review_session(
            self.ctx,
            session=snapshot.sessions[0],
            snapshot=snapshot,
            classification=self.classification(),
            cwd=self.tmp_dir,
            review_guard_excludes=(),
            review_guard_include_ignored=False,
        )

        self.assertIs(snapshot, result.snapshot)
        self.assertIsNone(result.workspace_snapshot)
        self.assertIsNone(result.session_version)
        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.pauses)

    def test_a_scope_that_cannot_be_re_derived_pauses(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        session.path.unlink()

        result = turn_preparation.prepare_review_session(
            self.ctx,
            session=session,
            snapshot=snapshot,
            classification=self.classification(review=True),
            cwd=self.tmp_dir,
            review_guard_excludes=(),
            review_guard_include_ignored=False,
        )

        self.assertTrue(result.paused)
        self.assertIsNone(result.workspace_snapshot)
        (pause,) = self.ctx.pauses
        self.assertEqual("malformed-plan", pause["reason"])
        self.assertIn("error", pause["details"])
        self.assertEqual([snapshot], self.ctx.final_snapshots)

    def test_a_refreshed_contract_forces_a_fresh_plan_image(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        refreshed = plans.inspect_plan(self.plan_dir)
        with (
            mock.patch.object(
                turn_preparation.review_session_contract,
                "refreshed_generated_review_contract",
                return_value="# refreshed\n",
            ),
            mock.patch.object(
                turn_preparation.review_delta,
                "_refresh_pending_review_scope",
                return_value=False,
            ),
            mock.patch.object(
                turn_preparation.readiness_gate,
                "_refresh_active_readiness_state",
            ) as refresh_readiness,
            mock.patch.object(
                turn_preparation.review_session_contract,
                "generated_review_session_version",
                return_value=2,
            ),
            mock.patch.object(self.ctx.plan_cache, "get_fresh", return_value=refreshed),
        ):
            result = turn_preparation.prepare_review_session(
                self.ctx,
                session=session,
                snapshot=snapshot,
                classification=self.classification(review=True),
                cwd=self.tmp_dir,
                review_guard_excludes=(),
                review_guard_include_ignored=False,
            )

        self.assertFalse(result.paused)
        self.assertIs(refreshed, result.snapshot)
        self.assertEqual(2, result.session_version)
        self.assertEqual("# refreshed\n", session.path.read_text(encoding="utf-8"))
        refresh_readiness.assert_called_once()

    def test_an_unchanged_contract_keeps_the_image_the_loop_had(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        original = session.path.read_text(encoding="utf-8")
        with (
            mock.patch.object(
                turn_preparation.review_delta,
                "_refresh_pending_review_scope",
                return_value=False,
            ),
            mock.patch.object(
                turn_preparation.review_session_contract,
                "generated_review_session_version",
                return_value=1,
            ),
            mock.patch.object(
                turn_preparation.readiness_gate,
                "_refresh_active_readiness_state",
            ) as refresh_readiness,
        ):
            result = turn_preparation.prepare_review_session(
                self.ctx,
                session=session,
                snapshot=snapshot,
                classification=self.classification(review=True),
                cwd=self.tmp_dir,
                review_guard_excludes=(),
                review_guard_include_ignored=False,
            )

        self.assertIs(snapshot, result.snapshot)
        self.assertEqual(original, session.path.read_text(encoding="utf-8"))
        refresh_readiness.assert_not_called()


class PacketInputTests(PreparationTestCase):
    """The session-shaped facts every prompt shape is built from."""

    def collect(self, **overrides: Any) -> turn_preparation.PacketInputs:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        kwargs: dict[str, Any] = {
            "classification": self.classification(),
            "args": self.args(),
            "session_id": "1",
            "mailbox_enabled": False,
        }
        kwargs.update(overrides)
        return turn_preparation.collect_packet_inputs(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def test_the_worker_mode_is_read_from_the_environment(self) -> None:
        with patched_env(ASL_WORKER_MODE="skill"):
            self.assertEqual(task_packet.WORKER_MODE_SKILL, self.collect().worker_mode)

    def test_an_expansion_and_a_progress_file_are_detected(self) -> None:
        snapshot = self.snapshot()
        self.assertFalse(self.collect(snapshot=snapshot).has_expansion)
        self.assertFalse(self.collect(snapshot=snapshot).has_progress)

        (self.plan_dir / "session-1.expand.md").write_text("# x\n", encoding="utf-8")
        (self.plan_dir / plans.PROGRESS_FILE_NAME).write_text("# p\n", encoding="utf-8")

        inputs = self.collect(snapshot=snapshot)
        self.assertTrue(inputs.has_expansion)
        self.assertTrue(inputs.has_progress)

    def test_verdict_references_are_added_only_for_a_review_session(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.review_delta,
                "_verification_repair_state",
                return_value={"references": ("a",)},
            ),
            mock.patch.object(
                turn_preparation.review_transitions,
                "review_verdict_repair_references",
                return_value=("b",),
            ),
        ):
            implementation = self.collect()
            review = self.collect(classification=self.classification(review=True))

        self.assertEqual(("a",), implementation.repair_references)
        self.assertEqual(("a", "b"), review.repair_references)

    def test_guidance_is_not_read_when_the_mailbox_is_disabled(self) -> None:
        self.assertEqual((), self.collect().pending_guidance)


class RepairResumptionTests(PreparationTestCase):
    """A repair child's scope, eligibility and clean tree, settled up front."""

    def resume(self, **overrides: Any) -> turn_preparation.RepairResumption:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        kwargs: dict[str, Any] = {
            "session": snapshot.sessions[0],
            "classification": self.classification(repair=True),
            "args": self.args(),
            "cwd": self.tmp_dir,
            "workspace": None,
            "session_scope": None,
            "session_label": "Session 1",
            "session_identity": {"session_id": "1"},
            "session_details": {"session_id": "1"},
            "retained_scope_repair": None,
            "max_review_fix_cycles": 1,
            "review_requested": False,
        }
        kwargs.update(overrides)
        return turn_preparation.resume_repair_session(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def test_an_ordinary_session_is_left_alone(self) -> None:
        result = self.resume(classification=self.classification())

        self.assertIsNone(result.session_scope)
        self.assertIsNone(result.regenerated_write_scope)
        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.persistence.events)

    def test_an_ineligible_child_never_reaches_commit_preflight(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.write_guards,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=False, blocker="repair-scope-stale"
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard, "_repair_commit_preflight"
            ) as preflight,
        ):
            result = self.resume()

        self.assertTrue(result.paused)
        preflight.assert_not_called()
        (pause,) = self.ctx.pauses
        self.assertEqual(events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, pause["reason"])
        self.assertIn("repair-scope-stale", pause["message"])
        self.assertIn("review_resume", pause["details"])

    def test_a_dirty_writable_set_pauses_before_the_turn(self) -> None:
        error = review_guard.RepairCommitPreflightError("boom", {"dirty": ["a.py"]})
        with (
            mock.patch.object(
                turn_preparation.write_guards,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=True, blocker=None
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard,
                "_repair_commit_preflight",
                side_effect=error,
            ),
        ):
            result = self.resume()

        self.assertTrue(result.paused)
        (pause,) = self.ctx.pauses
        self.assertEqual("commit-preflight", pause["reason"])
        self.assertEqual(["a.py"], pause["details"]["dirty"])
        self.assertEqual([], self.ctx.persistence.events)

    def test_a_clean_child_records_its_preflight_once(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.write_guards,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=True, blocker=None
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard,
                "_repair_commit_preflight",
                return_value={"clean": True},
            ),
        ):
            result = self.resume()

        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(
            [{"session_id": "1", "clean": True}],
            self.ctx.payload["repair_commit_preflights"],
        )
        ((name, payload),) = self.ctx.persistence.events
        self.assertEqual(events.REPAIR_COMMIT_PREFLIGHT_EVENT, name)
        self.assertEqual(RUN_ID, payload["run_id"])
        self.assertEqual(1, len(self.ctx.persistence.states))


class TurnPromptTests(PreparationTestCase):
    """One of three prompt shapes, then the gate that judges what it reaches."""

    def prepare(self, **overrides: Any) -> turn_preparation.TurnPrompt:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        inputs = overrides.pop("inputs", None) or turn_preparation.PacketInputs(
            worker_mode=task_packet.WORKER_MODE_PACKET,
            predecessor_id=None,
            has_expansion=False,
            has_progress=False,
            repair_references=(),
            pending_guidance=(),
        )
        kwargs: dict[str, Any] = {
            "classification": self.classification(),
            "inputs": inputs,
            "args": self.args(),
            "cwd": self.tmp_dir,
            "harness": "codex",
            "session_id": "1",
            "session_label": "Session 1",
            "session_details": {"session_id": "1"},
            "session_scope": None,
            "workspace": None,
            "mailbox_enabled": False,
            "reply": None,
            "resolution": None,
        }
        kwargs.update(overrides)
        return turn_preparation.prepare_turn_prompt(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def reply(self) -> run_prompts._PendingReply:
        return run_prompts._PendingReply(
            text="do the thing",
            context=self.qna_context(),
            origin=run_prompts.REPLY_ORIGIN_ARGV,
            process_start=False,
            question_id="q1",
        )

    def resolution(self) -> run_prompts._PendingResolution:
        return run_prompts._PendingResolution(
            action=mailbox_vocabulary.RESOLUTION_ACTIONS[0],
            note="a note",
            context=self.qna_context(),
            question_id="q1",
            resolution_message_id="m1",
            resolution_seq=4,
        )

    def test_a_fresh_start_persists_the_prompt_it_sends(self) -> None:
        result = self.prepare()

        self.assertEqual("session", result.prompt_kind)
        self.assertEqual(result.prompt, result.persisted_prompt)
        self.assertIsNone(result.reply_source)
        self.assertIsNone(result.resolution_source)
        self.assertFalse(result.paused)

    def test_a_reply_persists_a_placeholder_rather_than_the_words(self) -> None:
        reply = self.reply()

        result = self.prepare(reply=reply)

        self.assertEqual("reply", result.prompt_kind)
        self.assertNotIn(reply.text, result.persisted_prompt)
        self.assertIn(reply.text, result.prompt)
        assert result.reply_source is not None
        self.assertEqual(len(reply.text), result.reply_source["reply_length"])
        self.assertEqual("q1", result.reply_source["question_id"])
        self.assertNotIn("reply_message_id", result.reply_source)
        self.assertIsNone(result.resolution_source)

    def test_a_resolution_persists_a_placeholder_and_a_body_free_source(self) -> None:
        resolution = self.resolution()

        result = self.prepare(resolution=resolution)

        self.assertEqual("resolution", result.prompt_kind)
        assert resolution.note is not None
        self.assertNotIn(resolution.note, result.persisted_prompt)
        assert result.resolution_source is not None
        self.assertEqual(len(resolution.note), result.resolution_source["note_length"])
        self.assertNotIn("note", result.resolution_source)
        self.assertIsNone(result.reply_source)

    def test_a_reply_and_a_resolution_together_are_a_programming_error(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.prepare(reply=self.reply(), resolution=self.resolution())

        self.assertIn("mutually exclusive", str(caught.exception))

    def test_skill_mode_renders_through_the_legacy_builder(self) -> None:
        inputs = turn_preparation.PacketInputs(
            worker_mode=task_packet.WORKER_MODE_SKILL,
            predecessor_id=None,
            has_expansion=False,
            has_progress=False,
            repair_references=(),
            pending_guidance=(),
        )
        with (
            mock.patch.object(
                turn_preparation.run_prompts,
                "build_session_prompt",
                return_value="skill prompt",
            ) as legacy,
            mock.patch.object(turn_preparation.task_packet, "build_task_packet") as pkt,
        ):
            result = self.prepare(inputs=inputs)

        self.assertEqual("skill prompt", result.prompt)
        legacy.assert_called_once()
        pkt.assert_not_called()

    def test_the_intent_gate_reads_the_rendered_prompt(self) -> None:
        with mock.patch.object(
            turn_preparation.run_inputs,
            "_task_packet_repository_intent_report",
            return_value={"registry_required": True},
        ) as report:
            result = self.prepare()

        self.assertTrue(result.paused)
        self.assertEqual(
            result.prompt,
            report.call_args.args[0],
            "the gate must judge the text the worker receives, not the plan "
            "the text was built from",
        )
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED, pause["reason"]
        )
        self.assertIn("Session 1", pause["message"])
        self.assertEqual(
            {"registry_required": True}, pause["details"]["repository_intent"]
        )

    def test_a_registered_workspace_skips_the_gate_entirely(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.run_inputs,
                "_task_packet_repository_intent_report",
            ) as report,
            mock.patch.object(turn_preparation.task_packet, "build_task_packet"),
        ):
            result = self.prepare(workspace=mock.Mock(), session_scope=mock.Mock())

        self.assertFalse(result.paused)
        report.assert_not_called()

    def test_state_root_gate_applies_to_every_prompt_shape_and_workspace_mode(
        self,
    ) -> None:
        for mode in (task_packet.WORKER_MODE_PACKET, task_packet.WORKER_MODE_SKILL):
            for shape in (
                {},
                {"reply": self.reply()},
                {"resolution": self.resolution()},
            ):
                for registered in (False, True):
                    with self.subTest(mode=mode, shape=shape, registered=registered):
                        self.ctx.pauses.clear()
                        inputs = turn_preparation.PacketInputs(
                            worker_mode=mode,
                            predecessor_id=None,
                            has_expansion=False,
                            has_progress=False,
                            repair_references=(),
                            pending_guidance=(),
                        )
                        report = {"required_state_roots": ["/external/store"]}
                        with (
                            mock.patch.object(
                                turn_preparation.run_inputs,
                                "_session_plan_store_state_root_report",
                                return_value=report,
                            ) as gate,
                            mock.patch.object(
                                turn_preparation.task_packet, "build_task_packet"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_session_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_reply_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_resolution_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_inputs,
                                "_task_packet_repository_intent_report",
                                return_value=None,
                            ),
                        ):
                            result = self.prepare(
                                inputs=inputs,
                                workspace=mock.Mock() if registered else None,
                                session_scope=mock.Mock() if registered else None,
                                **shape,
                            )
                        self.assertTrue(result.paused)
                        gate.assert_called_once()
                        (pause,) = self.ctx.pauses
                        self.assertEqual(
                            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                            pause["reason"],
                        )
                        self.assertEqual(
                            report, pause["details"]["plan_store_state_root"]
                        )
                        self.assertIn("no worker was launched", pause["message"])


if __name__ == "__main__":
    unittest.main()

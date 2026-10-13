"""Idle-time accounting and the idle-baseline structural guard."""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path
from types import ModuleType

from agent_session_loop import clock, events, run_commit, run_reporting
from tests import support_asl_run_loop_view as run_loop_view
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.harness_integration_support import IDLE_BASELINE_DELEGATES
from tests.support import (
    FakeRunControlListener,
    init_git_repo,
    read_jsonl,
    write_plan,
)


class IdleBaselineTests(FakeHarnessIntegrationBase):
    def test_live_turn_action_required_payload_includes_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": clock.isoformat(clock.utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=run_reporting._ProgressReporter(enabled=False),
                active_turn=active_turn,
                heartbeat_interval=60,
            )

            live_turn.start()
            live_turn.record_activity("stderr", "approval required before continuing")
            # The exceptional transition is durable before the turn ends;
            # ordinary activity waits for the heartbeat instead.
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            live_turn.stop()
            live_turn.finish(clock.utc_now())

            event_records = read_jsonl(event_path)
            action_events = [
                record["payload"]
                for record in event_records
                if record["name"] == events.ACTION_REQUIRED_EVENT
            ]
            self.assertEqual(1, len(action_events))
            self.assertEqual("codex", action_events[0]["harness"])
            self.assertEqual("codex", state_payload["action_required"]["harness"])

    def test_idle_time_excludes_the_previous_turns_verification(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        slow_check = Path(tmp.name) / "slow-check.sh"
        slow_check.write_text("#!/bin/sh\nsleep 1\nexit 0\n", encoding="utf-8")
        contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- it runs",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": "1",
                        "checks": [
                            {
                                "name": "slow-check",
                                "type": "command",
                                "argv": ["sh", str(slow_check)],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                    indent=2,
                ),
                "```",
            ]
        )

        def inject_contract(plan_dir: Path, _state_dir: Path) -> None:
            write_plan(
                plan_dir, ("pending", "pending"), contracts_by_id={"1": contract}
            )

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=FakeRunControlListener(),
                before_run=inject_contract,
            )
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        verification_finished_at = next(
            clock.parse_datetime(record["payload"]["finished_at"])
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        harness_exited_at = clock.parse_datetime(turns[0]["finished_at"])
        idle_started_at = self.idle_started_at(turns[1])
        # The idle clock starts after the runner's own verification and
        # finalization, so it cannot begin back at the harness's exit.
        self.assertGreaterEqual(idle_started_at, verification_finished_at)
        self.assertGreaterEqual(
            (idle_started_at - harness_exited_at).total_seconds(), 1.0
        )

    def test_idle_time_excludes_generated_review_creation(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="review-creation-idle",
            review_outcomes="all-clear",
            max_review_fix_cycles=0,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2"], [turn["session_id"] for turn in turns])
        review_created_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_SESSION_CREATED_EVENT,
        )
        # Creating the generated review session is runner-owned work between
        # the two turns, so the review turn's idle clock starts after it.
        self.assertGreaterEqual(self.idle_started_at(turns[1]), review_created_at)

    def test_idle_time_excludes_review_repair_materialization(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)
        # The repair child runs behind `review_guard._repair_commit_preflight`,
        # which
        # refuses to start on a dirty writable repository. Give the run its own
        # clean repository so the gate answers to this test rather than to
        # whatever is uncommitted in this checkout.
        repo_dir = plan_dir.parent / "repo"
        init_git_repo(repo_dir)

        _first_exit, first_payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="repair-idle-seed",
            review_outcomes="findings,all-clear",
            max_review_fix_cycles=0,
            cwd=repo_dir,
        )
        self.assertEqual(
            "higher-cap-required",
            first_payload["review_resume"]["blocker"],
            first_payload,
        )
        # Reopening the implementation session makes the resumed run execute a
        # turn *before* the all-complete branch materializes the repair child,
        # which is the only arrangement where that materialization can be
        # charged to a later turn as idle time.
        session_one = plan_dir / "session-1.md"
        session_one.write_text(
            session_one.read_text(encoding="utf-8").replace(
                "- **Status**: complete", "- **Status**: pending", 1
            ),
            encoding="utf-8",
        )

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="repair-idle-resume",
            review_outcomes="findings,all-clear",
            max_review_fix_cycles=1,
            cwd=repo_dir,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2.1", "3"], [turn["session_id"] for turn in turns])
        reopened_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_REPAIR_REOPENED_EVENT,
        )
        # Materializing the repair child, re-gating the grown plan and
        # refreshing readiness are all runner-owned work, so the repair turn's
        # idle clock cannot start back at the previous turn's finalization.
        self.assertGreaterEqual(self.idle_started_at(turns[1]), reopened_at)

    def test_idle_time_excludes_review_verdict_retry(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="verdict-retry-idle",
            review_outcomes="invalid-artifact,all-clear",
            max_review_fix_cycles=0,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2", "2"], [turn["session_id"] for turn in turns])
        retried_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_VERDICT_RETRY_EVENT,
        )
        # Rejecting the verdict and reverting the review session is
        # loop-owned work on a path that returns to the loop top with a
        # bare `continue`, so the retried turn's idle clock starts after it.
        self.assertGreaterEqual(self.idle_started_at(turns[2]), retried_at)

    def loop_source(self) -> str:
        return run_loop_view.assembled_source()

    def names_loop_state(self, node: ast.AST, field: str) -> bool:
        """Is this node the loop's ``field``, held bare or on the run context?

        The counters and flags this guard tracks live on ``RunContext`` so the
        loop's helpers can state that they read them, but the guard is about
        where in the loop they are written, not about which object holds them.
        Matching both spellings keeps the structural claim intact across that
        move instead of retiring it.
        """
        if isinstance(node, ast.Name):
            return node.id == field
        return (
            isinstance(node, ast.Attribute)
            and node.attr == field
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
        )

    def session_loop_node(self, tree: ast.Module | None = None) -> ast.While:
        if tree is None:
            tree = ast.parse(self.loop_source())
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.While)
            and any(
                self.names_loop_state(name, "runner_work_finished_at")
                for statement in node.body
                for name in ast.walk(statement)
            )
        ]
        self.assertEqual(1, len(loops), "expected exactly one session loop")
        return loops[0]

    def idle_baseline_assignments(self, node: ast.AST) -> list[ast.Assign]:
        return [
            statement
            for statement in ast.walk(node)
            if isinstance(statement, ast.Assign)
            and any(
                self.names_loop_state(target, "runner_work_finished_at")
                for target in statement.targets
            )
        ]

    def assigns_idle_baseline_pin(self, statement: ast.AST, value: bool) -> bool:
        return (
            isinstance(statement, ast.Assign)
            and any(
                self.names_loop_state(target, "idle_baseline_pinned")
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is value
        )

    def pins_idle_baseline(self, statement: ast.stmt) -> bool:
        return self.assigns_idle_baseline_pin(statement, True)

    def clears_idle_baseline_pin(self, statement: ast.AST) -> bool:
        return self.assigns_idle_baseline_pin(statement, False)

    def pinned_loop_head_arm(self, loop: ast.While) -> ast.If:
        arm: ast.stmt | None = loop.body[0]
        while isinstance(arm, ast.If):
            if self.names_loop_state(arm.test, "idle_baseline_pinned"):
                return arm
            arm = arm.orelse[0] if len(arm.orelse) == 1 else None
        self.fail(
            "the session loop's first statement must branch on "
            "idle_baseline_pinned alone; an arm selected by anything else "
            "leaves paths where the pin is neither honoured nor cleared"
        )

    def idle_baseline_roots(self, loop: ast.While) -> list[ast.AST]:
        """The loop plus every boundary a terminating branch delegates to.

        The pin used to sit in the loop, so counting pins meant walking the
        loop. Session 35 moved the mailbox reply wait into
        ``turn_dispositions``, and a guard that kept walking only the loop
        would now pass by finding nothing at all. The claim is about the run's
        idle clock, not about one file, so the search follows the branches
        wherever they went: a module named here is one the loop hands a
        terminating disposition to, and a second pin appearing in any of them
        is the same defect it always was.
        """
        delegates = (
            ast.parse(self.module_source(module)) for module in IDLE_BASELINE_DELEGATES
        )
        return [loop, *delegates]

    def module_source(self, module: ModuleType) -> str:
        return Path(module.__file__ or "").read_text(encoding="utf-8")

    def followed_by_pin(self, loop: ast.AST, assignment: ast.Assign) -> bool:
        for node in ast.walk(loop):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for index, statement in enumerate(block):
                    if statement is not assignment:
                        continue
                    following = block[index + 1 :]
                    return bool(following) and self.pins_idle_baseline(following[0])
        return False

    def check_the_pinned_arm_clears_the_pin(self, tree: ast.Module) -> None:
        arm = self.pinned_loop_head_arm(self.session_loop_node(tree))
        for statement in arm.body:
            if self.clears_idle_baseline_pin(statement):
                return
            self.assertFalse(
                any(
                    self.clears_idle_baseline_pin(node) for node in ast.walk(statement)
                ),
                "the pinned arm clears idle_baseline_pinned only on some "
                "branch; a pass that leaves it set suppresses every later "
                "loop-top refresh, so the baseline stays at the pinning "
                f"path's instant (agent_session_loop/runner.py:{statement.lineno})",
            )
            self.assertFalse(
                any(
                    isinstance(node, ast.Break | ast.Continue | ast.Return | ast.Raise)
                    for node in ast.walk(statement)
                ),
                "the pinned arm can leave the loop head before clearing "
                "idle_baseline_pinned, which strands the pin for every later "
                f"pass (agent_session_loop/runner.py:{statement.lineno})",
            )
        self.fail(
            "the pinned arm of the session loop's first statement must reset "
            "idle_baseline_pinned to False: the pin is a one-shot opt-out, and "
            "leaving it set turns the single structural refresh into a "
            "baseline that never moves again"
        )

    def tree_with_pinned_arm_body(self, body: tuple[str, ...]) -> ast.Module:
        source = self.loop_source()
        arm = self.pinned_loop_head_arm(self.session_loop_node(ast.parse(source)))
        first, last = arm.body[0], arm.body[-1]
        assert last.end_lineno is not None
        lines = source.splitlines()
        lines[first.lineno - 1 : last.end_lineno] = [
            " " * first.col_offset + line for line in body
        ]
        return ast.parse("\n".join(lines))

    def test_idle_baseline_is_refreshed_where_the_loop_cannot_bypass_it(self) -> None:
        tree = ast.parse(self.loop_source())
        self.check_the_pinned_arm_clears_the_pin(tree)
        for mutation in (
            ("pass",),
            ("if ctx.first_loop_pass:", "    ctx.idle_baseline_pinned = False"),
        ):
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(AssertionError),
            ):
                self.check_the_pinned_arm_clears_the_pin(
                    self.tree_with_pinned_arm_body(mutation)
                )

        loop = self.session_loop_node(tree)
        head = loop.body[0]
        structural = self.idle_baseline_assignments(head)
        self.assertEqual(
            1,
            len(structural),
            "the idle baseline must be refreshed by the session loop's first "
            "statement, so no path back to the loop top can bypass it",
        )

        roots = self.idle_baseline_roots(loop)
        pins = [
            statement
            for root in roots
            for statement in ast.walk(root)
            if self.pins_idle_baseline(statement)
        ]
        self.assertEqual(
            1,
            len(pins),
            "only the mailbox reply wait may opt out of the structural "
            "refresh; a second opt-out is a path whose idle clock silently "
            "stops tracking runner work",
        )

        for root in roots:
            for assignment in self.idle_baseline_assignments(root):
                if assignment in structural:
                    continue
                self.assertTrue(
                    self.followed_by_pin(root, assignment),
                    "a hand-stamped idle baseline outside the loop top must "
                    "pin itself, otherwise the next pass overwrites it and "
                    f"the per-path stamping this replaced is back (line "
                    f"{assignment.lineno})",
                )

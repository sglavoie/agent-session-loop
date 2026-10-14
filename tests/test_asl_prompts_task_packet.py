"""Task packet builder tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import (
    harnesses,
    mailbox_vocabulary,
    plans_handoff,
    plans_payloads,
    plans_session_scope,
    review,
    run_prompts,
    task_packet,
    workspaces,
)
from tests.support_asl_prompts import TaskPacketFixtureMixin


class TaskPacketBuilderTests(TaskPacketFixtureMixin, unittest.TestCase):
    def test_packet_names_selected_session_and_file_path(self) -> None:
        packet = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertEqual("9", packet.session_id)
        self.assertEqual(self._canon("session-9.md"), packet.session_path)
        rendered = packet.render()
        self.assertIn("- Session ID: 9", rendered)
        self.assertIn(self._canon("session-9.md"), rendered)
        self.assertIn("fixed — do not reinterpret", rendered)

    def test_packet_references_context_and_progress_as_paths(self) -> None:
        packet = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertEqual(self._canon("context.md"), packet.context_path)
        self.assertEqual(self._canon("progress.md"), packet.progress_path)
        rendered = packet.render()
        self.assertIn(f"- Shared context: {self._canon('context.md')}", rendered)
        self.assertIn(self._canon("progress.md"), rendered)

    def test_progress_reference_omitted_when_absent(self) -> None:
        packet = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9", has_progress=False
        )
        self.assertIsNone(packet.progress_path)
        self.assertNotIn("Progress journal", packet.render())

    def test_expansion_reference_only_when_present(self) -> None:
        without = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertIsNone(without.expansion_path)
        self.assertNotIn("session-9.expand.md", without.render())
        self.assertNotIn("Expansion notes", without.render())

        with_expansion = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9", has_expansion=True
        )
        self.assertEqual(
            self._canon("session-9.expand.md"),
            with_expansion.expansion_path,
        )
        self.assertIn("session-9.expand.md", with_expansion.render())

    def test_predecessor_handoff_only_when_applicable(self) -> None:
        first = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="1")
        self.assertIsNone(first.predecessor_path)
        self.assertNotIn("Predecessor handoff", first.render())

        later = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9", predecessor_id="8"
        )
        self.assertEqual("8", later.predecessor_id)
        self.assertEqual(self._canon("session-8.md"), later.predecessor_path)
        rendered = later.render()
        self.assertIn(f"Predecessor handoff: {self._canon('session-8.md')}", rendered)
        self.assertIn("`## Handoff`", rendered)

    def test_repair_references_only_when_provided(self) -> None:
        without = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertNotIn("Verification repair references", without.render())

        with_refs = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            repair_references=["fingerprint=verify-failed:check-1", "prior fail"],
        )
        self.assertEqual(
            ("fingerprint=verify-failed:check-1", "prior fail"),
            with_refs.repair_references,
        )
        rendered = with_refs.render()
        self.assertIn("## Verification repair references", rendered)
        self.assertIn("- fingerprint=verify-failed:check-1", rendered)

    def test_packet_states_handoff_mutation_and_note_behavior(self) -> None:
        rendered = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9"
        ).render()
        self.assertIn("- **Status**: complete", rendered)
        self.assertIn(plans_handoff.DISCOVERED_FACTS_HEADING, rendered)
        self.assertIn(plans_handoff.OPEN_QUESTIONS_HEADING, rendered)
        self.assertIn(".expand.md", rendered)
        self.assertIn("never inline in chat", rendered)

    def test_review_session_packet_renders_reviewer_role_not_worker_role(self) -> None:
        implementation = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9"
        )
        review_packet = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            session_kind=plans_payloads.SESSION_KIND_REVIEW,
        )

        self.assertFalse(implementation.is_review)
        self.assertTrue(review_packet.is_review)
        rendered = review_packet.render()
        self.assertIn("You are an autonomous ASL reviewer", rendered)
        self.assertNotIn("You are an autonomous ASL worker", rendered)
        self.assertIn("## Read before reviewing", rendered)
        self.assertNotIn("## Read before implementing", rendered)
        self.assertIn("## Review", rendered)
        self.assertNotIn("## Implement\n", rendered)
        self.assertIn("Judge the work; do not repair it", rendered)
        self.assertIn("run focused read-only checks", rendered)
        self.assertIn(review.FINAL_REVIEW_FILENAME, rendered)
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, rendered)
        self.assertIn("a fix is not yours to write", rendered)
        self.assertIn("as a finding with its own repair group", rendered)
        self.assertIn("unmet accepted requirement or a concrete defect", rendered)
        self.assertIn("Optional improvements are not new acceptance criteria", rendered)

    def test_unknown_session_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex", self.PLAN_DIR, session_id="9", session_kind="auditor"
            )

    def test_packet_states_runner_signal_protocol(self) -> None:
        for kind in (
            plans_payloads.SESSION_KIND_IMPLEMENTATION,
            plans_payloads.SESSION_KIND_REVIEW,
        ):
            with self.subTest(kind=kind):
                rendered = task_packet.build_task_packet(
                    "codex", self.PLAN_DIR, session_id="9", session_kind=kind
                ).render()
                self.assertIn("Runner status: complete", rendered)
                self.assertIn("Runner status: paused", rendered)
                self.assertIn("RUNNER_PAUSE: <reason> - <detail>", rendered)
                self.assertIn("every background command you started", rendered)
                self.assertIn("commands the harness auto-backgrounded", rendered)
                self.assertIn(
                    "Reading a command's output file does not prove its process exited",
                    rendered,
                )

    def test_packet_separates_focused_validation_from_runner_verification(self) -> None:
        rendered = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9"
        ).render()

        self.assertIn("runner owns authoritative final verification", rendered)
        self.assertIn("you own focused validation during implementation", rendered)
        self.assertIn("run focused checks that help you iterate", rendered)
        self.assertIn("Do not rerun the full Completion Contract", rendered)
        self.assertIn("restores this session to pending", rendered)
        self.assertNotIn("run any tests the session names", rendered)
        self.assertIn("Reuse still-valid evidence for unchanged behavior", rendered)
        self.assertIn("do not redesign boundaries or remove required checks", rendered)

    def test_packet_states_when_repeated_or_broader_checking_is_justified(
        self,
    ) -> None:
        """The final-gate ban must not read as a ban on checking at all."""
        for kind in (
            plans_payloads.SESSION_KIND_IMPLEMENTATION,
            plans_payloads.SESSION_KIND_REVIEW,
        ):
            with self.subTest(kind=kind):
                rendered = task_packet.build_task_packet(
                    "codex", self.PLAN_DIR, session_id="9", session_kind=kind
                ).render()

                # The three cases that justify running a check again.
                self.assertIn(
                    "Running a check again, or running a broader one, is justified",
                    rendered,
                )
                self.assertIn("invalidated it", rendered)
                self.assertIn("a failure needs diagnosis", rendered)
                self.assertIn(
                    "concrete correctness concern none of your checks covered",
                    rendered,
                )
                # A justified repeat is argued in the handoff, not assumed.
                self.assertIn(
                    "record the brief reason in this session's handoff", rendered
                )
                # No blanket cap on how much checking a turn may do.
                self.assertIn(
                    "Nothing here caps how many checks you run or how long they take",
                    rendered,
                )

    def test_packet_repeat_policy_matches_the_session_role(self) -> None:
        implementation = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9"
        ).render()
        review_rendered = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            session_kind=plans_payloads.SESSION_KIND_REVIEW,
        ).render()

        self.assertIn(
            "keep the focused checks that cover the behavior this session changed",
            implementation,
        )
        self.assertNotIn("read-only checks that substantiate", implementation)
        self.assertIn(
            "keep the focused read-only checks that substantiate a finding",
            review_rendered,
        )
        self.assertNotIn("behavior this session changed", review_rendered)

    def test_packet_has_no_manual_mode_policy(self) -> None:
        rendered = task_packet.build_task_packet(
            "codex", self.PLAN_DIR, session_id="9", predecessor_id="8"
        ).render()
        lowered = rendered.lower()
        # No manual-mode readiness/lock-repair/preflight policy.
        for forbidden in (
            "preflight",
            "asl unlock",
            "--mark-loop-ready",
            "backfill-progress",
            "readiness sha",
            "next_pending",
            "auto-advance",
        ):
            self.assertNotIn(forbidden, lowered)
        # No manual skill invocation drives session selection.
        self.assertNotIn("session-run", lowered)

    def test_reply_context_only_when_provided(self) -> None:
        without = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertFalse(without.has_reply_context)
        self.assertNotIn(run_prompts.REPLY_CONTEXT_HEADING, without.render())

        with_reply = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            reply="1a, 2b",
            reply_context=self._make_reply_context(),
        )
        self.assertTrue(with_reply.has_reply_context)
        rendered = with_reply.render()
        self.assertIn(run_prompts.REPLY_CONTEXT_HEADING, rendered)
        self.assertIn("### Operator reply", rendered)
        self.assertIn("1a, 2b", rendered)
        self.assertIn("### Instructions", rendered)

    def test_reply_block_matches_reply_prompt_body(self) -> None:
        context = self._make_reply_context(pause_details={"question_index": 1})
        packet = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            reply="  1a  ",
            reply_context=context,
        )
        reply_prompt = run_prompts.build_reply_prompt(
            "codex", self.PLAN_DIR, reply="  1a  ", context=context
        )
        _, _, reply_body = reply_prompt.partition(run_prompts.REPLY_CONTEXT_HEADING)
        _, _, packet_body = packet.render().partition(run_prompts.REPLY_CONTEXT_HEADING)
        self.assertEqual(reply_body, packet_body)

    def test_reply_and_context_must_be_provided_together(self) -> None:
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex", self.PLAN_DIR, session_id="9", reply="1a"
            )
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex",
                self.PLAN_DIR,
                session_id="9",
                reply_context=self._make_reply_context(),
            )

    def test_resolution_context_only_when_provided(self) -> None:
        without = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertFalse(without.has_resolution_context)
        self.assertNotIn(run_prompts.RESOLUTION_CONTEXT_HEADING, without.render())

        with_resolution = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            resolution_note="Proceed without choosing a variant.",
            resolution_context=self._make_reply_context(),
        )
        self.assertTrue(with_resolution.has_resolution_context)
        self.assertFalse(with_resolution.has_reply_context)
        rendered = with_resolution.render()
        self.assertIn(run_prompts.RESOLUTION_CONTEXT_HEADING, rendered)
        self.assertIn("### Supervisor resolution", rendered)
        self.assertIn("No operator answer was supplied", rendered)

    def test_resolution_block_matches_resolution_prompt_body(self) -> None:
        context = self._make_reply_context(pause_details={"question_index": 1})
        packet = task_packet.build_task_packet(
            "codex",
            self.PLAN_DIR,
            session_id="9",
            resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            resolution_note="keep scope",
            resolution_context=context,
        )
        resolution_prompt = run_prompts.build_resolution_prompt(
            "codex",
            self.PLAN_DIR,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="keep scope",
            context=context,
        )
        _, _, resolution_body = resolution_prompt.partition(
            run_prompts.RESOLUTION_CONTEXT_HEADING
        )
        _, _, packet_body = packet.render().partition(
            run_prompts.RESOLUTION_CONTEXT_HEADING
        )
        self.assertEqual(resolution_body, packet_body)

    def test_reply_and_resolution_contexts_are_mutually_exclusive(self) -> None:
        context = self._make_reply_context()
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex",
                self.PLAN_DIR,
                session_id="9",
                reply="1a",
                reply_context=context,
                resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                resolution_context=context,
            )

    def test_resolution_action_and_context_must_be_provided_together(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex",
                self.PLAN_DIR,
                session_id="9",
                resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            )
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex",
                self.PLAN_DIR,
                session_id="9",
                resolution_context=self._make_reply_context(),
            )

    def test_empty_reply_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            task_packet.build_task_packet(
                "codex",
                self.PLAN_DIR,
                session_id="9",
                reply="   \n ",
                reply_context=self._make_reply_context(),
            )

    def test_packets_are_harness_neutral(self) -> None:
        renders = {
            harness: task_packet.build_task_packet(
                harness, self.PLAN_DIR, session_id="9", predecessor_id="8"
            ).render()
            for harness in ("codex", "claude", "copilot")
        }
        self.assertEqual(1, len(set(renders.values())))

    def test_unknown_harness_raises(self) -> None:
        with self.assertRaises(harnesses.HarnessError):
            task_packet.build_task_packet("bogus", self.PLAN_DIR, session_id="9")

    def test_plan_dir_is_canonicalized_with_trailing_slash(self) -> None:
        packet = task_packet.build_task_packet("codex", "/tmp/plan/", session_id="9")
        self.assertTrue(packet.plan_dir.endswith("/tmp/plan/"))

    def test_nested_session_id_is_preserved(self) -> None:
        packet = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="2.1")
        self.assertEqual("2.1", packet.session_id)
        self.assertEqual(self._canon("session-2.1.md"), packet.session_path)

    def test_disabled_packet_keeps_historical_prompt(self) -> None:
        packet = task_packet.build_task_packet("codex", self.PLAN_DIR, session_id="9")
        self.assertFalse(packet.mailbox_enabled)
        self.assertIsNone(packet.mailbox_read_command)
        self.assertIsNone(packet.mailbox_notify_command)
        self.assertIsNone(packet.mailbox_ask_command)
        self.assertIsNone(packet.mailbox_resume_ask_command)
        self.assertNotIn(run_prompts.MAILBOX_PROTOCOL_HEADING, packet.render())


class MultiRepositoryTaskPacketTests(unittest.TestCase):
    def test_packet_renders_registry_scope_and_only_read_roots(self) -> None:
        svc = workspaces.Repository("SVC", "svc", "git@github.com:example/svc.git")
        oak = workspaces.Repository("OAK", "oak", "git@github.com:example/oak.git")
        workspace = workspaces.ResolvedWorkspace(
            root=Path("/tmp/workspace"),
            repositories=(
                workspaces.ResolvedRepository(
                    repository=svc,
                    root=Path("/tmp/workspace/svc"),
                    origin=svc.origin,
                ),
                workspaces.ResolvedRepository(
                    repository=oak,
                    root=Path("/tmp/workspace/oak"),
                    origin=oak.origin,
                ),
            ),
        )

        rendered = task_packet.build_task_packet(
            "codex",
            "/tmp/plan",
            session_id="1",
            workspace=workspace,
            session_scope=plans_session_scope.SessionScope(
                reads=("SVC",), writes=("SVC",)
            ),
        ).render()

        self.assertIn("Expected read aliases: SVC", rendered)
        self.assertIn("Writable aliases: SVC", rendered)
        self.assertIn("OAK: path=oak", rendered)
        self.assertIn(str(workspace.get("SVC").root), rendered)
        self.assertNotIn(str(workspace.get("OAK").root), rendered)
        self.assertIn("Alias:path/to/file", rendered)

        for writes in (("SVC",), ()):
            scope = plans_session_scope.SessionScope(
                reads=("SVC", "OAK"), writes=writes
            )
            prompts = {
                "packet": task_packet.build_task_packet(
                    "codex",
                    "/tmp/plan",
                    session_id="1",
                    workspace=workspace,
                    session_scope=scope,
                ).render(),
                "review-packet": task_packet.build_task_packet(
                    "codex",
                    "/tmp/plan",
                    session_id="1",
                    session_kind=plans_payloads.SESSION_KIND_REVIEW,
                    workspace=workspace,
                    session_scope=scope,
                ).render(),
                "skill": run_prompts.build_session_prompt(
                    "codex",
                    "/tmp/plan",
                    workspace=workspace,
                    session_scope=scope,
                ),
            }
            for mode, prompt in prompts.items():
                with self.subTest(mode=mode, writes=writes):
                    self.assertIn("including untracked and ignored files", prompt)
                    self.assertIn("`--no-cache`", prompt)
                    self.assertIn("already-authorized scratch location", prompt)
                    self.assertIn("Do not widen write scope", prompt)
                    self.assertIn(
                        "Writable aliases: " + (", ".join(writes) or "(none)"),
                        prompt,
                    )

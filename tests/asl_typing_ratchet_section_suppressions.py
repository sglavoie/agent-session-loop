# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_typing_ratchet_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_typing_ratchet_shared import *
from .support_base import load_section_tests
from .asl_typing_ratchet_section_codes import *
from .asl_typing_ratchet_section_modules import *
from .asl_typing_ratchet_section_tables import *

import re


class DurableBoundaryTypingTests(unittest.TestCase):
    """Boundary modules keep their durable-boundary payloads typed.

    Scoped to the Boundary table. These four rules are extraction discipline,
    which the Checked table's members are on that table precisely because they
    cannot meet.
    """

    def boundary_modules(self) -> list[str]:
        return list(ratcheted_override(BOUNDARY_TABLE)["module"])

    def modules(self) -> list[tuple[str, ast.Module]]:
        parsed: list[tuple[str, ast.Module]] = []
        for dotted in self.boundary_modules():
            path = module_path(dotted)
            parsed.append((dotted, ast.parse(path.read_text(encoding="utf-8"))))
        return parsed

    def test_public_signatures_are_fully_annotated(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    if node.name != "__init__":
                        self.assertIsNotNone(
                            node.returns,
                            f"{dotted}.{name} has no return annotation",
                        )
                    for argument in annotated_arguments(node):
                        self.assertIsNotNone(
                            argument.annotation,
                            f"{dotted}.{name} takes an unannotated {argument.arg}",
                        )

    def test_public_signatures_carry_no_bare_containers(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    annotations = [
                        argument.annotation
                        for argument in annotated_arguments(node)
                        if argument.annotation is not None
                    ]
                    if node.returns is not None:
                        annotations.append(node.returns)
                    for annotation in annotations:
                        bare = unparameterized_container(annotation)
                        self.assertIsNone(
                            bare,
                            f"{dotted}.{name} crosses the boundary with a bare "
                            f"{bare}; name what it holds",
                        )

    def test_declared_attributes_carry_no_bare_containers(self) -> None:
        """Dataclass fields and module constants are durable payload slots too."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                if not isinstance(node, ast.AnnAssign):
                    continue
                bare = unparameterized_container(node.annotation)
                with self.subTest(module=dotted, line=node.lineno):
                    self.assertIsNone(
                        bare,
                        f"{dotted}:{node.lineno} declares a bare {bare}",
                    )

    def test_no_ratcheted_module_suppresses_an_error_inline(self) -> None:
        """``# type: ignore`` would reopen locally what the module opted in to."""
        for dotted in self.boundary_modules():
            source = module_path(dotted).read_text(encoding="utf-8")
            with self.subTest(module=dotted):
                self.assertNotIn("type: ignore", source)
                self.assertNotIn("mypy: disable-error-code", source)

    def test_no_ratcheted_module_imports_the_runner(self) -> None:
        """The extraction only holds while the dependency runs one way."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [alias.name for alias in node.names]
                    if node.module is not None:
                        imported.append(node.module)
                with self.subTest(module=dotted, line=getattr(node, "lineno", 0)):
                    self.assertNotIn(
                        "runner", [name.split(".")[-1] for name in imported]
                    )


#: An inline mypy configuration comment. mypy reads one from any line that
#: starts with it, not only the first.
_INLINE_MYPY_COMMENT = re.compile(r"^#\s*mypy:\s*(?P<options>.*?)\s*$")
_INLINE_DISABLE = re.compile(r'disable-error-code\s*=\s*"(?P<codes>[^"]*)"')

#: ``ignore-errors`` is recorded as a pseudo-code so the ceiling below can say
#: which files mypy does not check at all.
IGNORE_ERRORS = "ignore-errors"

#: Every file that carried an inline mypy suppression when this ratchet was
#: added, grouped by what it suppresses. Nearly all are part modules of the
#: split-module pattern, which share one namespace through ``globals().update``
#: and star imports that mypy cannot follow, so it reports the shared names as
#: undefined (``name-defined``, ``attr-defined``) or ignores the module
#: outright (``ignore-errors``). This is a ceiling: a file may leave it, or drop
#: a code, once it is checkable, but no file and no code may join it. Splitting
#: a module along the same pattern means typing the shared names instead.
_INLINE_MYPY_SUPPRESSION_GROUPS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (
        frozenset({"ignore-errors"}),
        (
            "agent_session_loop/commit_settlement.py",
            "agent_session_loop/commit_settlement_finish.py",
            "agent_session_loop/commit_settlement_groups.py",
            "agent_session_loop/commit_settlement_ledger.py",
            "agent_session_loop/commit_settlement_paused.py",
            "agent_session_loop/commit_settlement_targets.py",
            "agent_session_loop/commit_settlement_types.py",
            "agent_session_loop/copilot_acp.py",
            "agent_session_loop/copilot_acp_protocol.py",
            "agent_session_loop/copilot_acp_transport.py",
            "agent_session_loop/copilot_acp_turn.py",
            "agent_session_loop/copilot_acp_types.py",
            "agent_session_loop/copilot_acp_utils.py",
            "agent_session_loop/guards.py",
            "agent_session_loop/guards_controller.py",
            "agent_session_loop/guards_models.py",
            "agent_session_loop/guards_policy.py",
            "agent_session_loop/guards_rules.py",
            "skills/session-resplit/scripts/resplit_contracts.py",
            "skills/session-resplit/scripts/resplit_execution.py",
            "skills/session-resplit/scripts/resplit_parsing.py",
            "skills/session-resplit/scripts/resplit_render.py",
            "skills/session-resplit/scripts/resplit_shared.py",
            "tests/_test_asl_conversation_resume_planning.py",
            "tests/_test_asl_conversation_resume_fresh_start.py",
            "tests/_test_asl_conversation_resume_turn_outcomes.py",
            "tests/_test_asl_conversation_resume_support.py",
            "tests/_test_asl_conversations_records.py",
            "tests/_test_asl_conversations_selection.py",
            "tests/_test_asl_conversations_support.py",
            "tests/_test_asl_copilot_resume_restore.py",
            "tests/_test_asl_copilot_resume_adapter.py",
            "tests/_test_asl_copilot_resume_support.py",
            "tests/_test_asl_harness_lifecycle_turn_state.py",
            "tests/_test_asl_harness_lifecycle_cancellation.py",
            "tests/_test_asl_harness_lifecycle_continuity.py",
            "tests/_test_asl_harness_lifecycle_capabilities.py",
            "tests/_test_asl_harness_lifecycle_support.py",
            "tests/_test_asl_redirect_ack_coalescing.py",
            "tests/_test_asl_redirect_ack_receipts.py",
            "tests/_test_asl_redirect_ack_command.py",
            "tests/_test_asl_redirect_ack_support.py",
            "tests/_test_asl_redirect_store_submission.py",
            "tests/_test_asl_redirect_store_replay.py",
            "tests/_test_asl_redirect_store_launch_records.py",
            "tests/_test_asl_redirect_store_durability.py",
            "tests/_test_asl_redirect_store_support.py",
            "tests/_test_asl_workbench_redirect_submission.py",
            "tests/_test_asl_workbench_redirect_replay.py",
            "tests/_test_asl_workbench_redirect_recovery.py",
            "tests/_test_asl_workbench_redirect_support.py",
            "tests/_test_workbench_cutover_backups.py",
            "tests/_test_workbench_cutover_journal.py",
            "tests/_test_workbench_cutover_phone_acceptance.py",
            "tests/_test_workbench_cutover_support.py",
            "tests/asl_commit_groups_cases_coordinator.py",
            "tests/asl_commit_groups_cases_paused.py",
            "tests/asl_commit_groups_cases_settlement.py",
            "tests/asl_commit_groups_support.py",
            "tests/asl_overseer_history_cases_mismatch.py",
            "tests/asl_overseer_history_cases_request.py",
            "tests/asl_redirect_supervision_cases_recovery.py",
            "tests/asl_redirect_supervision_cases_stops.py",
            "tests/asl_review_cycle_cases_boundary.py",
            "tests/asl_review_cycle_cases_reopen.py",
            "tests/asl_review_transitions_cases_cycle.py",
            "tests/asl_review_transitions_cases_models.py",
            "tests/asl_startup_context_cases_installed.py",
            "tests/asl_startup_context_cases_renderer.py",
            "tests/asl_supervisor_audit_checks_settlement_cases_lanes.py",
            "tests/asl_supervisor_audit_checks_settlement_cases_records.py",
            "tests/asl_workbench_launch_cases_profiles.py",
            "tests/asl_workbench_launch_cases_spawn.py",
            "tests/asl_write_guards_cases_boundary.py",
            "tests/asl_write_guards_cases_protected.py",
            "tests/asl_write_guards_cases_repair.py",
            "tests/fixtures/asl_worker_verification/evaluate.py",
            "tests/fixtures/asl_worker_verification/evaluate_evidence.py",
            "tests/fixtures/asl_worker_verification/evaluate_policy.py",
            "tests/fixtures/asl_worker_verification/evaluate_shared.py",
            "tests/fixtures/asl_worker_verification/evaluate_trials.py",
            "tests/test_asl_commit_groups.py",
            "tests/test_asl_conversation_resume.py",
            "tests/test_asl_conversations.py",
            "tests/test_asl_copilot_resume.py",
            "tests/test_asl_harness_lifecycle.py",
            "tests/test_asl_redirect_ack.py",
            "tests/test_asl_redirect_store.py",
            "tests/test_asl_workbench_redirect.py",
            "tests/test_workbench_cutover.py",
        ),
    ),
    (
        frozenset({"attr-defined", "name-defined"}),
        (
            "agent_session_loop/overseer_checkpoint_groups_adapter_operations.py",
            "agent_session_loop/overseer_checkpoint_groups_adapter_prepare.py",
            "tests/asl_assurance_models_support.py",
            "tests/asl_copilot_acp_support.py",
            "tests/test_asl_assurance_models.py",
            "tests/test_asl_assurance_models_identity.py",
            "tests/test_asl_assurance_models_serialization.py",
            "tests/test_asl_assurance_models_summary.py",
            "tests/test_asl_copilot_acp.py",
            "tests/test_asl_copilot_acp_rendering.py",
            "tests/test_asl_copilot_acp_transport.py",
            "tests/test_workbench_integrations.py",
            "tests/test_workbench_integrations_publisher.py",
            "tests/test_workbench_launch_reply.py",
            "tests/test_workbench_launch_reply_actions.py",
            "tests/test_workbench_launch_reply_surfaces.py",
            "tests/workbench_integrations_support.py",
            "tests/workbench_launch_reply_support.py",
        ),
    ),
    (
        frozenset({"attr-defined", "name-defined", "no-redef"}),
        (
            "agent_session_loop/overseer_checkpoint_groups_part_1.py",
            "agent_session_loop/overseer_checkpoint_groups_part_2.py",
            "agent_session_loop/overseer_checkpoint_groups_part_3.py",
            "agent_session_loop/overseer_checkpoint_groups_shared.py",
            "agent_session_loop/overseer_history_part_1.py",
            "agent_session_loop/overseer_history_part_2.py",
            "agent_session_loop/overseer_history_part_3.py",
            "agent_session_loop/overseer_history_shared.py",
            "agent_session_loop/review_part_1.py",
            "agent_session_loop/review_part_2.py",
            "agent_session_loop/review_part_3.py",
            "agent_session_loop/review_shared.py",
            "agent_session_loop/workbench_controls_part_1.py",
            "agent_session_loop/workbench_controls_part_2.py",
            "agent_session_loop/workbench_controls_part_3.py",
            "agent_session_loop/workbench_controls_shared.py",
            "tests/asl_baseline_section_contract.py",
            "tests/asl_baseline_section_cache.py",
            "tests/asl_baseline_section_cli.py",
            "tests/asl_bridge_projection_section_hello.py",
            "tests/asl_bridge_projection_section_runs.py",
            "tests/asl_bridge_projection_section_session.py",
            "tests/asl_overseer_watch_section_enrollment.py",
            "tests/asl_overseer_watch_section_fleet.py",
            "tests/asl_overseer_watch_section_leases.py",
            "tests/asl_qna_cli.py",
            "tests/asl_typing_ratchet_section_codes.py",
            "tests/asl_typing_ratchet_section_modules.py",
            "tests/asl_typing_ratchet_section_tables.py",
            "tests/asl_typing_ratchet_section_suppressions.py",
            "tests/test_asl_baseline_shared.py",
            "tests/test_asl_bridge_projection_shared.py",
            "tests/test_asl_overseer_watch_shared.py",
            "tests/test_asl_qna_shared.py",
            "tests/test_asl_typing_ratchet_shared.py",
        ),
    ),
    (
        frozenset({"attr-defined", "call-arg", "has-type", "name-defined"}),
        (
            "agent_session_loop/claude_stream_control.py",
            "agent_session_loop/claude_stream_guidance.py",
            "agent_session_loop/claude_stream_io.py",
            "agent_session_loop/claude_stream_state.py",
            "agent_session_loop/intervention_actions.py",
            "agent_session_loop/intervention_controls.py",
            "agent_session_loop/intervention_questions.py",
            "agent_session_loop/verification_repair.py",
            "agent_session_loop/verification_terminal.py",
            "agent_session_loop/workbench_connector_http.py",
            "agent_session_loop/workbench_connector_observation.py",
            "agent_session_loop/workbench_connector_runtime.py",
            "agent_session_loop/workspace_evidence_guard.py",
            "agent_session_loop/workspace_evidence_scope_repair.py",
            "asl_workbench/remote_read_cache.py",
            "asl_workbench/remote_read_client.py",
        ),
    ),
)


INLINE_MYPY_SUPPRESSIONS: dict[str, frozenset[str]] = {
    path: codes for codes, paths in _INLINE_MYPY_SUPPRESSION_GROUPS for path in paths
}


def inline_suppression_codes(path: Path) -> frozenset[str]:
    """The codes a file suppresses through inline ``# mypy:`` comments."""
    codes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _INLINE_MYPY_COMMENT.match(line)
        if match is None:
            continue
        options = match.group("options")
        if options == IGNORE_ERRORS:
            codes.add(IGNORE_ERRORS)
            continue
        disabled = _INLINE_DISABLE.fullmatch(options)
        if disabled is None:
            # Any other inline option relaxes checking in a way this ratchet
            # does not model, so it is recorded verbatim and fails the ceiling.
            codes.add(options)
            continue
        codes.update(
            code.strip() for code in disabled.group("codes").split(",") if code.strip()
        )
    return frozenset(codes)


def mypy_checked_files() -> list[Path]:
    """Every ``.py`` file the mypy configuration checks."""
    config = load_mypy_config()
    exclude = re.compile(config["exclude"])
    checked: list[Path] = []
    for entry in config["files"]:
        for path in sorted((REPO_ROOT / entry).rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in path.parts or exclude.search(relative):
                continue
            checked.append(path)
    return checked


def inline_suppressions() -> dict[str, frozenset[str]]:
    """Each checked file that suppresses anything inline, by repository path."""
    found: dict[str, frozenset[str]] = {}
    for path in mypy_checked_files():
        codes = inline_suppression_codes(path)
        if codes:
            found[path.relative_to(REPO_ROOT).as_posix()] = codes
    return found


class InlineSuppressionRatchetTests(unittest.TestCase):
    """Inline ``# mypy:`` suppressions may only shrink."""

    def test_no_file_adds_an_inline_suppression(self) -> None:
        for path, codes in sorted(inline_suppressions().items()):
            allowed = INLINE_MYPY_SUPPRESSIONS.get(path, frozenset())
            added = codes - allowed
            with self.subTest(path=path):
                self.assertEqual(
                    added,
                    set(),
                    f"{path} adds inline mypy suppressions {sorted(added)}; "
                    "type the names instead of disabling the check",
                )

    def test_the_product_pin_is_kept_honest(self) -> None:
        """A product file that drops a suppression must be dropped here too.

        Without this the ceiling drifts upward for free, as it would for the
        repository-wide list. It covers everything outside ``tests/``: test
        modules are still being consolidated, so a test file that sheds its
        comment only leaves slack in the ceiling, never a hole.
        """
        current = inline_suppressions()
        for path, allowed in sorted(INLINE_MYPY_SUPPRESSIONS.items()):
            if path.startswith("tests/"):
                continue
            with self.subTest(path=path):
                self.assertEqual(
                    current.get(path, frozenset()),
                    allowed,
                    f"{path} no longer suppresses what this pin records; "
                    "shrink INLINE_MYPY_SUPPRESSIONS to match",
                )

    def test_every_pinned_file_is_listed_once(self) -> None:
        pinned = [
            path for _, group in _INLINE_MYPY_SUPPRESSION_GROUPS for path in group
        ]
        self.assertEqual(len(pinned), len(set(pinned)))

    def test_the_scan_reads_both_comment_forms(self) -> None:
        """The parser is what the ceiling trusts, so it is pinned on samples."""
        sample = REPO_ROOT / "agent_session_loop" / "copilot_acp.py"
        self.assertIn(IGNORE_ERRORS, inline_suppression_codes(sample))
        self.assertEqual(
            INLINE_MYPY_SUPPRESSIONS[
                "tests/asl_typing_ratchet_section_suppressions.py"
            ],
            inline_suppression_codes(Path(__file__)),
        )


class ScriptsTableTests(unittest.TestCase):
    """The skill scripts table lists every script and keeps its six codes."""

    def test_every_script_is_on_the_scripts_table(self) -> None:
        listed = set(scripts_override()["module"])
        missing = script_modules() - listed
        self.assertEqual(
            missing,
            set(),
            f"add {sorted(missing)} to the scripts enable_error_code table",
        )

    def test_the_scripts_table_lists_only_scripts(self) -> None:
        listed = set(scripts_override()["module"])
        self.assertEqual(listed - script_modules(), set())

    def test_the_scripts_table_re_enables_its_codes(self) -> None:
        """A code may join the scripts' set once they are clean under it."""
        enabled = set(scripts_override()["enable_error_code"])
        self.assertLessEqual(SCRIPT_RATCHETED_CODES, enabled)
        self.assertLessEqual(enabled, RATCHETED_CODES)

    def test_the_scripts_table_stays_sorted(self) -> None:
        listed = list(scripts_override()["module"])
        self.assertEqual(listed, sorted(listed))


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)

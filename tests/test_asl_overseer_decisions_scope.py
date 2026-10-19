"""Tests for test asl overseer decisions scope."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import get_args

from agent_session_loop import (
    overseer_decisions,
)
from tests import support_asl_overseer_decisions as fixtures
from tests.support import assert_no_runner_import
from tests.support_asl_overseer_decisions import (
    OTHER_PROJECT,
    PLAN,
    PLAN_SCOPE,
    PROJECT,
    RUN,
)


class ScopeTests(unittest.TestCase):
    """Coverage is one rule, and an empty field is never a wildcard on a target."""

    def test_an_unnamed_project_covers_nothing(self) -> None:
        empty = overseer_decisions.Scope(project="")

        self.assertFalse(empty.covers(overseer_decisions.Scope(project=PROJECT)))
        self.assertFalse(overseer_decisions.Scope(project=PROJECT).covers(empty))

    def test_a_narrowed_field_requires_the_target_to_name_it(self) -> None:
        narrowed = overseer_decisions.Scope(
            project=PROJECT,
            run_id=RUN,
        )

        self.assertTrue(narrowed.covers(PLAN_SCOPE))
        self.assertFalse(narrowed.covers(overseer_decisions.Scope(project=PROJECT)))

    def test_within_is_covers_read_from_the_other_side(self) -> None:
        broad = overseer_decisions.Scope(project=PROJECT)

        self.assertTrue(PLAN_SCOPE.within(broad))
        self.assertFalse(broad.within(PLAN_SCOPE))

    def test_comparison_is_exact_rather_than_by_containment(self) -> None:
        """A nested directory is a different project until somebody says so."""
        nested = overseer_decisions.Scope(project=f"{PROJECT}/agent_session_loop")

        self.assertFalse(overseer_decisions.Scope(project=PROJECT).covers(nested))

    def test_a_scope_names_each_project_once_however_it_was_given(self) -> None:
        """One project named twice is one project, and order is not identity."""
        one = overseer_decisions.Scope(
            project=PROJECT,
            projects=(PROJECT,),
        )
        both = overseer_decisions.Scope(
            projects=(
                OTHER_PROJECT,
                PROJECT,
                PROJECT,
            )
        )

        self.assertEqual(PROJECT, one.project)
        self.assertEqual((PROJECT,), one.named_projects)
        self.assertEqual(
            (
                OTHER_PROJECT,
                PROJECT,
            ),
            tuple(sorted(both.named_projects)),
        )
        self.assertEqual(
            overseer_decisions.Scope(
                projects=(
                    PROJECT,
                    OTHER_PROJECT,
                )
            ),
            both,
        )

    def test_covering_several_projects_means_covering_every_one(self) -> None:
        """A plan in two repositories is not reached by one of them."""
        both = overseer_decisions.Scope(
            projects=(
                PROJECT,
                OTHER_PROJECT,
            ),
            plan_dir=PLAN,
            run_id=RUN,
        )

        self.assertFalse(overseer_decisions.Scope(project=PROJECT).covers(both))
        self.assertFalse(overseer_decisions.Scope(project=OTHER_PROJECT).covers(both))
        self.assertTrue(
            overseer_decisions.Scope(
                projects=(
                    PROJECT,
                    OTHER_PROJECT,
                )
            ).covers(both)
        )

    def test_a_decision_about_two_projects_reaches_one_of_them(self) -> None:
        """Coverage is a subset test: naming more than the target is not wider."""
        both = overseer_decisions.Scope(
            projects=(
                PROJECT,
                OTHER_PROJECT,
            )
        )

        self.assertTrue(both.covers(PLAN_SCOPE))
        self.assertFalse(both.covers(overseer_decisions.Scope(project="/home/dev/web")))


class RepositoryIdentityTests(unittest.TestCase):
    """Which project a conversation was in, once the run says what exists.

    AgentMux scopes a conversation to the directory it was started in, and
    people start them wherever they happen to be standing -- usually inside a
    repository rather than at its root. The run declares its repositories, so
    a directory below one of them is that project speaking. A directory below
    none of them is only itself, which is what keeps a conversation in the
    workspace above two checkouts from speaking for either.
    """

    def test_a_conversation_below_a_repository_speaks_for_it(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory=f"{PROJECT}/agent_session_loop",
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            repositories=(PROJECT,),
        )

        self.assertEqual(PROJECT, statements[0].scope.project)

    def test_the_declared_root_is_the_identity_a_statement_carries(self) -> None:
        """The spelling the run declares, so both sides name the same string."""
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory=PROJECT,
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            repositories=(f"{PROJECT}/",),
        )

        self.assertEqual(f"{PROJECT}/", statements[0].scope.project)

    def test_a_directory_no_repository_holds_is_only_itself(self) -> None:
        """The finding, at its source: a workspace is not one of its projects."""
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory="/home/dev",
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            repositories=(
                PROJECT,
                OTHER_PROJECT,
            ),
        )

        self.assertEqual("/home/dev", statements[0].scope.project)

    def test_a_checkout_inside_a_checkout_is_the_nested_one(self) -> None:
        nested = f"{PROJECT}/vendor/tool"
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory=nested,
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            repositories=(PROJECT, nested),
        )

        self.assertEqual(nested, statements[0].scope.project)

    def test_a_sibling_sharing_a_prefix_is_a_different_project(self) -> None:
        """Containment is by path boundary, not by string prefix."""
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory=f"{PROJECT}-fork",
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            repositories=(PROJECT,),
        )

        self.assertEqual(
            f"{PROJECT}-fork",
            statements[0].scope.project,
        )


class MultiProjectAuthorityTests(unittest.TestCase):
    """A plan declared over two repositories, and what speaks for both.

    Authority is held per project. A claim covering two of them rests on a
    person having spoken in each, cited together; no reading of one project's
    instruction supplies the other's, and no third project lends what neither
    of them said.
    """

    def setUp(self) -> None:
        self.scope = overseer_decisions.Scope(
            projects=(
                PROJECT,
                OTHER_PROJECT,
            ),
            plan_dir=PLAN,
            run_id=RUN,
        )
        self.in_scripts = fixtures._direct_statement(
            statement_id="transcript:claude:sess-1#0",
            scope=overseer_decisions.Scope(project=PROJECT),
        )
        self.in_api = fixtures._direct_statement(
            statement_id="transcript:claude:sess-2#0",
            scope=overseer_decisions.Scope(project=OTHER_PROJECT),
        )

    def test_one_projects_instruction_does_not_speak_for_the_other(self) -> None:
        register = overseer_decisions.register(
            statements=(self.in_scripts, self.in_api),
            claims=(fixtures._claim(self.in_scripts.statement_id, scope=self.scope),),
        )

        self.assertEqual((), register.decisions)
        self.assertEqual("scope_widened", register.rejections[0].reason)
        self.assertIn(OTHER_PROJECT, register.rejections[0].detail)

    def test_a_statement_in_each_project_carries_the_claim(self) -> None:
        register = overseer_decisions.register(
            statements=(self.in_scripts, self.in_api),
            claims=(
                fixtures._claim(
                    self.in_scripts.statement_id,
                    scope=self.scope,
                    corroborating=(self.in_api.statement_id,),
                ),
            ),
        )

        ruling = overseer_decisions.resolve(
            register=register,
            request=overseer_decisions.Request(operation="resume", scope=self.scope),
        )

        self.assertEqual((), register.rejections)
        self.assertTrue(ruling.authorized, ruling.summary)

    def test_corroboration_from_a_third_project_covers_nothing(self) -> None:
        elsewhere = fixtures._direct_statement(
            statement_id="transcript:claude:sess-3#0",
            scope=overseer_decisions.Scope(project="/home/dev/web"),
        )

        register = overseer_decisions.register(
            statements=(self.in_scripts, elsewhere),
            claims=(
                fixtures._claim(
                    self.in_scripts.statement_id,
                    scope=self.scope,
                    corroborating=(elsewhere.statement_id,),
                ),
            ),
        )

        self.assertEqual((), register.decisions)
        self.assertEqual("scope_widened", register.rejections[0].reason)
        self.assertIn(OTHER_PROJECT, register.rejections[0].detail)

    def test_an_interpretation_cannot_stand_in_for_the_missing_project(self) -> None:
        """Corroboration carries a project only from an authorizing voice."""
        reading = fixtures._direct_statement(
            statement_id="transcript:claude:sess-2#1",
            voice="assistant_interpretation",
            scope=overseer_decisions.Scope(project=OTHER_PROJECT),
        )

        register = overseer_decisions.register(
            statements=(self.in_scripts, reading),
            claims=(
                fixtures._claim(
                    self.in_scripts.statement_id,
                    scope=self.scope,
                    corroborating=(reading.statement_id,),
                ),
            ),
        )

        self.assertEqual((), register.decisions)
        self.assertEqual("scope_widened", register.rejections[0].reason)


class VocabularyTests(unittest.TestCase):
    """Each closed vocabulary is published twice and must agree with itself."""

    def test_each_literal_matches_its_tuple(self) -> None:
        for name, literal, published in (
            ("Voice", overseer_decisions.Voice, overseer_decisions.VOICES),
            (
                "Operation",
                overseer_decisions.Operation,
                overseer_decisions.OPERATIONS,
            ),
            ("Topic", overseer_decisions.Topic, overseer_decisions.TOPICS),
            ("Stance", overseer_decisions.Stance, overseer_decisions.STANCES),
            (
                "RejectionReason",
                overseer_decisions.RejectionReason,
                overseer_decisions.CLAIM_REJECTIONS,
            ),
            ("Refusal", overseer_decisions.Refusal, overseer_decisions.REFUSALS),
        ):
            with self.subTest(vocabulary=name):
                self.assertEqual(set(get_args(literal)), set(published))
                self.assertEqual(len(set(published)), len(published))

    def test_only_a_user_instruction_authorizes(self) -> None:
        self.assertEqual(overseer_decisions.AUTHORIZING_VOICES, ("user_instruction",))
        self.assertTrue(
            set(overseer_decisions.AUTHORIZING_VOICES) <= set(overseer_decisions.VOICES)
        )

    def test_the_human_topics_are_topics_and_are_not_operational(self) -> None:
        self.assertTrue(
            set(overseer_decisions.HUMAN_TOPICS) <= set(overseer_decisions.TOPICS)
        )
        self.assertNotIn("operations", overseer_decisions.HUMAN_TOPICS)

    def test_the_public_surface_is_what_the_module_exports(self) -> None:
        for name in overseer_decisions.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(overseer_decisions, name))


class IsolationTests(unittest.TestCase):
    """The boundary has no way to reach a run, a file, or a process."""

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_decisions)

    def test_the_module_imports_nothing_that_touches_the_world(self) -> None:
        """Purity is asserted from the import graph rather than promised.

        A decision derived from evidence must be derived from the evidence it
        was handed. A module that could open a plan's state, run a subprocess or
        write a ledger row could also decide something the caller never showed
        it, so the absence of those imports is the property worth pinning.

        ``hashlib`` is permitted because summarizing evidence is still a pure
        function of the evidence handed over: it reads nothing and reaches
        nowhere, which is the property this test is about rather than the
        length of the list.
        """
        facade = Path(overseer_decisions.__file__ or "")
        sources = [facade, *sorted(facade.parent.glob("overseer_decision_*.py"))]
        internal = {path.stem for path in sources}
        permitted = {
            "__future__",
            "collections.abc",
            "dataclasses",
            "datetime",
            "hashlib",
            "typing",
            "clock",
            "mailbox_constants",
            "mailbox_models",
            "mailbox_vocabulary",
            "overseer_history",
            "overseer_policy_client",
        }
        imported: set[str] = set()
        for path in sources:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module is not None:
                        imported.add(node.module)
                    elif node.level:
                        imported.update(alias.name for alias in node.names)

        self.assertEqual(imported - permitted - internal, set())

    def test_nothing_here_is_convertible_to_permission(self) -> None:
        """A ruling is an answer about evidence, never a grant to act on."""
        for name in ("Ruling", "Decision", "Settlement", "Statement"):
            attribute = getattr(overseer_decisions, name)
            for forbidden in ("grant", "authorization", "permits"):
                with self.subTest(record=name, member=forbidden):
                    self.assertFalse(hasattr(attribute, forbidden))


if __name__ == "__main__":
    unittest.main()

# ruff: noqa: F403, F405
from .asl_overseer_checkpoint_support import *


class ClosedVocabulary(_CheckpointCase):
    """The refusal words are a consumer contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint.REFUSALS),
            set(get_args(overseer_checkpoint.Refusal)),
        )
        self.assertEqual(
            len(overseer_checkpoint.REFUSALS),
            len(set(overseer_checkpoint.REFUSALS)),
        )

    def test_verification_states_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint.VERIFICATION_STATES),
            set(get_args(overseer_checkpoint.VerificationState)),
        )

    def test_the_registry_holds_the_commit_operation_only(self) -> None:
        registry = overseer_checkpoint.adapters(
            authority=self.authority(), state_dir=self.root
        )
        self.assertTrue(registry.supports("commit"))
        for operation in ("reply", "guidance", "verify", "resume"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_checkpoint)


class DerivedIdentity(_CheckpointCase):
    """The checkpoint id is a function of the row, so a crash is answerable."""

    def test_identity_is_the_same_for_the_same_three_fields(self) -> None:
        base = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        self.assertEqual(
            overseer_checkpoint.checkpoint_id(**base),
            overseer_checkpoint.checkpoint_id(**base),
        )

    def test_identity_moves_with_every_field_it_is_derived_from(self) -> None:
        base: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        identity = overseer_checkpoint.checkpoint_id(**base)
        for name, value in (
            ("plan_dir", self.new_plan("plan-other")),
            ("incident_id", self.incident_id + 1),
            ("target", "t-2"),
        ):
            moved = dict(base)
            moved[name] = value
            self.assertNotEqual(
                identity, overseer_checkpoint.checkpoint_id(**moved), msg=name
            )

    def test_the_message_carries_the_trailer_and_keeps_the_subject(self) -> None:
        message = overseer_checkpoint.checkpoint_message(MESSAGE, "id-1")
        self.assertTrue(message.startswith(MESSAGE))
        self.assertIn(f"{overseer_checkpoint.CHECKPOINT_TRAILER}: id-1", message)


class Attribution(_CheckpointCase):
    """Ownership is established from the run's own evidence, or not at all."""

    def test_a_recorded_path_is_owned(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertTrue(attribution.safe)
        self.assertEqual(attribution.owned, ("notes.md",))
        self.assertEqual(attribution.unattributed, ())
        self.assertEqual(attribution.baseline, self.head())

    def test_a_commit_artifact_supplies_the_baseline_and_the_paths(self) -> None:
        self.record_commit(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertEqual(attribution.owned, ("notes.md",))

    def test_an_unrecorded_path_is_unattributed(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        self.write("somebody-else.md", "not this run\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "changes_unattributed")
        self.assertEqual(attribution.unattributed, ("somebody-else.md",))
        self.assertIn("somebody-else.md", attribution.detail)

    def test_a_path_qualified_by_another_repository_is_not_owned(self) -> None:
        self.record_turn(paths=("notes.md",), alias="other")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_unknown")
        self.assertEqual(attribution.activity, ())

    def test_a_single_repository_run_may_qualify_with_a_dot(self) -> None:
        self.record_turn(paths=("notes.md",), alias=".")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertEqual(attribution.owned, ("notes.md",))

    def test_a_clean_repository_has_nothing_to_checkpoint(self) -> None:
        self.record_turn(paths=("notes.md",))

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "nothing_to_commit")
        self.assertTrue(attribution.clean)
        self.assertEqual(attribution.dirty, ())

    def test_a_run_that_recorded_no_head_establishes_no_baseline(self) -> None:
        self.record_turn(guard=False)
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_unknown")

    def test_a_head_that_moved_is_not_a_baseline_to_measure_from(self) -> None:
        stale = self.head()
        self.record_turn(paths=("notes.md",), head=stale)
        self.write("elsewhere.md", "another actor\n")
        self.git("add", "elsewhere.md")
        self.git("commit", "-m", "somebody else committed")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_moved")
        self.assertIn(stale, attribution.detail)

    def test_two_repositories_cannot_be_isolated_together(self) -> None:
        other = overseer_policy_client.Repository(alias="other", path=str(self.repo))

        attribution = self.attribute(repositories=(self.repository, other))

        self.assertEqual(attribution.refusal, "repository_ambiguous")

    def test_no_repository_is_refused_rather_than_defaulted(self) -> None:
        self.assertEqual(
            self.attribute(repositories=()).refusal, "repository_ambiguous"
        )

    def test_a_directory_that_is_not_a_worktree_is_unreadable(self) -> None:
        outside = self.root / "not-a-repo"
        outside.mkdir()
        elsewhere = overseer_policy_client.Repository(alias=ALIAS, path=str(outside))

        self.assertEqual(
            self.attribute(repositories=(elsewhere,)).refusal, "repository_unreadable"
        )

    def test_both_ends_of_a_rename_must_be_owned(self) -> None:
        self.write("notes.md", "work in progress\n")
        self.git("add", "notes.md")
        self.git("commit", "-m", "add notes")
        self.record_turn(paths=("renamed.md",))
        self.git("mv", "notes.md", "renamed.md")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "changes_unattributed")
        self.assertIn("notes.md", attribution.unattributed)


__all__ = tuple(name for name in globals() if not name.startswith("__"))

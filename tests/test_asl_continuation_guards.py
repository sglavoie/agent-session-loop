"""Continuation merging accumulates deltas without upgrading a guard verdict."""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import continuation_guards

from .support_asl_continuation_guards import legacy_guard, registered_guard

# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


class MergeAccumulatesDeltaTests(unittest.TestCase):
    """What survives a merge, and in what order."""

    def test_a_repository_only_the_earlier_attempt_touched_is_carried(self) -> None:
        merged = continuation_guards.merge_session_write_evidence(
            registered_guard(changes={"SVC": ("first.txt",)}),
            registered_guard(changes={"OAK": ("second.txt",)}),
        )

        assert merged is not None
        # Sorted by repository key, exactly as the comparison this record
        # replaces orders its own map.
        self.assertEqual(["OAK", "SVC"], list(merged["changed"]))
        self.assertEqual(["SVC:first.txt"], merged["changed"]["SVC"]["qualified_paths"])
        self.assertEqual(
            ["OAK:second.txt"], merged["changed"]["OAK"]["qualified_paths"]
        )
        block = merged[continuation_guards.CONTINUATION_EVIDENCE_KEY]
        self.assertEqual(["SVC"], block["carried_repositories"])
        self.assertEqual([], block["extended_repositories"])

    def test_a_repository_both_attempts_touched_keeps_both_path_sets(self) -> None:
        merged = continuation_guards.merge_session_write_evidence(
            registered_guard(changes={"SVC": ("first.txt", "shared.txt")}),
            registered_guard(changes={"SVC": ("shared.txt", "second.txt")}),
        )

        assert merged is not None
        record = merged["changed"]["SVC"]
        # Earlier first, deduplicated, so the staging pathspec a commit builds
        # from this is stable across re-attempts rather than reordered by one.
        self.assertEqual(
            ["first.txt", "shared.txt", "second.txt"], record["untracked_added"]
        )
        self.assertEqual(
            ["SVC:first.txt", "SVC:shared.txt", "SVC:second.txt"],
            record["qualified_paths"],
        )
        self.assertEqual(
            ["SVC"],
            merged[continuation_guards.CONTINUATION_EVIDENCE_KEY][
                "extended_repositories"
            ],
        )

    def test_authorized_aliases_follow_the_registry_order_of_the_merged_set(
        self,
    ) -> None:
        # `_registered_session_commit_targets` refuses a record whose authorized
        # aliases are not exactly its changed aliases in registration order, so
        # a merge that appended would make the record it produced uncommittable.
        merged = continuation_guards.merge_session_write_evidence(
            registered_guard(changes={"OAK": ("second.txt",)}),
            registered_guard(changes={"SVC": ("first.txt",)}),
        )

        assert merged is not None
        self.assertEqual(["SVC", "OAK"], merged["authorized_aliases"])

    def test_a_legacy_record_keeps_its_one_pseudo_alias(self) -> None:
        merged = continuation_guards.merge_session_write_evidence(
            legacy_guard("first.txt"), legacy_guard("second.txt")
        )

        assert merged is not None
        self.assertEqual(["legacy"], merged["authorized_aliases"])
        self.assertEqual(
            [".:first.txt", ".:second.txt"],
            merged["changed"]["."]["qualified_paths"],
        )


class MergeRefreshesStateTests(unittest.TestCase):
    """Which fields the newer observation replaces outright."""

    def test_topology_and_scope_fields_come_from_the_current_attempt(self) -> None:
        earlier = registered_guard(changes={"SVC": ("first.txt",)}, added=("/extra",))
        earlier["external_writes"] = {"/tmp/stale": {"verdict": "unclassified"}}
        current = registered_guard(changes={"SVC": ("second.txt",)})

        merged = continuation_guards.merge_session_write_evidence(earlier, current)

        assert merged is not None
        # A repository that existed one attempt ago is not evidence about the
        # tree the commit is about to run in.
        self.assertEqual([], merged["added"])
        self.assertEqual({}, merged["external_writes"])
        self.assertEqual([], merged["violations"])
        self.assertEqual({}, merged["unavailable"])

    def test_the_head_transition_spans_the_session_not_the_turn(self) -> None:
        earlier = registered_guard(changes={"SVC": ("first.txt",)})
        earlier["changed"]["SVC"].update(
            {"head_before": "aaa", "head_after": "bbb", "head_changed": True}
        )
        current = registered_guard(changes={"SVC": ("second.txt",)})
        current["changed"]["SVC"].update(
            {"head_before": "bbb", "head_after": "ccc", "head_changed": True}
        )

        merged = continuation_guards.merge_session_write_evidence(earlier, current)

        assert merged is not None
        record = merged["changed"]["SVC"]
        self.assertEqual("aaa", record["head_before"])
        self.assertEqual("ccc", record["head_after"])
        self.assertTrue(record["head_changed"])

    def test_a_head_that_returns_to_where_it_started_reads_as_unchanged(self) -> None:
        earlier = registered_guard(changes={"SVC": ("first.txt",)})
        earlier["changed"]["SVC"].update(
            {"head_before": "aaa", "head_after": "bbb", "head_changed": True}
        )
        current = registered_guard(changes={"SVC": ("second.txt",)})
        current["changed"]["SVC"].update(
            {"head_before": "bbb", "head_after": "aaa", "head_changed": True}
        )

        merged = continuation_guards.merge_session_write_evidence(earlier, current)

        assert merged is not None
        self.assertFalse(merged["changed"]["SVC"]["head_changed"])

    def test_ref_transitions_compose_rather_than_concatenate(self) -> None:
        # A ref the first attempt created and the second deleted moved nowhere
        # over the session; concatenating the lists would report it as both
        # added and removed, and a twice-moved ref as sitting at its midpoint.
        earlier = registered_guard(changes={"SVC": ("first.txt",)})
        earlier["changed"]["SVC"].update(
            {
                "refs_added": [{"ref": "refs/heads/scratch", "after": "111"}],
                "refs_removed": [],
                "refs_changed": [
                    {"ref": "refs/heads/main", "before": "aaa", "after": "bbb"}
                ],
            }
        )
        current = registered_guard(changes={"SVC": ("second.txt",)})
        current["changed"]["SVC"].update(
            {
                "refs_added": [],
                "refs_removed": [{"ref": "refs/heads/scratch", "before": "111"}],
                "refs_changed": [
                    {"ref": "refs/heads/main", "before": "bbb", "after": "ccc"}
                ],
            }
        )

        merged = continuation_guards.merge_session_write_evidence(earlier, current)

        assert merged is not None
        record = merged["changed"]["SVC"]
        self.assertEqual([], record["refs_added"])
        self.assertEqual([], record["refs_removed"])
        self.assertEqual(
            [{"ref": "refs/heads/main", "before": "aaa", "after": "ccc"}],
            record["refs_changed"],
        )


class MergeNeverUpgradesAVerdictTests(unittest.TestCase):
    """The four ways the merge refuses, and the one it declines to bother."""

    def base(self) -> dict[str, Any]:
        return registered_guard(changes={"SVC": ("first.txt",)})

    def test_a_missing_or_absent_record_on_either_side_refuses(self) -> None:
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(None, self.base())
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(self.base(), None)
        )

    def test_a_record_that_did_not_pass_is_never_merged(self) -> None:
        failed = registered_guard(changes={"SVC": ("first.txt",)}, passed=False)
        current = registered_guard(changes={"OAK": ("second.txt",)})
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(failed, current)
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(current, failed)
        )

    def test_a_guard_that_could_not_see_is_not_a_guard_that_saw_nothing(self) -> None:
        blind = registered_guard(
            changes={"SVC": ("first.txt",)},
            unavailable={"/repos/OAK": {"reason": "repository-unavailable"}},
        )
        current = registered_guard(changes={"OAK": ("second.txt",)})
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(blind, current)
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(current, blind)
        )

    def test_a_convicted_record_is_never_merged(self) -> None:
        convicted = registered_guard(
            changes={"SVC": ("first.txt",)},
            violations=({"kind": "outside-read-scope", "alias": "OAK"},),
        )
        current = registered_guard(changes={"OAK": ("second.txt",)})
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(convicted, current)
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(current, convicted)
        )

    def test_an_authorized_topology_change_does_not_block_the_merge(self) -> None:
        # A linked worktree of a repository the session may write is authorized
        # work, so its record still passes -- and refusing to carry the earlier
        # attempt's paths on its account would drop work for an unrelated reason.
        earlier = registered_guard(
            changes={"SVC": ("first.txt",)}, added=("/repos/SVC+wt",)
        )
        merged = continuation_guards.merge_session_write_evidence(
            earlier, registered_guard(changes={"OAK": ("second.txt",)})
        )

        assert merged is not None
        self.assertIn("SVC", merged["changed"])

    def test_a_merge_that_carries_nothing_rewrites_nothing(self) -> None:
        current = registered_guard(changes={"SVC": ("first.txt", "second.txt")})
        # The earlier attempt saw a strict subset of what this one sees, so the
        # accumulated view is the current one and there is nothing to persist.
        earlier = registered_guard(changes={"SVC": ("first.txt",)})
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(earlier, current)
        )
        # And an attempt that changed nothing at all carries nothing either.
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(
                registered_guard(changes={}), current
            )
        )

    def test_the_merge_leaves_both_inputs_alone(self) -> None:
        earlier = registered_guard(changes={"SVC": ("first.txt",)})
        current = registered_guard(changes={"OAK": ("second.txt",)})
        earlier_copy = json.loads(json.dumps(earlier))
        current_copy = json.loads(json.dumps(current))

        merged = continuation_guards.merge_session_write_evidence(earlier, current)

        assert merged is not None
        self.assertEqual(earlier_copy, earlier)
        self.assertEqual(current_copy, current)


if __name__ == "__main__":
    unittest.main()

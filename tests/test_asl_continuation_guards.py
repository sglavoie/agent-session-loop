"""What a session's earlier attempts are still owed when the loop re-attempts it.

A session that the run loop re-attempts inside one run is one piece of work
spread across several turns, and since Session 37 the later turns continue the
earlier turn's conversation. Every post-turn guard, though, is scoped to a turn:
``capture_turn_baselines`` takes a fresh before-image each time, so the second
attempt's write-guard record describes only the second attempt. That record is
what the protected-path guard, the per-session commit and the paused-work
settlement all read, so an unaccumulated record loses the first attempt's work
from all three at once.

The claims here are the ones that fix says are true, stated as properties:

* the merge accumulates deltas and refreshes state, and says so about which
  repositories it carried;
* the merge refuses to upgrade a verdict, and a merge that carries nothing
  rewrites nothing;
* the ledger only ever holds an attempt a post-turn guard cleared, and retires
  with the session;
* the sequence in ``turn_postflight`` hands the protected-path guard the
  session's whole delta rather than its last turn's, and leaves a finalization
  refusal's own record alone;
* implementation, review, repair-child and multi-repository settlements commit
  the retained work of every attempt, not just the last;
* and an interrupted attempt earns nothing by any of it -- no checkpoint commit,
  no verification credit, no repair budget consumed -- while an ordinary timeout
  keeps the settlement and lineage it already had.
"""

from __future__ import annotations

import ast
import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_message,
    commit_settlement,
    continuation_guards,
    events,
    handoff_guards,
    harnesses,
    pause_policy,
    plans_snapshot,
    review_guard,
    review_transitions,
    run_commit,
    run_persistence,
    run_selection,
    turn_postflight,
    verification_cli,
    verification_models,
    verification_transitions,
    write_guards,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import (
    DEFAULT_MULTI_REPO_SPECS,
    assert_no_runner_import,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)

RUN_ID = "run-continuation-1"
SESSION_ID = "1"
SESSION_LABEL = "Session 1"
SPECS = DEFAULT_MULTI_REPO_SPECS[:2]


# ---------------------------------------------------------------------------
# Guard-record fixtures
# ---------------------------------------------------------------------------


def registered_guard(
    *,
    changes: dict[str, tuple[str, ...]],
    aliases: tuple[str, ...] = ("SVC", "OAK"),
    passed: bool = True,
    violations: tuple[dict[str, Any], ...] = (),
    unavailable: dict[str, Any] | None = None,
    added: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The evidence one passing registered turn leaves in its result artifact.

    ``changes`` maps an alias to the untracked paths that attempt added, which
    is the shape both the commit staging and the protected-path projection read
    back out.
    """
    return {
        "passed": passed,
        "read_aliases": list(aliases),
        "write_aliases": list(aliases),
        "registered_repositories": [
            {"alias": alias, "path": f"/repos/{alias}"} for alias in aliases
        ],
        "changed": {
            alias: {
                "repository": f"/repos/{alias}",
                "alias": alias,
                "tracked": [],
                "untracked_added": list(paths),
                "untracked_changed": [],
                "untracked_removed": [],
                "worktree_ignore": [],
                "ignore_config": [],
                "qualified_paths": [f"{alias}:{path}" for path in paths],
                "worktree_of": None,
            }
            for alias, paths in changes.items()
        },
        "authorized_aliases": [alias for alias in aliases if alias in changes],
        "added": list(added),
        "removed": [],
        "unavailable": dict(unavailable or {}),
        "violations": list(violations),
        "external_writes": {},
        "excluded": [".venv"],
        "include_ignored": False,
    }


def legacy_guard(*paths: str) -> dict[str, Any]:
    """The evidence one passing legacy single-repository turn leaves behind."""
    return {
        "passed": True,
        "scope_mode": "legacy-single-repository",
        "repository_root": "/repo",
        "read_aliases": ["legacy"],
        "write_aliases": ["legacy"],
        "registered_repositories": [],
        "changed": {
            ".": {
                "tracked": [],
                "untracked_added": list(paths),
                "qualified_paths": [f".:{path}" for path in paths],
            }
        },
        "authorized_aliases": ["legacy"] if paths else [],
        "added": [],
        "removed": [],
        "unavailable": {},
        "violations": [],
        "external_writes": {},
        "excluded": [],
        "include_ignored": False,
    }


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


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class SessionAttemptLedgerTests(unittest.TestCase):
    """What the run-scoped ledger keeps, publishes and forgets."""

    def setUp(self) -> None:
        self.ledger = continuation_guards.SessionAttempts()

    def test_the_newest_cleared_attempt_is_what_the_next_one_merges_against(
        self,
    ) -> None:
        first = registered_guard(changes={"SVC": ("first.txt",)})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=1, disposition="timeout", guard=first
        )
        self.assertEqual(first, self.ledger.latest(SESSION_ID))

        second = registered_guard(changes={"SVC": ("first.txt", "second.txt")})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=2, disposition=None, guard=second
        )
        self.assertEqual(second, self.ledger.latest(SESSION_ID))

    def test_a_record_that_cannot_be_merged_never_becomes_the_latest(self) -> None:
        cleared = registered_guard(changes={"SVC": ("first.txt",)})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=1, disposition="timeout", guard=cleared
        )
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=2,
            disposition="timeout",
            guard=registered_guard(changes={"OAK": ("blind.txt",)}, passed=False),
        )
        # The cleared attempt is still what a third attempt accumulates onto:
        # an unmergeable record neither replaces it nor joins it.
        self.assertEqual(cleared, self.ledger.latest(SESSION_ID))

    def test_sessions_do_not_borrow_each_others_evidence(self) -> None:
        self.ledger.record(
            session_id="1",
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        self.assertIsNone(self.ledger.latest("2"))

    def test_every_attempt_is_summarized_even_when_it_is_not_merged(self) -> None:
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        self.ledger.record(
            session_id=SESSION_ID, turn_index=2, disposition=None, guard=None
        )

        summaries = self.ledger.summaries(SESSION_ID)
        self.assertEqual((1, 2), tuple(item.turn_index for item in summaries))
        self.assertEqual(
            ("timeout", continuation_guards.DISPOSITION_SETTLED),
            tuple(item.disposition for item in summaries),
        )
        self.assertEqual(
            {
                SESSION_ID: [
                    {
                        "turn_index": 1,
                        "disposition": "timeout",
                        "repositories": ["SVC"],
                        "qualified_paths": 1,
                    },
                    {
                        "turn_index": 2,
                        "disposition": continuation_guards.DISPOSITION_SETTLED,
                        "repositories": [],
                        "qualified_paths": 0,
                    },
                ]
            },
            self.ledger.to_payload(),
        )

    def test_a_completed_session_stops_being_accumulated(self) -> None:
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        retired = self.ledger.retire(SESSION_ID)

        self.assertEqual((1,), tuple(item.turn_index for item in retired))
        self.assertIsNone(self.ledger.latest(SESSION_ID))
        self.assertEqual((), self.ledger.summaries(SESSION_ID))
        self.assertEqual({}, self.ledger.to_payload())


# ---------------------------------------------------------------------------
# The guard sequence
# ---------------------------------------------------------------------------


class RecordingPersistence:
    """The run's durable-write mutex, kept real, with its events retained."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeRunContext:
    """A :class:`turn_postflight.RunContext` with a real attempt ledger on it."""

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {"run_id": RUN_ID}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.guard_controller = mock.Mock()
        self.session_attempts = continuation_guards.SessionAttempts()
        self.pauses: list[dict[str, Any]] = []

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class ContinuationPostflightTests(unittest.TestCase):
    """The sequence's own step, with the change-path projection left real.

    Every guard is replaced by a recorder, but
    ``write_guards._protected_path_changed_paths`` is deliberately *not*: it is
    the consumer whose answer the accumulation is supposed to change, so
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
                    write_guards, "_enforce_protected_path_guard", protected
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


# ---------------------------------------------------------------------------
# Settlement over real repositories
# ---------------------------------------------------------------------------


class ContinuationCommitScopeTests(unittest.TestCase):
    """What a recoverable pause commits after the loop re-attempted a session.

    Driven straight at the settlement with a fake ``asl commit`` that performs
    the real ``git add``/``git commit`` the staging asks for, so the claim that
    the earlier attempt's file was committed is answered by the tree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.registered = verification_models.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        self.legacy_root = self.tmp_path / "legacy"
        init_git_repo(self.legacy_root)
        self.legacy = verification_models.VerificationContext.legacy(self.legacy_root)
        self.run_dir = self.tmp_path / "run"
        self.event_path = self.tmp_path / "events.jsonl"
        self.artifact_dir = self.tmp_path / "turn-0002"
        self.payload: dict[str, Any] = {"commits": []}
        self.launched: list[dict[str, Any]] = []
        self.soft_failures: list[dict[str, Any]] = []

    # -- fixtures ---------------------------------------------------------

    def write(self, root: Path, relative: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n", encoding="utf-8")

    def accumulated(
        self, first: dict[str, tuple[str, ...]], second: dict[str, tuple[str, ...]]
    ) -> dict[str, Any]:
        """The record the guard sequence leaves after two cleared attempts."""
        ledger = continuation_guards.SessionAttempts()
        ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes=first),
        )
        current = registered_guard(changes=second)
        merged = continuation_guards.merge_session_write_evidence(
            ledger.latest(SESSION_ID), current
        )
        return current if merged is None else merged

    def fake_commit(self, **kwargs: Any) -> dict[str, Any]:
        self.launched.append(dict(kwargs))
        root = Path(str(kwargs["cwd"]))
        staging = list(kwargs["staging_paths"])
        if staging:
            review_guard._run_git(
                root,
                ["add", "--", *commit_message.dequalify_paths(staging)],
            )
        else:
            review_guard._run_git(root, ["add", "-A"])
        message_file = kwargs["generation"].message_file
        assert message_file is not None
        review_guard._run_git(root, ["commit", "--no-verify", "-F", str(message_file)])
        return {
            "success": True,
            "commit_sha": review_guard._run_git(
                root, ["rev-parse", "HEAD"]
            ).stdout.strip(),
            "commit_index": kwargs["commit_index"],
            "repository_key": kwargs["repository_key"],
        }

    def settle(
        self,
        context: verification_models.VerificationContext,
        *,
        guard: dict[str, Any] | None,
        commit_mode: str | None = run_selection.COMMIT_MODE_PER_SESSION,
        pause_reason: str = "verify-failed",
        is_review_session: bool = False,
        is_repair_session: bool = False,
    ) -> dict[str, Any] | None:
        turn_payload: dict[str, Any] = {"turn_index": 2, "session": SESSION_ID}
        if guard is not None:
            turn_payload["workspace_write_guard"] = guard
        retained = commit_settlement.RetainedWorkContext(
            session_id=SESSION_ID,
            session_label=SESSION_LABEL,
            turn_payload=turn_payload,
            result_path=self.artifact_dir / "result.json",
            artifact_dir=self.artifact_dir,
            is_review_session=is_review_session,
            is_repair_session=is_repair_session,
        )
        with mock.patch.object(
            run_commit, "_run_asl_commit", side_effect=self.fake_commit
        ):
            return commit_settlement._settle_paused_retained_work(
                event_path=self.event_path,
                payload=self.payload,
                progress=mock.Mock(),
                verification_context=context,
                run_dir=self.run_dir,
                run_id=RUN_ID,
                retained=retained,
                pause_reason=pause_reason,
                pause_details={
                    "verification_repair_attempt": 1,
                    "verification_repair_max_attempts": 2,
                    "verification_repair_exhausted": False,
                },
                commit_mode=commit_mode,
                generation=run_commit._CommitGeneration(),
                soft_fail=lambda **kwargs: self.soft_failures.append(dict(kwargs)),
                persistence=run_persistence._RunPersistence(),
            )

    def dirty(self, root: Path) -> list[str]:
        return review_guard._porcelain_dirty_paths(
            review_guard._run_git(
                root, ["status", "--porcelain", "-z", "--untracked-files=all"]
            ).stdout
        )

    def staged_by_repository(self) -> dict[str, list[str]]:
        return {
            str(call["repository_key"]): list(call["staging_paths"])
            for call in self.launched
        }

    # -- the four fixtures ------------------------------------------------

    def test_an_implementation_session_commits_both_of_its_attempts(self) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["SVC"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"SVC": ("second.txt",)}),
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt", "SVC:second.txt"]}, self.staged_by_repository()
        )
        # The whole point: nothing the first attempt wrote is left behind.
        self.assertEqual([], self.dirty(self.repositories["SVC"]))

    def test_a_multi_repository_session_commits_every_repository_it_touched(
        self,
    ) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["OAK"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"OAK": ("second.txt",)}),
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt"], "OAK": ["OAK:second.txt"]},
            self.staged_by_repository(),
        )
        self.assertEqual([], self.dirty(self.repositories["SVC"]))
        self.assertEqual([], self.dirty(self.repositories["OAK"]))
        self.assertEqual({"repositories": []}, record["residue"])

    def test_a_repair_child_owns_its_commit_without_a_mode_of_its_own(self) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["SVC"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"SVC": ("second.txt",)}),
            commit_mode=None,
            is_repair_session=True,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt", "SVC:second.txt"]}, self.staged_by_repository()
        )
        self.assertEqual([], self.dirty(self.repositories["SVC"]))

    def test_a_review_session_still_commits_nothing_however_many_attempts(
        self,
    ) -> None:
        self.write(self.repositories["SVC"], "first.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"OAK": ("second.txt",)}),
            is_review_session=True,
        )

        # Accumulating evidence changes what a commit would stage, never
        # whether one is owed. A review session is never auto-committed.
        self.assertIsNone(record)
        self.assertEqual([], self.launched)
        self.assertEqual(["first.txt"], self.dirty(self.repositories["SVC"]))

    def test_the_legacy_lane_keeps_sweeping_its_one_repository(self) -> None:
        self.write(self.legacy_root, "first.txt")
        self.write(self.legacy_root, "second.txt")

        record = self.settle(self.legacy, guard=legacy_guard("first.txt"))

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual([], self.dirty(self.legacy_root))


# ---------------------------------------------------------------------------
# What an interruption cannot earn
# ---------------------------------------------------------------------------


class InterruptionEarnsNothingTests(unittest.TestCase):
    """An urgent redirection retains bytes and gains nothing else.

    Accumulating evidence across attempts widens what a *recoverable* pause
    settles, so the boundary that keeps an interruption from settling at all has
    to be re-stated against the same machinery rather than assumed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.event_path = self.tmp_path / "events.jsonl"

    def retained(self, **overrides: Any) -> commit_settlement.RetainedWorkContext:
        values: dict[str, Any] = {
            "session_id": SESSION_ID,
            "session_label": SESSION_LABEL,
            "turn_payload": {
                "workspace_write_guard": registered_guard(
                    changes={"SVC": ("first.txt", "second.txt")}
                )
            },
            "result_path": self.tmp_path / "result.json",
            "artifact_dir": self.tmp_path / "turn-0002",
            "is_review_session": False,
            "is_repair_session": False,
        }
        values.update(overrides)
        return commit_settlement.RetainedWorkContext(**values)

    def test_an_interruption_is_not_a_recoverable_pause(self) -> None:
        self.assertNotIn("interrupted", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertIn("timeout", pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_no_checkpoint_commit_is_owed_for_an_interruption(self) -> None:
        for commit_mode in (run_selection.COMMIT_MODE_PER_SESSION, None):
            for is_repair_session in (False, True):
                with self.subTest(mode=commit_mode, repair=is_repair_session):
                    self.assertFalse(
                        commit_settlement._paused_work_commit_is_owed(
                            self.retained(is_repair_session=is_repair_session),
                            pause_reason="interrupted",
                            commit_mode=commit_mode,
                        )
                    )

    def test_an_ordinary_timeout_keeps_the_settlement_it_already_had(self) -> None:
        self.assertTrue(
            commit_settlement._paused_work_commit_is_owed(
                self.retained(),
                pause_reason="timeout",
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            )
        )

    def write_events(self, *reasons: str) -> None:
        """A run's pause history, as the repair counters read it back."""
        self.event_path.write_text(
            "".join(
                json.dumps(
                    {
                        "name": events.RUN_PAUSED_EVENT,
                        "payload": {
                            "reason": reason,
                            "details": {"session": SESSION_ID},
                        },
                    }
                )
                + "\n"
                for reason in reasons
            ),
            encoding="utf-8",
        )

    def test_an_interrupted_attempt_consumes_no_repair_budget(self) -> None:
        # The budget is derived from the session's own `verify-failed` history,
        # and an interrupted attempt never verified, so it appends nothing the
        # counter can read. No counter is decremented to achieve that.
        self.write_events("interrupted", "interrupted")

        budget = verification_transitions.verification_repair_budget(
            self.event_path, SESSION_ID
        )

        self.assertEqual(1, budget.attempt)
        self.assertEqual((), budget.prior_fingerprints)
        self.assertEqual((), budget.references)

    def test_a_verification_failure_beside_it_still_spends_one(self) -> None:
        # The companion claim: the budget is not simply inert here.
        self.write_events(
            "interrupted", verification_cli.VERIFY_EXIT_REASON_FAILED, "interrupted"
        )

        self.assertEqual(
            2,
            verification_transitions.verification_repair_budget(
                self.event_path, SESSION_ID
            ).attempt,
        )

    def test_an_interrupted_review_attempt_consumes_no_verdict_retry(self) -> None:
        self.write_events("interrupted", "interrupted")

        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(
                self.event_path, SESSION_ID
            ),
        )

    def test_an_interruption_earns_no_completion(self) -> None:
        # `interrupted` is a terminating disposition that breaks the loop before
        # `settle_turn`, so there is no path on which it publishes a turn. The
        # runner body is the proof: the branch pauses and breaks.
        source = run_loop_view.assembled_source()
        self.assertRegex(
            source,
            r"if terminating_disposition == ['\"]interrupted['\"]:\s+"
            r"turn_dispositions\.pause_interrupted_run\(",
        )


# ---------------------------------------------------------------------------
# The boundary's own shape
# ---------------------------------------------------------------------------


class ContinuationBoundaryTests(unittest.TestCase):
    """The module's one-way dependency and its single wiring sites."""

    def test_continuation_guards_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, continuation_guards)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        # Every durable write of the accumulated record goes through the
        # persistence seam its caller holds; a file opened here would be a
        # second writer of the same evidence.
        source = ast.parse(
            Path(continuation_guards.__file__).read_text(encoding="utf-8")
        )
        stray = [
            f"line {node.lineno}: {ast.unparse(node.func)}"
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"open", "write_text", "write_bytes", "mkdir"}
        ]
        self.assertEqual([], stray)

    def test_one_arming_site_and_one_recording_site(self) -> None:
        # The ledger's meaning depends on where it is written: an attempt joins
        # it exactly where retained work is armed, which is the one place every
        # post-turn write guard has already cleared.
        source = run_loop_view.assembled_source()
        self.assertEqual(1, source.count("ctx.retained_work = commit_settlement."))
        self.assertEqual(1, source.count("ctx.session_attempts.record("))

    def test_the_ledger_retires_where_the_conversation_does(self) -> None:
        from agent_session_loop import turn_settlement

        source = Path(turn_settlement.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("ctx.session_attempts.retire("))
        self.assertLess(
            source.index("conversation_resume.retire_session_conversations("),
            source.index("ctx.session_attempts.retire("),
        )

    def test_the_events_the_settlement_writes_are_unchanged_in_shape(self) -> None:
        # Guard against the accumulation quietly acquiring an event stream of
        # its own: the record it produces is published by the turn's own
        # artifact and the run state, not by a new event name.
        self.assertFalse(
            any(
                name.startswith("continuation")
                for name in dir(events)
                if name.endswith("_EVENT")
            )
        )

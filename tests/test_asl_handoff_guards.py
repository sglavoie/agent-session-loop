"""What a turn's completion claim has to survive, asserted at the boundary.

The behavioural coverage of the guards themselves already lives where it always
has — ``tests/test_asl_run_gates.py`` drives real runs into the plan-artifact
and mailbox refusals, ``tests/test_asl_review.py`` and
``tests/test_asl_review_lifecycle.py`` own the review-mutation guard's run-level
accounting, and ``tests/test_asl_progress.py`` owns what an operator reads out
of ``progress.md``. What is asserted here is the contract those modules cannot
state, because they exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* the **verify → progress → ledgers** ordering that :func:`_append_progress_entry`
  is the middle step of, pinned against the loop that implements it rather than
  restated in prose — a journal row written before verification would record
  work nobody checked, and a ledger synced before the journal would describe a
  session the journal never got;
* every durable write this boundary makes, named per function, and every
  result-file write taken under the run's one mutex — the guards write a plan
  file, a journal row, a turn result and two events, which is more filesystem
  reach than any other extracted module has, so "which function may write what"
  has to be a checked list rather than a convention;
* the two seams the loop still binds are structural, so drift in either is
  silent: ``handoff_correction._append_handoff_note`` has to keep matching
  :class:`handoff_guards.HandoffNoteAppender`, which stays a Protocol because
  ``handoff_correction`` imports this module and so cannot be imported back,
  and ``session_verification._reinspect_after_revert`` has to keep matching
  ``run_termination.SnapshotReinspector``; and
* neither seam is ever defaulted, so no path can silently skip the note or the
  post-revert re-read.

The seam and ordering groups are why this file exists. A note appender whose
signature drifted still type-checks at call sites that pass it by keyword, and
a revert that skipped its re-read publishes a snapshot describing the plan as
it was *before* the guard put the session back to pending — which is exactly
the record an operator reaches for to find out what the guard did.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    handoff_correction,
    handoff_guards,
    harnesses,
    plans,
    repository_intent,
    run_persistence,
    run_termination,
    runner,
    session_verification,
    turn_settlement,
)
from tests.support import assert_no_runner_import, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_session_path_in_snapshot",
    "_append_progress_entry",
    "_revert_session_status_to_pending",
    "_mailbox_completion_guard_evidence",
    "_enforce_mailbox_completion_guard",
    "_read_review_verdict",
    "_enforce_review_mutation_guard",
    "_terminating_result_disposition",
    "_enforce_plan_artifact_guard",
    "_capture_workspace_write_guard_snapshot",
    "_capture_legacy_implementation_snapshot",
    "_legacy_unobserved_repositories",
)

#: Every call this boundary can make that outlives the process: a file write, a
#: journal append, a turn-result write, and the two events a refusal emits.
#: Anything a guard learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when a
#: new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
        "mkdir",
        "record_turn_rejected",
        "replace",
        "unlink",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
        "_pause_run",
    }
)

#: Which module-level function makes which durable write. Stated per function
#: rather than per module because the interesting failure is not "this module
#: writes" -- it plainly does -- but a *guard* learning to append to the journal,
#: or the journal append learning to rewrite a session file.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_append_progress_entry": frozenset({"append_progress_entry"}),
    "_revert_session_status_to_pending": frozenset({"write_text"}),
    "_enforce_mailbox_completion_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
    "_enforce_review_mutation_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
    "_enforce_plan_artifact_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
}


def _called_names(node: ast.AST) -> set[str]:
    """Every name called under ``node``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, because the substring
    ``open(`` also occurs inside ``_capture_review_repo_fingerprint(`` -- the
    kind of false positive that gets a real assertion deleted rather than fixed.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


class _RecordingAppender:
    """A ``HandoffNoteAppender`` that records instead of rewriting."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        lines: list[str],
        *,
        handoff_index: int,
        section_end: int,
        note: str,
    ) -> list[str]:
        self.calls.append(
            {
                "handoff_index": handoff_index,
                "section_end": section_end,
                "note": note,
            }
        )
        return [*lines, f"- **Note**: {note}"]


class HandoffGuardsBoundaryTests(unittest.TestCase):
    """``handoff_guards`` is runner-free and writes only where it says it does."""

    def setUp(self) -> None:
        self.source = Path(handoff_guards.__file__ or "").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, handoff_guards)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(handoff_guards, name))

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = _called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_every_result_file_write_is_taken_under_the_run_mutex(self) -> None:
        # The turn result is the record a resumed run reads back to find out
        # what the guards decided. A write outside `persistence.lock` races the
        # heartbeat threads that write the same run's state.
        guarded: set[int] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            if not any(
                ast.unparse(item.context_expr) == "persistence.lock"
                for item in node.items
            ):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "write_json_file"
                ):
                    guarded.add(id(child))
        every = [
            child
            for child in ast.walk(self.tree)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "write_json_file"
        ]
        self.assertTrue(every)
        self.assertEqual(
            [],
            [ast.unparse(call) for call in every if id(call) not in guarded],
        )

    def test_neither_collaborator_seam_is_ever_defaulted(self) -> None:
        # A defaulted seam is a path that can skip the note or the re-read
        # without a call site ever saying so. Session 15's rule: loop-owned
        # collaborators are required keyword-only arguments.
        for name in ("append_handoff_note", "reinspect"):
            for function_name in (
                "_revert_session_status_to_pending",
                "_enforce_mailbox_completion_guard",
                "_enforce_review_mutation_guard",
                "_enforce_plan_artifact_guard",
            ):
                signature = inspect.signature(getattr(handoff_guards, function_name))
                parameter = signature.parameters.get(name)
                if parameter is None:
                    # `_revert_session_status_to_pending` re-reads nothing.
                    continue
                with self.subTest(function=function_name, parameter=name):
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_runner_note_appender_still_satisfies_the_seam(self) -> None:
        """``HandoffNoteAppender`` is structural, so drift here is silent.

        Nothing raises when a function passed as a plain callable stops
        satisfying a Protocol. The note is the only place a session file says
        which guard refused it, so an appender that quietly stopped honouring
        ``section_end`` would write the explanation into the wrong section of a
        file the next session reads as its own instructions.
        """
        declared = inspect.signature(handoff_guards.HandoffNoteAppender.__call__)
        actual = inspect.signature(handoff_correction._append_handoff_note)
        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )

    def test_the_bound_reinspector_still_satisfies_the_seam(self) -> None:
        """The post-revert plan re-read is a callback, not an import.

        Session 23 gave ``_reinspect_after_revert`` a home in
        ``session_verification``, but it stays a seam here: this boundary is
        called *by* the verification gate, so importing it back would close a
        cycle. A positional-shape change there would be caught by mypy at the
        call sites, but the *meaning* would not: the fallback and the reverted
        path are both accepted positionally, and swapping them would publish the
        pre-revert snapshot as the final one.
        """
        actual = inspect.signature(session_verification._reinspect_after_revert)
        self.assertEqual(
            ["plan_dir", "fallback", "reverted_path", "plan_cache"],
            list(actual.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                for parameter in actual.parameters.values()
            )
        )

    def test_the_bound_reverter_matches_the_shape_run_termination_expects(
        self,
    ) -> None:
        declared = inspect.signature(run_termination.SessionStatusReverter.__call__)
        actual = inspect.signature(
            handoff_guards.session_status_reverter(_RecordingAppender())
        )
        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )


class VerifyThenProgressThenLedgersTests(unittest.TestCase):
    """The ordering the journal's meaning depends on, read off the settlement.

    ``_append_progress_entry`` is the middle step of a three-step sequence whose
    order is the whole guarantee: only verified work reaches the journal, and
    the journal row lands before the derived ledgers are re-synced. Both halves
    fail silently. A row appended before verification looks exactly like a row
    appended after it, and a ledger synced first only diverges from the journal
    on the run that crashes in between.
    """

    def setUp(self) -> None:
        # Read off `turn_settlement.settle_turn` since Session 36: the three
        # steps left the loop together, and the sequence they form is now that
        # function's whole reason for being one function.
        self.source = inspect.getsource(turn_settlement.settle_turn)

    def positions(self) -> tuple[int, int, int]:
        return (
            self.source.index("_enforce_session_verification("),
            self.source.index("handoff_guards._append_progress_entry("),
            self.source.index("plans.sync_derived_status_ledgers("),
        )

    def test_the_loop_verifies_before_it_appends_to_the_journal(self) -> None:
        verify, progress, _ = self.positions()
        self.assertLess(verify, progress)

    def test_the_loop_appends_to_the_journal_before_it_syncs_the_ledgers(self) -> None:
        _, progress, ledgers = self.positions()
        self.assertLess(progress, ledgers)

    def test_the_loop_appends_through_this_boundary_and_not_a_local_copy(self) -> None:
        # The qualified call is what keeps the ordering above meaningful: a
        # runner-local reimplementation would satisfy the two assertions and
        # leave the boundary's idempotence behind.
        self.assertNotIn("\n    _append_progress_entry(", self.source)


class _PlanCase(unittest.TestCase):
    """Shared scaffolding: a real two-session plan on disk."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.appender = _RecordingAppender()

    def snapshot(self) -> plans.PlanSnapshot:
        return plans.inspect_plan(self.plan_dir)

    def session_path(self, session_id: str) -> Path:
        return self.plan_dir / f"session-{session_id}.md"


class AppendProgressEntryTests(_PlanCase):
    """The one durable append, and the crash it has to survive twice."""

    def test_a_verified_session_gets_one_journal_row(self) -> None:
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-01T00:00:00Z"
            )
        )
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(["1"], [str(entry.session_id) for entry in entries])
        self.assertEqual("2026-01-01T00:00:00Z", entries[0].completed_at)

    def test_repeating_the_same_completion_is_a_no_op(self) -> None:
        # A run that crashed after writing the row and before recording that it
        # had would otherwise publish the session twice, and the journal is what
        # every later session reads as the record of what has been done.
        stamp = "2026-01-01T00:00:00Z"
        self.assertTrue(
            handoff_guards._append_progress_entry(self.snapshot(), "1", stamp)
        )
        self.assertFalse(
            handoff_guards._append_progress_entry(self.snapshot(), "1", stamp)
        )
        self.assertEqual(1, len(plans.read_progress_entries(self.plan_dir)))

    def test_a_second_completion_at_a_different_time_is_a_new_row(self) -> None:
        # Idempotence is keyed on the pair, not the session: a session reopened
        # by a guard and completed again is a genuinely new row.
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-01T00:00:00Z"
            )
        )
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-02T00:00:00Z"
            )
        )
        self.assertEqual(2, len(plans.read_progress_entries(self.plan_dir)))

    def test_the_inline_files_modified_bullet_is_split_on_commas(self) -> None:
        path = self.session_path("1")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "- **Files modified**: (none)",
                "- **Files modified**: a.py, b.py ,  c.py",
            ),
            encoding="utf-8",
        )
        handoff_guards._append_progress_entry(
            self.snapshot(), "1", "2026-01-01T00:00:00Z"
        )
        entry = plans.read_progress_entries(self.plan_dir)[0]
        self.assertEqual(("a.py", "b.py", "c.py"), entry.files_modified)

    def test_a_session_absent_from_the_snapshot_raises_rather_than_writes(self) -> None:
        # The caller records the failure as `progress-journal-append-failed` and
        # carries on; a silent no-op would leave the journal short a row with
        # nothing anywhere saying so.
        with self.assertRaises(plans.PlanError):
            handoff_guards._append_progress_entry(
                self.snapshot(), "99", "2026-01-01T00:00:00Z"
            )
        self.assertEqual((), plans.read_progress_entries(self.plan_dir))


class RevertSessionStatusTests(_PlanCase):
    """The one way a ``complete`` claim is taken back."""

    def revert(self, session_id: str = "1", **overrides: Any) -> Path | None:
        return handoff_guards._revert_session_status_to_pending(
            self.snapshot(),
            session_id,
            failure_note=overrides.pop("failure_note", "guard refused it"),
            append_handoff_note=self.appender,
        )

    def test_the_status_line_goes_back_to_pending(self) -> None:
        path = self.revert()
        self.assertEqual(self.session_path("1"), path)
        self.assertIn(
            f"{plans.STATUS_PREFIX} pending",
            (path or Path()).read_text(encoding="utf-8"),
        )
        self.assertEqual("pending", self.snapshot().sessions[0].status)

    def test_the_note_is_offered_the_bounds_of_the_handoff_section(self) -> None:
        self.revert(failure_note="plan artifact guard failed")
        self.assertEqual(1, len(self.appender.calls))
        call = self.appender.calls[0]
        lines = self.session_path("1").read_text(encoding="utf-8").splitlines()
        self.assertEqual(plans.HANDOFF_HEADING, lines[call["handoff_index"]].strip())
        self.assertLessEqual(call["section_end"], len(lines))
        self.assertLess(call["handoff_index"], call["section_end"])
        self.assertEqual("plan artifact guard failed", call["note"])

    def test_the_trailing_newline_of_the_session_file_is_preserved(self) -> None:
        # The file is rewritten wholesale, and a lost trailing newline shows up
        # as a diff on every session the run touches afterwards.
        original = self.session_path("1").read_text(encoding="utf-8")
        self.assertTrue(original.endswith("\n"))
        self.revert()
        self.assertTrue(
            self.session_path("1").read_text(encoding="utf-8").endswith("\n")
        )

    def test_a_session_absent_from_the_snapshot_reverts_nothing(self) -> None:
        self.assertIsNone(self.revert("99"))
        self.assertEqual([], self.appender.calls)

    def test_a_session_file_without_a_handoff_heading_reverts_nothing(self) -> None:
        # The caller reads the `None` as "the snapshot you hold is still
        # accurate", so returning a path here would republish an unchanged plan
        # as if it had been reverted.
        path = self.session_path("1")
        path.write_text("# Session 1: Test\n\nInstructions.\n", encoding="utf-8")
        self.assertIsNone(self.revert())
        self.assertEqual([], self.appender.calls)

    def test_the_last_handoff_heading_wins(self) -> None:
        # A session whose body quotes an earlier `## Handoff` -- which the
        # extraction sessions' own files all do -- must still have its real
        # status line rewritten, not the quoted one.
        path = self.session_path("1")
        original = path.read_text(encoding="utf-8")
        path.write_text(
            "## Handoff\n\n- **Status**: complete\n\n## Body\n\n" + original,
            encoding="utf-8",
        )
        self.revert()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(f"{plans.STATUS_PREFIX} pending"))
        self.assertLess(
            text.index(f"{plans.STATUS_PREFIX} complete"),
            text.index(f"{plans.STATUS_PREFIX} pending"),
        )


class PlanArtifactAdjudicationTests(_PlanCase):
    """What the artifact guard records for a turn something else already ended.

    A turn can breach its write scope and rewrite a packet it was never given in
    the same turn. The write guard pauses first, and before ASI-119 that ended
    the turn before this guard ran at all, leaving the changed packets in the
    plan with nothing anywhere saying they were changed. Adjudicating records
    the verdict without taking the outcome away from the guard that reached it
    first.
    """

    def setUp(self) -> None:
        super().setUp()
        self.event_path = self.tmp_dir / "events.jsonl"
        self.result_path = self.tmp_dir / "result.json"
        self.persistence = run_persistence._RunPersistence()
        self.turn_payload: dict[str, Any] = {"turn_index": 1}
        # The shape a paused write guard leaves behind: the run is already
        # stopped, under a reason an operator is about to act on.
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "paused": True,
            "pause_reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            "pause_details": {"session_id": "1"},
        }

    def enforce(self, **overrides: Any) -> bool:
        before = self.snapshot()
        return handoff_guards._enforce_plan_artifact_guard(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            before=before,
            after=before,
            digests_before=overrides.pop(
                "digests_before",
                plans.session_artifact_digests(self.plan_dir, exclude_session_id="1"),
            ),
            runner_written=(),
            session_id="1",
            session_label="session 1",
            session_details={"session_id": "1"},
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
            plan_cache=plans.PlanSnapshotCache(),
            append_handoff_note=self.appender,
            reinspect=session_verification._reinspect_after_revert,
            adjudicate_only=True,
            **overrides,
        )

    def tamper(self) -> None:
        path = self.session_path("2")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- **Note**: rewritten\n",
            encoding="utf-8",
        )

    def test_an_unchanged_packet_set_still_records_that_it_was_checked(self) -> None:
        self.assertFalse(self.enforce())
        guard = self.turn_payload["plan_artifact_guard"]
        self.assertTrue(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])

    def test_a_tampered_packet_is_convicted_without_ending_the_turn_again(
        self,
    ) -> None:
        digests_before = plans.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.assertFalse(self.enforce(digests_before=digests_before))

        guard = self.turn_payload["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertTrue(guard["adjudicated_only"])

    def test_the_conviction_defers_to_the_pause_that_ended_the_turn(self) -> None:
        digests_before = plans.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        guard = self.turn_payload["plan_artifact_guard"]
        # The reason an operator has to act on is the one the first guard
        # published; this verdict is recorded beside it, not over it.
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            self.payload["pause_reason"],
        )
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            guard["deferred_to_pause_reason"],
        )
        self.assertEqual(guard, self.payload["pause_details"]["plan_artifact_guard"])

    def test_a_deferred_conviction_does_not_revert_the_session_twice(self) -> None:
        digests_before = plans.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        # Whatever ended the turn first already withdrew the session if it
        # needed withdrawing; a second revert would append a second failure
        # note for one turn.
        self.assertFalse(
            self.turn_payload["plan_artifact_guard"]["reverted_to_pending"]
        )
        self.assertEqual([], self.appender.calls)
        self.assertEqual(
            "complete", plans.inspect_plan(self.plan_dir).status_by_id["1"]
        )

    def test_a_deferred_conviction_still_rejects_the_turn_in_the_event_log(
        self,
    ) -> None:
        digests_before = plans.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        rejections = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(rejections))
        self.assertEqual(events.TURN_REJECTED_EVENT, rejections[0]["name"])
        rejection = rejections[0]["payload"]
        self.assertEqual("plan-artifact-guard", rejection["guard"])
        self.assertFalse(rejection["reopened"])
        self.assertEqual(["session-2.md"], rejection["detail"]["changed_artifacts"])


class TerminatingResultDispositionTests(unittest.TestCase):
    """Which branch ends the turn, and in which order it is asked."""

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "argv": ("codex",),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def disposition(
        self,
        *,
        after: plans.PlanSnapshot | None = None,
        turn_payload: dict[str, Any] | None = None,
        **result_fields: Any,
    ) -> str | None:
        return handoff_guards._terminating_result_disposition(
            self.result(**result_fields),
            after if after is not None else object(),  # type: ignore[arg-type]
            {} if turn_payload is None else turn_payload,
        )

    def test_a_clean_turn_reaches_the_status_guards(self) -> None:
        self.assertIsNone(self.disposition())

    def test_each_terminating_branch_has_a_name(self) -> None:
        for name, kwargs in (
            ("interrupted", {"interrupted": True}),
            ("timeout", {"timed_out": True}),
            ("aborted", {"aborted": True}),
            ("nonzero-exit", {"returncode": 1}),
        ):
            with self.subTest(name=name):
                self.assertEqual(name, self.disposition(**kwargs))

    def test_a_plan_that_could_not_be_inspected_is_named_malformed(self) -> None:
        self.assertEqual(
            "malformed-plan",
            handoff_guards._terminating_result_disposition(self.result(), None, {}),
        )

    def test_a_worker_pause_is_named_before_a_nonzero_exit(self) -> None:
        # A worker that printed `RUNNER_PAUSE` and then exited nonzero has said
        # why it stopped; reporting the exit code instead loses the reason.
        self.assertEqual(
            "runner-pause",
            self.disposition(
                returncode=1, turn_payload={"runner_pause": {"reason": "blocked"}}
            ),
        )

    def test_an_interrupt_outranks_every_other_branch(self) -> None:
        self.assertEqual(
            "interrupted",
            self.disposition(
                interrupted=True,
                timed_out=True,
                aborted=True,
                returncode=1,
                turn_payload={"runner_pause": {"reason": "blocked"}},
            ),
        )


class LegacyUnobservedRepositoriesTests(unittest.TestCase):
    """Silence about a place you did not look is not evidence of no change."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bound = self.tmp_dir / "bound"
        (self.bound / ".git").mkdir(parents=True)
        self.plan_dir = self.tmp_dir / "plan"
        self.plan_dir.mkdir()

    def make_repo(self, name: str) -> Path:
        repo = self.tmp_dir / name
        (repo / ".git").mkdir(parents=True)
        return repo

    def artifact(self, text: str, name: str = "session-1.md") -> Path:
        path = self.plan_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def unobserved(self, *artifacts: Path, **overrides: Any) -> dict[str, Any]:
        return handoff_guards._legacy_unobserved_repositories(
            cwd=self.bound,
            artifact_paths=artifacts,
            exempt_roots=(self.plan_dir,),
            provenance_roots=(),
            **overrides,
        )

    def test_the_bound_repository_is_not_reported_as_unobserved(self) -> None:
        self.assertEqual(
            {}, self.unobserved(self.artifact(f"Work in `{self.bound}`.\n"))
        )

    def test_a_second_repository_named_by_an_artifact_is_reported(self) -> None:
        other = self.make_repo("other")
        payload = self.unobserved(self.artifact(f"Also patch `{other}/mod.py`.\n"))
        self.assertEqual([str(other)], list(payload))
        self.assertEqual("repository-not-observed", payload[str(other)]["reason"])
        self.assertEqual(str(self.bound), payload[str(other)]["observed_repository"])

    def test_an_unreadable_artifact_names_no_repository_and_does_not_raise(
        self,
    ) -> None:
        # The turn is still judged on every artifact that can be read; a
        # fingerprint missing because a file vanished is caught by the
        # comparison, not by this scan.
        other = self.make_repo("other")
        missing = self.plan_dir / "session-2.md"
        payload = self.unobserved(missing, self.artifact(f"Patch `{other}`.\n"))
        self.assertEqual([str(other)], list(payload))

    def test_a_truncated_list_still_reports_the_count_it_dropped(self) -> None:
        # The guard fails closed on the first entry either way, so once the list
        # is cut the number matters more than the names -- and a truncation that
        # said nothing would read as a shorter list, not a longer one.
        repos = [self.make_repo(f"repo-{index}") for index in range(4)]
        body = "\n".join(f"Patch `{repo}`." for repo in repos)
        payload = self.unobserved(self.artifact(body + "\n"), limit=2)
        self.assertEqual(2 + 1, len(payload))
        self.assertIn("<omitted>", payload)
        self.assertEqual(2, payload["<omitted>"]["omitted_repositories"])

    def test_the_default_limit_is_the_one_the_intent_report_uses(self) -> None:
        # The pre-turn packet gate and this post-turn scan are read side by
        # side; two different truncation points would make them disagree about
        # how many repositories a turn reached.
        signature = inspect.signature(handoff_guards._legacy_unobserved_repositories)
        self.assertEqual(
            repository_intent.INTENT_REPORT_LIMIT,
            signature.parameters["limit"].default,
        )


if __name__ == "__main__":
    unittest.main()

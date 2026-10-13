"""Boundary tests for `agent_session_loop.session_verification`.

The behavioural coverage of the two gates is end to end and lives where it
always has: `tests/test_asl_run_verification_enforcement.py` drives real contracts
through real checks and asserts what an operator sees after a failure,
`tests/test_asl_review_lifecycle_*.py` owns the review-verdict retry and
exhaustion accounting, and `tests/test_asl_e2e.py` owns the verification repair
budget. None of them can cheaply state the things this module owns, so they are
stated here: the extraction contract (runner-free, no alias left behind, the one
remaining seam required rather than defaulted and supplied at both call sites),
the **verify -> progress -> ledgers** ordering this gate is the first step of,
the refusal shape both gates perform, and the post-mutation re-read every guard
in the package reaches through a seam.

Two of the module's paths are driven for real against a plan on disk -- the
refusal a contract-less session earns and the waiver the same session earns
under `--allow-not-loop-ready`. Neither reaches `verification.run_verification`,
so nothing here spawns a process, which is what keeps this module in
`PORTABLE_MODULES`.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from agent_session_loop import (
    handoff_correction,
    handoff_guards,
    plans_snapshot,
    review_transitions,
    run_persistence,
    run_reporting,
    runner,
    session_verification,
    turn_settlement,
    verification_transitions,
)
from tests.support import (
    _contract_block,
    assert_no_runner_import,
    called_names,
    read_jsonl,
    write_plan,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_enforce_review_verdict",
    "_enforce_session_verification",
    "_reinspect_after_revert",
    "_reinspect_after_plan_mutation",
)

#: The two gates, which are the only functions here that take the seam and the
#: only ones that can refuse a turn.
ENFORCEMENT_FUNCTIONS = (
    "_enforce_review_verdict",
    "_enforce_session_verification",
)

#: The four constants the session file listed alongside the functions, mapped
#: to the transition module that owns each. This module defines none of them --
#: a second copy here would fork a repair budget the owners adjudicate against
#: theirs -- and since Session 38 retired the runner's re-export lines the
#: runner holds none of them either, so the owner is the only place to read one.
OWNED_CONSTANTS = {
    "MAX_VERIFICATION_REPAIR_ATTEMPTS": verification_transitions,
    "VERIFICATION_FINGERPRINT_PREFIX": verification_transitions,
    "MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS": review_transitions,
    "REVIEW_VERDICT_FINGERPRINT_PREFIX": review_transitions,
}

#: The collaborator this boundary does not own -- supplied by
#: ``handoff_correction._append_handoff_note`` -- and the name this module
#: receives it under. It may not acquire a default: a refusal that silently
#: skipped the note would reopen a session without saying why.
SEAM_PARAMETERS = ("append_handoff_note",)

#: Every call this boundary could make that outlives the process. Anything a
#: function here learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when a
#: new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)

#: Which module-level function writes directly, and what it writes. The
#: session-file revert belongs to `handoff_guards`, the rejection event to
#: `turn_execution_commit` and the pause event to `run_termination`, each of which owns
#: that write at its own boundary; what is left here is the turn result both
#: gates write under the mutex, plus the one retry event the verdict gate
#: appends on its own behalf because the retry budget is its own accounting.
#: Stating it per function rather than as a flat prohibition is what makes a
#: *third* writer appearing here a test failure instead of a review catch.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_enforce_review_verdict": frozenset({"write_json_file", "append_event"}),
    "_enforce_session_verification": frozenset({"write_json_file"}),
}


class SessionVerificationBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in two places."""

    def setUp(self) -> None:
        source_path = Path(session_verification.__file__ or "")
        self.source = source_path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, session_verification)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(session_verification, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), defined)

    def test_the_module_defines_none_of_the_reexported_constants(self) -> None:
        # The session file listed all four alongside the functions, but the
        # extraction ledger assigns them to the transition modules that already
        # own them. Redefining one here would fork a repair budget the two
        # `*_transitions` modules adjudicate against their own copy.
        assigned = {
            target.id
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(set(), assigned & set(OWNED_CONSTANTS))

    def test_the_runner_keeps_no_alias_for_the_owned_constants(self) -> None:
        # Session 23 rewrote the readers and left the runner's alias lines
        # standing so `runner.MAX_VERIFICATION_REPAIR_ATTEMPTS` kept resolving;
        # Session 38 deleted them with the other 222. The owner still holds the
        # name, so a re-export restored here to rescue a stale spelling fails.
        for name, owner in OWNED_CONSTANTS.items():
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(owner, name))

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_every_result_file_write_is_taken_under_the_run_mutex(self) -> None:
        # The turn result is the record a resumed run reads back to find out
        # what this gate decided. A write outside `persistence.lock` races the
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


class SeamTests(unittest.TestCase):
    """The one runner-owned collaborator enters as a required keyword-only seam."""

    def test_the_seam_is_required_keyword_only_on_both_gates(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            signature = inspect.signature(getattr(session_verification, name))
            for seam in SEAM_PARAMETERS:
                with self.subTest(name=name, seam=seam):
                    parameter = signature.parameters[seam]
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_seam_protocol_is_satisfied_by_the_symbol_the_loop_passes(self) -> None:
        # Session 24 gave `_append_handoff_note` a home, but the seam stays:
        # `handoff_correction` imports this module, so importing the appender
        # back would close a cycle. This is what checks the declared shape
        # against the symbol actually passed, rather than only the loop's call
        # sites doing so at runtime.
        appender: handoff_guards.HandoffNoteAppender = (
            handoff_correction._append_handoff_note
        )
        self.assertEqual(
            ["- **Notes**:", "  - verification failed"],
            appender(
                ["## Handoff", "", "- **Notes**:"],
                handoff_index=0,
                section_end=3,
                note="verification failed",
            )[2:],
        )

    def test_the_loop_passes_the_seam_at_every_gate_call(self) -> None:
        # Session 36 moved both gate calls out of the loop and into
        # `turn_settlement`; the claim is unchanged -- every call site supplies
        # the seam -- only the file that holds the call sites moved.
        tree = ast.parse(
            Path(turn_settlement.__file__ or "").read_text(encoding="utf-8")
        )
        calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if (
                not isinstance(func.value, ast.Name)
                or func.value.id != "session_verification"
            ):
                continue
            if func.attr not in ENFORCEMENT_FUNCTIONS:
                continue
            calls += 1
            passed = {keyword.arg for keyword in node.keywords}
            with self.subTest(line=node.lineno, name=func.attr):
                self.assertLessEqual(set(SEAM_PARAMETERS), passed)
        # One per gate: verification on every turn, the verdict on review turns.
        self.assertEqual(2, calls)


class VerifyThenProgressThenLedgersTests(unittest.TestCase):
    """The ordering the journal's meaning depends on, read off the settlement.

    This gate is the first step of a three-step sequence whose order is the
    whole guarantee: only verified work reaches ``progress.md``, and the journal
    row lands before the derived ledgers are re-synced. Both halves fail
    silently -- a row appended before verification looks exactly like a row
    appended after it -- so Session 23's contract requires this be asserted
    rather than assumed. It is the counterpart to the same three positions read
    from the journal's side in ``tests/test_asl_handoff_guards.py``.
    """

    def setUp(self) -> None:
        # Read off `turn_settlement.settle_turn` since Session 36: the three
        # steps left the loop together, and the sequence they form is now that
        # function's whole reason for being one function.
        self.source = inspect.getsource(turn_settlement.settle_turn)

    def positions(self) -> tuple[int, int, int]:
        return (
            self.source.index("session_verification._enforce_session_verification("),
            self.source.index("handoff_guards._append_progress_entry("),
            self.source.index("plans_status_tables.sync_derived_status_ledgers("),
        )

    def test_the_loop_verifies_before_it_appends_to_the_journal(self) -> None:
        verify, progress, _ = self.positions()
        self.assertLess(verify, progress)

    def test_the_loop_verifies_before_it_syncs_the_ledgers(self) -> None:
        verify, _, ledgers = self.positions()
        self.assertLess(verify, ledgers)

    def test_the_loop_verifies_through_this_boundary_and_not_a_local_copy(self) -> None:
        # The qualified call is what keeps the ordering above meaningful: a
        # runner-local reimplementation would satisfy both assertions and leave
        # the state machine and the repair budget behind.
        self.assertNotIn("\n    if _enforce_session_verification(", self.source)

    def test_the_review_verdict_gate_runs_after_verification(self) -> None:
        # A review session is verified like any other session first; the verdict
        # gate is what runs afterwards on the review lane only. Reversing them
        # would adjudicate a verdict produced by a turn whose own checks failed.
        verify, _, _ = self.positions()
        self.assertLess(
            verify,
            self.source.index("session_verification._enforce_review_verdict("),
        )


class RefusalShapeTests(unittest.TestCase):
    """A refusal reverts, re-reads, records, and pauses, in that order."""

    def _source(self, name: str) -> str:
        return inspect.getsource(getattr(session_verification, name))

    def test_every_refusal_reverts_rereads_writes_records_then_pauses(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                source = self._source(name)
                reverted = source.index(
                    "handoff_guards._revert_session_status_to_pending("
                )
                reinspected = source.index("_reinspect_after_revert(")
                # The accepted lane writes the result too, so the refusal's own
                # write is the last one rather than the first.
                written = source.rindex("durability.write_json_file(")
                recorded = source.rindex("turn_execution_commit.record_turn_rejected(")
                paused = source.rindex("run_termination._pause_run(")
                self.assertLess(reverted, reinspected)
                self.assertLess(reinspected, written)
                self.assertLess(written, recorded)
                self.assertLess(recorded, paused)

    def test_every_refusal_republishes_the_snapshot_it_just_reread(self) -> None:
        # The revert moved a status, so the snapshot the run publishes must be
        # the post-revert one; publishing the pre-revert snapshot would report a
        # session as complete that the gate had just reopened.
        republished = (
            'payload["final_snapshot"] = '
            "plans_payloads.snapshot_payload(snapshot_after)"
        )
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                self.assertIn(republished, self._source(name))

    def test_the_repair_budget_is_read_before_this_failure_is_recorded(self) -> None:
        # Both gates grade the current failure against the priors already on the
        # event log. Appending first would make every failure its own precedent
        # and burn an attempt the operator never used.
        verdict = self._source("_enforce_review_verdict")
        self.assertLess(
            verdict.index("read_prior_review_verdict_retries("),
            verdict.index("REVIEW_VERDICT_RETRY_EVENT"),
        )
        verification = self._source("_enforce_session_verification")
        self.assertLess(
            verification.index("verification_repair_budget("),
            verification.rindex("run_termination._pause_run("),
        )


class ReinspectTests(unittest.TestCase):
    """The post-mutation re-read every guard in the package reaches by seam."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.cache = plans_snapshot.PlanSnapshotCache()
        self.fallback = plans_snapshot.inspect_plan(self.plan_dir)

    def test_nothing_reverted_returns_the_fallback_untouched(self) -> None:
        # Not merely equal: an unnecessary re-read of a large plan on every
        # cleared guard is the cost this early return exists to avoid.
        self.assertIs(
            self.fallback,
            session_verification._reinspect_after_revert(
                self.plan_dir, self.fallback, None, self.cache
            ),
        )

    def test_a_revert_rereads_the_plan_the_runner_just_rewrote(self) -> None:
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "- **Status**: complete", "- **Status**: pending"
            ),
            encoding="utf-8",
        )
        refreshed = session_verification._reinspect_after_revert(
            self.plan_dir, self.fallback, session, self.cache
        )
        self.assertEqual(
            ["complete"],
            [entry.status for entry in self.fallback.sessions if entry.id == "1"],
        )
        self.assertEqual(
            ["pending"],
            [entry.status for entry in refreshed.sessions if entry.id == "1"],
        )

    def test_an_unparsable_rewrite_keeps_the_pre_mutation_snapshot(self) -> None:
        # The caller already reports the parse failure; degrading to the stale
        # snapshot here is what keeps the guard's own pause reason the one an
        # operator sees, rather than a `PlanError` raised from a re-read.
        for entry in self.plan_dir.iterdir():
            entry.unlink()
        self.assertIs(
            self.fallback,
            session_verification._reinspect_after_plan_mutation(
                self.cache, self.fallback
            ),
        )


class _Turn:
    """One turn's worth of scaffolding for a gate driven against a real plan."""

    def __init__(self, tmp_dir: Path, *, contract: str | None = None) -> None:
        self.plan_dir = tmp_dir / "plan"
        write_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id=None if contract is None else {"1": contract},
        )
        self.event_path = tmp_dir / "events.jsonl"
        self.paths = {
            "result": tmp_dir / "result.json",
            "artifact_dir": tmp_dir / "artifacts",
        }
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.turn_payload: dict[str, Any] = {}
        self.snapshot = plans_snapshot.inspect_plan(self.plan_dir)

    def enforce(self, *, allow_not_loop_ready: bool) -> bool:
        return session_verification._enforce_session_verification(
            event_path=self.event_path,
            payload=self.payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            progress=cast(run_reporting._ProgressReporter, SimpleNamespace()),
            before=self.snapshot,
            after=self.snapshot,
            cwd=self.plan_dir,
            session_id="1",
            session_label="session 1",
            session_details={"session_id": "1"},
            allow_not_loop_ready=allow_not_loop_ready,
            turn_payload=self.turn_payload,
            paths=self.paths,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            append_handoff_note=handoff_correction._append_handoff_note,
            run_id="run-1",
            turn_index=1,
            total_sessions=1,
        )

    def status(self) -> str | None:
        for session in plans_snapshot.inspect_plan(self.plan_dir).sessions:
            if session.id == "1":
                return session.status
        return None

    def result(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(self.paths["result"].read_text(encoding="utf-8")),
        )


class ContractlessSessionTests(unittest.TestCase):
    """A session with no `Verify` recipe, on both sides of the loop-ready flag.

    These are the two gate outcomes reachable without running a check, so they
    are the two this module can assert end to end: the refusal a strict run
    earns, and the waiver `--allow-not-loop-ready` buys. What matters about the
    pair is that they are *different records* -- the waiver has to be legible as
    "nothing was enforced" rather than indistinguishable from a pass.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_a_strict_run_refuses_and_reopens_the_session(self) -> None:
        turn = _Turn(self.tmp_dir)
        self.assertTrue(turn.enforce(allow_not_loop_ready=False))
        self.assertEqual("pending", turn.status())
        self.assertEqual(
            verification_transitions.VERIFY_REFUSAL_NO_CONTRACT,
            turn.result()["verification"]["reason"],
        )
        self.assertTrue(turn.result()["verification"]["enforced"])

    def test_the_refusal_names_itself_in_the_handoff_it_reopens(self) -> None:
        turn = _Turn(self.tmp_dir)
        turn.enforce(allow_not_loop_ready=False)
        self.assertIn(
            "missing a Completion Contract",
            (turn.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_the_refusal_records_the_rejection_and_then_the_pause(self) -> None:
        turn = _Turn(self.tmp_dir)
        turn.enforce(allow_not_loop_ready=False)
        names = [entry["name"] for entry in read_jsonl(turn.event_path)]
        self.assertIn("turn_rejected", names)
        self.assertIn("run_paused", names)
        self.assertLess(names.index("turn_rejected"), names.index("run_paused"))

    def test_the_refusal_publishes_the_snapshot_it_reopened(self) -> None:
        # Publishing the pre-revert snapshot would report the session as
        # complete in the very payload that says the run paused for it.
        turn = _Turn(self.tmp_dir)
        turn.enforce(allow_not_loop_ready=False)
        statuses = {
            entry["id"]: entry["status"]
            for entry in turn.payload["final_snapshot"]["sessions"]
        }
        self.assertEqual("pending", statuses["1"])

    def test_a_waived_run_continues_without_claiming_checks_ran(self) -> None:
        turn = _Turn(self.tmp_dir)
        self.assertFalse(turn.enforce(allow_not_loop_ready=True))
        self.assertEqual("complete", turn.status())
        verification = turn.result()["verification"]
        self.assertFalse(verification["enforced"])
        self.assertEqual(
            verification_transitions.VERIFY_REFUSAL_NO_CONTRACT,
            verification["reason"],
        )
        self.assertNotIn("passed_count", verification)

    def test_a_waiver_reopens_nothing_and_pauses_nothing(self) -> None:
        turn = _Turn(self.tmp_dir)
        turn.enforce(allow_not_loop_ready=True)
        self.assertFalse(turn.event_path.exists())

    def test_a_malformed_contract_outranks_the_loop_ready_waiver(self) -> None:
        # An unreadable recipe is a plan defect, not an absent gate, so
        # `--allow-not-loop-ready` must not wave it through.
        turn = _Turn(self.tmp_dir, contract=_contract_block("{not json", session="1"))
        self.assertTrue(turn.enforce(allow_not_loop_ready=True))
        self.assertEqual("pending", turn.status())
        self.assertEqual(
            verification_transitions.VERIFY_REFUSAL_MALFORMED_CONTRACT,
            turn.result()["verification"]["reason"],
        )


if __name__ == "__main__":
    unittest.main()

"""Whether a plan may run at all, asserted at the seam rather than through a run.

The behavioural coverage of the gate already lives where it always has —
``tests/test_asl_run_readiness_hard_gate.py`` drives a run into every refusal the gate
can issue, ``tests/test_asl_review_*.py`` owns the re-gate a grown plan must pass, and
``tests/test_asl_review_lifecycle_*.py`` owns what the auto-repair rewrote. What is
asserted here is the contract those modules cannot state, because they exercise
the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* the only file it opens of its own is the plan manifest, through ``plans`` —
  every record it writes into the run payload goes through the
  ``_RunPersistence`` handle the caller supplied, under that handle's lock;
* ``write_readiness_state`` still satisfies
  ``run_termination.ReadinessStateWriter`` exactly, because that Protocol is
  structural and a widened parameter here would break the terminal path with no
  error anywhere near it; and
* the three gates stay ordered — review-only ⊂ loop-ready ⊂ resume — so a
  session that loosens one of them has to say so.

The last two are why this file exists. Both failure modes are silent: a
readiness writer whose signature drifted still type-checks at every call site
that passes it positionally, and a gate quietly widened to accept ``running``
would let a crash-resumed plan start without the normalization the strict gate
exists to force.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    plans,
    preflight,
    readiness_gate,
    run_persistence,
    run_termination,
    runner,
    state,
)
from tests.support import assert_no_runner_import, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_allow_not_loop_ready",
    "_readiness_repair_hints",
    "_readiness_gate_payload",
    "_non_bypassable_blockers",
    "_readiness_gate_passed",
    "_resume_gate_passed",
    "_reopened_plan_gate_payload",
    "_regate_reopened_plan",
    "_review_only_complete_start",
    "_readiness_gate_failure_message",
    "_refresh_active_readiness_state",
    "_final_readiness_state",
    "_strip_own_lock_findings",
    "_readiness_gate_blockers",
    "_reduced_preflight_report",
    "_repair_readiness_gate",
)

#: ``_write_readiness_state`` is the one moved symbol that changed name. It is
#: published because it *is* the seam ``run_termination.ReadinessStateWriter``
#: names: a private name here would be a name every terminal path reaches
#: around.
RENAMED_SYMBOLS = {"_write_readiness_state": "write_readiness_state"}


def _durable_write_receivers(source: str) -> set[str]:
    """Every receiver of a ``write_state`` or ``append_event`` call, as source."""
    tree = ast.parse(source)
    receivers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"write_state", "append_event"}:
            continue
        receivers.add(ast.unparse(node.func.value))
    return receivers


class _RecordingPersistence(run_persistence._RunPersistence):
    """A real handle over a temp directory that also records what it wrote."""

    def __init__(self) -> None:
        super().__init__(write_state=False)
        self.state_writes: list[dict[str, Any]] = []

    def write_state(self, payload: dict[str, Any]) -> None:
        self.state_writes.append(dict(payload))


class ReadinessGateBoundaryTests(unittest.TestCase):
    """The structural contract of the boundary itself."""

    @property
    def source(self) -> str:
        return Path(readiness_gate.__file__ or "").read_text(encoding="utf-8")

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, readiness_gate)

    def test_the_only_file_it_opens_is_the_manifest_through_plans(self) -> None:
        # The gate reads a lot and writes one thing. Every payload record it
        # owns goes through the caller's mutex, and the single durable write
        # that leaves the run directory is the readiness metadata, which
        # `plans` owns the format and the atomic replace of.
        for reach in ("write_text(", "open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)
        self.assertIn("plans.write_readiness_metadata(", self.source)

    def test_every_payload_write_goes_through_a_persistence_handle(self) -> None:
        self.assertEqual({"persistence"}, _durable_write_receivers(self.source))

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(readiness_gate, name))

    def test_the_published_readiness_writer_replaced_its_private_name(self) -> None:
        for old, new in RENAMED_SYMBOLS.items():
            with self.subTest(name=old):
                self.assertFalse(hasattr(runner, old))
                self.assertFalse(hasattr(readiness_gate, old))
                self.assertTrue(hasattr(readiness_gate, new))

    def test_the_readiness_writer_still_satisfies_the_termination_seam(self) -> None:
        """``ReadinessStateWriter`` is structural, so drift here is silent.

        Nothing raises when a Protocol stops being satisfied by a function
        passed as a plain callable; the terminal path would simply start calling
        this with an argument it no longer accepts, at the moment a run ends.
        """
        declared = inspect.signature(run_termination.ReadinessStateWriter.__call__)
        actual = inspect.signature(readiness_gate.write_readiness_state)

        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )


class GateOrderingTests(unittest.TestCase):
    """Review-only ⊂ loop-ready ⊂ resume, stated rather than implied."""

    def _report(
        self,
        *,
        readiness_state: str | None,
        findings: tuple[preflight.PreflightFinding, ...] = (),
        recorded_sha: str = "0" * 64,
    ) -> preflight.PreflightReport:
        readiness = None
        if readiness_state is not None:
            readiness = plans.ReadinessMetadata(
                state=readiness_state,
                readiness_sha256=recorded_sha,
                checked_at="2026-09-01T00:00:00Z",
            )
        return preflight.PreflightReport(
            plan_dir=Path("/plan"),
            findings=findings,
            readiness=readiness,
            expected_readiness_sha256="0" * 64,
        )

    def test_the_resume_gate_accepts_everything_the_strict_gate_does(self) -> None:
        for readiness_state in preflight.RESUME_READINESS_STATES:
            report = self._report(readiness_state=readiness_state)
            with self.subTest(state=readiness_state):
                self.assertTrue(readiness_gate._resume_gate_passed(report))
        strict = self._report(readiness_state="loop-ready")
        self.assertTrue(readiness_gate._readiness_gate_passed(strict))

    def test_only_the_resume_gate_accepts_a_crash_resumed_plan(self) -> None:
        report = self._report(readiness_state="running")

        self.assertTrue(readiness_gate._resume_gate_passed(report))
        self.assertFalse(readiness_gate._readiness_gate_passed(report))

    def test_neither_gate_passes_a_report_that_is_not_ok(self) -> None:
        report = self._report(
            readiness_state="loop-ready",
            findings=(
                preflight.PreflightFinding(
                    severity="red", category="env-harness", message="broken"
                ),
            ),
        )

        self.assertFalse(readiness_gate._resume_gate_passed(report))
        self.assertFalse(readiness_gate._readiness_gate_passed(report))

    def test_missing_readiness_metadata_fails_every_gate(self) -> None:
        report = self._report(readiness_state=None)

        self.assertFalse(readiness_gate._resume_gate_passed(report))
        self.assertFalse(readiness_gate._readiness_gate_passed(report))
        self.assertEqual(
            "plan manifest is missing readiness metadata",
            readiness_gate._readiness_gate_failure_message(report),
        )

    def test_a_review_only_complete_start_needs_all_four_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "complete"))
            complete = plans.inspect_plan(plan_dir)
            write_plan(plan_dir, ("complete", "pending"))
            pending = plans.inspect_plan(plan_dir)

        report = self._report(readiness_state="complete")
        self.assertTrue(
            readiness_gate._review_only_complete_start(
                report, review_requested=True, snapshot=complete
            )
        )
        # The strict gate refuses exactly this report, which is the whole point.
        self.assertFalse(readiness_gate._readiness_gate_passed(report))

        for kwargs, snapshot in (
            ({"review_requested": False}, complete),
            ({"review_requested": True}, pending),
        ):
            with self.subTest(kwargs=kwargs, pending=snapshot is pending):
                self.assertFalse(
                    readiness_gate._review_only_complete_start(
                        report, snapshot=snapshot, **kwargs
                    )
                )

        stale = self._report(readiness_state="complete", recorded_sha="1" * 64)
        self.assertFalse(
            readiness_gate._review_only_complete_start(
                stale, review_requested=True, snapshot=complete
            )
        )

    def test_the_escape_hatch_never_covers_a_non_bypassable_category(self) -> None:
        findings = tuple(
            preflight.PreflightFinding(
                severity="red", category=category, message=category
            )
            for category in sorted(preflight.NON_BYPASSABLE_PREFLIGHT_CATEGORIES)
        )
        report = self._report(readiness_state="loop-ready", findings=findings)

        self.assertEqual(
            sorted(preflight.NON_BYPASSABLE_PREFLIGHT_CATEGORIES),
            list(readiness_gate._non_bypassable_blockers(report)),
        )

    def test_a_yellow_finding_is_a_blocker_but_never_non_bypassable(self) -> None:
        category = sorted(preflight.NON_BYPASSABLE_PREFLIGHT_CATEGORIES)[0]
        report = self._report(
            readiness_state="loop-ready",
            findings=(
                preflight.PreflightFinding(
                    severity="yellow", category=category, message="soft"
                ),
            ),
        )

        self.assertEqual((), readiness_gate._non_bypassable_blockers(report))
        self.assertEqual(1, len(readiness_gate._readiness_gate_blockers(report)))

    def test_the_gate_payload_dedupes_repair_hints_by_check_id(self) -> None:
        report = self._report(
            readiness_state="running",
            findings=(
                preflight.PreflightFinding(
                    severity="red", category="manifest-readiness", message="one"
                ),
                preflight.PreflightFinding(
                    severity="red", category="manifest-readiness", message="two"
                ),
                preflight.PreflightFinding(
                    severity="green", category="manifest-readiness", message="fine"
                ),
            ),
        )

        payload = readiness_gate._readiness_gate_payload(report, cwd=Path("/repo"))

        self.assertFalse(payload["ok"])
        self.assertEqual(3, len(payload["findings"]))
        self.assertEqual(1, len(payload["repair_hints"]))
        self.assertEqual("/plan", payload["repair_hints"][0]["message"].split()[3])


class OwnLockTests(unittest.TestCase):
    """A run never refuses to start because it started."""

    def _lock_info(self, path: Path, pid: int | None) -> state.LockInfo:
        return state.LockInfo(
            path=path,
            pid=pid,
            plan_dir="/plan",
            created_at="2026-09-01T00:00:00Z",
            stale=False,
            reason="held",
            raw={},
        )

    def _report(
        self, findings: tuple[preflight.PreflightFinding, ...]
    ) -> preflight.PreflightReport:
        return preflight.PreflightReport(
            plan_dir=Path("/plan"),
            findings=findings,
            readiness=None,
            expected_readiness_sha256="0" * 64,
        )

    def test_our_own_lock_finding_is_dropped(self) -> None:
        import os

        lock_path = Path("/state/run.lock")
        report = self._report(
            (
                preflight.PreflightFinding(
                    severity="red",
                    category="env-lock",
                    message="held",
                    path=str(lock_path),
                ),
            )
        )

        stripped = readiness_gate._strip_own_lock_findings(
            report, lock_info=self._lock_info(lock_path, os.getpid())
        )

        self.assertEqual((), stripped.findings)

    def test_another_process_lock_survives(self) -> None:
        import os

        lock_path = Path("/state/run.lock")
        report = self._report(
            (
                preflight.PreflightFinding(
                    severity="red",
                    category="env-lock",
                    message="held",
                    path=str(lock_path),
                ),
            )
        )

        stripped = readiness_gate._strip_own_lock_findings(
            report, lock_info=self._lock_info(lock_path, os.getpid() + 1)
        )

        self.assertEqual(report.findings, stripped.findings)

    def test_an_unrelated_finding_returns_the_same_report_object(self) -> None:
        report = self._report(
            (
                preflight.PreflightFinding(
                    severity="red", category="env-harness", message="broken"
                ),
            )
        )

        stripped = readiness_gate._strip_own_lock_findings(
            report, lock_info=self._lock_info(Path("/state/run.lock"), None)
        )

        self.assertIs(report, stripped)


class ReadinessStateWriteTests(unittest.TestCase):
    """The one durable write that leaves the run directory."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))

    def test_a_write_stamps_the_plan_s_own_structural_digest(self) -> None:
        success, error = readiness_gate.write_readiness_state(
            self.plan_dir, "loop-ready"
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir), metadata.readiness_sha256
        )

    def test_an_expected_digest_is_recorded_verbatim(self) -> None:
        readiness_gate.write_readiness_state(
            self.plan_dir, "running", expected_sha256="a" * 64
        )

        self.assertEqual(
            "a" * 64, plans.read_readiness_metadata(self.plan_dir).readiness_sha256
        )

    def test_a_failure_is_reported_rather_than_raised(self) -> None:
        success, error = readiness_gate.write_readiness_state(
            self.tmp_dir / "absent", "loop-ready"
        )

        self.assertFalse(success)
        self.assertIsNotNone(error)

    def test_the_refresh_is_a_no_op_for_an_inactive_lifecycle(self) -> None:
        persistence = _RecordingPersistence()
        payload: dict[str, Any] = {}

        readiness_gate._refresh_active_readiness_state(
            payload, self.plan_dir, persistence
        )

        self.assertEqual({}, payload)
        self.assertEqual([], persistence.state_writes)

    def test_the_refresh_records_the_moment_it_succeeded(self) -> None:
        persistence = _RecordingPersistence()
        payload: dict[str, Any] = {"readiness_state_active": True}

        readiness_gate._refresh_active_readiness_state(
            payload, self.plan_dir, persistence
        )

        self.assertEqual("running", payload["readiness_state_current"])
        self.assertIsNone(payload["readiness_state_refresh_error"])
        self.assertIn("readiness_state_refreshed_at", payload)
        self.assertEqual(1, len(persistence.state_writes))

    def test_a_refused_refresh_records_the_error_and_holds_its_state(self) -> None:
        persistence = _RecordingPersistence()
        payload: dict[str, Any] = {"readiness_state_active": True}

        readiness_gate._refresh_active_readiness_state(
            payload, self.tmp_dir / "absent", persistence
        )

        self.assertNotIn("readiness_state_current", payload)
        self.assertIsNotNone(payload["readiness_state_refresh_error"])
        self.assertEqual(1, len(persistence.state_writes))

    def test_the_final_state_reads_the_run_s_verdict_then_the_plan(self) -> None:
        snapshot = plans.inspect_plan(self.plan_dir)
        write_plan(self.plan_dir, ("complete", "complete"))
        finished = plans.inspect_plan(self.plan_dir)

        self.assertEqual(
            "reviewed",
            readiness_gate._final_readiness_state({"paused": True}, finished),
        )
        self.assertEqual(
            "reviewed", readiness_gate._final_readiness_state({"ok": False}, finished)
        )
        self.assertEqual(
            "complete", readiness_gate._final_readiness_state({"ok": True}, finished)
        )
        self.assertEqual(
            "loop-ready", readiness_gate._final_readiness_state({"ok": True}, snapshot)
        )


class ReopenedPlanGateTests(unittest.TestCase):
    """Plan growth is re-gated rather than grandfathered."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan_dir = self.tmp_dir / "plan"

    def _snapshot(self, statuses: tuple[str, ...]) -> plans.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans.inspect_plan(self.plan_dir)

    def test_a_plan_within_budget_is_authorized(self) -> None:
        gate = readiness_gate._reopened_plan_gate_payload(
            self._snapshot(("complete", "pending")),
            max_pending_sessions=1,
            repair_session_ids=[],
        )

        self.assertTrue(gate["ok"])
        self.assertEqual(1, gate["pending_sessions"])
        self.assertEqual(2, gate["total_sessions"])

    def test_growth_past_the_budget_is_refused(self) -> None:
        gate = readiness_gate._reopened_plan_gate_payload(
            self._snapshot(("pending", "pending", "pending")),
            max_pending_sessions=1,
            repair_session_ids=["2.1", "2.2"],
        )

        self.assertFalse(gate["ok"])
        self.assertEqual(["2.1", "2.2"], gate["repair_session_ids"])
        self.assertEqual("env-turn-budget", gate["findings"][0]["category"])

    def test_the_trigger_tells_an_audit_which_growth_asked(self) -> None:
        snapshot = self._snapshot(("complete", "pending"))

        repairs = readiness_gate._reopened_plan_gate_payload(
            snapshot, max_pending_sessions=1, repair_session_ids=["2.1"]
        )
        appended = readiness_gate._reopened_plan_gate_payload(
            snapshot,
            max_pending_sessions=1,
            repair_session_ids=[],
            trigger="review_session_created",
        )

        self.assertNotEqual(repairs["trigger"], appended["trigger"])
        self.assertEqual("review_session_created", appended["trigger"])


if __name__ == "__main__":
    unittest.main()

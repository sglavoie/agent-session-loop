"""Boundary tests for `agent_session_loop.run_payloads`.

The behavioural coverage of these three builders already exists and stays where
it is: `tests/test_asl_supervisor_audit.py` owns the persisted `run_config` and
the audit block, `tests/test_asl_assurance_lifecycle.py` and
`tests/test_asl_harness_integration.py` drive real runs through the seeded
payload, and `tests/test_asl_cli.py` owns the `--dry-run` output. None of them
can cheaply state the thing this module actually owns, which is the *shape* the
rest of the package inherits, so that is what is stated here.

Four contracts:

* The extraction contract -- runner-free, no alias left behind, exactly the
  three symbols the session moved, and no durable write anywhere.
* The seeded-key contract -- every key a later writer indexes without a presence
  check is present with its empty value of the right type, and the two keys the
  package deliberately reaches through `setdefault` are deliberately absent.
* The resume contract -- every dest `run_entry._RESUME_OVERLAY_DESTS` restores has
  a key in the `run_config` block, because the overlay skips a missing dest
  silently rather than failing.
* The preview contract -- `_dry_run_payload` renders through the same builders a
  real turn calls and refuses what a real turn would refuse.

Every payload here is built for real against a plan on disk. Nothing reaches
Git or a harness, so nothing spawns a process, which is what keeps this module
in `PORTABLE_MODULES`; `test_building_every_payload_spawns_no_process` asserts
that rather than assuming it.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    commit_settlement,
    events,
    plans,
    run_entry,
    run_flags,
    run_payloads,
    run_selection,
    runner,
    state,
)
from agent_session_loop.run_errors import RunError
from tests.support import assert_no_runner_import, write_plan

RUN_ID = "20260903T000000Z-run-payloads"

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_run_config_payload",
    "_initial_payload",
    "_dry_run_payload",
)

#: Every call this boundary could make that outlives the process. This module is
#: three pure builders, so the assertion below is a flat prohibition rather than
#: the per-function map the write-owning boundaries use: the caller performs the
#: `state.json` write, and a first writer appearing here is a test failure.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
        "mkdir",
        "rename",
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

#: Keys seeded to an *empty* container, with the container type a later writer
#: assumes. Each one is indexed without a presence check somewhere in the
#: package -- `turn_execution` appends to `turns`, `commit_settlement` to
#: `commits`, the run loop to `completed_sessions` -- so a key dropped from the
#: seed turns a mutation into a `KeyError` mid-run rather than at startup.
SEEDED_CONTAINERS: dict[str, type] = {
    "details": dict,
    "pause_details": dict,
    "turns": list,
    "commits": list,
    "commit_groups": list,
    "completed_sessions": list,
    "completed_session_ids": list,
    "review_cycles": list,
    "model_projection": list,
}

#: Nested blocks seeded already *populated*, each delegated to the module that
#: owns it. They are the reason the seed cannot be reduced to "everything starts
#: empty": a run whose `control` block arrived empty would have no field for the
#: control plane to compare-and-set against, and one whose `run_config` arrived
#: empty could not be resumed at all.
SEEDED_BLOCKS = (
    "control",
    "run_config",
    "assurance",
    "harness_config",
    "initial_snapshot",
    "final_snapshot",
    "mailbox_activation",
)

#: Keys seeded to `None` rather than left out. A reader may say
#: `payload["active_turn"]` instead of `payload.get("active_turn")`, and the
#: outcome printer and the status projections both do.
SEEDED_NULLS = (
    "run_finished_at",
    "duration_seconds",
    "active_turn",
    "active_verification",
    "action_required",
    "pause_reason",
    "max_pending_sessions",
    "note",
    "readiness_gate",
    "readiness_state_initial",
    "readiness_state_final",
    "review_session_id",
    "review_session_executed",
    "review_verdict",
    "review_skipped_reason",
    "review_fix_disposition",
    "review_resume",
    "review_reopen",
)

#: The two run-state keys this boundary deliberately does *not* seed, which is
#: why the package reaches them through `setdefault`. They exist only on a run
#: that got as far as the feature that writes them, so seeding them would put an
#: empty list in the state of every run that never repaired a commit.
UNSEEDED_LATE_KEYS = ("repair_commit_preflights", commit_settlement.COMMIT_FAILURES_KEY)


def _called_names(node: ast.AST) -> set[str]:
    """Every name called under ``node``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, so a name that also occurs
    as a substring of a longer call does not read as a match.
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


def _parse(tokens: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(tokens)


class _PlanFixture:
    """A plan on disk plus the resolved paths `_initial_payload` is handed."""

    def __init__(self, case: unittest.TestCase, statuses: tuple[str, ...]) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # Resolve the temp root: `plans.inspect_plan` canonicalizes the macOS
        # `/var/folders/...` symlink and a later path comparison would differ.
        self.root = Path(tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, statuses)
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.run_dir = self.plan_state / "runs" / RUN_ID
        self.lock_info = state.LockInfo(
            path=self.plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans.canonicalize_path(self.plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )

    def args(self, *tokens: str) -> argparse.Namespace:
        return _parse(
            ["run", str(self.plan_dir), "--state-dir", str(self.state_dir), *tokens]
        )

    def snapshot(self) -> plans.PlanSnapshot:
        return plans.inspect_plan(self.plan_dir)

    def initial(self, *tokens: str) -> dict[str, Any]:
        args = self.args(*tokens)
        return run_payloads._initial_payload(
            args=args,
            snapshot=self.snapshot(),
            state_dir=self.plan_state,
            run_dir=self.run_dir,
            event_path=self.plan_state / events.EVENTS_FILE_NAME,
            state_path=self.plan_state / events.STATE_FILE_NAME,
            lock_info=self.lock_info,
            cwd=self.root,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=run_selection._mailbox_activation_payload(
                args, self.plan_dir
            ),
        )

    def dry_run(self, *tokens: str) -> dict[str, Any]:
        args = self.args(*tokens)
        return run_payloads._dry_run_payload(
            args=args,
            snapshot=self.snapshot(),
            cwd=self.root,
            run_dir=self.run_dir,
            mailbox_activation=run_selection._mailbox_activation_payload(
                args, self.plan_dir
            ),
        )


class RunPayloadsBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes nothing."""

    def setUp(self) -> None:
        source_path = Path(run_payloads.__file__ or "")
        self.source = source_path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_payloads)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_payloads, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        functions = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        classes = {
            node.name for node in self.tree.body if isinstance(node, ast.ClassDef)
        }
        constants = {
            target.id
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), functions)
        self.assertEqual(set(), classes)
        self.assertEqual(set(), constants)

    def test_this_boundary_makes_no_durable_write(self) -> None:
        # All three functions return a dictionary and nothing else. The write of
        # `state.json` belongs to `run_persistence` and the caller performs it,
        # so a builder that learned to persist its own output would be writing
        # outside the run's mutex.
        self.assertEqual(frozenset(), _called_names(self.tree) & DURABLE_WRITE_NAMES)


class PayloadContractTests(unittest.TestCase):
    """The shape `_initial_payload` declares once for every later writer."""

    def setUp(self) -> None:
        self.plan = _PlanFixture(self, ("pending", "pending"))

    def test_every_container_a_writer_indexes_is_seeded_with_its_type(self) -> None:
        payload = self.plan.initial()
        for key, kind in SEEDED_CONTAINERS.items():
            with self.subTest(key=key):
                self.assertIn(key, payload)
                self.assertIsInstance(payload[key], kind)
                self.assertFalse(payload[key], f"{key} must start empty")

    def test_every_delegated_block_is_seeded_already_populated(self) -> None:
        # These are not "empty containers with a type"; each is a whole block
        # built by the module that owns it, and each is read by field rather
        # than tested for presence.
        payload = self.plan.initial()
        for key in SEEDED_BLOCKS:
            with self.subTest(key=key):
                self.assertIsInstance(payload[key], dict)
                self.assertTrue(payload[key], f"{key} must arrive populated")

    def test_every_optional_field_is_seeded_null_rather_than_left_out(self) -> None:
        payload = self.plan.initial()
        for key in SEEDED_NULLS:
            with self.subTest(key=key):
                self.assertIn(key, payload)
                self.assertIsNone(payload[key])

    def test_the_late_feature_keys_are_deliberately_not_seeded(self) -> None:
        # These two are the exception that proves the rule, and the reason the
        # package reaches them through `setdefault`. Seeding them would put an
        # empty list into the state of every run that never repaired a commit.
        payload = self.plan.initial()
        for key in UNSEEDED_LATE_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, payload)

    def test_the_two_counters_are_seeded_side_by_side_and_are_distinct(self) -> None:
        # The lifecycle sequence counts notifications; the projection generation
        # counts durable transitions. Collapsing them into one key would make
        # either one read as a stand-in for the other.
        payload = self.plan.initial()
        from agent_session_loop import agent_mux_projection

        self.assertEqual(0, payload["agent_mux_sequence"])
        self.assertEqual(0, payload[agent_mux_projection.GENERATION_KEY])
        self.assertNotEqual("agent_mux_sequence", agent_mux_projection.GENERATION_KEY)

    def test_both_spellings_of_the_event_path_are_written(self) -> None:
        # `event_path` and `events_path` are a compatibility surface for readers
        # of either spelling; dropping either is an observable change to
        # `state.json`.
        payload = self.plan.initial()
        self.assertEqual(payload["event_path"], payload["events_path"])
        self.assertEqual(
            str(self.plan.plan_state / events.EVENTS_FILE_NAME), payload["event_path"]
        )

    def test_a_dry_run_is_never_audit_eligible(self) -> None:
        # Eligibility starts with the run ID and run directory of a loop that
        # actually executes, so a preview must not claim an auditable run.
        self.assertTrue(self.plan.initial()["assurance"]["eligible"])
        self.assertFalse(self.plan.initial("--dry-run")["assurance"]["eligible"])


class ResumeConfigContractTests(unittest.TestCase):
    """`run_config` is the resume contract, and the overlay fails silently."""

    def setUp(self) -> None:
        self.plan = _PlanFixture(self, ("pending",))

    def _config(self, *tokens: str) -> dict[str, Any]:
        return run_payloads._run_config_payload(
            self.plan.args(*tokens), cwd=self.plan.root, commit_mode=None
        )

    def test_every_dest_the_resume_overlay_restores_has_a_key_here(self) -> None:
        # `_apply_resume_config` skips a dest it does not find in the persisted
        # block, with no error and no warning, so a flag missing from here is a
        # flag silently reset to its default on `asl run --resume`. This is the
        # one containment that makes the block a contract rather than a summary.
        config = self._config()
        missing = sorted(set(run_entry._RESUME_OVERLAY_DESTS) - set(config))
        self.assertEqual([], missing)

    def test_every_overlay_dest_is_a_real_flag_dest(self) -> None:
        # The overlay is derived from `RUN_FLAG_SPEC`, so a new ordinary run
        # flag joins it automatically -- which is exactly why the containment
        # above has to be asserted rather than reviewed.
        spec_dests = {flag.dest for flag in run_flags.RUN_FLAG_SPEC}
        self.assertTrue(set(run_entry._RESUME_OVERLAY_DESTS) <= spec_dests)

    def test_the_block_carries_two_keys_that_are_not_flag_dests(self) -> None:
        # `state_dir_override` is persisted under a different key than its dest
        # and `repair_commit_mode` is a resolved constant rather than a flag;
        # both are read by name elsewhere, so neither is dead weight.
        config = self._config()
        extra = sorted(set(config) - {flag.dest for flag in run_flags.RUN_FLAG_SPEC})
        self.assertEqual(["repair_commit_mode", "state_dir_override"], extra)

    def test_the_resolved_values_are_snapshotted_not_the_raw_flags(self) -> None:
        # The point of the block is the *resolved* run: a resume that re-derived
        # these from defaults would lose the allowlist and the bypass decision.
        config = self._config("--review-guard-exclude", "docs/**", "--unsafe-bypass")
        self.assertEqual(["docs/**"], config["review_guard_exclude"])
        self.assertTrue(config["unsafe_bypass"])
        self.assertEqual(str(self.plan.root), config["cwd"])
        self.assertEqual(str(self.plan.state_dir), config["state_dir_override"])

    def test_the_nested_block_is_the_same_shape_as_the_standalone_one(self) -> None:
        nested = self.plan.initial()["run_config"]
        self.assertEqual(set(self._config()), set(nested))


class DryRunPreviewTests(unittest.TestCase):
    """The preview renders through the builders a real turn would call."""

    def setUp(self) -> None:
        self.plan = _PlanFixture(self, ("pending", "pending"))

    def test_the_preview_names_the_session_the_loop_would_select_next(self) -> None:
        payload = self.plan.dry_run()
        self.assertEqual(
            self.plan.snapshot().next_pending.id, payload["next_pending_id"]
        )
        self.assertTrue(payload["prompt"])
        self.assertEqual("session", payload["prompt_kind"])
        self.assertIsNotNone(payload["invocation"])
        self.assertIsNone(payload["reply_source"])
        self.assertIsNone(payload["resolver_prompt"])

    def test_a_plan_with_nothing_pending_previews_no_turn(self) -> None:
        finished = _PlanFixture(self, ("complete",))
        payload = finished.dry_run()
        self.assertIsNone(payload["next_pending"])
        self.assertIsNone(payload["next_pending_id"])
        self.assertIsNone(payload["prompt"])
        self.assertIsNone(payload["invocation"])
        # The selection block is still reported, so `--dry-run` on a finished
        # plan still answers what harness and model a turn would have used.
        self.assertIn("spark", payload)
        self.assertIn("mailbox_activation", payload)

    def test_the_preview_refuses_a_review_session_with_no_review_harness(self) -> None:
        # A real turn raises here; a preview that rendered a prompt anyway would
        # be previewing a turn the loop could not run.
        plan = _PlanFixture(self, ("pending",))
        with (
            mock.patch.object(
                run_payloads.review_session_contract,
                "is_generated_review_session_file",
                return_value=True,
            ),
            self.assertRaises(RunError) as caught,
        ):
            plan.dry_run()
        self.assertIn("--review-harness", str(caught.exception))

    def test_the_preview_reports_the_review_output_path_when_asked(self) -> None:
        without = self.plan.dry_run()
        self.assertFalse(without["review_requested"])
        self.assertIsNone(without["review_output_path"])
        with_review = self.plan.dry_run("--review-harness", "claude")
        self.assertTrue(with_review["review_requested"])
        self.assertEqual("claude", with_review["review_harness"])
        self.assertIsNotNone(with_review["review_output_path"])

    def test_the_preview_and_the_seed_agree_on_the_selection(self) -> None:
        # Both call `run_selection` rather than each deriving their own answer,
        # which is what keeps `--dry-run` an honest preview of the run.
        preview = self.plan.dry_run("--harness", "claude")
        seed = self.plan.initial("--harness", "claude")
        self.assertEqual(preview["mailbox_enabled"], seed["mailbox_enabled"])
        self.assertEqual(preview["no_mailbox"], seed["no_mailbox"])
        self.assertEqual(preview["review_requested"], seed["review_requested"])
        self.assertEqual(seed["harness"], "claude")


class PortabilityTests(unittest.TestCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_building_every_payload_spawns_no_process(self) -> None:
        plan = _PlanFixture(self, ("pending", "pending"))

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            self.assertTrue(plan.initial())
            self.assertTrue(plan.dry_run())
            self.assertTrue(
                run_payloads._run_config_payload(
                    plan.args(), cwd=plan.root, commit_mode=None
                )
            )


if __name__ == "__main__":
    unittest.main()

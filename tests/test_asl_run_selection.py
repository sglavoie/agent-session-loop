"""The turn-selection boundary: what it decides, and what it may not reach.

The behavioural coverage of these decisions lives where it always has —
``tests/test_asl_spark.py`` routes whole runs through Spark precedence,
``tests/test_asl_harness_integration.py`` drives real harness preflight, and
``tests/test_asl_control_run_control.py`` exercises live overrides. What is asserted
here is the boundary those modules cannot state, because they exercise the loop
rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it owns exactly one durable write — the guidance-delivery receipt — and it
  reaches the run's event log through a callback rather than through the run's
  persistence handle; and
* the invariants that make selection separable at all, each of which is a
  place where a quiet regression would produce a run that looks fine.

That last group is the reason this file exists. A selection bug does not fail
a gate: it launches a turn against the wrong model, under the wrong commit
scoping, or with a supervisor's channel silently dark, and the run reports
success either way.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import config, events, mailbox, run_selection
from agent_session_loop.run_errors import RunError
from tests.support import assert_no_runner_import, patched_env


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "harness": "codex",
        "harness_model": None,
        "review_harness_model": None,
        "effort": None,
        "spark": False,
        "state_dir": None,
        "commit_mode": None,
        "unsafe_bypass": True,
        "no_mailbox": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RunSelectionBoundaryTests(unittest.TestCase):
    """``run_selection`` is runner-free and writes one receipt, on purpose."""

    def test_run_selection_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_selection)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        source = Path(run_selection.__file__ or "").read_text(encoding="utf-8")

        # Every read this module performs goes through `config`, `prepare`,
        # `mailbox`, or `state`, and its one write goes through `mailbox`. A
        # path opened here would be a durable surface with no owner.
        for writer in ("write_text(", "open(", "mkdir(", "unlink(", "read_text("):
            self.assertNotIn(writer, source, msg=writer)

    def test_the_event_append_arrives_as_a_callback(self) -> None:
        source = Path(run_selection.__file__ or "").read_text(encoding="utf-8")

        # The receipt's ledger append is the module's own durable write; the
        # matching event append is the run's, and it arrives as a callable so
        # this module never sees the persistence type the run loop owns.
        self.assertEqual(1, source.count("mailbox.append_guidance_delivery("))
        self.assertIn(
            "GuidanceEventAppender = Callable[[Path, str, dict[str, Any]], None]",
            source,
        )
        self.assertIn("append_event: GuidanceEventAppender", source)
        self.assertNotIn("persistence:", source)

    def test_a_delivered_receipt_reaches_the_event_log_through_the_callback(
        self,
    ) -> None:
        appended: list[tuple[Path, str, dict[str, Any]]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            message = mailbox.append_guidance(
                root / "plan", "keep going", state_dir=root / "state"
            )
            run_selection._record_next_turn_guidance_delivery(
                plan_dir=root / "plan",
                state_dir_arg=root / "state",
                event_path=root / "events.jsonl",
                append_event=lambda path, name, payload: appended.append(
                    (path, name, payload)
                ),
                pending_guidance=(message,),
                harness="claude",
                run_id="run-1",
                session_id="1",
                turn_index=3,
            )

        self.assertEqual(1, len(appended))
        path, name, payload = appended[0]
        self.assertEqual(root / "events.jsonl", path)
        self.assertEqual(events.MAILBOX_EVENT, name)
        self.assertEqual(events.MAILBOX_ACTION_GUIDANCE_DELIVERED, payload["action"])
        self.assertEqual(message.id, payload["guidance_id"])
        self.assertEqual("claude", payload["harness"])
        self.assertEqual(3, payload["turn_index"])

    def test_an_unwritable_ledger_leaves_the_guidance_deliverable(self) -> None:
        appended: list[tuple[Path, str, dict[str, Any]]] = []
        message = mailbox.MailboxMessage(
            seq=1,
            id="g-1",
            kind=mailbox.KIND_GUIDANCE,
            created_at="2026-01-01T00:00:00Z",
            text="keep going",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A file where the state root has to be a directory: the ledger
            # append fails, and a failed receipt must not raise out of turn
            # setup -- the next turn's prompt carries the guidance again.
            blocked = root / "state"
            blocked.write_text("", encoding="utf-8")
            run_selection._record_next_turn_guidance_delivery(
                plan_dir=root / "plan",
                state_dir_arg=blocked,
                event_path=root / "events.jsonl",
                append_event=lambda path, name, payload: appended.append(
                    (path, name, payload)
                ),
                pending_guidance=(message,),
                harness="claude",
                run_id="run-1",
                session_id="1",
                turn_index=3,
            )

        self.assertEqual([], appended)


class ModelSelectionInvariantTests(unittest.TestCase):
    """Resolution reports; the request forwards. They are not the same value."""

    def test_an_omitted_model_is_reported_but_never_requested(self) -> None:
        args = _args(harness="claude")

        self.assertIsNone(run_selection._requested_model(args, harness="claude"))

        payload = run_selection._selected_model_effort_payload(args)
        self.assertEqual(config.NATIVE_MODEL_DEFAULT, payload["model"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, payload["model_source"])

    def test_a_switched_harness_starts_at_its_own_default(self) -> None:
        args = _args(harness="codex", harness_model="gpt-declared")

        self.assertEqual(
            "gpt-declared", run_selection._requested_model(args, harness="codex")
        )
        # The declared implementation model belongs to the declared harness. A
        # live switch to another one must not carry it across.
        self.assertIsNone(run_selection._requested_model(args, harness="claude"))

    def test_a_live_override_is_attributed_to_the_control_surface(self) -> None:
        payload = run_selection._selected_model_effort_payload(
            _args(harness="claude"),
            harness="claude",
            implementation_overrides={"claude": {"model": "opus", "effort": "high"}},
        )

        self.assertEqual("opus", payload["model"])
        self.assertEqual(config.MODEL_SOURCE_CONTROL, payload["model_source"])
        self.assertEqual("high", payload["effort"])
        self.assertEqual(config.EFFORT_SOURCE_CONTROL, payload["effort_source"])

    def test_an_unsupported_selected_harness_is_refused(self) -> None:
        with self.assertRaisesRegex(RunError, "unsupported selected harness"):
            run_selection._selected_future_harness(
                _args(), {"next_harness": "not-a-harness"}
            )


class SparkPrecedenceTests(unittest.TestCase):
    """Spark is an optimization: every doubt routes to the baseline."""

    def _route(self, session_path: Path, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "plan_dir": session_path.parent,
            "session_path": session_path,
            "session_id": "1",
            "harness": "codex",
            "is_review_turn": False,
            "implementation_overrides": {},
            "spark_state": {"available": True},
            "run_suppression": None,
        }
        args = overrides.pop("args", _args(spark=True, harness_model="gpt-baseline"))
        kwargs.update(overrides)
        _model, _selected, route = run_selection._spark_turn_route(args, **kwargs)
        return route

    def test_missing_eligibility_metadata_routes_to_the_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session-1.md"
            session_path.write_text("# Session 1\n", encoding="utf-8")
            route = self._route(session_path)

        self.assertEqual("baseline", route["decision"])
        self.assertEqual("session-ineligible", route["suppression_cause"])
        self.assertIn("missing", route["eligibility_reason"])

    def test_every_baseline_decision_names_its_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session-1.md"
            session_path.write_text("# Session 1\n", encoding="utf-8")
            cases = (
                ({"is_review_turn": True}, "review-or-audit"),
                ({"args": _args(spark=False)}, "spark-disabled"),
                (
                    {"implementation_overrides": {"codex": {"model": "gpt-live"}}},
                    "live-set-model-override",
                ),
                ({"harness": "claude"}, "non-codex-harness"),
                ({"run_suppression": "cooldown"}, "cooldown"),
                (
                    {"spark_state": {"available": False, "reason": "rate-limited"}},
                    "rate-limited",
                ),
            )
            for overrides, cause in cases:
                with self.subTest(cause=cause):
                    route = self._route(session_path, **overrides)
                    self.assertEqual("baseline", route["decision"])
                    self.assertEqual(cause, route["suppression_cause"])

    def test_precedence_reports_the_first_cause_not_the_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session-1.md"
            session_path.write_text("# Session 1\n", encoding="utf-8")
            # A review turn on a non-codex harness with Spark disabled: three
            # causes hold at once, and the reported one is the first in the
            # documented order rather than whichever check ran last.
            route = self._route(
                session_path,
                args=_args(spark=False),
                is_review_turn=True,
                harness="claude",
            )

        self.assertEqual("review-or-audit", route["suppression_cause"])

    def test_a_disabled_run_still_reports_its_baseline(self) -> None:
        payload = run_selection._initial_spark_payload(
            _args(harness_model="gpt-baseline")
        )

        self.assertFalse(payload["enabled"])
        self.assertEqual("disabled", payload["reason"])
        self.assertEqual(run_selection.SPARK_MODEL, payload["spark_model"])
        self.assertEqual("gpt-baseline", payload["baseline_model"])


class RunSwitchTests(unittest.TestCase):
    """Validation refuses; projection degrades. Both are on purpose."""

    def test_an_unknown_commit_mode_is_refused_but_still_reportable(self) -> None:
        args = _args(commit_mode="per-hour")

        with self.assertRaisesRegex(RunError, "commit_mode must be"):
            run_selection._commit_mode(args)
        # The payload describing the run that was just refused still has to be
        # writable, so the same input degrades rather than raising here.
        self.assertIsNone(run_selection._commit_mode_payload(args))

    def test_the_recognized_commit_modes_round_trip(self) -> None:
        for mode in run_selection.COMMIT_MODES:
            with self.subTest(mode=mode):
                args = _args(commit_mode=mode)
                self.assertEqual(mode, run_selection._commit_mode(args))
                self.assertEqual(mode, run_selection._commit_mode_payload(args))

    def test_a_repair_child_commits_outside_the_run_modes(self) -> None:
        # A generated repair child owns its own commit whatever the run's mode
        # is, which is why this constant is deliberately not a member.
        self.assertNotIn(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD, run_selection.COMMIT_MODES
        )

    def test_approval_mode_follows_the_bypass_switch(self) -> None:
        self.assertEqual(
            "unsafe-bypass", run_selection._approval_mode(_args(unsafe_bypass=True))
        )
        self.assertEqual(
            "harness-default", run_selection._approval_mode(_args(unsafe_bypass=False))
        )

    def test_only_a_positive_integer_environment_value_counts(self) -> None:
        for raw, expected in (("7", 7), ("0", None), ("-1", None), ("x", None)):
            with self.subTest(raw=raw), patched_env(KITTY_WINDOW_ID=raw):
                self.assertEqual(expected, run_selection._kitty_window_id_from_env())
        with patched_env(KITTY_WINDOW_ID=None):
            self.assertIsNone(run_selection._kitty_window_id_from_env())


class MailboxActivationTests(unittest.TestCase):
    """A supervisor's channel failing is a paused run, never an exception."""

    def test_the_flag_disables_activation_without_touching_the_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = run_selection._mailbox_activation_payload(
                _args(state_dir=str(root / "state"), no_mailbox=True),
                root / "plan",
            )

        self.assertFalse(payload["enabled"])
        self.assertEqual("disabled-by-flag", payload["reason"])
        self.assertTrue(payload["disabled_by_flag"])
        self.assertFalse(payload["mailbox_present"])
        self.assertIsNone(payload["lease_reason"])

    def test_no_mailbox_and_no_lease_is_inactive_rather_than_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = run_selection._mailbox_activation_payload(
                _args(state_dir=str(root / "state")),
                root / "plan",
            )

        self.assertFalse(payload["enabled"])
        self.assertEqual("inactive", payload["reason"])
        self.assertFalse(payload["lease_fresh"])
        self.assertIsNone(payload["error"])

    def test_a_disabled_mailbox_yields_an_empty_guidance_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                (),
                run_selection._pending_guidance_snapshot(
                    enabled=False,
                    plan_dir=root / "plan",
                    state_dir_arg=root / "state",
                ),
            )

    def test_an_unreadable_mailbox_yields_an_empty_guidance_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "state"
            blocked.write_text("", encoding="utf-8")
            self.assertEqual(
                (),
                run_selection._pending_guidance_snapshot(
                    enabled=True,
                    plan_dir=root / "plan",
                    state_dir_arg=blocked,
                ),
            )


if __name__ == "__main__":
    unittest.main()

"""Overseer leaves meeting hostile records, stores and inputs fail closed."""

from __future__ import annotations

import dataclasses
import io
import shutil
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_cli_claims,
    overseer_cli_common,
    overseer_context,
    overseer_context_reader,
    overseer_discovery,
    overseer_store_ledger,
    overseer_store_models,
    state,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import WATCHER

ASL_TEST_PORTABLE = True


class PlanResolutionTests(_OverseerCase):
    """A plan resolves to the record that proves it is that plan's."""

    def _copy_state_aside(self, name: str) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        shutil.copytree(own, own.parent / name)

    def test_a_stray_copy_sorting_first_does_not_shadow_the_real_record(
        self,
    ) -> None:
        self._copy_state_aside("0" * 64)

        self.watch()
        payload = self.inspect()

        self.assertEqual(f"{self.plan.name}-run-1", payload["run_id"])

    def test_two_records_proving_one_plan_are_refused_as_ambiguous(self) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        inventory = overseer_discovery.build_inventory(state_dir=self.fleet.state_root)
        (candidate,) = inventory.candidates
        twin = overseer_discovery.Inventory(
            state_root=inventory.state_root,
            candidates=(candidate, candidate),
            warnings=(),
            complete=True,
        )

        with self.assertRaises(overseer_cli_common.OverseerCliError) as caught:
            overseer_cli_claims._resolve_candidate(twin, str(self.plan))

        self.assertEqual("plan_identity_ambiguous", caught.exception.reason)
        self.assertIn(str(own), caught.exception.message)


class ClaimsInputTests(_OverseerCase):
    """Claims of the wrong shape or encoding are a usage error, not a crash."""

    def _decide(self, *extra: str) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide", str(self.plan), "--operation", "guidance", *extra
        )

    def _refused(self, status: int, payload: dict[str, Any]) -> None:
        self.assertEqual(2, status, payload)
        self.assertEqual("claims_invalid", payload["reason"])

    def test_a_non_utf8_claims_file_is_refused(self) -> None:
        path = self.fleet.root / "claims-bad.json"
        path.write_bytes(b'[{"statement_id": "\xff"}]')

        self._refused(*self._decide("--claims-file", str(path)))

    def test_non_utf8_claims_on_stdin_are_refused(self) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(b"[\xff]"), encoding="utf-8")
        with mock.patch("sys.stdin", stdin):
            self._refused(*self._decide("--claims-stdin"))

    def test_claims_nested_too_deeply_are_refused(self) -> None:
        path = self.fleet.root / "claims-deep.json"
        path.write_text("[" * 1_000_000, encoding="utf-8")

        self._refused(*self._decide("--claims-file", str(path)))

    def test_operations_and_corroborating_must_be_lists_of_strings(self) -> None:
        for key, value in (
            ("operations", 7),
            ("operations", "guidance"),
            ("operations", [1]),
            ("corroborating", 7),
            ("corroborating", "abc"),
        ):
            with self.subTest(key=key, value=value):
                claim = {
                    "statement_id": "s1",
                    "topic": "operations",
                    "stance": "allow",
                    key: value,
                }
                path = self.fleet.claims_file(self, [claim])

                status, payload = self._decide("--claims-file", path)

                self._refused(status, payload)
                self.assertIn(key, payload["error"])


class DomainRefusalTests(_OverseerCase):
    """A context or ledger refusal is an exit-2 envelope, never a traceback."""

    def _inspect(self, plan: str, *extra: str) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "inspect", plan, "--kind", "question_pending", "--summary", "s", *extra
        )

    def test_an_unknown_incident_is_refused_incident_unknown(self) -> None:
        self.watch()

        status, payload = self._inspect(str(self.plan), "--incident-id", "999")

        self.assertEqual(2, status, payload)
        self.assertEqual("incident_unknown", payload["reason"])

    def test_an_ineligible_plan_is_refused_plan_ineligible(self) -> None:
        dry = self.fleet.write_run("plan-dry", dry_run=True)

        status, payload = self._inspect(str(dry))

        self.assertEqual(2, status, payload)
        self.assertEqual("plan_ineligible", payload["reason"])

    def test_a_ledger_error_raised_anywhere_in_a_leaf_is_an_envelope(self) -> None:
        def refusing(**_: Any) -> overseer_store_ledger.Ledger:
            raise overseer_store_models.LedgerError("no ledger today")

        seams = dataclasses.replace(self.fleet.seams(), open_ledger=refusing)
        with mock.patch.object(self.fleet, "seams", return_value=seams):
            for argv in (
                ("watch", "--watcher-id", WATCHER),
                ("watch", "--watcher-id", WATCHER, "--release"),
                ("inspect", str(self.plan)),
                ("reconcile", str(self.plan)),
            ):
                with self.subTest(argv=argv):
                    status, payload = self.fleet.invoke(*argv)

                    self.assertEqual(2, status, payload)
                    self.assertEqual("ledger_error", payload["reason"])

    def test_a_corrupt_ledger_is_refused_ledger_unreadable(self) -> None:
        path = overseer_store_ledger.ledger_path(self.fleet.state_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a database " * 512)

        with self.assertRaises(overseer_store_models.LedgerError) as caught:
            overseer_store_ledger.open_ledger(state_dir=self.fleet.state_root)
        self.assertEqual("ledger_unreadable", caught.exception.reason)

        for argv in (
            ("watch", "--watcher-id", WATCHER),
            ("watch", "--watcher-id", WATCHER, "--release"),
            ("inspect", str(self.plan)),
            ("reconcile", str(self.plan)),
        ):
            with self.subTest(argv=argv):
                status, payload = self.fleet.invoke(*argv)

                self.assertEqual(2, status, payload)
                self.assertEqual("ledger_unreadable", payload["reason"])

    def test_a_database_error_past_the_open_is_ledger_unreadable(self) -> None:
        def corrupt(**_: Any) -> overseer_store_ledger.Ledger:
            raise sqlite3.DatabaseError("database disk image is malformed")

        seams = dataclasses.replace(self.fleet.seams(), open_ledger=corrupt)
        with mock.patch.object(self.fleet, "seams", return_value=seams):
            status, payload = self.fleet.invoke("watch", "--watcher-id", WATCHER)

        self.assertEqual(2, status, payload)
        self.assertEqual("ledger_unreadable", payload["reason"])

    def test_a_blank_watcher_id_is_refused_by_the_parser(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                with (
                    redirect_stderr(io.StringIO()),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    self.fleet.parse("watch", "--watcher-id", value)

                self.assertEqual(2, caught.exception.code)


class RefusedContextGrantTests(_OverseerCase):
    """No context grant means an unseen record, not an empty one."""

    def test_decide_refuses_rather_than_listing_zero_statements(self) -> None:
        for kind, expected in (
            ("refused", "unauthorized"),
            ("unavailable", "policy_unavailable"),
        ):
            getattr(self.fleet.policy, kind).add(str(self.plan))
            for extra in (("--list-statements",), ("--operation", "guidance")):
                with self.subTest(kind=kind, extra=extra):
                    status, payload = self.fleet.invoke(
                        "decide", str(self.plan), *extra
                    )

                    self.assertEqual(1, status, payload)
                    self.assertEqual(expected, payload["reason"])
                    self.assertIsNone(payload["evidence_digest"])
                    self.assertNotIn("statements", payload)
            getattr(self.fleet.policy, kind).clear()


class ExpansionCeilingTests(_OverseerCase):
    """No --limit widens an expansion past the ceiling the packet advertises."""

    def test_a_limit_above_the_ceiling_is_refused_by_the_parser(self) -> None:
        ceiling = overseer_context.MAX_EXCERPT_CHARS
        self.assertEqual(ceiling, self.fleet.parse("inspect", str(self.plan)).limit)
        self.assertEqual(
            ceiling,
            self.fleet.parse("inspect", str(self.plan), "--limit", str(ceiling)).limit,
        )
        with (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            self.fleet.parse("inspect", str(self.plan), "--limit", str(ceiling + 1))
        self.assertEqual(2, caught.exception.code)

    def test_expand_itself_narrows_a_wider_limit_to_the_ceiling(self) -> None:
        packet = self.observed()
        ledger = self.fleet.open_ledger(state_dir=self.fleet.state_root)
        decision = self.fleet.policy(
            plan_dir=str(self.plan),
            access="context",
            repositories=(),
            timeout=1.0,
        )
        long_text = "x" * (overseer_context.MAX_EXCERPT_CHARS * 3)
        projection = mock.Mock(detail=long_text, shown=1, records=1)
        with (
            mock.patch.object(
                overseer_context_reader._Reader, "project", return_value=projection
            ),
            mock.patch.object(
                overseer_context_reader._Reader,
                "fingerprint",
                return_value=("d" * 64, len(long_text)),
            ),
        ):
            excerpt = overseer_context.expand(
                grant=decision.grant(),
                ledger=ledger,
                incident_id=packet["packet"]["incident_id"],
                kind="status",
                state_dir=self.fleet.state_root,
                limit=10**9,
            )

        self.assertIsNone(excerpt.refusal)
        self.assertEqual(overseer_context.MAX_EXCERPT_CHARS, len(excerpt.text))
        self.assertTrue(excerpt.truncated)


class UnmatchedSelectorTests(_OverseerCase):
    """A --plan nobody has a record of is the wrong question, as in inspect."""

    def test_scan_and_watch_refuse_a_plan_discovery_never_saw(self) -> None:
        nobody = str(self.fleet.plans_root / "plan-nobody-has")
        for argv in (
            ("scan", "--plan", nobody),
            ("scan", "--plan", str(self.plan), "--plan", nobody),
            ("watch", "--watcher-id", WATCHER, "--plan", nobody),
            ("watch", "--watcher-id", WATCHER, "--plan", nobody, "--for-seconds", "1"),
        ):
            with self.subTest(argv=argv):
                status, payload = self.fleet.invoke(*argv)

                self.assertEqual(2, status, payload)
                self.assertEqual("plan_unobserved", payload["reason"])
                self.assertIn("plan-nobody-has", payload["error"])

    def test_a_plan_whose_record_is_unreadable_is_still_matched(self) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        (own / "state.json").write_bytes(b"\xff")

        status, payload = self.fleet.invoke("scan", "--plan", str(self.plan))

        # A partial view, which is a refusal, and not an unknown plan.
        self.assertEqual(1, status, payload)
        self.assertFalse(payload["complete"])

    def test_a_named_plan_with_a_record_still_watches(self) -> None:
        payload = self.watch("--plan", str(self.plan))

        self.assertEqual(
            [str(self.plan)],
            [item["plan_dir"] for item in payload["sweeps"][0]["enrolled"]],
        )


class DurationCeilingTests(_OverseerCase):
    """A duration too large for a timedelta is refused by the parser."""

    def _refused(self, *argv: str) -> None:
        with (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            self.fleet.parse(*argv)
        self.assertEqual(2, caught.exception.code)

    def test_watch_durations_and_timeouts_have_a_ceiling(self) -> None:
        plan = str(self.plan)
        watch = ("watch", "--watcher-id", WATCHER)
        cases = [
            (watch, flag, "86400")
            for flag in (
                "--for-seconds",
                "--interval-seconds",
                "--sweep-seconds",
                "--lease-seconds",
            )
        ]
        cases.append((watch, "--timeout", "3600"))
        cases.extend(
            ((leaf, plan), "--timeout", "3600")
            for leaf in ("inspect", "decide", "reconcile")
        )
        for prefix, flag, ceiling in cases:
            with self.subTest(leaf=prefix[0], flag=flag):
                self.fleet.parse(*prefix, flag, ceiling)
                for value in (str(float(ceiling) + 1), "1e300"):
                    self._refused(*prefix, flag, value)

    def test_the_largest_bounded_watch_does_not_overflow(self) -> None:
        payload = self.watch("--for-seconds", "86400", "--interval-seconds", "86400")

        self.assertTrue(payload["complete"])


class IncidentOwnershipTests(_OverseerCase):
    """An incident is reloaded only under the plan it was filed against."""

    def test_another_plans_incident_is_refused_incident_unknown(self) -> None:
        other = self.fleet.write_run("plan-beta")
        self.fleet.ask_question(other)
        self.watch()
        packet = self.inspect()

        status, payload = self.fleet.invoke(
            "inspect",
            str(other),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
        )

        self.assertEqual(2, status, payload)
        self.assertEqual("incident_unknown", payload["reason"])
        self.assertIn(str(self.plan), payload["error"])

    def test_the_plans_own_incident_still_reloads(self) -> None:
        self.watch()
        packet = self.inspect()

        status, payload = self.fleet.invoke(
            "inspect",
            str(self.plan),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
        )

        self.assertEqual(0, status, payload)
        self.assertEqual(
            packet["packet"]["incident_id"], payload["packet"]["incident_id"]
        )


if __name__ == "__main__":
    unittest.main()
